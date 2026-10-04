"""Timed voting: activating a group with a duration (voting-only, no
presentation timer) schedules `close_voting_window` for the deadline — that
closes the window but is deliberately non-terminal, leaving the next-group
move to the lecturer. Auto-advance itself is exercised directly, and by
`AutoAdvanceTests` below, which calls `assessments.turns.auto_advance` the
same way the `auto_advance_turn` Celery task does. Celery itself is never
exercised here — `apply_async` is mocked in every test that would trigger
it — so these tests don't need a broker running.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from accounts.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, PresentationGroup, PresentationTurn, Student,
)
from assessments.tasks import auto_advance_turn
from assessments.turns import activate_turn, auto_advance, close_turn


def _expire_now(turn):
    """Backdate a turn's deadline into the past, simulating 'the scheduled
    moment has arrived' without actually waiting for it in a test."""
    turn.voting_ends_at = timezone.now() - timedelta(seconds=1)
    turn.save(update_fields=['voting_ends_at'])


class ActivateTurnTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_no_duration_sets_no_deadline_and_schedules_nothing(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1)
        self.assertIsNone(turn.voting_ends_at)
        mock_apply_async.assert_not_called()
        self.session.refresh_from_db()
        self.assertIsNone(self.session.default_turn_seconds)

    @patch('assessments.tasks.close_voting_window.apply_async')
    def test_duration_sets_deadline_and_schedules_voting_close(self, mock_apply_async):
        # Voting-only mode (no presentation timer) is deliberately
        # non-terminal: reaching the deadline closes the voting window but
        # leaves the next-group move to the lecturer, so this schedules
        # `close_voting_window`, not `auto_advance_turn` — see the comment
        # on `close_voting_window` in assessments/tasks.py.
        before = timezone.now()
        turn = activate_turn(self.session, self.group1, duration_seconds=90)
        after = timezone.now()

        self.assertIsNotNone(turn.voting_ends_at)
        self.assertTrue(before + timedelta(seconds=90) <= turn.voting_ends_at <= after + timedelta(seconds=90))
        mock_apply_async.assert_called_once()
        args, kwargs = mock_apply_async.call_args
        self.assertEqual(args[0], (turn.pk,))
        self.assertEqual(kwargs['eta'], turn.voting_ends_at)

        self.session.refresh_from_db()
        self.assertEqual(self.session.default_turn_seconds, 90)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_activating_closes_previous_active_turn(self, mock_apply_async):
        group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)
        first = activate_turn(self.session, self.group1)
        activate_turn(self.session, group2)
        first.refresh_from_db()
        self.assertEqual(first.status, PresentationTurn.Status.CLOSED)


class AutoAdvanceTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_auto_advance_closes_current_and_opens_next(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=60)
        mock_apply_async.reset_mock()
        _expire_now(turn)

        auto_advance(turn)

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.CLOSED)
        new_turn = self.session.active_turn()
        self.assertIsNotNone(new_turn)
        self.assertEqual(new_turn.group, self.group2)

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_auto_advance_chains_the_same_duration(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=45)
        _expire_now(turn)
        auto_advance(turn)
        new_turn = self.session.active_turn()
        self.assertIsNotNone(new_turn.voting_ends_at)

    def test_auto_advance_with_no_remaining_groups_just_closes(self):
        # Only one group total — nothing left to advance to.
        turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group1,
            status=PresentationTurn.Status.ACTIVE, opened_at=timezone.now(),
            voting_ends_at=timezone.now() - timedelta(seconds=1))
        # Mark group2 as already presented so it's not a candidate either.
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group2,
            status=PresentationTurn.Status.CLOSED, opened_at=timezone.now(),
            closed_at=timezone.now())

        auto_advance(turn)

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.CLOSED)
        self.assertIsNone(self.session.active_turn())

    def test_auto_advance_is_a_noop_if_turn_already_closed_manually(self):
        turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group1,
            status=PresentationTurn.Status.ACTIVE, opened_at=timezone.now())
        close_turn(turn)  # lecturer closed it manually before the timer fired

        auto_advance(turn)  # the (late) scheduled call arrives afterward

        self.assertIsNone(self.session.active_turn())  # did not open group2

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_racing_auto_advance_calls_do_not_skip_a_group(self, mock_apply_async):
        # Regression: with a presentation timer, the `finish_presentation`
        # Celery task and a client's own self-heal ping both land for the
        # same expired deadline. Both used to read the turn as ACTIVE
        # before either had written, so both closed it and both called
        # `_advance_to` — the second `activate_turn` call closed the
        # first's brand-new turn a few milliseconds after opening it
        # (marking that group "presented" without it ever really
        # presenting) and opened a third group in its place, skipping one.
        group3 = PresentationGroup.objects.create(
            assessment_session=self.session, name='Group 3', order=3)
        turn = activate_turn(self.session, self.group1, duration_seconds=60)
        _expire_now(turn)
        # Two independent reads of the same still-ACTIVE turn, simulating
        # the race: both see ACTIVE before either has written.
        turn_a = PresentationTurn.objects.get(pk=turn.pk)
        turn_b = PresentationTurn.objects.get(pk=turn.pk)

        auto_advance(turn_a)
        auto_advance(turn_b)  # the loser of the race — must be a no-op

        self.assertEqual(
            PresentationTurn.objects.filter(status=PresentationTurn.Status.CLOSED).count(), 1)
        active = self.session.active_turn()
        self.assertIsNotNone(active)
        self.assertEqual(active.group, self.group2)  # not skipped to group3

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_auto_advance_is_a_noop_if_a_different_group_was_activated(self, mock_apply_async):
        turn = activate_turn(self.session, self.group1, duration_seconds=30)
        activate_turn(self.session, self.group2)  # lecturer jumped ahead manually

        auto_advance(turn)  # group1's stale scheduled call arrives late

        current = self.session.active_turn()
        self.assertEqual(current.group, self.group2)  # untouched


