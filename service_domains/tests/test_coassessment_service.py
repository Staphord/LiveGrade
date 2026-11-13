"""Tests for the Co-Assessment domain models, operations, forms, and views."""

import csv
from decimal import Decimal
import hashlib
import io
import json
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.models import (
    CoAssessmentSession,
    CoAssessmentCriterion,
    CoAssessmentLevelDescriptor,
    CoAssessmentGroup,
    CoAssessmentParticipant,
    CoAssessmentExaminer,
    CoAssessmentTurn,
    CoAssessmentScorecard,
    CoAssessmentScoreValue,
    CoAssessmentModeration,
)
from service_domains.coassessment_operations import (
    activate_stage_turn,
    apply_parsed_roster,
    compute_session_results,
    compute_turn_results,
    conclude_stage_turn,
    get_coassessment_live_state,
    parse_roster_spreadsheet,
    pause_stage_turn,
    record_chair_moderation,
    release_session_results,
    sign_off_session,
    submit_examiner_scorecard,
    submit_peer_scorecard,
)
from service_domains.coassessment_forms import (
    CoAssessmentPolicyForm,
    CoAssessmentCriterionForm,
    CoAssessmentGroupForm,
    CoAssessmentParticipantForm,
    CoAssessmentExaminerForm,
    CoAssessmentModerationForm,
)
from service_domains.coassessment_views import coassessment_public_results


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentPolicyAndWizardTests(TestCase):
    """Tests for wizard steps, policy configuration, and weight validation."""

    def setUp(self):
        self.org = make_org('CoAssessment Wizard Org')
        self.owner = make_user('co-wizard-owner')
        self.other_user = make_user('co-wizard-other')
        sign_in(self.client, self.owner, self.org)

    def test_policy_form_validates_100_percent_weight_sum(self):
        # Valid 60/40 split
        valid_form = CoAssessmentPolicyForm(data={
            'name': 'Capstone 2026',
            'panel_weight': '60.00',
            'peer_weight': '40.00',
            'moderation_threshold': '10.00',
            'minimum_panel_evaluators': 2,
            'minimum_peer_evaluators': 5,
            'presentation_duration_seconds': 600,
            'qa_duration_seconds': 300,
            'allow_anonymous_peer_comments': True,
        })
        self.assertTrue(valid_form.is_valid(), valid_form.errors)

        # Invalid split summing to 90%
        invalid_form = CoAssessmentPolicyForm(data={
            'name': 'Capstone 2026',
            'panel_weight': '50.00',
            'peer_weight': '40.00',
            'moderation_threshold': '10.00',
            'minimum_panel_evaluators': 2,
            'minimum_peer_evaluators': 5,
            'presentation_duration_seconds': 600,
            'qa_duration_seconds': 300,
            'allow_anonymous_peer_comments': True,
        })
        self.assertFalse(invalid_form.is_valid())
        self.assertIn('peer_weight', invalid_form.errors)
        self.assertIn('total exactly 100%', invalid_form.errors['peer_weight'][0])

    def test_wizard_create_step1_persists_draft_session(self):
        url = reverse('coassessment_create')
        response = self.client.post(url, {
            'name': 'Engineering Defenses 2026',
            'description': 'Auditorium 101',
            'panel_weight': '70.00',
            'peer_weight': '30.00',
            'moderation_threshold': '8.00',
            'minimum_panel_evaluators': 3,
            'minimum_peer_evaluators': 10,
            'presentation_duration_seconds': 720,
            'qa_duration_seconds': 240,
            'allow_anonymous_peer_comments': 'on',
        })
        session = CoAssessmentSession.objects.filter(name='Engineering Defenses 2026').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.status, CoAssessmentSession.Status.DRAFT)
        self.assertEqual(session.panel_weight, Decimal('70.00'))
        self.assertEqual(session.peer_weight, Decimal('30.00'))
        self.assertRedirects(response, reverse('coassessment_setup_rubric', kwargs={'session_id': session.pk}))

    def test_wizard_step2_rubric_management(self):
        session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Rubric Test Session',
        )
        url = reverse('coassessment_setup_rubric', kwargs={'session_id': session.pk})

        # Add criterion 1
        resp = self.client.post(url, {
            'action': 'add_criterion',
            'name': 'Technical Depth',
            'weight': '60.00',
            'description': 'Evaluation of architecture and algorithms.',
            'order': 1,
        })
        self.assertRedirects(resp, url)
        self.assertEqual(session.criteria.count(), 1)
        crit1 = session.criteria.first()
        self.assertEqual(crit1.name, 'Technical Depth')
        self.assertEqual(crit1.weight, Decimal('60.00'))

        # Add criterion 2
        self.client.post(url, {
            'action': 'add_criterion',
            'name': 'Presentation Delivery',
            'weight': '40.00',
            'description': 'Clarity, slide design, and timing.',
            'order': 2,
        })
        self.assertEqual(session.criteria.count(), 2)

    def test_wizard_step3_roster_group_management(self):
        session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Roster Test Session',
        )
        url = reverse('coassessment_setup_roster', kwargs={'session_id': session.pk})

        # Add group
        resp = self.client.post(url, {
            'action': 'add_group',
            'name': 'Alpha Team',
            'topic': 'Solar Energy Optimization',
            'location': 'Pod A',
            'order': 1,
        })
        self.assertRedirects(resp, url)
        self.assertEqual(session.groups.count(), 1)
        grp = session.groups.first()
        self.assertEqual(grp.name, 'Alpha Team')
        self.assertEqual(grp.topic, 'Solar Energy Optimization')

        # Add participant to group
        participant = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=session,
            group=grp,
            full_name='Alice Smith',
            student_id='STU-1001',
            email='alice@example.com',
        )
        self.assertEqual(grp.members.count(), 1)

    def test_wizard_step4_examiner_invitation_creates_secure_token(self):
        session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Examiner Test Session',
        )
        url = reverse('coassessment_setup_examiners', kwargs={'session_id': session.pk})

        resp = self.client.post(url, {
            'action': 'invite_examiner',
            'display_name': 'Prof. Charles Xavier',
            'email': 'cxavier@example.edu',
            'is_chair': 'on',
        })
        self.assertRedirects(resp, url)
        examiner = session.examiners.first()
        self.assertIsNotNone(examiner)
        self.assertEqual(examiner.display_name, 'Prof. Charles Xavier')
        self.assertTrue(examiner.is_chair)
        self.assertTrue(examiner.token_digest)
        self.assertEqual(len(examiner.token_digest), 64)

    def test_publish_validation_blocks_imbalanced_weights_or_missing_roster(self):
        session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Publish Session',
        )
        url = reverse('coassessment_publish', kwargs={'session_id': session.pk})

        # Attempt to publish with no criteria (weight = 0 != 100)
        resp1 = self.client.post(url)
        self.assertRedirects(resp1, reverse('coassessment_setup_rubric', kwargs={'session_id': session.pk}))
        session.refresh_from_db()
        self.assertEqual(session.status, CoAssessmentSession.Status.DRAFT)

        # Add criteria totaling 100%
        CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=session,
            name='Criterion 1', weight=Decimal('100.00'), order=1,
        )

        # Attempt to publish without presenting groups
        resp2 = self.client.post(url)
        self.assertRedirects(resp2, reverse('coassessment_setup_roster', kwargs={'session_id': session.pk}))
        session.refresh_from_db()
        self.assertEqual(session.status, CoAssessmentSession.Status.DRAFT)

        # Add group
        CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=session,
            name='Beta Team', topic='Robotics', order=1,
        )

        # Publish should now succeed
        resp3 = self.client.post(url)
        session.refresh_from_db()
        self.assertEqual(session.status, CoAssessmentSession.Status.ACTIVE)
        self.assertTrue(session.public_join_code)
        self.assertRedirects(resp3, reverse('coassessment_live_console', kwargs={'session_id': session.pk}))


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentAtomicStageTurnTests(TestCase):
    """Tests atomic stage turns, transitions, sequential advancement, and validation."""

    def setUp(self):
        self.org = make_org('CoAssessment Turn Org')
        self.owner = make_user('co-turn-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Live Session',
            status=CoAssessmentSession.Status.ACTIVE,
        )
        self.group1 = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Team One', order=1,
        )
        self.group2 = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Team Two', order=2,
        )
        self.group3 = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Team Three', order=3,
        )
        sign_in(self.client, self.owner, self.org)

    def test_activate_stage_turn_opens_turn_and_closes_prior_turn(self):
        turn1 = activate_stage_turn(self.session, self.group1, user=self.owner)
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.ACTIVE)
        self.assertIsNotNone(turn1.opened_at)
        self.assertIsNone(turn1.closed_at)

        # Activate group 2
        turn2 = activate_stage_turn(self.session, self.group2, user=self.owner)
        turn1.refresh_from_db()
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.CLOSED)
        self.assertIsNotNone(turn1.closed_at)
        self.assertEqual(turn2.status, CoAssessmentTurn.Status.ACTIVE)
        self.assertIsNotNone(turn2.opened_at)

    def test_activating_closed_turn_raises_validation_error(self):
        turn1 = activate_stage_turn(self.session, self.group1, user=self.owner)
        activate_stage_turn(self.session, self.group2, user=self.owner)
        turn1.refresh_from_db()
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.CLOSED)

        with self.assertRaises(ValidationError) as ctx:
            activate_stage_turn(self.session, self.group1, user=self.owner)
        self.assertIn('already presented and its turn is closed', str(ctx.exception))

    def test_activating_turn_in_inactive_session_raises_validation_error(self):
        self.session.status = CoAssessmentSession.Status.DRAFT
        self.session.save(update_fields=['status'])
        with self.assertRaises(ValidationError) as ctx:
            activate_stage_turn(self.session, self.group1, user=self.owner)
        self.assertIn('session must be active', str(ctx.exception))

    def test_pause_stage_turn_toggles_pause_state(self):
        activate_stage_turn(self.session, self.group1, user=self.owner)
        paused = pause_stage_turn(self.session, user=self.owner)
        self.assertIsNotNone(paused)
        self.assertEqual(paused.status, CoAssessmentTurn.Status.ACTIVE)
        self.assertTrue(paused.is_paused)

        # Pausing again toggles resume
        resumed = pause_stage_turn(self.session, user=self.owner)
        self.assertFalse(resumed.is_paused)

    def test_conclude_stage_turn_closes_active_turn(self):
        activate_stage_turn(self.session, self.group1, user=self.owner)
        concluded = conclude_stage_turn(self.session, user=self.owner)
        self.assertIsNotNone(concluded)
        self.assertEqual(concluded.status, CoAssessmentTurn.Status.CLOSED)

    def test_advance_turn_view_sequentially_moves_through_lineup(self):
        url = reverse('coassessment_advance_turn', kwargs={'session_id': self.session.pk})

        # Advance 1: calls group 1
        self.client.post(url)
        active_turn = self.session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
        self.assertEqual(active_turn.group, self.group1)

        # Advance 2: locks group 1 and calls group 2
        self.client.post(url)
        active_turn = self.session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
        self.assertEqual(active_turn.group, self.group2)
        turn1 = self.session.turns.get(group=self.group1)
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.CLOSED)


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentScoringAndSelfVotingTests(TestCase):
    """Tests peer and examiner scoring, self-voting prevention, and validation rules."""

    def setUp(self):
        self.org = make_org('CoAssessment Scoring Org')
        self.owner = make_user('co-score-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Defenses Live',
            status=CoAssessmentSession.Status.ACTIVE,
            panel_weight=Decimal('60.00'),
            peer_weight=Decimal('40.00'),
            moderation_threshold=Decimal('10.00'),
            minimum_panel_evaluators=1,
            minimum_peer_evaluators=1,
        )
        self.crit1 = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Content', weight=Decimal('50.00'), order=1,
        )
        self.crit2 = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Execution', weight=Decimal('50.00'), order=2,
        )
        self.presenting_group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Presenters Inc', order=1,
        )
        self.other_group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Audience Group', order=2,
        )
        self.presenter_student = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            group=self.presenting_group, full_name='Alice Presenter', student_id='STU-A',
        )
        self.peer_student = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            group=self.other_group, full_name='Bob Peer', student_id='STU-B',
        )
        self.examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr. Smith', email='drsmith@example.edu',
        )
        self.turn = activate_stage_turn(self.session, self.presenting_group, user=self.owner)

    def test_self_voting_prevention_raises_validation_error(self):
        scores = {self.crit1.pk: 95, self.crit2.pk: 90}
        with self.assertRaises(ValidationError) as ctx:
            submit_peer_scorecard(self.turn, self.presenter_student, scores, feedback='Great work')
        self.assertIn('prohibited from evaluating their own team', str(ctx.exception))

    def test_peer_from_another_group_submits_scorecard_successfully(self):
        scores = {self.crit1.pk: 85, self.crit2.pk: 75}
        card = submit_peer_scorecard(self.turn, self.peer_student, scores, feedback='Solid explanation')
        self.assertEqual(card.evaluator_tier, CoAssessmentScorecard.Tier.PEER)
        self.assertEqual(card.peer_evaluator, self.peer_student)
        self.assertEqual(card.values.count(), 2)

        v1 = card.values.get(criterion=self.crit1)
        self.assertEqual(v1.score, Decimal('85'))
        v2 = card.values.get(criterion=self.crit2)
        self.assertEqual(v2.score, Decimal('75'))

    def test_duplicate_peer_scorecard_raises_validation_error(self):
        scores = {self.crit1.pk: 80, self.crit2.pk: 80}
        submit_peer_scorecard(self.turn, self.peer_student, scores)
        with self.assertRaises(ValidationError) as ctx:
            submit_peer_scorecard(self.turn, self.peer_student, scores)
        self.assertIn('already submitted a scorecard', str(ctx.exception))

    def test_submitting_scorecard_on_closed_turn_raises_validation_error(self):
        conclude_stage_turn(self.session, user=self.owner)
        self.turn.refresh_from_db()
        self.assertEqual(self.turn.status, CoAssessmentTurn.Status.CLOSED)

        scores = {self.crit1.pk: 80, self.crit2.pk: 80}
        with self.assertRaises(ValidationError) as ctx:
            submit_peer_scorecard(self.turn, self.peer_student, scores)
        self.assertIn('Scoring is closed for this presentation', str(ctx.exception))

    def test_examiner_scorecard_submission_and_in_turn_revision(self):
        scores = {self.crit1.pk: 90, self.crit2.pk: 92}
        card = submit_examiner_scorecard(self.turn, self.examiner, scores, feedback='Excellent rigour')
        self.assertEqual(card.evaluator_tier, CoAssessmentScorecard.Tier.PANEL)
        self.assertEqual(card.examiner_evaluator, self.examiner)

        # In-turn revision allowed while turn is active
        revised_scores = {self.crit1.pk: 95, self.crit2.pk: 96}
        updated_card = submit_examiner_scorecard(self.turn, self.examiner, revised_scores, feedback='Revised after Q&A')
        self.assertEqual(updated_card.pk, card.pk)
        self.assertEqual(updated_card.qualitative_feedback, 'Revised after Q&A')
        val1 = updated_card.values.get(criterion=self.crit1)
        self.assertEqual(val1.score, Decimal('95.00'))

    def test_score_values_must_be_within_0_to_100_range(self):
        invalid_scores = {self.crit1.pk: 120, self.crit2.pk: 80}
        with self.assertRaises(ValidationError) as ctx:
            submit_peer_scorecard(self.turn, self.peer_student, invalid_scores)
        self.assertIn('between 0 and 100', str(ctx.exception))


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentCalculationAndModerationTests(TestCase):
    """Tests score calculations, discrepancy threshold detection, and moderation."""

    def setUp(self):
        self.org = make_org('CoAssessment Calc Org')
        self.owner = make_user('co-calc-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Capstone Scoring Session',
            status=CoAssessmentSession.Status.ACTIVE,
            panel_weight=Decimal('60.00'),
            peer_weight=Decimal('40.00'),
            moderation_threshold=Decimal('10.00'),
            minimum_panel_evaluators=1,
            minimum_peer_evaluators=2,
        )
        self.crit = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Overall', weight=Decimal('100.00'), order=1,
        )
        self.group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Delta Team', order=1,
        )
        self.peer1 = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            full_name='Peer One', student_id='STU-1',
        )
        self.peer2 = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            full_name='Peer Two', student_id='STU-2',
        )
        self.examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Chair Examiner', is_chair=True,
        )
        self.turn = activate_stage_turn(self.session, self.group, user=self.owner)

    def test_turn_calculations_and_discrepancy_trigger(self):
        # Examiner gives 90
        submit_examiner_scorecard(self.turn, self.examiner, {self.crit.pk: 90})
        # Peer 1 gives 70, Peer 2 gives 72 (average = 71.00)
        submit_peer_scorecard(self.turn, self.peer1, {self.crit.pk: 70})
        submit_peer_scorecard(self.turn, self.peer2, {self.crit.pk: 72})

        results = compute_turn_results(self.turn)
        self.assertEqual(results['panel_score'], Decimal('90.00'))
        self.assertEqual(results['panel_raters'], 1)
        self.assertTrue(results['panel_quorum_met'])
        self.assertEqual(results['peer_score'], Decimal('71.00'))
        self.assertEqual(results['peer_raters'], 2)
        self.assertTrue(results['peer_quorum_met'])

        # Discrepancy = |90 - 71| = 19.00 > threshold 10.00
        self.assertEqual(results['discrepancy'], Decimal('19.00'))
        self.assertTrue(results['moderation_required'])

        # Weighted calculation: (90 * 0.60) + (71 * 0.40) = 54 + 28.4 = 82.40
        self.assertEqual(results['combined_score'], Decimal('82.40'))
        self.assertEqual(results['final_mark'], Decimal('82.40'))

    def test_sign_off_blocks_when_moderation_is_pending(self):
        # Create discrepancy
        submit_examiner_scorecard(self.turn, self.examiner, {self.crit.pk: 95})
        submit_peer_scorecard(self.turn, self.peer1, {self.crit.pk: 60})
        submit_peer_scorecard(self.turn, self.peer2, {self.crit.pk: 60})
        conclude_stage_turn(self.session, user=self.owner)

        with self.assertRaises(ValidationError) as ctx:
            sign_off_session(self.session, user=self.owner)
        self.assertIn('requires a recorded chair moderation decision before sign-off', str(ctx.exception))

    def test_sign_off_blocks_when_presentations_remain_unpresented(self):
        # self.turn is still ACTIVE
        with self.assertRaises(ValidationError) as ctx:
            sign_off_session(self.session, user=self.owner)
        self.assertIn('All scheduled teams must complete their presentation turns before sign-off', str(ctx.exception))

    def test_record_chair_moderation_resolves_discrepancy_and_enables_sign_off_and_release(self):
        submit_examiner_scorecard(self.turn, self.examiner, {self.crit.pk: 95})
        submit_peer_scorecard(self.turn, self.peer1, {self.crit.pk: 60})
        submit_peer_scorecard(self.turn, self.peer2, {self.crit.pk: 60})
        conclude_stage_turn(self.session, user=self.owner)

        # Moderate and set final score to 80.00
        mod = record_chair_moderation(
            self.turn,
            action=CoAssessmentModeration.Action.OVERRIDE_PANEL,
            final_score=Decimal('80.00'),
            justification='Peer scoring lacked technical calibration; panel calibrated score adjusted.',
            user=self.owner,
        )
        self.assertEqual(mod.final_score, Decimal('80.00'))

        # Check turn results reflect moderation
        results = compute_turn_results(self.turn)
        self.assertEqual(results['final_mark'], Decimal('80.00'))
        self.assertIsNotNone(results['latest_moderation'])

        # Sign-off now succeeds
        signed_session = sign_off_session(self.session, user=self.owner)
        self.assertEqual(signed_session.status, CoAssessmentSession.Status.SIGNED_OFF)
        self.assertIsNotNone(signed_session.signed_off_at)

        # Release now succeeds
        released_session = release_session_results(signed_session, user=self.owner)
        self.assertEqual(released_session.status, CoAssessmentSession.Status.RELEASED)
        self.assertIsNotNone(released_session.released_at)

    def test_moderation_requires_non_empty_justification(self):
        with self.assertRaises(ValidationError) as ctx:
            record_chair_moderation(
                self.turn,
                action=CoAssessmentModeration.Action.APPROVE_COMBINED,
                final_score=Decimal('85.00'),
                justification='   ',
                user=self.owner,
            )
        self.assertIn('justification is required', str(ctx.exception))


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentSpreadsheetRosterTests(TestCase):
    """Tests parsing and applying roster spreadsheets."""

    def setUp(self):
        self.org = make_org('CoAssessment Roster Org')
        self.owner = make_user('co-roster-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Roster Upload Session',
        )

    def test_parse_roster_spreadsheet_from_csv(self):
        headers = ['Group', 'Topic', 'Student Name', 'Student ID', 'Email']
        rows = [
            ['Team Solar', 'Renewable Grid', 'Alice Green', 'STU-201', 'alice@example.com'],
            ['Team Solar', 'Renewable Grid', 'Bob Blue', 'STU-202', 'bob@example.com'],
            ['Team Wind', 'Turbine Design', 'Charlie White', 'STU-203', 'charlie@example.com'],
        ]
        wb = workbook_bytes(headers, rows)
        f = SimpleUploadedFile("roster.xlsx", wb.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        result = parse_roster_spreadsheet(f)

        self.assertIsNone(result['fatal'])
        groups = result['groups']
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]['name'], 'Team Solar')
        self.assertEqual(len(groups[0]['members']), 2)
        self.assertEqual(groups[1]['name'], 'Team Wind')
        self.assertEqual(len(groups[1]['members']), 1)

        # Apply to session
        apply_parsed_roster(self.session, groups, user=self.owner)
        self.assertEqual(self.session.groups.count(), 2)
        self.assertEqual(self.session.participants.count(), 3)

    def test_parse_roster_spreadsheet_missing_group_header_returns_fatal(self):
        headers = ['WrongCol', 'Topic', 'Student Name']
        rows = [['A', 'B', 'C']]
        wb = workbook_bytes(headers, rows)
        f = SimpleUploadedFile("roster.xlsx", wb.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        result = parse_roster_spreadsheet(f)
        self.assertIsNotNone(result['fatal'])
        self.assertIn('Could not find a "Group" or "Team" column', result['fatal'])


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentViewIntegrationAndTenantTests(TestCase):
    """End-to-end HTTP tests covering dashboard, console, scorecards, and multi-tenant security."""

    def setUp(self):
        self.org = make_org('Main University')
        self.other_org = make_org('Other University')
        self.owner = make_user('lead-organizer')
        self.intruder = make_user('intruder-user')

        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Autonomous Vehicle Presentations',
            status=CoAssessmentSession.Status.ACTIVE,
            public_join_code='AVP26',
            panel_weight=Decimal('60.00'),
            peer_weight=Decimal('40.00'),
            moderation_threshold=Decimal('10.00'),
            minimum_panel_evaluators=1,
            minimum_peer_evaluators=1,
        )
        self.crit = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Safety Analysis', weight=Decimal('100.00'), order=1,
        )
        self.group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Vision AI Group', topic='LIDAR Object Detection', order=1,
        )
        self.participant = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            full_name='David Kim', student_id='DKIM-99',
        )
        self.examiner_token = 'secret-examiner-token-123'
        self.examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            display_name='Dr. Helen Cho',
            token_digest=hashlib.sha256(self.examiner_token.encode('utf-8')).hexdigest(),
            is_chair=True,
        )
        self.turn = activate_stage_turn(self.session, self.group, user=self.owner)
        sign_in(self.client, self.owner, self.org)

    def test_dashboard_renders_kpis_and_live_attention(self):
        url = reverse('coassessment_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Co-Assessment dashboard')
        self.assertContains(response, 'Autonomous Vehicle Presentations')
        self.assertContains(response, 'Live and moderation queue')
        self.assertContains(response, 'Total co-assessments')
        self.assertContains(response, 'In progress')
        self.assertContains(response, 'Presenting groups')

    def test_table_renders_with_search_and_filters(self):
        url = reverse('coassessment_table')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Autonomous Vehicle Presentations')

        # Status filter
        filtered_resp = self.client.get(url, {'status': 'active'})
        self.assertEqual(filtered_resp.status_code, 200)
        self.assertContains(filtered_resp, 'Autonomous Vehicle Presentations')

        draft_resp = self.client.get(url, {'status': 'draft'})
        self.assertEqual(draft_resp.status_code, 200)
        self.assertNotContains(draft_resp, 'Autonomous Vehicle Presentations')

        # Search query filter
        search_resp = self.client.get(url, {'q': 'Autonomous'})
        self.assertEqual(search_resp.status_code, 200)
        self.assertContains(search_resp, 'Autonomous Vehicle Presentations')

        nomatch_resp = self.client.get(url, {'q': 'NonexistentEventNameXYZ'})
        self.assertEqual(nomatch_resp.status_code, 200)
        self.assertNotContains(nomatch_resp, 'Autonomous Vehicle Presentations')
        self.assertContains(nomatch_resp, 'No matching co-assessments')

    def test_table_ajax_request_returns_only_results_partial(self):
        url = reverse('coassessment_table')
        response = self.client.get(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="coassessment-results"')
        self.assertNotContains(response, '<!DOCTYPE html>')
        self.assertNotContains(response, '<nav class="nav-shell"')

    def test_single_delete_deletes_session_and_redirects(self):
        del_url = reverse('coassessment_delete', kwargs={'session_id': self.session.pk})
        resp = self.client.post(del_url)
        self.assertRedirects(resp, reverse('coassessment_table'))
        self.assertFalse(CoAssessmentSession.objects.filter(pk=self.session.pk).exists())

    def test_bulk_delete_deletes_selected_sessions(self):
        s2 = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Second Co-Assessment',
            status=CoAssessmentSession.Status.DRAFT,
        )
        bulk_url = reverse('coassessment_bulk_delete')
        resp = self.client.post(bulk_url, {'session_ids': [str(self.session.pk), str(s2.pk)]})
        self.assertRedirects(resp, reverse('coassessment_table') + '?per_page=25')
        self.assertFalse(CoAssessmentSession.objects.filter(pk__in=[self.session.pk, s2.pk]).exists())

    def test_bulk_delete_rejects_foreign_or_invalid_ids(self):
        foreign_session = CoAssessmentSession.objects.create(
            organization_id=self.other_org.pk,
            created_by=self.intruder,
            name='Foreign University Event',
            status=CoAssessmentSession.Status.DRAFT,
        )
        bulk_url = reverse('coassessment_bulk_delete')

        # Foreign ID inside selection should abort entire batch
        resp = self.client.post(bulk_url, {'session_ids': [str(self.session.pk), str(foreign_session.pk)]})
        self.assertRedirects(resp, reverse('coassessment_table') + '?per_page=25')
        self.assertTrue(CoAssessmentSession.objects.filter(pk=self.session.pk).exists())
        self.assertTrue(CoAssessmentSession.objects.filter(pk=foreign_session.pk).exists())

        # Invalid format IDs
        invalid_resp = self.client.post(bulk_url, {'session_ids': ['abc', '-1']})
        self.assertRedirects(invalid_resp, reverse('coassessment_table') + '?per_page=25')
        self.assertTrue(CoAssessmentSession.objects.filter(pk=self.session.pk).exists())

    def test_delete_endpoints_enforce_post_and_ownership(self):
        del_url = reverse('coassessment_delete', kwargs={'session_id': self.session.pk})
        get_resp = self.client.get(del_url)
        self.assertEqual(get_resp.status_code, 405)

        bulk_url = reverse('coassessment_bulk_delete')
        bulk_get = self.client.get(bulk_url)
        self.assertEqual(bulk_get.status_code, 405)

        # Intruder attempts to delete owner's session
        sign_in(self.client, self.intruder, self.other_org)
        intruder_del = self.client.post(del_url)
        self.assertEqual(intruder_del.status_code, 404)
        self.assertTrue(CoAssessmentSession.objects.filter(pk=self.session.pk).exists())


    def test_live_console_and_state_api(self):
        console_url = reverse('coassessment_live_console', kwargs={'session_id': self.session.pk})
        resp = self.client.get(console_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Vision AI Group')

        # Live State JSON API
        state_url = reverse('coassessment_live_state', kwargs={'session_id': self.session.pk})
        api_resp = self.client.get(state_url)
        self.assertEqual(api_resp.status_code, 200)
        data = json.loads(api_resp.content.decode('utf-8'))
        self.assertTrue(data['has_active_turn'])
        self.assertEqual(data['active_group_name'], 'Vision AI Group')
        self.assertEqual(data['panel_count'], 0)
        self.assertEqual(data['peer_count'], 0)

    def test_peer_join_flow_and_scorecard_submission(self):
        join_url = reverse('coassessment_peer_join', kwargs={'code': 'AVP26'})
        resp = self.client.get(join_url)
        self.assertEqual(resp.status_code, 200)

        # Post student ID to join
        post_resp = self.client.post(join_url, {'identifier': 'DKIM-99'})
        scorecard_url = reverse('coassessment_peer_scorecard', kwargs={'session_id': self.session.pk})
        self.assertRedirects(post_resp, scorecard_url)

        # Access scorecard
        sc_resp = self.client.get(scorecard_url)
        self.assertEqual(sc_resp.status_code, 200)
        self.assertContains(sc_resp, 'Vision AI Group')

        # Submit peer evaluation
        submit_url = reverse('coassessment_peer_submit', kwargs={'session_id': self.session.pk})
        sub_resp = self.client.post(submit_url, {
            f'criterion_{self.crit.pk}': '88',
            'qualitative_feedback': 'Clear presentation on edge detection.',
        })
        self.assertRedirects(sub_resp, scorecard_url)
        self.assertEqual(self.turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PEER).count(), 1)

    def test_peer_scorecard_starts_unscored_and_rejects_missing_scores(self):
        self.client.post(reverse('coassessment_peer_join', kwargs={'code': 'AVP26'}), {'identifier': 'DKIM-99'})
        scorecard_url = reverse('coassessment_peer_scorecard', kwargs={'session_id': self.session.pk})
        html = self.client.get(scorecard_url).content.decode()
        self.assertIn('value="0"', html)
        self.assertNotIn('value="50"', html)
        self.assertNotIn('peerMemberTabs', html)
        submit_url = reverse('coassessment_peer_submit', kwargs={'session_id': self.session.pk})
        rejected = self.client.post(submit_url, {}, follow=True)
        self.assertContains(rejected, 'Please score')
        self.assertContains(rejected, self.crit.name)
        self.assertEqual(self.turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PEER).count(), 0)
        self.client.post(submit_url, {f'criterion_{self.crit.pk}': '0'})
        self.assertEqual(self.turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PEER).count(), 1)

    def test_examiner_portal_access_and_submission(self):
        portal_url = reverse('coassessment_examiner_portal', kwargs={'token': self.examiner_token})
        resp = self.client.get(portal_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Vision AI Group')
        self.assertContains(resp, 'Dr. Helen Cho')

        # Submit score
        post_resp = self.client.post(portal_url, {
            f'criterion_{self.crit.pk}': '92',
            'qualitative_feedback': 'Strong technical grounding and sound statistical validation.',
        })
        self.assertRedirects(post_resp, portal_url)
        self.assertEqual(self.turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PANEL).count(), 1)

    def test_results_moderation_view_and_chair_moderation_action(self):
        # Create scores
        submit_examiner_scorecard(self.turn, self.examiner, {self.crit.pk: 95})
        submit_peer_scorecard(self.turn, self.participant, {self.crit.pk: 70})

        results_url = reverse('coassessment_results', kwargs={'session_id': self.session.pk})
        resp = self.client.get(results_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Discrepancy')

        # Post chair moderation
        mod_url = reverse('coassessment_moderate', kwargs={'session_id': self.session.pk, 'turn_id': self.turn.pk})
        post_resp = self.client.post(mod_url, {
            'action': CoAssessmentModeration.Action.OVERRIDE_PANEL,
            'final_score': '88.00',
            'justification': 'Audited review confirmed panel evaluation accurately reflected algorithm depth.',
        })
        self.assertRedirects(post_resp, results_url)
        self.turn.refresh_from_db()
        self.assertEqual(self.turn.moderations.count(), 1)

    def test_stage_projector_display_view(self):
        owner_url = reverse('coassessment_stage_display', kwargs={'session_id': self.session.pk})
        public_url = reverse('coassessment_public_screen', kwargs={'code': self.session.public_join_code})
        self.assertRedirects(self.client.get(owner_url), public_url, fetch_redirect_response=False)
        self.client.logout()
        resp = self.client.get(public_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Vision AI Group')
        self.assertContains(resp, 'LIDAR Object Detection')

    def test_csv_export_response(self):
        submit_examiner_scorecard(self.turn, self.examiner, {self.crit.pk: 90})
        submit_peer_scorecard(self.turn, self.participant, {self.crit.pk: 85})

        export_url = reverse('coassessment_export_csv', kwargs={'session_id': self.session.pk})
        resp = self.client.get(export_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')
        self.assertIn('attachment; filename="coassessment_', resp['Content-Disposition'])

        content = resp.content.decode('utf-8')
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 'Order')
        self.assertEqual(rows[0][1], 'Team')
        self.assertEqual(rows[1][1], 'Vision AI Group')

    def test_tenant_isolation_prevents_cross_organization_access(self):
        sign_in(self.client, self.intruder, self.other_org)

        # Attempt to access live console of another org's session
        console_url = reverse('coassessment_live_console', kwargs={'session_id': self.session.pk})
        resp = self.client.get(console_url)
        self.assertEqual(resp.status_code, 404)

        # Attempt to mutate active turn of another org's session
        activate_url = reverse(
            'coassessment_activate_turn',
            kwargs={'session_id': self.session.pk, 'group_id': self.group.pk},
        )
        post_resp = self.client.post(activate_url)
        self.assertEqual(post_resp.status_code, 404)

    def test_weighted_rubric_calculation_applies_criterion_weights(self):
        # Create session with uneven rubric criterion weights (20% and 80%)
        weighted_session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Weighted Rubric Defense',
            status=CoAssessmentSession.Status.ACTIVE,
            panel_weight=Decimal('100.00'),
            peer_weight=Decimal('0.00'),
            moderation_threshold=Decimal('10.00'),
            minimum_panel_evaluators=1,
            minimum_peer_evaluators=1,
        )
        c1 = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=weighted_session,
            name='Presentation Delivery', weight=Decimal('20.00'), order=1,
        )
        c2 = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=weighted_session,
            name='Technical Depth', weight=Decimal('80.00'), order=2,
        )
        group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=weighted_session,
            name='Robotics Unit', order=1,
        )
        examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=weighted_session,
            display_name='Dr. Smith', is_chair=True,
        )
        turn = activate_stage_turn(weighted_session, group, user=self.owner)

        # Rater gives 100 on Delivery (20%) and 50 on Technical Depth (80%)
        # Weighted result must be (100 * 0.20) + (50 * 0.80) = 20 + 40 = 60.00
        # An unweighted arithmetic average would give 75.00
        submit_examiner_scorecard(turn, examiner, {c1.pk: 100, c2.pk: 50})
        res = compute_turn_results(turn)
        self.assertEqual(res['panel_score'], Decimal('60.00'))

    def test_peer_denominator_excludes_presenting_team_members(self):
        # Add a presenting group member
        CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            full_name='Group Member 1', student_id='GM-1', group=self.group,
        )
        # Outside rater
        CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            full_name='Outside Audience', student_id='AUD-1', group=None,
        )

        state = get_coassessment_live_state(self.session)
        # Active turn group members are excluded from peer quorum denominator
        presenting_member_count = self.group.members.count()
        total_session_participants = self.session.participants.count()
        expected_peer_total = total_session_participants - presenting_member_count
        self.assertEqual(state['peer_total'], expected_peer_total)

    def test_live_state_endpoint_contract(self):
        sign_in(self.client, self.owner, self.org)
        state_url = reverse('coassessment_live_state', kwargs={'session_id': self.session.pk})
        resp = self.client.get(state_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['session_id'], self.session.pk)
        self.assertTrue(data['has_active_turn'])
        self.assertEqual(data['active_group_name'], 'Vision AI Group')
        self.assertIn('presentation_clock_formatted', data)
        self.assertIn('qa_clock_formatted', data)
        self.assertIn('panel_quorum_met', data)
        self.assertIn('peer_quorum_met', data)

    def test_examiner_token_persistence_and_rotation(self):
        sign_in(self.client, self.owner, self.org)
        wizard_url = reverse('coassessment_setup_examiners', kwargs={'session_id': self.session.pk})

        # Invite new examiner
        post_resp = self.client.post(wizard_url, {
            'action': 'invite_examiner',
            'display_name': 'Prof. Davis',
            'email': 'davis@example.edu',
            'is_chair': False,
        })
        self.assertRedirects(post_resp, wizard_url)

        new_ex = self.session.examiners.get(display_name='Prof. Davis')
        self.assertTrue(bool(new_ex.invite_token))
        initial_token = new_ex.invite_token
        initial_digest = new_ex.token_digest

        # Rotate token
        rot_resp = self.client.post(wizard_url, {
            'action': 'rotate_token',
            'examiner_id': new_ex.pk,
        })
        self.assertRedirects(rot_resp, wizard_url)
        new_ex.refresh_from_db()
        self.assertNotEqual(new_ex.invite_token, initial_token)
        self.assertNotEqual(new_ex.token_digest, initial_digest)


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentVerticalStepperTests(TestCase):
    """Tests for the vertical stepper setup navigation flow."""

    def setUp(self):
        self.org = make_org('Vertical Stepper Org')
        self.owner = make_user('stepper-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Senior Defense Showcase',
            description='Final year project defenses',
            panel_weight=Decimal('70.00'),
            peer_weight=Decimal('30.00'),
        )
        CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Technical Execution',
            weight=Decimal('50.00'),
            order=1,
        )
        CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Presentation Clarity',
            weight=Decimal('50.00'),
            order=2,
        )
        self.group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Robotics Alpha',
            order=1,
        )

    def test_wizard_steps_link_each_step_and_track_completion(self):
        sign_in(self.client, self.owner, self.org)
        resp = self.client.get(reverse('coassessment_setup_rubric', kwargs={'session_id': self.session.pk}))

        steps = {s['key']: s for s in resp.context['stepper_steps']}
        self.assertEqual(list(steps), ['policy', 'rubric', 'roster', 'examiners', 'review'])
        self.assertTrue(steps['rubric']['current'])
        self.assertTrue(steps['policy']['done'])
        self.assertEqual(steps['roster']['href'],
                         reverse('coassessment_setup_roster', args=[self.session.pk]))
        self.assertFalse(any(s['disabled'] for s in steps.values()))
        self.assertContains(resp, f'href="{steps["examiners"]["href"]}"')

    def test_wizard_stepper_is_inert_before_the_session_is_saved(self):
        sign_in(self.client, self.owner, self.org)
        resp = self.client.get(reverse('coassessment_create'))

        steps = resp.context['stepper_steps']
        self.assertTrue(steps[0]['current'])
        self.assertFalse(steps[0]['done'])
        self.assertTrue(all(s['disabled'] and not s['href'] for s in steps[1:]))

    def test_step1_create_renders_vertical_stepper(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('coassessment_create')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode('utf-8')
        self.assertIn('class="panel-step-layout"', content)
        self.assertIn('class="stepper-nav"', content)
        self.assertIn('Session &amp; Weights', content)
        self.assertIn('aria-current="step"', content)
        self.assertIn('disabled title="Save the details on the first step to unlock this step"', content)

    def test_step1_edit_policy_route_renders_existing_values(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('coassessment_setup_policy', kwargs={'session_id': self.session.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode('utf-8')
        self.assertIn('class="panel-step-layout"', content)
        self.assertIn('Senior Defense Showcase', content)
        self.assertIn('value="70.00"', content)

    def test_step2_rubric_renders_vertical_stepper(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('coassessment_setup_rubric', kwargs={'session_id': self.session.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode('utf-8')
        self.assertIn('class="panel-step-layout"', content)
        self.assertIn('class="stepper-nav"', content)
        self.assertIn('Shared Rubric', content)

    def test_step3_roster_renders_vertical_stepper(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('coassessment_setup_roster', kwargs={'session_id': self.session.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode('utf-8')
        self.assertIn('class="panel-step-layout"', content)
        self.assertIn('Presenting Groups', content)
        self.assertIn('Robotics Alpha', content)

    def test_step4_examiners_renders_vertical_stepper(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('coassessment_setup_examiners', kwargs={'session_id': self.session.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode('utf-8')
        self.assertIn('class="panel-step-layout"', content)
        self.assertIn('Panel examiners', content)
        self.assertIn('Invite an examiner', content)

    def test_session_rubric_balance_helpers(self):
        self.assertEqual(self.session.rubric_total_weight(), Decimal('100.00'))
        self.assertTrue(self.session.is_rubric_balanced())
        crit = self.session.criteria.first()
        crit.weight = Decimal('30.00')
        crit.save()
        self.assertFalse(self.session.is_rubric_balanced())


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentIndividualRubricTests(TestCase):
    """Tests for individual-wise and group-wise rubric scoring."""

    def setUp(self):
        self.org = make_org('Individual Rubric Org')
        self.owner = make_user('ind-rubric-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Autonomous Systems Defense',
            panel_weight=Decimal('70.00'),
            peer_weight=Decimal('30.00'),
            minimum_panel_evaluators=1,
            minimum_peer_evaluators=1,
            status=CoAssessmentSession.Status.ACTIVE,
        )
        self.crit_group = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='System Architecture',
            scope=CoAssessmentCriterion.Scope.GROUP,
            weight=Decimal('60.00'),
            order=1,
        )
        self.crit_ind = CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Individual Q&A Mastery',
            scope=CoAssessmentCriterion.Scope.INDIVIDUAL,
            weight=Decimal('40.00'),
            order=2,
        )
        self.group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Team Drone',
            order=1,
        )
        self.alice = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            group=self.group,
            full_name='Alice Smith',
            student_id='STU-001',
        )
        self.bob = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            group=self.group,
            full_name='Bob Jones',
            student_id='STU-002',
        )
        self.peer_rater = CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            group=None,
            full_name='Peer Rater',
            student_id='AUD-001',
        )
        self.examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            display_name='Dr. Miller',
            email='miller@example.edu',
            invite_token='tok_ind_miller',
            token_digest=hashlib.sha256(b'tok_ind_miller').hexdigest(),
        )
        self.turn = activate_stage_turn(self.session, self.group, user=self.owner)

    def test_unscored_items_name_each_presenter_and_criterion(self):
        from service_domains.coassessment_views import _unscored_items
        members = [self.alice, self.bob]
        self.assertEqual(
            _unscored_items(self.session, members, {}, {(self.crit_ind.pk, self.alice.pk): '10'}),
            ['System Architecture', 'Individual Q&A Mastery for Bob Jones'])
        self.assertEqual(_unscored_items(self.session, [self.alice], {self.crit_group.pk: '1'},
                                         {(self.crit_ind.pk, self.alice.pk): '1'}), [])

    def test_rubric_scoping_and_totals(self):
        self.assertEqual(self.session.group_weight_total(), Decimal('60.00'))
        self.assertEqual(self.session.individual_weight_total(), Decimal('40.00'))
        self.assertEqual(self.session.rubric_total_weight(), Decimal('100.00'))
        self.assertTrue(self.session.is_rubric_balanced())

    def test_score_value_target_participant_validation(self):
        card = CoAssessmentScorecard.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            turn=self.turn,
            evaluator_tier=CoAssessmentScorecard.Tier.PANEL,
            examiner_evaluator=self.examiner,
        )
        # Group criterion with a target participant raises error
        val_group_invalid = CoAssessmentScoreValue(
            scorecard=card,
            criterion=self.crit_group,
            target_participant=self.alice,
            score=Decimal('90.00'),
        )
        with self.assertRaises(ValidationError):
            val_group_invalid.clean()

        # Individual criterion without a target participant raises error
        val_ind_invalid = CoAssessmentScoreValue(
            scorecard=card,
            criterion=self.crit_ind,
            target_participant=None,
            score=Decimal('90.00'),
        )
        with self.assertRaises(ValidationError):
            val_ind_invalid.clean()

    def test_peer_and_panel_submission_with_individual_scores(self):
        # Examiner rates group at 90, Alice at 100, Bob at 50
        submit_examiner_scorecard(
            self.turn,
            self.examiner,
            criterion_scores={self.crit_group.pk: 90},
            individual_scores={
                (self.crit_ind.pk, self.alice.pk): 100,
                (self.crit_ind.pk, self.bob.pk): 50,
            },
        )

        # Peer rates group at 80, Alice at 90, Bob at 60
        submit_peer_scorecard(
            self.turn,
            self.peer_rater,
            criterion_scores={self.crit_group.pk: 80},
            individual_scores={
                (self.crit_ind.pk, self.alice.pk): 90,
                (self.crit_ind.pk, self.bob.pk): 60,
            },
        )

        res = compute_turn_results(self.turn)
        self.assertEqual(res['group_mark'], Decimal('52.20'))

        # Alice: 52.20 (group) + 38.80 (individual) = 91.00
        alice_res = next(m for m in res['member_results'] if m['participant_id'] == self.alice.pk)
        self.assertEqual(alice_res['group_score'], Decimal('52.20'))
        self.assertEqual(alice_res['individual_score'], Decimal('38.80'))
        self.assertEqual(alice_res['final_score'], Decimal('91.00'))

        # Bob: 52.20 (group) + 21.20 (individual) = 73.40
        bob_res = next(m for m in res['member_results'] if m['participant_id'] == self.bob.pk)
        self.assertEqual(bob_res['group_score'], Decimal('52.20'))
        self.assertEqual(bob_res['individual_score'], Decimal('21.20'))
        self.assertEqual(bob_res['final_score'], Decimal('73.40'))

    def test_csv_export_includes_individual_student_rows(self):
        submit_examiner_scorecard(
            self.turn,
            self.examiner,
            criterion_scores={self.crit_group.pk: 85},
            individual_scores={
                (self.crit_ind.pk, self.alice.pk): 95,
                (self.crit_ind.pk, self.bob.pk): 70,
            },
        )
        sign_in(self.client, self.owner, self.org)
        export_url = reverse('coassessment_export_csv', kwargs={'session_id': self.session.pk})
        resp = self.client.get(export_url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode('utf-8')

        self.assertIn('Student Name', content)
        self.assertIn('Weighted Individual Mark', content)
        self.assertIn('Alice Smith', content)
        self.assertIn('Bob Jones', content)

    def test_scorecard_views_render_member_individual_sections(self):
        # Peer scorecard view
        session_cookie = self.client.session
        session_cookie[f'co_peer_{self.session.pk}'] = self.peer_rater.pk
        session_cookie.save()

        peer_url = reverse('coassessment_peer_scorecard', kwargs={'session_id': self.session.pk})
        resp_peer = self.client.get(peer_url)
        self.assertEqual(resp_peer.status_code, 200)
        peer_html = resp_peer.content.decode('utf-8')
        self.assertIn('Group Presentation Criteria', peer_html)
        self.assertIn('Individual Student Contribution', peer_html)
        self.assertIn('Alice Smith', peer_html)
        self.assertIn('Bob Jones', peer_html)

        # Examiner portal view
        ex_url = reverse('coassessment_examiner_portal', kwargs={'token': 'tok_ind_miller'})
        resp_ex = self.client.get(ex_url)
        self.assertEqual(resp_ex.status_code, 200)
        ex_html = resp_ex.content.decode('utf-8')
        self.assertIn('Group Presentation Criteria', ex_html)
        self.assertIn('Individual Student Delivery', ex_html)
        self.assertIn('Alice Smith', ex_html)
        self.assertIn('Bob Jones', ex_html)


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentWorkflowRefinementsTests(TestCase):
    """Validation tests for public results routing, roster import confirmation, and stage turn order."""

    def setUp(self):
        self.org = make_org('CoAssessment Workflow Org')
        self.owner = make_user('co-workflow-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Senior Capstone Presentations',
            public_join_code='CAP2026',
            status=CoAssessmentSession.Status.ACTIVE,
            presentation_duration_seconds=600,
            qa_duration_seconds=300,
            minimum_panel_evaluators=1,
            minimum_peer_evaluators=1,
        )
        self.group1 = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Alpha Team',
            order=1,
        )
        self.group2 = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            session=self.session,
            name='Beta Team',
            order=2,
        )

    def test_public_results_route_and_view_embargo_and_release(self):
        CoAssessmentTurn.objects.create(
            session=self.session,
            group=self.group1,
            organization_id=self.org.pk,
            created_by=self.owner,
            status=CoAssessmentTurn.Status.CLOSED,
        )
        CoAssessmentTurn.objects.create(
            session=self.session,
            group=self.group2,
            organization_id=self.org.pk,
            created_by=self.owner,
            status=CoAssessmentTurn.Status.CLOSED,
        )

        # 1. URL resolution points to coassessment_public_results
        public_url = reverse('coassessment_public_results', kwargs={'code': self.session.public_join_code})
        match = resolve(public_url)
        self.assertEqual(match.func, coassessment_public_results)

        # 2. Results embargoed when session is ACTIVE
        resp = self.client.get(public_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')
        self.assertIn('Results Pending Release', html)
        self.assertIn('Evaluation and moderation for this event are currently in progress.', html)
        self.assertNotIn('Official Released Results', html)

        # 3. Results displayed when session is RELEASED
        self.session.status = CoAssessmentSession.Status.RELEASED
        self.session.save(update_fields=['status'])

        resp_released = self.client.get(public_url)
        self.assertEqual(resp_released.status_code, 200)
        html_released = resp_released.content.decode('utf-8')
        self.assertIn('Official Released Results', html_released)
        self.assertIn('Alpha Team', html_released)
        self.assertIn('Beta Team', html_released)
        self.assertNotIn('Results Pending Release', html_released)

    def test_roster_import_preview_confirmation_and_idless_students(self):
        sign_in(self.client, self.owner, self.org)
        roster_url = reverse('coassessment_setup_roster', kwargs={'session_id': self.session.pk})

        # Build spreadsheet with two ID-less students in Team Gamma and one in Team Delta
        headers = ['Group', 'Topic', 'Student Name', 'Student ID', 'Email']
        rows = [
            ['Team Gamma', 'Autonomous Systems', 'Carol Danvers', '', 'carol@example.com'],
            ['Team Gamma', 'Autonomous Systems', 'David Banner', '', 'david@example.com'],
            ['Team Delta', 'Quantum Physics', 'Elena Belova', 'STU9901', 'elena@example.com'],
        ]
        file_bytes = workbook_bytes(headers, rows).getvalue()
        upload = SimpleUploadedFile(
            'roster.xlsx',
            file_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        import_url = reverse('coassessment_roster_import', kwargs={'session_id': self.session.pk})

        # Step A: Upload initiates preview, does not commit rows immediately
        self.client.post(import_url, {'file': upload})
        resp_upload = self.client.get(roster_url)
        self.assertEqual(resp_upload.status_code, 200)
        self.assertFalse(self.session.groups.filter(name='Team Gamma').exists())
        self.assertFalse(self.session.groups.filter(name='Team Delta').exists())

        # Step B: Preview is rendered on GET
        html = resp_upload.content.decode('utf-8')
        self.assertIn('Spreadsheet preview', html)
        self.assertIn('Team Gamma', html)
        self.assertIn('Carol Danvers', html)
        self.assertIn('David Banner', html)
        self.assertIn('2 groups and 3 students will be added', html)

        # Step C: Cancel preview clears preview state
        self.client.post(import_url, {'action': 'cancel'})
        resp_cancel = self.client.get(roster_url)
        self.assertNotIn('Spreadsheet preview', resp_cancel.content.decode('utf-8'))
        self.assertFalse(self.session.groups.filter(name='Team Gamma').exists())

        # Step D: Upload again and apply the import
        upload_second = SimpleUploadedFile('roster.xlsx', file_bytes)
        self.client.post(import_url, {'file': upload_second})
        token = self.client.get(roster_url).context['import_state']['token']
        resp_confirm = self.client.post(
            import_url, {'action': 'apply', 'preview_token': token}, follow=True)
        self.assertEqual(resp_confirm.status_code, 200)

        # Verify groups created
        gamma = self.session.groups.get(name='Team Gamma')
        self.assertEqual(gamma.topic, 'Autonomous Systems')

        # Verify two distinct students without student IDs are created
        gamma_members = list(gamma.members.all().order_by('full_name'))
        self.assertEqual(len(gamma_members), 2)
        self.assertEqual(gamma_members[0].full_name, 'Carol Danvers')
        self.assertEqual(gamma_members[0].student_id, '')
        self.assertEqual(gamma_members[1].full_name, 'David Banner')
        self.assertEqual(gamma_members[1].student_id, '')
        self.assertNotEqual(gamma_members[0].pk, gamma_members[1].pk)

        # Verify Delta member with ID
        delta = self.session.groups.get(name='Team Delta')
        delta_member = delta.members.get()
        self.assertEqual(delta_member.full_name, 'Elena Belova')
        self.assertEqual(delta_member.student_id, 'STU9901')

    def test_activate_stage_turn_protects_closed_turns_and_active_state(self):
        # 1. Activate turn 1
        turn1 = activate_stage_turn(self.session, self.group1, user=self.owner)
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.ACTIVE)

        # 2. Activate turn 2: closes turn 1 and activates turn 2
        turn2 = activate_stage_turn(self.session, self.group2, user=self.owner)
        turn1.refresh_from_db()
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.CLOSED)
        self.assertEqual(turn2.status, CoAssessmentTurn.Status.ACTIVE)

        # 3. Attempting to activate closed turn 1 must raise ValidationError
        # and must not mutate turn 2 status
        with self.assertRaises(ValidationError) as ctx:
            activate_stage_turn(self.session, self.group1, user=self.owner)
        self.assertIn('already presented', str(ctx.exception))

        turn1.refresh_from_db()
        turn2.refresh_from_db()
        self.assertEqual(turn1.status, CoAssessmentTurn.Status.CLOSED)
        self.assertEqual(turn2.status, CoAssessmentTurn.Status.ACTIVE)

        # 4. Activating already active turn returns the turn without error
        same_turn = activate_stage_turn(self.session, self.group2, user=self.owner)
        self.assertEqual(same_turn.pk, turn2.pk)
        self.assertEqual(same_turn.status, CoAssessmentTurn.Status.ACTIVE)

    def test_live_console_lineup_hides_call_to_stage_for_closed_teams(self):
        # Set turn 1 as CLOSED and turn 2 as ACTIVE
        turn1 = activate_stage_turn(self.session, self.group1, user=self.owner)
        turn2 = activate_stage_turn(self.session, self.group2, user=self.owner)
        turn1.refresh_from_db()

        sign_in(self.client, self.owner, self.org)
        console_url = reverse('coassessment_live_console', kwargs={'session_id': self.session.pk})
        resp = self.client.get(console_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')

        # Alpha Team is closed: must show Completed, must not have Call to Stage button
        alpha_action = reverse('coassessment_activate_turn', kwargs={'session_id': self.session.pk, 'group_id': self.group1.pk})
        self.assertNotIn(f'action="{alpha_action}"', html)
        self.assertIn('Presented', html)

        # Beta Team is active: shows the on-stage indicator
        self.assertIn('On stage', html)





@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class CoAssessmentSetupSimplificationTests(TestCase):
    """Tests for automatic ordering, member management, roster format, timers, and invitation email."""

    def setUp(self):
        self.org = make_org('CoAssessment Setup Org')
        self.owner = make_user('co-setup-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Setup Session',
            status=CoAssessmentSession.Status.ACTIVE,
        )
        sign_in(self.client, self.owner, self.org)

    def test_policy_form_has_no_duration_or_order_fields(self):
        self.assertNotIn('presentation_duration_seconds', CoAssessmentPolicyForm().fields)
        self.assertNotIn('qa_duration_seconds', CoAssessmentPolicyForm().fields)
        self.assertNotIn('order', CoAssessmentCriterionForm().fields)
        self.assertNotIn('order', CoAssessmentGroupForm().fields)
        self.assertNotIn('location', CoAssessmentGroupForm().fields)

    def test_criteria_and_groups_are_ordered_as_added(self):
        rubric_url = reverse('coassessment_setup_rubric', kwargs={'session_id': self.session.pk})
        for name in ('First', 'Second', 'Third'):
            self.client.post(rubric_url, {'action': 'add_criterion', 'name': name, 'weight': '30.00', 'scope': 'group'})
        self.assertEqual([c.order for c in self.session.criteria.order_by('order')], [1, 2, 3])
        self.assertEqual([c.name for c in self.session.criteria.order_by('order')], ['First', 'Second', 'Third'])

        roster_url = reverse('coassessment_setup_roster', kwargs={'session_id': self.session.pk})
        for name in ('Alpha', 'Beta'):
            self.client.post(roster_url, {'action': 'add_group', 'name': name, 'topic': 'T'})
        self.assertEqual([g.order for g in self.session.groups.order_by('order')], [1, 2])

    def test_add_and_remove_member_on_empty_group(self):
        group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, name='Empty', order=1)
        url = reverse('coassessment_setup_roster', kwargs={'session_id': self.session.pk})

        self.client.post(url, {'action': 'add_member', 'group_id': group.pk, 'full_name': 'Zed Zane', 'student_id': 'Z1'})
        member = group.members.get()
        self.assertEqual(member.full_name, 'Zed Zane')

        # Duplicate student ID is rejected
        self.client.post(url, {'action': 'add_member', 'group_id': group.pk, 'full_name': 'Other', 'student_id': 'Z1'})
        self.assertEqual(group.members.count(), 1)

        # Blank name is rejected
        self.client.post(url, {'action': 'add_member', 'group_id': group.pk, 'full_name': '   '})
        self.assertEqual(group.members.count(), 1)

        # Unknown group is rejected
        self.client.post(url, {'action': 'add_member', 'group_id': 999999, 'full_name': 'Ghost'})
        self.assertFalse(CoAssessmentParticipant.objects.filter(full_name='Ghost').exists())

        self.client.post(url, {'action': 'remove_member', 'participant_id': member.pk})
        self.assertEqual(group.members.count(), 0)

    def _import_roster(self, upload):
        import_url = reverse('coassessment_roster_import', kwargs={'session_id': self.session.pk})
        page_url = reverse('coassessment_setup_roster', kwargs={'session_id': self.session.pk})
        self.client.post(import_url, {'file': upload})
        token = self.client.get(page_url).context['import_state']['token']
        return import_url, token

    def test_roster_upload_accepts_classroom_format_and_sample_downloads(self):
        headers = ['Group Name', 'Name', 'Student ID', 'Topic']
        rows = [['Aurora', 'Ann Lee, Bo Ng', 'A1, A2', 'Map']]
        upload = SimpleUploadedFile('g.xlsx', workbook_bytes(headers, rows).getvalue())
        import_url, token = self._import_roster(upload)
        self.client.post(import_url, {'action': 'apply', 'preview_token': token})
        group = self.session.groups.get(name='Aurora')
        self.assertEqual(
            sorted(group.members.values_list('full_name', 'student_id')),
            [('Ann Lee', 'A1'), ('Bo Ng', 'A2')],
        )

        sample = self.client.get(reverse('coassessment_roster_sample', kwargs={'session_id': self.session.pk}))
        self.assertEqual(sample.status_code, 200)
        self.assertIn('spreadsheetml', sample['Content-Type'])

    def test_update_timers_validates_and_saves_minutes(self):
        url = reverse('coassessment_update_timers', kwargs={'session_id': self.session.pk})
        ok = self.client.post(url, {'presentation_minutes': '12', 'qa_minutes': '4'})
        self.assertEqual(ok.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.presentation_duration_seconds, 720)
        self.assertEqual(self.session.qa_duration_seconds, 240)

        for bad in ({'presentation_minutes': '0', 'qa_minutes': '4'},
                    {'presentation_minutes': 'abc', 'qa_minutes': '4'},
                    {'presentation_minutes': '10', 'qa_minutes': '999'},
                    {'presentation_minutes': '', 'qa_minutes': ''}):
            self.assertEqual(self.client.post(url, bad).status_code, 400)
        self.session.refresh_from_db()
        self.assertEqual(self.session.presentation_duration_seconds, 720)

        self.assertEqual(self.client.get(url).status_code, 405)

    def test_examiner_invite_and_rotation_queue_email(self):
        from unittest.mock import patch
        url = reverse('coassessment_setup_examiners', kwargs={'session_id': self.session.pk})
        with patch('service_domains.coassessment_views.send_coassessment_examiner_invitation_email.delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {'action': 'invite_examiner', 'display_name': 'Dr Hale', 'email': 'hale@example.com'})
        examiner = self.session.examiners.get()
        self.assertEqual(delay.call_count, 1)
        self.assertEqual(delay.call_args[0][0], examiner.pk)

        with patch('service_domains.coassessment_views.send_coassessment_examiner_invitation_email.delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {'action': 'rotate_token', 'examiner_id': examiner.pk})
        self.assertEqual(delay.call_count, 1)

    def test_invitation_task_sends_private_link(self):
        from django.core import mail
        from service_domains.tasks import send_coassessment_examiner_invitation_email
        examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr Hale', email='hale@example.com', token_digest='d' * 64,
            invite_token='tok_hale_invite', is_chair=True)
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            result = send_coassessment_examiner_invitation_email(examiner.pk, 'https://example.test')
        self.assertEqual(result, 'sent')
        message = mail.outbox[0]
        self.assertEqual(message.to, ['hale@example.com'])
        self.assertIn('/coassessment/examiner/tok_hale_invite/', message.body)
        self.assertIn('panel chair', message.body)
        self.assertIn('Setup Session', message.subject)
        self.assertEqual(send_coassessment_examiner_invitation_email(999999), 'ignored')
