"""Mid-countdown control: extending/shrinking a running timer, and pausing
actually freezing it rather than letting it keep running unseen underneath
a paused session (a real gap identified after the timer feature shipped).
"""

from datetime import timedelta
from unittest.mock import patch

from accounts.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, PresentationGroup, PresentationTurn
from assessments.turns import activate_turn, adjust_timer, pause_timer, resume_timer


class AdjustTimerTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_positive_delta_extends_deadline(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=60)
        original_end = turn.voting_ends_at
        mock_apply_async.reset_mock()

        result = adjust_timer(turn, 30)

        self.assertEqual(result.voting_ends_at, original_end + timedelta(seconds=30))
        mock_apply_async.assert_called_once()

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_negative_delta_shrinks_deadline(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=60)
        original_end = turn.voting_ends_at

        result = adjust_timer(turn, -30)

        self.assertEqual(result.voting_ends_at, original_end - timedelta(seconds=30))

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_shrinking_past_zero_closes_and_advances_immediately(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=10)

        result = adjust_timer(turn, -3600)  # way more than the time remaining

        self.assertIsNone(result)
        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.CLOSED)
        new_turn = self.session.active_turn()
        self.assertIsNotNone(new_turn)
        self.assertEqual(new_turn.group, self.group2)

    def test_adjusting_a_turn_with_no_timer_raises(self):
        turn = activate_turn(self.session, self.group1)  # no duration
        with self.assertRaises(ValueError):
            adjust_timer(turn, 30)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_a_stale_scheduled_call_does_not_fire_early_after_extension(self, mock_apply_async):
        from assessments.turns import auto_advance

        turn = activate_turn(self.session, self.group1, duration_seconds=30)
        adjust_timer(turn, 300)  # far future now
        turn.refresh_from_db()

        auto_advance(turn)  # the original (now stale) scheduled call arrives

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.ACTIVE)  # untouched


class PauseResumeTimerTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_pause_clears_deadline_and_stores_remaining(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=100)

        pause_timer(self.session)

        turn.refresh_from_db()
        self.assertIsNone(turn.voting_ends_at)
        self.assertIsNotNone(turn.paused_remaining_seconds)
        self.assertTrue(90 <= turn.paused_remaining_seconds <= 100)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_resume_restores_deadline_from_remaining(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=100)
        pause_timer(self.session)
        mock_apply_async.reset_mock()

        resume_timer(self.session)

        turn.refresh_from_db()
        self.assertIsNotNone(turn.voting_ends_at)
        self.assertIsNone(turn.paused_remaining_seconds)
        remaining = (turn.voting_ends_at - timezone.now()).total_seconds()
        self.assertTrue(85 <= remaining <= 100)
        mock_apply_async.assert_called_once()

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_paused_timer_does_not_auto_advance_while_paused(self, mock_apply_async):
        from assessments.turns import auto_advance

        turn = activate_turn(self.session, self.group1, duration_seconds=10)
        pause_timer(self.session)
        turn.refresh_from_db()

        auto_advance(turn)  # the originally-scheduled call arrives during the pause

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.ACTIVE)  # still running, not advanced

    def test_pause_with_no_timer_running_is_a_noop(self):
        activate_turn(self.session, self.group1)  # no duration
        pause_timer(self.session)  # must not raise
        turn = self.session.active_turn()
        self.assertIsNone(turn.paused_remaining_seconds)

    def test_resume_with_nothing_paused_is_a_noop(self):
        activate_turn(self.session, self.group1)
        resume_timer(self.session)  # must not raise

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_activating_a_group_while_paused_and_idle_starts_frozen(self, mock_apply_async):
        """Regression: pausing while no group was presenting (nothing yet
        for `pause_timer` to freeze), then picking a group, used to start a
        normal running countdown that closed and auto-advanced right on
        schedule — even though the panel still said "Voting paused" and
        students still couldn't vote."""
        self.session.voting_paused = True
        self.session.save(update_fields=['voting_paused'])

        turn = activate_turn(self.session, self.group1, duration_seconds=100)

        self.assertIsNone(turn.voting_ends_at)
        self.assertEqual(turn.paused_remaining_seconds, 100)
        mock_apply_async.assert_not_called()  # no auto-advance scheduled while paused

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_activating_a_group_while_paused_does_not_auto_advance(self, mock_apply_async):
        from assessments.turns import auto_advance

        self.session.voting_paused = True
        self.session.save(update_fields=['voting_paused'])
        turn = activate_turn(self.session, self.group1, duration_seconds=10)

        auto_advance(turn)  # even if something calls this directly

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.ACTIVE)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_resuming_after_a_paused_activation_starts_the_countdown(self, mock_apply_async):
        self.session.voting_paused = True
        self.session.save(update_fields=['voting_paused'])
        turn = activate_turn(self.session, self.group1, duration_seconds=100)

        self.session.voting_paused = False
        self.session.save(update_fields=['voting_paused'])
        resume_timer(self.session)

        turn.refresh_from_db()
        self.assertIsNotNone(turn.voting_ends_at)
        self.assertIsNone(turn.paused_remaining_seconds)
        remaining = (turn.voting_ends_at - timezone.now()).total_seconds()
        self.assertTrue(85 <= remaining <= 100)
        mock_apply_async.assert_called_once()


class TimerViewsTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_adjust_timer_view_extends(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=60)
        original_end = turn.voting_ends_at

        response = self.client.post(
            f'/assessments/{self.session.pk}/adjust-timer/', {'delta_seconds': '30'})

        self.assertRedirects(response, f'/assessments/{self.session.pk}/')
        turn.refresh_from_db()
        self.assertEqual(turn.voting_ends_at, original_end + timedelta(seconds=30))

    def test_adjust_timer_view_with_no_active_turn_errors_gracefully(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/adjust-timer/', {'delta_seconds': '30'})
        self.assertEqual(response.status_code, 302)  # redirects with an error message, no crash

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_pause_then_resume_via_views_preserves_remaining_time(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=100)

        self.client.post(f'/assessments/{self.session.pk}/pause/')
        turn.refresh_from_db()
        self.assertIsNone(turn.voting_ends_at)
        self.assertIsNotNone(turn.paused_remaining_seconds)

        self.client.post(f'/assessments/{self.session.pk}/pause/')  # toggle back = resume
        turn.refresh_from_db()
        self.assertIsNotNone(turn.voting_ends_at)
        self.assertIsNone(turn.paused_remaining_seconds)
