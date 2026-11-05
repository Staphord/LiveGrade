"""End-to-end poll rules, identity isolation, ownership and ballot integrity."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, ClassroomPoll, ClassroomPollOption,
    ClassroomPollResponse, ClassroomPollVoter, ParticipationRecord)
from assessments.polls import PollForm, poll_results, save_response, transition


class ClassroomPollTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = make_org('Poll class')
        self.owner = make_user('poll-owner')
        sign_in(self.client, self.owner, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk,
            created_by=self.owner, name='Classroom')
        self.anna = self.session.students.create(full_name='Anna', student_id='A01')
        self.ben = self.session.students.create(full_name='Ben', student_id='B01')
        self.manage = reverse('assessment_polls', args=[self.session.pk])
        self.student = Client()

    def data(self, **overrides):
        data = {'action': 'save', 'question': 'Where should we present?', 'description': 'Choose a venue.',
                'option_lines': 'Main hall\nLibrary\nOnline', 'timezone_name': 'Africa/Dar_es_Salaam',
                'allow_abstain': 'on'}
        data.update(overrides)
        return data

    def create(self, **overrides):
        response = self.client.post(self.manage, self.data(**overrides))
        self.assertEqual(response.status_code, 302)
        return self.session.polls.first()

    def publish(self, **overrides):
        poll = self.create(**overrides)
        result = self.client.post(self.manage, {'poll': poll.uuid, 'action': 'publish'})
        self.assertEqual(result.status_code, 302)
        poll.refresh_from_db()
        self.assertEqual(poll.status, 'open')
        return poll

    def page(self, poll):
        return reverse('classroom_poll', args=[poll.uuid])

    def identify(self, poll, identifier='Anna'):
        result = self.student.post(self.page(poll), {'action': 'identify', 'identifier': identifier})
        self.assertEqual(result.status_code, 302)

    def vote(self, poll, options=(), **extra):
        return self.student.post(self.page(poll), {'action': 'vote', 'options': list(options), **extra})

    def test_any_topic_can_be_created_edited_and_published_from_a_draft_session(self):
        self.assertContains(self.client.get(self.manage), 'Ask your class one question or several in the same poll')
        poll = self.create()
        self.assertEqual(list(poll.options.values_list('label', flat=True)), ['Main hall', 'Library', 'Online'])
        self.client.post(self.manage, self.data(poll=poll.uuid, question='Which project theme?', option_lines='Health\nTransport'))
        poll.refresh_from_db()
        self.assertEqual(poll.question, 'Which project theme?')
        page = self.client.get(self.manage, {'poll': poll.uuid})
        self.assertContains(page, 'Publish poll')
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'publish'})
        self.assertEqual(poll.voters.count(), 2)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'draft')
        self.assertFalse(ParticipationRecord.objects.exists())

    def test_invalid_options_or_deadlines_do_not_save_partial_drafts(self):
        for options in ('One', 'Same\nsame', 'a' * 251 + '\nB', '\n'.join(map(str, range(51)))):
            with self.subTest(options=options[:20]):
                result = self.client.post(self.manage, self.data(option_lines=options))
                self.assertEqual(result.status_code, 200)
                self.assertFalse(self.session.polls.exists())
        for date in ('not-a-date', '2000-01-01T10:00', '2027-01-01T10:00+03:00'):
            self.assertEqual(self.client.post(self.manage, self.data(deadline_local=date)).status_code, 200)
        self.assertFalse(self.session.polls.exists())

    def test_extra_question_validation_is_shown_inline_and_preserves_submitted_question(self):
        extra_questions = [{
            'prompt': 'Which topic should come next?',
            'allow_multiple': False,
            'options': ['Planning', ' planning '],
        }]
        response = self.client.post(self.manage, self.data(
            extra_questions_json=json.dumps(extra_questions)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Answer options for question 2 must be different.')
        self.assertContains(response, 'Which topic should come next?')
        self.assertFalse(self.session.polls.exists())

    def test_timezone_deadline_conversion_and_dst_validation(self):
        form = PollForm(self.data(deadline_local='2027-01-01T10:00'))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['deadline'].astimezone(timezone.get_default_timezone()).hour, 7)
        for date in ('2027-03-14T02:30', '2027-11-07T01:30'):
            form = PollForm(self.data(timezone_name='America/New_York', deadline_local=date))
            self.assertFalse(form.is_valid())
        form = PollForm(self.data(timezone_name='not-a-zone'))
        self.assertFalse(form.is_valid())

    def test_single_choice_resubmission_replaces_response_instead_of_adding_votes(self):
        poll = self.publish()
        self.identify(poll, ' anna ')
        options = list(poll.options.all())
        self.assertEqual(self.vote(poll, [options[0].pk]).status_code, 302)
        self.assertEqual(self.vote(poll, [options[1].pk]).status_code, 302)
        self.assertEqual(ClassroomPollResponse.objects.count(), 1)
        results = poll_results(poll)
        self.assertEqual([option.vote_count for option in results['options']], [0, 1, 0])
        self.assertEqual((results['responded'], results['unanswered']), (1, 1))
        self.assertFalse(ParticipationRecord.objects.exists())
        self.assertContains(self.student.get(self.page(poll)), 'Update response')

    def test_multiple_answers_abstention_and_update_rules(self):
        poll = self.publish(allow_multiple='on')
        self.identify(poll)
        options = list(poll.options.values_list('pk', flat=True))
        self.assertEqual(self.vote(poll, options[:2]).status_code, 302)
        self.assertEqual([o.percent for o in poll_results(poll)['options']], [100, 100, 0])
        self.assertEqual(self.vote(poll, abstain='yes').status_code, 302)
        response = ClassroomPollResponse.objects.get()
        self.assertTrue(response.abstained)
        self.assertFalse(response.options.exists())
        self.assertEqual(poll_results(poll)['abstained_count'], 1)
        page = self.vote(poll, [options[0]], abstain='yes')
        self.assertContains(page, 'Choose an answer or “None of these options” for each question, not both.')
        self.assertEqual(self.vote(poll, options).status_code, 302)
        response.refresh_from_db()
        self.assertFalse(response.abstained)

    def test_empty_malformed_extra_and_foreign_choices_are_rejected(self):
        poll = self.publish(allow_abstain='')
        other = self.publish(question='Another question')
        self.identify(poll)
        choices = list(poll.options.values_list('pk', flat=True))
        for selected, extra in [([], {}), (['oops'], {}), (choices[:2], {}),
                                ([other.options.first().pk], {}), ([], {'abstain': 'yes'})]:
            with self.subTest(selected=selected):
                self.assertEqual(self.vote(poll, selected, **extra).status_code, 200)
                self.assertFalse(ClassroomPollResponse.objects.exists())

    def test_deadline_blocks_writes_without_workers_and_preserves_prior_response(self):
        poll = self.publish()
        self.identify(poll)
        option = poll.options.first()
        self.vote(poll, [option.pk])
        ClassroomPoll.objects.filter(pk=poll.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        result = self.vote(poll, [poll.options.last().pk])
        self.assertContains(result, 'This poll is closed')
        self.assertEqual(ClassroomPollResponse.objects.get().options.get(), option)
        self.assertContains(self.student.get(self.page(poll)), 'Voting is closed')

    def test_close_then_confirm_teacher_choice_and_announcement(self):
        poll = self.publish()
        self.identify(poll)
        choice = poll.options.last()
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'close'})
        self.assertContains(self.vote(poll, [choice.pk]), 'This poll is closed')
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'confirm',
            'option': choice.pk, 'decision_note': 'Bring your laptop.'})
        poll.refresh_from_db()
        self.assertEqual(poll.confirmed_option, choice)
        self.assertEqual(poll.status, 'confirmed')
        page = self.student.get(self.page(poll))
        self.assertContains(page, 'Teacher’s decision: Online')
        self.assertContains(page, 'Bring your laptop.')
        closed_saved_page = self.student.get(f'{self.page(poll)}?saved=1')
        self.assertContains(closed_saved_page, 'Your response was saved.')
        self.assertNotContains(closed_saved_page, 'You can change it while voting remains open.')
        self.assertIsNotNone(poll.closed_at)
        self.assertIsNotNone(poll.confirmed_at)

    def test_open_poll_can_be_confirmed_and_cancelled_poll_keeps_responses(self):
        poll = self.publish()
        self.identify(poll)
        self.vote(poll, [poll.options.first().pk])
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'cancel'})
        self.assertTrue(ClassroomPollResponse.objects.exists())
        self.assertContains(self.student.get(self.page(poll)), 'This poll was cancelled')
        other = self.publish()
        self.client.post(self.manage, {'poll': other.uuid, 'action': 'confirm', 'option': other.options.first().pk})
        other.refresh_from_db()
        self.assertEqual(other.status, 'confirmed')

    def test_no_roster_or_expired_deadline_prevents_publication(self):
        poll = self.create()
        self.session.students.all().delete()
        result = self.client.post(self.manage, {'poll': poll.uuid, 'action': 'publish'}, follow=True)
        self.assertContains(result, 'Add a student roster before publishing.')
        self.assertFalse(poll.voters.exists())
        self.session.students.create(full_name='Late student')
        ClassroomPoll.objects.filter(pk=poll.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        result = self.client.post(self.manage, {'poll': poll.uuid, 'action': 'publish'}, follow=True)
        self.assertContains(result, 'The response deadline has passed')

    def test_published_poll_is_immutable_and_new_students_are_not_silently_added(self):
        poll = self.publish()
        self.session.students.create(full_name='Late student')
        self.client.post(self.manage, self.data(poll=poll.uuid, question='Changed question'))
        poll.refresh_from_db()
        self.assertEqual(poll.question, 'Where should we present?')
        self.assertEqual(poll.voters.count(), 2)
        self.assertContains(self.student.post(self.page(poll), {'action': 'identify', 'identifier': 'Late student'}), 'could not identify you uniquely')

    def test_personal_links_only_rejects_shared_identity_and_rotation_revokes_browser(self):
        poll = self.publish(personal_links_only='on')
        result = self.student.post(self.page(poll), {'action': 'identify', 'identifier': 'Anna'})
        self.assertContains(result, 'Use the personal voting link')
        self.assertFalse(ClassroomPollResponse.objects.exists())
        voter = poll.voters.get(student=self.anna)
        old_token = voter.token
        invite = reverse('classroom_poll_invitation', args=[old_token])
        self.assertEqual(self.student.get(invite).status_code, 302)
        self.assertEqual(self.vote(poll, [poll.options.first().pk]).status_code, 302)
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'rotate-link', 'voter': voter.pk})
        self.assertEqual(self.student.get(invite).status_code, 404)
        self.assertContains(self.vote(poll, [poll.options.last().pk]), 'Open your voting link')
        with self.assertRaises(ValidationError):
            save_response(poll.pk, voter.pk, [poll.options.last().pk], False, token=old_token)

    def test_tokens_are_bound_to_poll_and_removed_roster_entries_cannot_vote(self):
        poll = self.publish()
        other = self.publish()
        voter = poll.voters.get(student=self.anna)
        self.student.get(reverse('classroom_poll_invitation', args=[voter.token]))
        self.assertContains(self.vote(other, [other.options.first().pk]), 'Open your voting link')
        self.vote(poll, [poll.options.first().pk])
        self.anna.delete()
        self.assertEqual(poll.voters.count(), 2)
        self.assertEqual(poll_results(poll)['responded'], 1)
        self.assertEqual(self.student.get(reverse('classroom_poll_invitation', args=[voter.token])).status_code, 404)

    def test_duplicate_name_requires_personal_link_and_id_mode_works(self):
        self.session.students.create(full_name='Anna', student_id='A02')
        poll = self.publish()
        self.assertContains(self.student.post(self.page(poll), {'action': 'identify', 'identifier': 'Anna'}), 'could not identify you uniquely')
        self.session.identify_by = 'student_id'
        self.session.save(update_fields=['identify_by'])
        other = self.publish()
        self.identify(other, 'a01')
        self.assertContains(self.student.get(self.page(other)), 'Responding as')

    def test_student_poll_state_requires_identity_and_returns_only_aggregate_results(self):
        poll = self.publish()
        url = self.page(poll)
        self.assertEqual(self.student.get(url, {'state': '1'}).status_code, 403)
        self.identify(poll)
        self.vote(poll, [poll.options.first().pk])
        result = self.student.get(url, {'state': '1'})
        self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(result['Referrer-Policy'], 'same-origin')
        public_html = result.json()['html']
        self.assertIn('Main hall', public_html)
        self.assertIn('total response', public_html)
        self.assertNotIn('Anna', public_html)
        self.assertNotIn('Awaiting response', public_html)
        self.assertTrue(result.json()['accepting'])
        self.assertContains(self.client.get(self.manage, {'poll': poll.uuid, 'state': '1'}), 'Main hall')

    def test_shared_qr_draft_secrecy_and_forgetting_identity(self):
        draft = self.create()
        self.assertEqual(self.student.get(self.page(draft)).status_code, 404)
        poll = self.publish()
        qr = self.student.get(reverse('classroom_poll_qr', args=[poll.uuid]))
        self.assertEqual(qr['Content-Type'], 'image/png')
        self.identify(poll)
        self.student.post(self.page(poll), {'action': 'forget'})
        self.assertNotContains(self.student.get(self.page(poll)), 'Responding as')

    def test_owner_results_personal_link_download_and_formula_safety(self):
        self.anna.full_name = '=SOMEFORMULA()'
        self.anna.save()
        poll = self.publish()
        response = self.client.get(self.manage, {'poll': poll.uuid, 'download': '1'})
        self.assertIn("'=SOMEFORMULA()", response.content.decode())
        self.assertIn('/assess/poll-invitation/', response.content.decode())
        page = self.client.get(self.manage, {'poll': poll.uuid})
        self.assertContains(page, 'Replace link')
        self.assertContains(page, 'Awaiting response')
        self.assertIn('no-store', response['Cache-Control'])

    def test_owner_org_and_service_scoping_apply_to_every_mutation(self):
        poll = self.publish()
        other_owner = make_user('poll-other')
        sign_in(self.client, other_owner, self.org)
        self.assertEqual(self.client.get(self.manage).status_code, 404)
        self.assertEqual(self.client.post(self.manage, {'poll': poll.uuid, 'action': 'cancel'}).status_code, 404)
        sign_in(self.client, self.owner, make_org('Other organization'))
        self.assertEqual(self.client.get(self.manage).status_code, 404)
        sign_in(self.client, self.owner, self.org)
        AssessmentSession.objects.filter(pk=self.session.pk).update(service='team')
        self.assertEqual(self.client.get(self.manage).status_code, 404)
        self.assertEqual(self.student.get(self.page(poll)).status_code, 404)

    def test_cross_session_poll_ids_and_invalid_ids_fail_closed(self):
        poll = self.publish()
        other_session = AssessmentSession.objects.create(organization_id=self.org.pk, created_by=self.owner, name='Other')
        url = reverse('assessment_polls', args=[other_session.pk])
        self.assertEqual(self.client.post(url, {'poll': poll.uuid, 'action': 'cancel'}).status_code, 404)
        self.assertEqual(self.client.get(self.manage, {'poll': 'invalid'}).status_code, 404)
        self.assertEqual(self.client.post(self.manage, {'poll': poll.uuid, 'action': 'rotate-link', 'voter': 'bad'}).status_code, 404)
        self.assertEqual(self.client.post(self.manage, {'poll': poll.uuid, 'action': 'rotate-link', 'voter': 999999}).status_code, 404)

    def test_invalid_transitions_do_not_change_poll_or_votes(self):
        poll = self.publish()
        other = self.publish()
        for action, extra in [('publish', {}), ('nonsense', {}), ('confirm', {'option': 'invalid'}),
                              ('confirm', {'option': other.options.first().pk}),
                              ('confirm', {'option': poll.options.first().pk, 'decision_note': 'x' * 2001})]:
            response = self.client.post(self.manage, {'poll': poll.uuid, 'action': action, **extra}, follow=True)
            self.assertEqual(response.status_code, 200)
            poll.refresh_from_db()
            self.assertEqual(poll.status, 'open')
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'cancel'})
        for action in ('close', 'confirm', 'cancel'):
            self.client.post(self.manage, {'poll': poll.uuid, 'action': action})
            poll.refresh_from_db()
            self.assertEqual(poll.status, 'cancelled')
        self.assertContains(self.client.post(self.manage, {'action': 'close'}), 'Choose a poll first')

    def test_vote_and_owner_updates_broadcast_after_commit_only(self):
        poll = self.publish()
        self.identify(poll)
        with patch('assessments.polls.broadcast_session_event') as broadcast:
            with self.captureOnCommitCallbacks(execute=True):
                self.vote(poll, [poll.options.first().pk])
                broadcast.assert_not_called()
            broadcast.assert_called_once_with(self.session, 'poll.changed', {})

    def test_database_constraints_prevent_duplicate_electorate_and_ballots(self):
        poll = self.publish()
        voter = poll.voters.first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassroomPollVoter.objects.create(poll=poll, student=voter.student, name='Duplicate', identifier='duplicate')
        ClassroomPollResponse.objects.create(voter=voter)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassroomPollResponse.objects.create(voter=voter)
        with self.assertRaises(IntegrityError), transaction.atomic():
            option = poll.options.first()
            ClassroomPollOption.objects.create(poll=poll, question=option.question, label=option.label)

    def test_model_rejects_cross_service_and_cross_poll_decisions(self):
        poll = self.publish()
        other = self.create()
        poll.confirmed_option = other.options.first()
        with self.assertRaises(ValidationError):
            poll.clean()
        self.session.service = 'team'
        poll.assessment_session = self.session
        with self.assertRaises(ValidationError):
            poll.clean()

    def test_rate_limit_and_expired_cache_counter(self):
        poll = self.publish()
        with patch('assessments.views.poll_views._may_identify', return_value=False):
            self.assertEqual(self.student.post(self.page(poll), {'action': 'identify', 'identifier': 'Anna'}).status_code, 429)
        from assessments.views.poll_views import _may_identify
        from django.test import RequestFactory
        request = RequestFactory().post('/')
        with patch('assessments.views.poll_views.cache.add', side_effect=[False, True]), patch(
                'assessments.views.poll_views.cache.incr', side_effect=ValueError):
            self.assertTrue(_may_identify(request, poll))
        with patch('assessments.views.poll_views.cache.add', return_value=False), patch(
                'assessments.views.poll_views.cache.incr', return_value=31):
            self.assertFalse(_may_identify(request, poll))

    @override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'team'])
    def test_classroom_only_controls_are_refused_on_team_origin(self):
        self.session.service = 'team'
        self.session.save(update_fields=['service'])
        self.assertEqual(self.client.get(f'/team/app/{self.session.pk}/polls/').status_code, 404)
        self.assertEqual(self.client.post(f'/team/app/{self.session.pk}/leaderboard-scores/', {'action': 'show'}).status_code, 404)

    def test_real_csrf_form_submission_uses_same_origin_referrer_policy(self):
        import re
        protected = Client(enforce_csrf_checks=True)
        sign_in(protected, self.owner, self.org)
        page = protected.get(self.manage)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        response = protected.post(self.manage, self.data(csrfmiddlewaretoken=token), HTTP_ORIGIN='http://testserver')
        self.assertEqual(response.status_code, 302)
        poll = self.session.polls.first()
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'publish'})
        voter = poll.voters.get(student=self.anna)
        invitation = protected.get(reverse('classroom_poll_invitation', args=[voter.token]))
        self.assertEqual(invitation['Referrer-Policy'], 'no-referrer')


    def test_shared_classroom_ip_does_not_share_one_identification_budget(self):
        from assessments.views.poll_views import _may_identify
        from django.test import RequestFactory
        poll = self.publish()
        factory = RequestFactory()
        anna = factory.post('/', {'identifier': 'Anna'}, REMOTE_ADDR='192.0.2.10')
        ben = factory.post('/', {'identifier': 'Ben'}, REMOTE_ADDR='192.0.2.10')
        for _ in range(30):
            self.assertTrue(_may_identify(anna, poll))
        self.assertFalse(_may_identify(anna, poll))
        self.assertTrue(_may_identify(ben, poll))
