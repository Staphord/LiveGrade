"""A configurable pause between one group's voting closing and the next
group auto-starting (`AssessmentSession.transition_gap_seconds`), so a
fully hands-off session doesn't snap from one group straight into the next
with literally zero delay.
"""

from datetime import timedelta
from unittest.mock import patch

from accounts.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, PresentationGroup, PresentationTurn, Student
from assessments.turns import (
    activate_pending_transition, activate_turn, auto_advance, ensure_turn_progressed,
)


class TransitionGapTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_zero_gap_advances_immediately_as_before(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=0)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])

        auto_advance(turn)

        self.assertEqual(self.session.active_turn().group, self.group2)
        mock_pending.assert_not_called()

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_positive_gap_holds_the_next_group_pending_instead_of_activating_it(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=15)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])

        auto_advance(turn)

        self.assertIsNone(self.session.active_turn())  # nothing presenting yet
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_next_group, self.group2)
        self.assertIsNotNone(self.session.pending_transition_at)
        mock_pending.assert_called_once()

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_activate_pending_transition_opens_the_group_once_the_gap_has_elapsed(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=15)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])
        auto_advance(turn)

        self.session.refresh_from_db()
        self.session.pending_transition_at = timezone.now() - timedelta(seconds=1)
        self.session.save(update_fields=['pending_transition_at'])

        activate_pending_transition(self.session)

        active = self.session.active_turn()
        self.assertIsNotNone(active)
        self.assertEqual(active.group, self.group2)
        self.session.refresh_from_db()
        self.assertIsNone(self.session.pending_next_group)
        self.assertIsNone(self.session.pending_transition_at)

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_activate_pending_transition_is_a_noop_before_the_gap_elapses(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=600)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])
        auto_advance(turn)

        activate_pending_transition(self.session)  # gap is nowhere near over

        self.assertIsNone(self.session.active_turn())
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_next_group, self.group2)

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_a_manual_activation_clears_a_pending_transition(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=600)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])
        auto_advance(turn)
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.pending_next_group)

        activate_turn(self.session, self.group2)  # lecturer jumps straight to it

        self.session.refresh_from_db()
        self.assertIsNone(self.session.pending_next_group)
        self.assertIsNone(self.session.pending_transition_at)

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_ensure_turn_progressed_self_heals_a_pending_transition_too(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=5)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])
        auto_advance(turn)
        self.session.refresh_from_db()
        self.session.pending_transition_at = timezone.now() - timedelta(seconds=1)
        self.session.save(update_fields=['pending_transition_at'])

        ensure_turn_progressed(self.session)

        self.assertEqual(self.session.active_turn().group, self.group2)


class ActivateGroupViewGapTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')

    def test_activating_a_group_remembers_the_chosen_gap(self):
        self.client.post(
            f'/assessments/{self.session.pk}/activate/{self.group.pk}/',
            {'duration_seconds': '60', 'gap_seconds': '20'})
        self.session.refresh_from_db()
        self.assertEqual(self.session.transition_gap_seconds, 20)

    def test_missing_gap_defaults_to_zero_not_an_error(self):
        response = self.client.post(f'/assessments/{self.session.pk}/activate/{self.group.pk}/')
        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.transition_gap_seconds, 0)


class SetTransitionGapViewTests(TestCase):
    """The gap picker's own endpoint — saves immediately, independent of
    ever activating a group, so it can affect a transition already in
    flight rather than only the next explicit activation."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    def test_saves_the_gap_without_activating_anything(self):
        response = self.client.post(f'/assessments/{self.session.pk}/set-gap/', {'gap_seconds': '15'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'gap_seconds': 15})
        self.session.refresh_from_db()
        self.assertEqual(self.session.transition_gap_seconds, 15)

    def test_non_digit_gap_defaults_to_zero(self):
        self.client.post(f'/assessments/{self.session.pk}/set-gap/', {'gap_seconds': 'nope'})
        self.session.refresh_from_db()
        self.assertEqual(self.session.transition_gap_seconds, 0)

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_a_gap_saved_mid_turn_applies_to_that_turns_own_auto_advance(self, mock_auto, mock_pending):
        # The gap is set to 0 (default) when group 1 is activated — then
        # changed via the dedicated endpoint *while it's still presenting*.
        turn = activate_turn(self.session, self.group1, duration_seconds=10)
        self.client.post(f'/assessments/{self.session.pk}/set-gap/', {'gap_seconds': '30'})

        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])
        from assessments.turns import auto_advance
        auto_advance(turn)

        # Not activated immediately — the newly-saved gap held it pending.
        self.assertIsNone(self.session.active_turn())
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_next_group, self.group2)

    def test_requires_post(self):
        response = self.client.get(f'/assessments/{self.session.pk}/set-gap/')
        self.assertEqual(response.status_code, 405)


class StudentPendingTransitionDisplayTests(TestCase):
    """The student waiting screen shows its own "next group starts in..."
    countdown during a configured gap, instead of the generic waiting copy —
    but never names which group, so nobody forms an impression early."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Session', status=AssessmentSession.Status.LIVE,
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID)
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)
        self.student = Student.objects.create(
            assessment_session=self.session, full_name='Someone', student_id='S1')

    def _join(self):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'S1'})

    @patch('assessments.tasks.activate_pending_group.apply_async')
    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_shows_a_countdown_while_a_transition_is_pending(self, mock_auto, mock_pending):
        turn = activate_turn(self.session, self.group1, duration_seconds=10, gap_seconds=20)
        turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
        turn.save(update_fields=['voting_ends_at'])
        auto_advance(turn)  # holds group2 pending for 20s

        self._join()
        response = self.client.get(f'/assess/{self.session.uuid}/evaluate/')

        self.assertContains(response, 'next-group-countdown')
        self.assertContains(response, 'Next group starting soon')
        self.assertNotContains(response, self.group2.name)

    def test_shows_the_generic_waiting_copy_with_no_pending_transition(self):
        self._join()
        response = self.client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(response, 'Waiting for a group to present')
        # The countdown element itself (identified by its data attribute,
        # not just the id string — which the page's script also mentions
        # unconditionally) should not be rendered when nothing is pending.
        self.assertNotContains(response, 'data-resumes-at')
