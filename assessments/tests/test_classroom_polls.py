"""End-to-end poll rules, identity isolation, ownership and ballot integrity."""
import json
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments import poll_import
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
        response = self.client.get(self.manage)
        self.assertContains(response, 'Ask your class one question or several in the same poll')
        self.assertContains(response, 'data-confirm-title="Delete polls"')
        self.assertContains(response, 'data-poll-delete-one')
        self.assertNotContains(response, 'window.confirm(')
        self.assertContains(response, 'Unsaved changes in the poll editor will also be discarded.')
        self.assertContains(response, 'novalidate data-poll-deadline-form data-poll-editor')
        self.assertContains(response, 'window.validatePollEditor = validate')
        self.assertContains(response, 'window.snapshotPollEditorState')
        self.assertContains(response, 'window.DevPerf.confirm')
        self.assertContains(response, 'Keep as draft')
        self.assertContains(response, "event.target.matches('[data-poll-question-import], [data-poll-import-cancel], [data-poll-import-action], [data-poll-bulk-form]')")
        self.assertContains(response, 'form.addEventListener(\'submit\', async function (event)')
        self.assertContains(response, "headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}")
        self.assertContains(response, "form.getAttribute('action') || window.location.href")
        self.assertContains(response, "warningText.dataset.inlineMessageText = ''")
        self.assertContains(response, 'custom-select-menu')
        self.assertContains(response, "option.value===''&&select.hasAttribute('data-hide-empty-option')")
        self.assertContains(response, 'window.initCustomSelects(typeGroup)')
        self.assertNotContains(response, 'poll-answer-type-menu')
        self.assertNotContains(response, 'window.enhancePollAnswerType')
        self.assertRegex(response.content.decode(), r'<select(?=[^>]*name="allow_multiple")(?=[^>]*hidden)[^>]*>')
        poll = self.create()
        self.assertEqual(list(poll.options.values_list('label', flat=True)), ['Main hall', 'Library', 'Online'])
        self.client.post(self.manage, self.data(poll=poll.uuid, question='Which project theme?', option_lines='Health\nTransport'))
        poll.refresh_from_db()
        self.assertEqual(poll.question, 'Which project theme?')
        page = self.client.get(self.manage, {'poll': poll.uuid})
        self.assertContains(page, 'Publish poll')
        self.assertContains(page, 'data-confirm-title="Publish poll?"')
        self.assertContains(page, 'name="action" value="publish"')
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

    def test_ajax_save_draft_and_delete_single_and_bulk(self):
        headers = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
        result = self.client.post(self.manage, self.data(), **headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['ok'])
        accept_result = self.client.post(self.manage, self.data(question='JSON accept poll?'),
            HTTP_ACCEPT='application/json')
        self.assertEqual(accept_result.status_code, 200)
        self.assertTrue(accept_result.json()['ok'])
        accept_poll = self.session.polls.get(uuid=accept_result.json()['uuid'])
        poll = self.session.polls.get(uuid=result.json()['uuid'])
        second = self.create(question='Second question?')
        deleted = self.client.post(reverse('assessment_poll_delete', args=[self.session.pk]),
            {'action': 'bulk-delete', 'polls': [str(poll.uuid), str(accept_poll.uuid), str(second.uuid)]})
        self.assertEqual(deleted.status_code, 302)
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

    def test_closed_and_confirmed_poll_states_do_not_ask_for_identity(self):
        poll = self.publish()
        question = poll.questions.first()
        option = question.options.first()

        self.identify(poll)
        self.assertEqual(self.vote(poll, [option.pk]).status_code, 302)
        transition(poll, 'close', self.owner)
        closed_page = self.student.get(self.page(poll))
        self.assertContains(closed_page, 'Voting is closed.')
        self.assertNotContains(closed_page, 'Your roster name or student ID')

        transition(poll, 'confirm', self.owner, question_id=question.pk)
        confirmed_page = self.student.get(self.page(poll))
        self.assertContains(confirmed_page, 'Decision confirmed')
        self.assertNotContains(confirmed_page, f'Teacher’s decision: {option.label}')
        self.assertContains(confirmed_page, f'Decision: {option.label}')
        self.assertNotContains(confirmed_page, 'Your roster name or student ID')

    def test_closed_poll_repairs_stale_result_projection_from_saved_ballots(self):
        poll = self.publish()
        voter = poll.voters.first()
        question = poll.questions.first()
        option = question.options.first()
        ClassroomPollResponse.objects.create(
            voter=voter,
            answers={str(question.pk): [option.pk]},
        )
        transition(poll, 'close', self.owner)

        result = poll_results(poll)
        self.assertEqual(result['responded'], 1)
        self.assertEqual(result['unanswered'], 1)
        self.assertEqual(result['questions'][0]['responded'], 1)
        self.assertEqual(result['questions'][0]['options'][0].vote_count, 1)
        poll.refresh_from_db()
        self.assertEqual(poll.result_counts['responded'], 1)

        detail = self.client.get(self.manage, {'poll': poll.uuid})
        self.assertEqual(detail.context['results']['responded'], 1)
        self.assertContains(detail, 'Approve poll results')
        self.assertContains(detail, 'Approve “Main hall”')
        self.assertNotContains(detail, 'data-poll-decision-option')

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
        self.assertEqual(self.vote(poll, [choice.pk]).status_code, 302)
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'close'})
        self.assertContains(self.vote(poll, [choice.pk]), 'This poll is closed')
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'confirm',
            'decision_note': 'Bring your laptop.'})
        poll.refresh_from_db()
        self.assertEqual(poll.confirmed_option, choice)
        self.assertEqual(poll.status, 'confirmed')
        page = self.student.get(self.page(poll))
        left_panel = page.content.decode().split(
            '<section class="s-card poll-response-card"', 1)[1].split(
                '<aside class="s-card poll-live-results-panel"', 1)[0]
        self.assertNotIn('Teacher’s decision: Online', left_panel)
        self.assertContains(page, 'Decision: Online')
        self.assertContains(page, 'Bring your laptop.')
        closed_saved_page = self.student.get(f'{self.page(poll)}?saved=1')
        self.assertContains(closed_saved_page, 'Your response was saved.')
        self.assertNotContains(closed_saved_page, 'You can change it while voting remains open.')
        self.assertIsNotNone(poll.closed_at)
        self.assertIsNotNone(poll.confirmed_at)

    def test_open_poll_must_close_before_approval_and_cancel_keeps_responses(self):
        poll = self.publish()
        self.identify(poll)
        self.vote(poll, [poll.options.first().pk])
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'cancel'})
        self.assertTrue(ClassroomPollResponse.objects.exists())
        self.assertContains(self.student.get(self.page(poll)), 'This poll was cancelled')
        other = self.publish()
        leader = other.options.first()
        self.identify(other, 'Ben')
        self.assertEqual(self.vote(other, [leader.pk]).status_code, 302)
        active_result = self.client.post(self.manage, {'poll': other.uuid, 'action': 'confirm'})
        self.assertContains(active_result, 'Close voting before approving a final result.')
        other.refresh_from_db()
        self.assertEqual(other.status, 'open')
        self.client.post(self.manage, {'poll': other.uuid, 'action': 'close'})
        self.client.post(self.manage, {'poll': other.uuid, 'action': 'confirm'})
        other.refresh_from_db()
        self.assertEqual(other.status, 'confirmed')
        self.assertEqual(other.confirmed_option, leader)

    def test_teacher_can_only_approve_the_unique_top_voted_option(self):
        poll = self.publish()
        question = poll.questions.first()
        leader, other = list(question.options.all())[:2]
        self.identify(poll)
        self.assertEqual(self.vote(poll, [leader.pk]).status_code, 302)
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'close'})

        rejected = self.client.post(self.manage, {
            'poll': poll.uuid, 'action': 'confirm', 'question': question.pk,
            'option': other.pk,
        })
        self.assertContains(rejected, 'You can only approve the option with the most selections.')
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'closed')
        self.assertIsNone(question.confirmed_option)

        accepted = self.client.post(self.manage, {
            'poll': poll.uuid, 'action': 'confirm', 'question': question.pk,
        })
        self.assertEqual(accepted.status_code, 302)
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'confirmed')
        self.assertEqual(question.confirmed_option, leader)

    def test_teacher_can_break_a_tie_only_by_choosing_a_tied_top_option(self):
        poll = self.publish()
        question = poll.questions.first()
        options = list(question.options.all())
        voters = list(poll.voters.all())
        for voter, option in zip(voters, options[:2]):
            save_response(poll.pk, voter.pk, [], token=voter.token,
                question_answers={str(question.pk): [option.pk]})
        self.client.post(self.manage, {'poll': poll.uuid, 'action': 'close'})

        detail = self.client.get(self.manage, {'poll': poll.uuid})
        self.assertContains(detail, 'The top options are tied at 1 selection.')
        self.assertContains(detail, 'Choose among the tied top options')
        self.assertContains(detail, 'name="option"')
        tied_options = detail.context['results']['questions'][0]['tied_options']
        self.assertEqual({option.pk for option in tied_options}, {options[0].pk, options[1].pk})
        rejected = self.client.post(self.manage, {
            'poll': poll.uuid, 'action': 'confirm', 'question': question.pk,
            'option': options[2].pk,
        })
        self.assertContains(rejected, 'For a tie, choose only among the tied top options.')
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'closed')
        self.assertIsNone(question.confirmed_option)

        accepted = self.client.post(self.manage, {
            'poll': poll.uuid, 'action': 'confirm', 'question': question.pk,
            'option': options[1].pk, 'decision_note': 'Teacher broke the tie.',
        })
        self.assertEqual(accepted.status_code, 302)
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'confirmed')
        self.assertEqual(question.confirmed_option, options[1])
        self.assertEqual(question.decision_note, 'Teacher broke the tie.')

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
        self.assertNotIn('poll-answer-track', public_html)
        self.assertNotIn('%', public_html)
        self.assertNotIn('Anna', public_html)
        self.assertNotIn('Awaiting response', public_html)
        self.assertTrue(result.json()['accepting'])
        response_page = self.student.get(url)
        self.assertNotContains(response_page, 'role="progressbar"')
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

    def test_classroom_poll_question_import_workflow(self):
        sample_file = SimpleUploadedFile(
            'sample.xlsx',
            poll_import.sample_workbook().getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        # Upload question sample
        res = self.client.post(self.manage, {'action': 'upload-questions', 'file': sample_file},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['ok'])
        self.assertIn('Review import', res.json()['html'])
        stash = self.client.session.get('classroom_poll_question_import')
        self.assertIsNotNone(stash)
        self.assertEqual(stash['session'], self.session.pk)
        preview_page = self.client.get(self.manage)
        self.assertContains(preview_page, 'data-poll-import-cancel')
        self.assertContains(preview_page, 'data-poll-import-action>', count=2)
        self.assertFalse(self.session.polls.exists(), 'Previewing an import must not save poll questions.')

        # Accessing with stash from another session resets stash
        session_other = self.client.session
        session_other['classroom_poll_question_import'] = {'session': 999999}
        session_other.save()
        res = self.client.get(self.manage)
        self.assertEqual(res.status_code, 200)

        # Re-upload and cancel question import
        sample_file.seek(0)
        self.client.post(self.manage, {'action': 'upload-questions', 'file': sample_file})
        res = self.client.post(self.manage, {'action': 'cancel-question-import'})
        self.assertEqual(res.status_code, 302)
        self.assertNotIn('classroom_poll_question_import', self.client.session)

        # Re-upload and apply to create new draft
        sample_file.seek(0)
        self.client.post(self.manage, {'action': 'upload-questions', 'file': sample_file})
        res = self.client.post(self.manage, {'action': 'apply-question-import'})
        self.assertEqual(res.status_code, 302)
        new_poll = self.session.polls.first()
        self.assertIsNotNone(new_poll)
        self.assertEqual(new_poll.status, 'draft')
        self.assertGreater(new_poll.questions.count(), 0)

        # Upload questions targeted to existing draft poll and apply
        sample_file.seek(0)
        res = self.client.post(self.manage, {'action': 'upload-questions', 'poll': str(new_poll.uuid), 'file': sample_file})
        self.assertEqual(res.status_code, 302)
        res = self.client.post(self.manage, {'action': 'apply-question-import', 'poll': str(new_poll.uuid)})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'already in this poll')
        self.assertEqual(new_poll.questions.count(), 2)

        # Apply question import error branches
        session_stash = self.client.session
        session_stash['classroom_poll_question_import'] = {'session': self.session.pk, 'errors': [(2, 'Error')]}
        session_stash.save()
        res = self.client.post(self.manage, {'action': 'apply-question-import', 'poll': str(new_poll.uuid)})
        self.assertEqual(res.status_code, 200)

        session_stash['classroom_poll_question_import'] = {'session': self.session.pk, 'target_poll': 999999, 'questions': [{'q': 'q'}]}
        session_stash.save()
        res = self.client.post(self.manage, {'action': 'apply-question-import', 'poll': str(new_poll.uuid)})
        self.assertEqual(res.status_code, 200)

        # Apply question import without stash in session
        self.client.session.pop('classroom_poll_question_import', None)
        res = self.client.post(self.manage, {'action': 'apply-question-import'})
        self.assertEqual(res.status_code, 200)

        # Upload empty questions file (triggers fatal error)
        from assessments.sheets import workbook_bytes
        empty_upload = SimpleUploadedFile('empty.xlsx', workbook_bytes([], []).getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        res = self.client.post(self.manage, {'action': 'upload-questions', 'file': empty_upload})
        self.assertEqual(res.status_code, 200)

        # Upload non-xlsx file (triggers invalid upload form)
        txt_upload = SimpleUploadedFile('bad.txt', b'plain text', content_type='text/plain')
        res = self.client.post(self.manage, {'action': 'upload-questions', 'file': txt_upload})
        self.assertEqual(res.status_code, 200)

    def test_classroom_poll_import_sample_and_public_results(self):
        # Sample file download
        sample_url = reverse('assessment_poll_questions_sample', args=[self.session.pk])
        res = self.client.get(sample_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        # Sample file 404 on wrong service via view handler
        from django.test import RequestFactory
        from django.http import Http404
        from assessments.views import poll_views
        rf = RequestFactory()
        req = rf.get('/')
        req.user = self.owner
        req.session = self.client.session
        req.organization = self.org.as_claim()
        with patch('assessments.views.poll_views._session') as mock_sess:
            mock_sess_obj = Mock()
            mock_sess_obj.service = 'team'
            mock_sess.return_value = mock_sess_obj
            with self.assertRaises(Http404):
                poll_views.classroom_poll_import_sample(req, self.session.pk)

        # Apply question import with no stash triggers expired preview error
        res = self.client.post(self.manage, {'action': 'apply-question-import'})
        self.assertEqual(res.status_code, 200)

        # Public poll results view
        poll = self.publish()
        results_url = reverse('classroom_poll_results', args=[poll.uuid])
        res = self.client.get(results_url)
        self.assertEqual(res.status_code, 200)
        self.assertIn('public_results', res.context)

        # Public state projection
        res = self.client.get(self.page(poll) + '?public_state=1')
        self.assertEqual(res.status_code, 200)

        # QR code download route
        res = self.client.get(reverse('classroom_poll_qr', args=[poll.uuid]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')

        # Non-digit question id in transition POST raises error
        res = self.client.post(self.manage, {'poll': str(poll.uuid), 'action': 'start-question', 'question': 'not-a-number'})
        self.assertEqual(res.status_code, 200)

        # Legacy options parameter in vote for single question poll
        single_voter = poll.voters.first()
        single_opt = poll.questions.first().options.first()
        self.client.get(reverse('classroom_poll_invitation', args=[single_voter.token]))
        res = self.client.post(self.page(poll), {
            'action': 'vote',
            'options': [str(single_opt.pk)],
        })
        self.assertEqual(res.status_code, 302)

        # Modern question-specific vote option
        res = self.client.post(self.page(poll), {
            'action': 'vote',
            f'question-{poll.questions.first().pk}': [str(single_opt.pk)],
        })
        self.assertEqual(res.status_code, 302)

    def test_poll_import_parser_error_scenarios(self):
        from assessments.sheets import workbook_bytes

        # Empty rows
        empty_wb = SimpleUploadedFile('empty.xlsx', workbook_bytes([], []).getvalue())
        self.assertIn('empty', poll_import.parse_questions(empty_wb)['fatal'].lower())

        # Header only, no data rows
        header_only = SimpleUploadedFile('header_only.xlsx', workbook_bytes(
            ['Question', 'Question Type', 'Answer Option'], []).getvalue())
        self.assertEqual(poll_import.parse_questions(header_only)['fatal'], 'No questions were found in the file.')

        # Missing headers
        bad_headers = SimpleUploadedFile('bad.xlsx', workbook_bytes(['WrongHeader'], [['val']]).getvalue())
        self.assertIn('Could not find', poll_import.parse_questions(bad_headers)['fatal'])

        # Unreadable file
        corrupt = SimpleUploadedFile('corrupt.xlsx', b'not a zip')
        self.assertEqual(poll_import.parse_questions(corrupt)['fatal'], poll_import.UNREADABLE)

        # Max rows exceeded
        too_many_rows = SimpleUploadedFile('too_many_rows.xlsx', workbook_bytes(
            ['Question', 'Question Type', 'Answer Option'],
            [['Q', 'Multiple choice', 'Opt']] * (poll_import.MAX_ROWS + 1)).getvalue())
        self.assertIn(f'at most {poll_import.MAX_ROWS}', poll_import.parse_questions(too_many_rows)['fatal'])

        # Max questions exceeded
        q_rows = []
        for i in range(poll_import.MAX_QUESTIONS + 1):
            q_rows.append([f'Question {i}', 'Multiple choice', 'Opt 1'])
            q_rows.append([f'Question {i}', 'Multiple choice', 'Opt 2'])
        too_many_q = SimpleUploadedFile('too_many_q.xlsx', workbook_bytes(
            ['Question', 'Question Type', 'Answer Option'], q_rows).getvalue())
        self.assertGreater(len(poll_import.parse_questions(too_many_q)['errors']), 0)

        # Max options exceeded
        too_many_opt = SimpleUploadedFile('too_many_opt.xlsx', workbook_bytes(
            ['Question', 'Question Type', 'Answer Option'],
            [['Q1', 'Multiple choice', f'Opt {i}'] for i in range(poll_import.MAX_OPTIONS + 1)]).getvalue())
        self.assertGreater(len(poll_import.parse_questions(too_many_opt)['errors']), 0)

        # Row validation errors
        headers = ['Question', 'Question Type', 'Answer Option']
        rows = [
            ['', 'Multiple choice', 'Opt 1'],  # Missing question
            ['A' * 251, 'Multiple choice', 'Opt 1'],  # Question too long
            ['Valid question', 'Invalid type', 'Opt 1'],  # Bad type
            ['Valid question 2', 'Multiple choice', ''],  # Missing option
            ['Valid question 3', 'Multiple choice', 'B' * 251],  # Option too long
            ['Mixed type', 'Multiple choice', 'Opt 1'],
            ['Mixed type', 'Checkbox', 'Opt 2'],  # Mismatched type
            ['Duplicate opt', 'Multiple choice', 'Same'],
            ['Duplicate opt', 'Multiple choice', 'Same'],  # Duplicate option
            ['Single opt question', 'Multiple choice', 'Only one opt'],  # Fewer than 2 options
        ]
        wb = SimpleUploadedFile('rows.xlsx', workbook_bytes(headers, rows).getvalue())
        result = poll_import.parse_questions(wb)
        self.assertGreater(len(result['errors']), 0)
