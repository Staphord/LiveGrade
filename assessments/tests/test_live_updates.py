"""Two real gaps reported after the timer feature shipped:

1. Auto-advance only ever happened via a Celery task scheduled for the exact
   deadline — with no worker running (or a dropped broker message), a turn
   would sit expired forever until someone manually intervened. `turns.
   ensure_turn_progressed` is a defensive, idempotent catch-up check now
   called from every request that touches a live session, plus a dedicated
   endpoint either side's countdown pings the instant it hits zero. This
   only applies to presentation-timed turns (`presentation_seconds`):
   voting-only turns (`duration_seconds` with no presentation timer) are
   deliberately left active past their deadline for the lecturer to close —
   see `close_voting_window` in assessments/tasks.py.
2. The control panel and the student's grading screen refreshed by
   reloading the whole page on every websocket event, which threw away any
   in-progress work (a partly-graded form) for something as small as a
   +30s nudge. `assessment_live_state` returns just the fragments that
   changed, and the "adjust timer" event no longer triggers a student
   reload at all.
"""

from datetime import timedelta
from unittest.mock import patch

from accounts.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, PresentationGroup, PresentationTurn, RubricCategory, Student,
)
from assessments.turns import activate_turn, ensure_turn_progressed


class EnsureTurnProgressedTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    @patch('assessments.tasks.open_scheduled_voting.apply_async')
    @patch('assessments.tasks.close_voting_window.apply_async')
    @patch('assessments.tasks.finish_presentation.apply_async')
    def test_advances_an_expired_presentation_turn_without_any_celery_task_firing(
            self, mock_finish, mock_close, mock_open, mock_apply_async):
        # Presentation mode (`presentation_seconds`) is the fully hands-off
        # case: once the presentation itself has ended, self-heal must close
        # and advance even if the `finish_presentation` Celery task never
        # fired. Plain voting-only mode (`duration_seconds` with no
        # presentation timer) is deliberately non-terminal instead — see
        # `close_voting_window` in assessments/tasks.py — so it's not what
        # this test exercises.
        turn = activate_turn(self.session, self.group1, presentation_seconds=10)
        turn.presentation_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['presentation_ends_at'])

        ensure_turn_progressed(self.session)

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.CLOSED)
        self.assertEqual(self.session.active_turn().group, self.group2)

    @patch('assessments.tasks.open_scheduled_voting.apply_async')
    @patch('assessments.tasks.close_voting_window.apply_async')
    @patch('assessments.tasks.finish_presentation.apply_async')
    def test_is_a_noop_while_time_remains(self, mock_finish, mock_close, mock_open):
        turn = activate_turn(self.session, self.group1, presentation_seconds=600)
        ensure_turn_progressed(self.session)
        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.ACTIVE)

    @patch('assessments.tasks.close_voting_window.apply_async')
    def test_voting_only_mode_leaves_an_expired_turn_active(self, mock_apply_async):
        # The deliberately non-terminal case: voting time runs out but
        # nothing here owns moving to the next group but the lecturer.
        turn = activate_turn(self.session, self.group1, duration_seconds=10)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])

        ensure_turn_progressed(self.session)

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.ACTIVE)

    def test_is_a_noop_with_no_active_turn(self):
        ensure_turn_progressed(self.session)  # must not raise


class StudentCheckExpiryViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Session', status=AssessmentSession.Status.LIVE)
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    @patch('assessments.tasks.open_scheduled_voting.apply_async')
    @patch('assessments.tasks.close_voting_window.apply_async')
    @patch('assessments.tasks.finish_presentation.apply_async')
    def test_check_expiry_advances_an_expired_presentation_turn(
            self, mock_finish, mock_close, mock_open, mock_apply_async):
        turn = activate_turn(self.session, self.group1, presentation_seconds=10)
        turn.presentation_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['presentation_ends_at'])

        response = self.client.post(f'/assess/{self.session.uuid}/check-expiry/')

        self.assertEqual(response.status_code, 200)
        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.CLOSED)
        self.assertEqual(self.session.active_turn().group, self.group2)

    def test_check_expiry_on_a_non_live_session_does_not_error(self):
        self.session.status = AssessmentSession.Status.DRAFT
        self.session.save(update_fields=['status'])
        response = self.client.post(f'/assess/{self.session.uuid}/check-expiry/')
        self.assertEqual(response.status_code, 200)

    def test_check_expiry_requires_post(self):
        response = self.client.get(f'/assess/{self.session.uuid}/check-expiry/')
        self.assertEqual(response.status_code, 405)


class AssessmentLiveStateViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Session', status=AssessmentSession.Status.LIVE)
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_returns_fragments_reflecting_the_active_turn(self, mock_apply_async):
        activate_turn(self.session, self.group1, duration_seconds=90)

        response = self.client.get(f'/assessments/{self.session.pk}/live-state/')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'live')
        self.assertEqual(data['active_turn']['group_id'], self.group1.pk)
        self.assertIn('Group 1', data['hero_html'])
        self.assertIn('Group 1', data['groups_html'])

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    @patch('assessments.tasks.open_scheduled_voting.apply_async')
    @patch('assessments.tasks.close_voting_window.apply_async')
    @patch('assessments.tasks.finish_presentation.apply_async')
    def test_self_heals_an_expired_presentation_turn_before_responding(
            self, mock_finish, mock_close, mock_open, mock_apply_async):
        turn = activate_turn(self.session, self.group1, presentation_seconds=10)
        turn.presentation_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['presentation_ends_at'])

        data = self.client.get(f'/assessments/{self.session.pk}/live-state/').json()

        self.assertEqual(data['active_turn']['group_id'], self.group2.pk)
        self.assertIn('Group 1 finished presenting', data['activity_html'])

    def test_reports_closed_status_for_a_closed_session(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        data = self.client.get(f'/assessments/{self.session.pk}/live-state/').json()
        self.assertEqual(data['status'], 'closed')

    def test_requires_admin(self):
        self.client.logout()
        response = self.client.get(f'/assessments/{self.session.pk}/live-state/')
        self.assertNotEqual(response.status_code, 200)


class EvaluationSubmitBroadcastTests(TestCase):
    """A vote landing must wake the lecturer's panel itself — rankings, the
    hero's "N of M voted" count, and participation are all computed fresh
    from submitted scores, so without a broadcast here they were correct on
    the next unrelated event but silently stale in between, only ever
    catching up to a mid-round vote once the turn closed."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Session',
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID,
            status=AssessmentSession.Status.LIVE)
        self.group_cat = RubricCategory.objects.create(
            assessment_session=self.session, name='Implementation',
            scope=RubricCategory.Scope.GROUP, max_points=10, weight=100)
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        self.voter = Student.objects.create(assessment_session=self.session, student_id='C1', full_name='Voter')
        self.turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group1,
            status=PresentationTurn.Status.ACTIVE)

    @patch('assessments.views.student_views.broadcast_session_event')
    def test_submitting_an_evaluation_broadcasts_immediately(self, mock_broadcast):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        mock_broadcast.reset_mock()  # drop the join event, only care about the submit

        self.client.post(f'/assess/{self.session.uuid}/evaluate/', {
            f'group_{self.group_cat.pk}': '8',
        })

        self.assertTrue(mock_broadcast.called)
        event_type = mock_broadcast.call_args[0][1]
        self.assertEqual(event_type, 'evaluation.submitted')


class SetDefaultTurnSecondsViewTests(TestCase):
    """The voting-timer picker looked like it saved (the button highlighted,
    the hidden form field updated) but `default_turn_seconds` was never
    actually written anywhere — only the transition gap had a persistence
    endpoint, so a lecturer's chosen timer silently reverted to whatever
    was set (often nothing) the moment the page was reloaded."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Session', status=AssessmentSession.Status.LIVE)

    def test_saves_the_chosen_duration_as_the_session_default(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/set-timer/', {'duration_seconds': '600'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'duration_seconds': 600})
        self.session.refresh_from_db()
        self.assertEqual(self.session.default_turn_seconds, 600)

    def test_blank_value_clears_the_timer_back_to_none(self):
        self.session.default_turn_seconds = 90
        self.session.save(update_fields=['default_turn_seconds'])

        response = self.client.post(f'/assessments/{self.session.pk}/set-timer/', {'duration_seconds': ''})

        self.assertEqual(response.json(), {'ok': True, 'duration_seconds': None})
        self.session.refresh_from_db()
        self.assertIsNone(self.session.default_turn_seconds)

    def test_persists_across_a_fresh_page_load(self):
        PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.client.post(f'/assessments/{self.session.pk}/set-timer/', {'duration_seconds': '180'})

        data = self.client.get(f'/assessments/{self.session.pk}/live-state/').json()

        self.assertIn('180', data['groups_html'])  # hidden duration-input carries the saved default forward
