"""The no-login student side: joining, grading, and the projected join screen.

Everything here is reached by a session's uuid and a cookie, with no account
behind either, so each refusal is asserted on what was *stored*, not only on
what was shown: a vote that was turned away must leave no evaluation behind.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assessments.models import (AssessmentSession, Evaluation, EvaluationScore,
                                ParticipationRecord, PresentationGroup,
                                PresentationTurn)
from assessments.tests.test_edges_core import SessionFixture


class StudentTestCase(SessionFixture):

    def setUp(self):
        super().setUp()
        AssessmentSession.objects.filter(pk=self.session.pk).update(
            status=AssessmentSession.Status.LIVE)
        self.session.refresh_from_db()
        cache.clear()

    def url(self, name):
        return reverse(name, args=[self.session.uuid])

    def join_as(self, student):
        response = self.client.post(self.url('student_join'), {'identifier': student.student_id})
        self.assertEqual(response.status_code, 302, 'joining should have worked')

    def grade(self, **extra):
        # A full vote scores the group and each member of it individually.
        data = {f'group_{self.quality.pk}': '8'}
        for member in (self.ann, self.bob):
            data[f'ind_{member.pk}_{self.teamwork.pk}'] = '6'
        data.update(extra)
        return self.client.post(self.url('student_evaluate'), data)


class JoinTests(StudentTestCase):

    def test_the_join_page_asks_for_whatever_the_session_identifies_by(self):
        response = self.client.get(self.url('student_join'))
        self.assertContains(response, 'your student ID')

    def test_a_name_session_asks_for_a_name(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(identify_by='full_name')
        self.assertContains(self.client.get(self.url('student_join')), 'your full name')

    def test_somebody_who_is_on_the_roster_is_let_in_and_recorded(self):
        response = self.client.post(self.url('student_join'), {'identifier': 's3'})
        self.assertRedirects(response, self.url('student_evaluate'), fetch_redirect_response=False)
        self.assertTrue(ParticipationRecord.objects.filter(student=self.cat).exists())

    def test_somebody_not_on_the_roster_is_turned_away(self):
        response = self.client.post(self.url('student_join'), {'identifier': 'S999'})
        self.assertContains(response, 'find you on the roster')
        self.assertFalse(ParticipationRecord.objects.exists())

    def test_a_blank_answer_is_shown_the_form_again(self):
        response = self.client.post(self.url('student_join'), {'identifier': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ParticipationRecord.objects.exists())

    def test_once_joined_the_join_page_goes_straight_to_grading(self):
        self.join_as(self.cat)
        response = self.client.get(self.url('student_join'))
        self.assertRedirects(response, self.url('student_evaluate'), fetch_redirect_response=False)

    def test_a_locked_room_lets_nobody_else_in(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(joining_locked=True)
        response = self.client.post(self.url('student_join'), {'identifier': 'S3'})
        self.assertContains(response, 'Joining is closed')
        self.assertFalse(ParticipationRecord.objects.exists())

    def test_an_unknown_session_is_a_404(self):
        import uuid
        response = self.client.get(reverse('student_join', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class EvaluateTests(StudentTestCase):

    def setUp(self):
        super().setUp()
        self.turn = self.active_turn(self.red)

    def test_somebody_who_has_not_joined_is_sent_to_join(self):
        for name in ('student_evaluate', 'student_submitted'):
            response = self.client.get(self.url(name))
            self.assertRedirects(response, self.url('student_join'), fetch_redirect_response=False)

    def test_a_closed_session_shows_the_closing_page(self):
        self.join_as(self.cat)
        AssessmentSession.objects.filter(pk=self.session.pk).update(
            status=AssessmentSession.Status.CLOSED)
        response = self.client.get(self.url('student_evaluate'))
        self.assertTemplateUsed(response, 'assessments/student/closed.html')

    def test_the_page_lists_the_queue_with_the_next_group_first(self):
        self.join_as(self.cat)
        response = self.client.get(self.url('student_evaluate'))
        self.assertEqual(response.context['current_index'], 1)
        self.assertEqual([g.name for g in response.context['upcoming_groups']], ['Blue'])
        self.assertTrue(response.context['eligible'])

    def test_a_member_of_the_presenting_group_may_not_grade_it(self):
        self.join_as(self.ann)
        response = self.client.get(self.url('student_evaluate'))
        self.assertFalse(response.context['eligible'])
        self.grade()
        self.assertFalse(Evaluation.objects.exists())

    def test_a_valid_vote_is_stored_and_the_student_is_thanked(self):
        self.join_as(self.cat)
        with mock.patch('assessments.views.student_views.broadcast_session_event') as broadcast:
            response = self.grade(**{f'ind_{self.ann.pk}_{self.teamwork.pk}': '7',
                                     f'ind_{self.bob.pk}_{self.teamwork.pk}': '9'})
        self.assertRedirects(response, self.url('student_submitted'), fetch_redirect_response=False)
        self.assertEqual(Evaluation.objects.filter(evaluator=self.cat).count(), 3)
        self.assertEqual(EvaluationScore.objects.count(), 3)
        self.assertEqual(broadcast.call_args.args[1], 'evaluation.submitted')
        self.assertEqual(self.client.get(self.url('student_submitted')).status_code, 200)

    def test_a_score_that_is_not_a_number_is_refused(self):
        self.join_as(self.cat)
        response = self.grade(**{f'group_{self.quality.pk}': 'ten'})
        self.assertContains(response, 'Scores must be numbers')
        self.assertFalse(Evaluation.objects.exists())

    def test_a_vote_with_a_category_left_out_is_refused(self):
        self.join_as(self.cat)
        response = self.client.post(self.url('student_evaluate'), {})
        self.assertContains(response, 'Please score every category')
        self.assertFalse(Evaluation.objects.exists())

    def test_a_score_above_the_maximum_is_refused_and_nothing_is_kept(self):
        self.join_as(self.cat)
        response = self.grade(**{f'group_{self.quality.pk}': '11'})
        self.assertContains(response, 'cannot exceed')
        self.assertFalse(Evaluation.objects.exists())
        self.assertFalse(EvaluationScore.objects.exists())

    def test_a_late_vote_is_not_counted(self):
        self.join_as(self.cat)
        PresentationTurn.objects.filter(pk=self.turn.pk).update(
            voting_ends_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(self.url('student_evaluate'), {
            f'group_{self.quality.pk}': '8', 'turn_id': self.turn.pk})
        self.assertContains(response, "Time ran out")
        self.assertFalse(Evaluation.objects.exists())

    def test_a_turn_from_another_session_cannot_be_voted_on(self):
        # The turn id arrives in the form, so it is untrusted: a student must
        # not be able to file a vote against a turn of somebody else's session.
        other_session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Other', status='live')
        other_group = PresentationGroup.objects.create(
            assessment_session=other_session, name='Elsewhere')
        foreign = PresentationTurn.objects.create(
            assessment_session=other_session, group=other_group,
            status=PresentationTurn.Status.ACTIVE, opened_at=timezone.now(),
            voting_opened_at=timezone.now())
        self.join_as(self.cat)
        response = self.client.post(self.url('student_evaluate'), {
            f'group_{self.quality.pk}': '8', 'turn_id': foreign.pk})
        self.assertContains(response, 'Time ran out')
        self.assertFalse(Evaluation.objects.exists())

    def test_nobody_votes_twice_through_the_form(self):
        self.join_as(self.cat)
        self.grade()
        self.grade(**{f'group_{self.quality.pk}': '1'})
        self.assertEqual(
            EvaluationScore.objects.get(rubric_category=self.quality).value, Decimal('8.00'))
        self.assertEqual(Evaluation.objects.filter(evaluator=self.cat).count(), 3)

    def test_a_student_cookie_from_one_session_does_nothing_in_another(self):
        self.join_as(self.cat)
        other_session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Other', status='live')
        response = self.client.get(reverse('student_evaluate', args=[other_session.uuid]))
        self.assertRedirects(
            response, reverse('student_join', args=[other_session.uuid]),
            fetch_redirect_response=False)


class TurnExpiryAndPingTests(StudentTestCase):

    def test_the_countdown_ping_progresses_a_live_session_only(self):
        with mock.patch('assessments.views.student_views.ensure_turn_progressed') as progress:
            self.assertEqual(self.client.post(self.url('student_check_expiry')).json(), {'ok': True})
            AssessmentSession.objects.filter(pk=self.session.pk).update(status='draft')
            self.client.post(self.url('student_check_expiry'))
        self.assertEqual(progress.call_count, 1)

    def test_the_countdown_ping_is_post_only(self):
        self.assertEqual(self.client.get(self.url('student_check_expiry')).status_code, 405)

    def test_a_ping_from_somebody_who_has_not_joined_is_refused(self):
        self.assertEqual(self.client.post(self.url('student_grading_ping')).status_code, 403)

    def test_a_ping_with_no_open_turn_does_nothing(self):
        self.join_as(self.cat)
        self.assertEqual(self.client.post(self.url('student_grading_ping')).json(), {'ok': False})

    def test_a_ping_from_somebody_who_may_not_grade_does_nothing(self):
        self.active_turn(self.red)
        self.join_as(self.ann)
        self.assertEqual(self.client.post(self.url('student_grading_ping')).json(), {'ok': False})

    def test_a_ping_from_somebody_who_already_voted_does_nothing(self):
        self.active_turn(self.red)
        self.join_as(self.cat)
        self.grade()
        self.assertEqual(self.client.post(self.url('student_grading_ping')).json(), {'ok': False})

    def test_a_ping_from_somebody_grading_is_broadcast_and_remembered(self):
        self.active_turn(self.red)
        self.join_as(self.cat)
        with mock.patch('assessments.views.student_views.broadcast_session_event') as broadcast:
            response = self.client.post(self.url('student_grading_ping'),
                                        {'answered': '1', 'total': '3'})
        self.assertEqual(response.json(), {'ok': True})
        payload = broadcast.call_args.args[2]
        self.assertEqual((payload['answered'], payload['total']), (1, 3))
        self.assertTrue(cache.get(f'assessment-grading:{self.session.uuid}:{self.cat.pk}'))

    def test_junk_progress_figures_count_as_zero(self):
        self.active_turn(self.red)
        self.join_as(self.cat)
        with mock.patch('assessments.views.student_views.broadcast_session_event') as broadcast:
            self.client.post(self.url('student_grading_ping'), {'answered': 'x', 'total': 'y'})
        payload = broadcast.call_args.args[2]
        self.assertEqual((payload['answered'], payload['total']), (0, 0))


class JoinScreenTests(StudentTestCase):
    """The projected room display: aggregates only, and it never names anyone."""

    def stage(self):
        return self.client.get(self.url('assessment_join_screen_state')).json()['public_stage']

    def set_status(self, status):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status=status)

    def test_the_page_and_its_qr_code_are_public(self):
        page = self.client.get(self.url('assessment_join_screen'))
        self.assertEqual(page.status_code, 200)
        qr = self.client.get(self.url('assessment_join_screen_qr'))
        self.assertEqual(qr['Content-Type'], 'image/png')
        self.assertTrue(qr.content.startswith(b'\x89PNG'))

    def test_each_stage_has_its_own_wording(self):
        self.set_status('closed')
        self.assertEqual(self.stage(), 'Session complete')
        self.set_status('draft')
        self.assertEqual(self.stage(), 'Waiting for session to start')
        self.set_status('live')
        self.assertEqual(self.stage(), 'Waiting for presentation')
        AssessmentSession.objects.filter(pk=self.session.pk).update(
            pending_next_group=self.blue, pending_transition_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.stage(), 'Transitioning to next group')

    def test_presenting_and_voting_are_told_apart(self):
        turn = self.active_turn(self.red)
        self.assertEqual(self.stage(), 'Voting open')
        PresentationTurn.objects.filter(pk=turn.pk).update(
            voting_opened_at=None, presentation_ends_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.stage(), 'Presentation in progress')

    def test_the_state_counts_groups_and_never_names_a_student(self):
        self.active_turn(self.red)
        self.join_as(self.cat)
        state = self.client.get(self.url('assessment_join_screen_state')).json()
        self.assertEqual((state['public_total_groups'], state['public_current_index'],
                          state['public_joined_count'], state['public_student_count']),
                         (2, 1, 1, 4))
        body = str(state)
        for student in (self.ann, self.bob, self.cat, self.dan):
            self.assertNotIn(student.full_name, body)


    def page(self):
        return self.client.get(self.url('assessment_join_screen')).content.decode()

    def test_the_presenting_group_s_topic_follows_its_name_on_the_page_and_in_the_live_state(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(topic='Smart maps')
        self.active_turn(self.red)
        self.assertIn('<span id="public-group">Red</span><span class="room-topic" id="public-group-topic"> - Smart maps</span>',
                      self.page())
        self.assertEqual(self.client.get(self.url('assessment_join_screen_state')).json()['public_group_topic'],
                         'Smart maps')

    def test_a_group_with_no_topic_shows_just_its_name(self):
        self.active_turn(self.red)
        self.assertIn('<span id="public-group">Red</span><span class="room-topic" id="public-group-topic"></span>',
                      self.page())
        self.assertEqual(self.client.get(self.url('assessment_join_screen_state')).json()['public_group_topic'], '')

    def test_with_nobody_presenting_there_is_no_topic(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(topic='Smart maps')
        self.assertIn('Ready to begin</span><span class="room-topic" id="public-group-topic"></span>', self.page())
        self.assertEqual(self.client.get(self.url('assessment_join_screen_state')).json()['public_group_topic'], '')

    def test_the_live_update_script_changes_the_topic_along_with_the_name(self):
        page = self.page()
        self.assertIn("document.getElementById('public-group-topic')", page)
        self.assertIn("' - ' + state.public_group_topic", page)


class JoinScreenInconsistencyTests(StudentTestCase):

    def test_a_turn_for_a_group_outside_the_session_has_no_position_but_still_renders(self):
        other = AssessmentSession.objects.create(organization_id=self.org.pk, name='Other')
        stray = PresentationGroup.objects.create(assessment_session=other, name='Stray')
        PresentationTurn.objects.create(
            assessment_session=self.session, group=stray,
            status=PresentationTurn.Status.ACTIVE, opened_at=timezone.now(),
            voting_opened_at=timezone.now())
        state = self.client.get(self.url('assessment_join_screen_state')).json()
        self.assertIsNone(state['public_current_index'])
