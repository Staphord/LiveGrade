"""Co-assessment rules at their edges: clocks, hostile score input, quorum, moderation, rosters."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.sheets import workbook_bytes
from service_domains import coassessment_operations as ops
from service_domains.models import (
    CoAssessmentCriterion, CoAssessmentExaminer, CoAssessmentGroup, CoAssessmentParticipant,
    CoAssessmentScorecard, CoAssessmentSession, CoAssessmentTurn)


class Fixture(TestCase):
    def setUp(self):
        self.org = make_org('Ops org')
        self.owner = make_user('ops-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Defense',
            status=CoAssessmentSession.Status.ACTIVE, presentation_duration_seconds=60,
            qa_duration_seconds=60, minimum_panel_evaluators=1, minimum_peer_evaluators=1)
        self.crit_group = self.criterion('Architecture', CoAssessmentCriterion.Scope.GROUP, 60, 1)
        self.crit_ind = self.criterion('Q and A', CoAssessmentCriterion.Scope.INDIVIDUAL, 40, 2)
        self.group = self.make_group('Aurora', 1)
        self.other_group = self.make_group('Borealis', 2)
        self.alice = self.person('Alice', 'S1', self.group)
        self.bob = self.person('Bob', 'S2', self.group)
        self.peer = self.person('Pat', 'S3', self.other_group)
        self.examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr Cho', email='cho@example.org', invite_token='t', token_digest='d' * 64)

    def criterion(self, name, scope, weight, order):
        return CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name=name, scope=scope, weight=Decimal(weight), order=order)

    def make_group(self, name, order):
        return CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, name=name, order=order)

    def person(self, name, student_id, group):
        return CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            group=group, full_name=name, student_id=student_id)

    def stage(self, group=None):
        return ops.activate_stage_turn(self.session, group or self.group, user=self.owner)


class StageTests(Fixture):
    def test_clock_moves_to_questions_after_the_presentation(self):
        turn = self.stage()
        turn.opened_at = timezone.now() - timedelta(seconds=90)
        turn.save(update_fields=['opened_at'])
        state = ops.get_coassessment_live_state(self.session)
        self.assertEqual(state['phase'], 'qa')
        self.assertEqual(state['presentation_remaining_seconds'], 0)
        self.assertLess(state['qa_remaining_seconds'], 60)

    def test_broadcast_is_best_effort(self):
        self.assertIsNone(ops.broadcast_coassessment_update(999999))
        with patch.object(ops, 'get_channel_layer', return_value=None):
            self.assertIsNone(ops.broadcast_coassessment_update(self.session.pk))
        with patch.object(ops, 'async_to_sync', side_effect=RuntimeError('down')):
            self.assertIsNone(ops.broadcast_coassessment_update(self.session.pk))

    def test_a_foreign_team_cannot_be_called(self):
        other = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Other')
        foreign = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=other, name='Foreign', order=1)
        with self.assertRaises(ValidationError):
            self.stage(foreign)

    def test_a_waiting_turn_is_reopened_and_resuming_adds_the_paused_time(self):
        waiting = CoAssessmentTurn.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, group=self.group,
            status=CoAssessmentTurn.Status.PENDING)
        reopened = self.stage()
        self.assertEqual(reopened.pk, waiting.pk)
        self.assertEqual(reopened.status, CoAssessmentTurn.Status.ACTIVE)
        ops.pause_stage_turn(self.session, user=self.owner)
        turn = self.session.turns.get(pk=waiting.pk)
        turn.paused_at = timezone.now() - timedelta(seconds=30)
        turn.save(update_fields=['paused_at'])
        resumed = ops.pause_stage_turn(self.session, user=self.owner)
        self.assertGreaterEqual(resumed.total_paused_seconds, 30)
        resumed.is_paused = True
        resumed.paused_at = None
        resumed.save(update_fields=['is_paused', 'paused_at'])
        self.assertFalse(ops.pause_stage_turn(self.session, user=self.owner).is_paused)

    def test_concluding_with_nobody_on_stage_does_nothing(self):
        self.assertIsNone(ops.conclude_stage_turn(self.session, user=self.owner))


class ScoringInputTests(Fixture):
    def test_hostile_score_input_is_skipped_or_rejected(self):
        turn = self.stage()
        late = self.person('Late', 'S9', self.group)
        card = ops.submit_peer_scorecard(
            turn, self.peer,
            {'': '5', 'junk': '5', str(self.crit_group.pk): '', '999999': '5', None: '1'},
            individual_scores={
                (self.crit_ind.pk, self.alice.pk): '',
                (self.crit_ind.pk, self.bob.pk): '70',
                (late.pk, self.crit_ind.pk): '60',
                (999999, 999999): '10',
                'bad': 'text',
                'x': {'1': '2'},
                str(self.alice.pk): {'': '1', 'z': '1', '999999': '1', str(self.crit_ind.pk): '',
                                     str(self.crit_ind.pk) + ' ': '50'},
                '999999': {str(self.crit_ind.pk): '50'},
            })
        scores = {(v.criterion_id, v.target_participant_id): v.score for v in card.values.all()}
        self.assertEqual(scores[(self.crit_ind.pk, self.bob.pk)], Decimal('70'))
        self.assertEqual(scores[(self.crit_ind.pk, self.alice.pk)], Decimal('50'))
        self.assertEqual(scores[(self.crit_ind.pk, late.pk)], Decimal('60'))
        self.assertNotIn((999999, 999999), scores)

    def test_out_of_range_scores_are_rejected(self):
        turn = self.stage()
        cases = (
            ({self.crit_group.pk: '101'}, None),
            ({}, {(self.crit_ind.pk, self.alice.pk): '-1'}),
            ({}, {str(self.alice.pk): {str(self.crit_ind.pk): '500'}}),
        )
        for group_scores, individual in cases:
            with self.assertRaises(ValidationError):
                ops.submit_peer_scorecard(turn, self.peer, group_scores, individual_scores=individual)

    def test_foreign_raters_and_closed_turns_are_refused(self):
        turn = self.stage()
        foreign = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Elsewhere')
        outsider = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=foreign, full_name='Out', student_id='X')
        stranger = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=foreign,
            display_name='Far', email='far@example.org', invite_token='f', token_digest='e' * 64)
        with self.assertRaises(ValidationError):
            ops.submit_peer_scorecard(turn, outsider, {})
        with self.assertRaises(ValidationError):
            ops.submit_examiner_scorecard(turn, stranger, {})
        ops.conclude_stage_turn(self.session, user=self.owner)
        turn.refresh_from_db()
        with self.assertRaises(ValidationError):
            ops.submit_examiner_scorecard(turn, self.examiner, {})


class ResultTests(Fixture):
    def test_empty_scorecards_are_ignored(self):
        turn = self.stage()
        CoAssessmentScorecard.objects.create(
            organization_id=self.org.pk, created_by=self.owner, turn=turn,
            evaluator_tier=CoAssessmentScorecard.Tier.PEER, peer_evaluator=self.peer)
        result = ops.compute_turn_results(turn)
        self.assertIsNone(result['peer_score'])
        self.assertEqual(result['peer_raters'], 0)

    def test_peer_only_marks_stand_in_when_the_panel_has_not_scored(self):
        turn = self.stage()
        ops.submit_peer_scorecard(turn, self.peer, {self.crit_group.pk: '80'},
                                  individual_scores={(self.crit_ind.pk, self.alice.pk): '70'})
        result = ops.compute_turn_results(turn)
        self.assertEqual(result['combined_score'], result['peer_score'])
        self.assertEqual(result['group_mark'], Decimal('48.00'))

    def test_moderation_rescales_each_member_mark(self):
        turn = self.stage()
        ops.submit_peer_scorecard(turn, self.peer, {self.crit_group.pk: '80'},
                                  individual_scores={(self.crit_ind.pk, self.alice.pk): '70',
                                                     (self.crit_ind.pk, self.bob.pk): '70'})
        ops.record_chair_moderation(turn, 'adjusted', '40.00', 'Calibrated', user=self.owner)
        result = ops.compute_turn_results(turn)
        self.assertEqual(result['final_mark'], Decimal('40.00'))
        self.assertTrue(all(row['final_score'] <= row['group_score'] + row['individual_score']
                            for row in result['member_results']))

    def test_sign_off_and_release_guards(self):
        with self.assertRaises(ValidationError):
            ops.release_session_results(self.session, user=self.owner)
        empty = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Empty',
            status=CoAssessmentSession.Status.ACTIVE)
        with self.assertRaises(ValidationError):
            ops.sign_off_session(empty, user=self.owner)

    def test_sign_off_needs_quorum_or_a_moderation_decision(self):
        for group in (self.group, self.other_group):
            self.stage(group)
        ops.conclude_stage_turn(self.session, user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'quorum'):
            ops.sign_off_session(self.session, user=self.owner)


class RosterTests(Fixture):
    def parse(self, headers, rows):
        return ops.parse_roster_spreadsheet(workbook_bytes(headers, rows))

    def test_unreadable_and_empty_workbooks_are_reported(self):
        import io
        self.assertTrue(ops.parse_roster_spreadsheet(io.BytesIO(b'not a workbook'))['fatal'])
        with patch.object(ops, 'read_rows', return_value=[]):
            self.assertIn('empty', ops.parse_roster_spreadsheet(io.BytesIO(b''))['fatal'])
        self.assertIn('Group', self.parse(['Name'], [['Ada']])['fatal'])

    def test_blank_rows_and_nameless_teams_are_skipped(self):
        parsed = self.parse(['Group', 'Name'], [[None, None], ['', 'Ada'], ['Team', 'Bo, Cy']])
        self.assertEqual([group['name'] for group in parsed['groups']], ['Team'])
        self.assertEqual(ops._split_cell(['x'], None), [])
        self.assertEqual(ops._split_cell(['x'], 5), [])

    def test_applying_a_roster_updates_existing_people(self):
        ops.apply_parsed_roster(self.session, [
            {'name': 'Aurora', 'members': [
                {'full_name': 'Alice Updated', 'student_id': 'S1', 'email': 'a@example.org'},
                {'full_name': '', 'student_id': ''},
                {'full_name': 'New Kid', 'student_id': '', 'email': ''},
                {'full_name': 'New Kid', 'student_id': '', 'email': 'kid@example.org'}]}], user=self.owner)
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.full_name, self.alice.email), ('Alice Updated', 'a@example.org'))
        ops.apply_parsed_roster(self.session, [
            {'name': 'Aurora', 'members': [{'full_name': '', 'student_id': 'S1'}]}], user=self.owner)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.full_name, 'Alice Updated')

    def test_deleting_nothing_is_a_no_op(self):
        self.assertEqual(ops.delete_coassessment_sessions([]), 0)