class AutoAdvanceTurnTaskTests(TestCase):
    """The Celery task wrapper, called directly (no broker involved)."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)

    def test_task_advances_active_turn(self):
        turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group1,
            status=PresentationTurn.Status.ACTIVE, opened_at=timezone.now(),
            voting_ends_at=timezone.now() - timedelta(seconds=1))

        auto_advance_turn(turn.pk)

        turn.refresh_from_db()
        self.assertEqual(turn.status, PresentationTurn.Status.CLOSED)
        self.assertEqual(self.session.active_turn().group, self.group2)

    def test_task_tolerates_a_deleted_turn(self):
        auto_advance_turn(999999)  # must not raise


class NextGroupAfterTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.g1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', order=1)
        self.g2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)
        self.g3 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 3', order=3)

    def test_returns_next_unpresented_group_in_order(self):
        self.assertEqual(self.session.next_group_after(self.g1), self.g2)

    def test_skips_already_presented_groups(self):
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.g2,
            status=PresentationTurn.Status.CLOSED, opened_at=timezone.now(), closed_at=timezone.now())
        self.assertEqual(self.session.next_group_after(self.g1), self.g3)

    def test_none_when_every_other_group_presented(self):
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.g2,
            status=PresentationTurn.Status.CLOSED, opened_at=timezone.now(), closed_at=timezone.now())
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.g3,
            status=PresentationTurn.Status.CLOSED, opened_at=timezone.now(), closed_at=timezone.now())
        self.assertIsNone(self.session.next_group_after(self.g1))


class ActivateGroupViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session')
        self.group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')

    @patch('assessments.tasks.close_voting_window.apply_async')
    def test_activate_with_duration_sets_deadline(self, mock_apply_async):
        response = self.client.post(
            f'/assessments/{self.session.pk}/activate/{self.group.pk}/',
            {'duration_seconds': '120'})
        self.assertEqual(response.status_code, 302)
        turn = self.session.active_turn()
        self.assertIsNotNone(turn.voting_ends_at)
        mock_apply_async.assert_called_once()

    @patch('assessments.tasks.auto_advance_turn.apply_async')
    def test_activate_without_duration_leaves_no_deadline(self, mock_apply_async):
        response = self.client.post(f'/assessments/{self.session.pk}/activate/{self.group.pk}/')
        self.assertEqual(response.status_code, 302)
        turn = self.session.active_turn()
        self.assertIsNone(turn.voting_ends_at)
        mock_apply_async.assert_not_called()

    def test_invalid_duration_is_ignored_not_rejected(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/activate/{self.group.pk}/',
            {'duration_seconds': 'not-a-number'})
        self.assertEqual(response.status_code, 302)
        turn = self.session.active_turn()
        self.assertIsNone(turn.voting_ends_at)
