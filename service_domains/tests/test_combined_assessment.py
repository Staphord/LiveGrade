from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.models import AssessmentSession, EvaluationScore, PresentationTurn, RubricCategory
from service_domains.combined_assessment import (
    combined_assessment_results,
    mapping_is_complete,
    record_combined_moderation,
)
from service_domains.models import (
    CombinedModerationDecision,
    CombinedPresentationAssessment,
    CombinedPresentationMapping,
    PanelCandidate,
    PanelCriterion,
    PanelDecision,
    PanelHearing,
    PanelResultAdjustment,
    PanelResultSnapshot,
)


@override_settings(LIVEGRADE_LIVE_SERVICES=('classroom', 'panel'))
class CombinedAssessmentDomainTests(TestCase):
    def setUp(self):
        self.org = make_org('Combined event org')
        self.owner = make_user('combined-owner')
        self.other = make_user('combined-other')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Peer event',
            service='classroom',
            status='live',
            ungraded_penalty=0,
        )
        self.group = self.session.groups.create(name='Team One')
        self.category = self.session.rubric_categories.create(
            name='Presentation', weight=60, scope=RubricCategory.Scope.GROUP)
        self.session.rubric_categories.create(
            name='Individual contribution', weight=40, scope=RubricCategory.Scope.INDIVIDUAL)
        self.session.students.create(full_name='Presenter')
        evaluator = self.session.students.create(full_name='Peer')
        self.turn = PresentationTurn.objects.create(
            assessment_session=self.session,
            group=self.group,
            status=PresentationTurn.Status.CLOSED,
        )
        evaluation = self.turn.evaluations.create(evaluator=evaluator)
        EvaluationScore.objects.create(
            evaluation=evaluation,
            rubric_category=self.category,
            value=80,
            max_value=100,
        )
        self.hearing = PanelHearing.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Panel event',
            status=PanelHearing.Status.REVIEW,
            workflow=PanelHearing.Workflow.SCORED,
        )
        self.candidate = PanelCandidate.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=self.hearing,
            display_name='Team One',
        )
        self.criterion = PanelCriterion.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=self.hearing,
            name='Quality',
        )
        self.snapshot = PanelResultSnapshot.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=self.hearing,
            candidate=self.candidate,
            criterion=self.criterion,
            score=Decimal('90'),
            rater_count=2,
            aggregation=PanelHearing.Aggregation.MEAN,
        )
        self.assessment = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Combined event',
            classroom_session=self.session,
            panel_hearing=self.hearing,
            result_policy=CombinedPresentationAssessment.ResultPolicy.WEIGHTED,
            peer_weight=Decimal('60'),
            panel_weight=Decimal('40'),
            moderation_threshold=Decimal('5'),
            minimum_peer_ratings=1,
            minimum_panel_raters=2,
        )
        self.mapping = CombinedPresentationMapping.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            assessment=self.assessment,
            group=self.group,
            candidate=self.candidate,
        )

    def finish_sources(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])

    def approve_moderation(self):
        return record_combined_moderation(
            self.mapping,
            CombinedModerationDecision.Decision.APPROVE,
            'The sources assess different aspects of the presentation.',
            organization=self.org.as_claim(),
            user=self.owner,
        )

    def test_weighted_results_require_moderation_and_keep_source_scores_visible(self):
        self.finish_sources()
        row = combined_assessment_results(self.assessment)[0]

        self.assertEqual(row['peer_score'], Decimal('80.000000'))
        self.assertEqual(row['panel_score'], Decimal('90.000000'))
        self.assertTrue(row['moderation_required'])
        self.assertIsNone(row['combined_score'])

        self.approve_moderation()
        result = combined_assessment_results(self.assessment)[0]

        self.assertEqual(result['combined_score'], Decimal('84.00'))
        self.assertEqual(result['peer_rater_count'], 1)
        self.assertEqual(result['panel_rater_count'], 2)

    def test_missing_coverage_never_becomes_zero_or_a_combined_mark(self):
        self.finish_sources()
        self.assessment.minimum_peer_ratings = 2
        self.assessment.save()

        row = combined_assessment_results(self.assessment)[0]

        self.assertEqual(row['peer_score'], Decimal('80.000000'))
        self.assertFalse(row['peer_covered'])
        self.assertIsNone(row['combined_score'])

    def test_weighted_mark_waits_until_both_sources_are_final(self):
        row = combined_assessment_results(self.assessment)[0]

        self.assertFalse(row['sources_final'])
        self.assertIsNone(row['combined_score'])

    def test_new_panel_entry_blocks_result_until_mapping_is_complete(self):
        self.finish_sources()
        PanelCandidate.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=self.hearing,
            display_name='Late candidate',
        )

        row = combined_assessment_results(self.assessment)[0]

        self.assertFalse(row['mapping_complete'])
        self.assertIsNone(row['combined_score'])

    def test_new_classroom_group_member_invalidates_the_saved_mapping(self):
        student = self.session.students.create(full_name='Late group member')
        self.group.memberships.create(student=student)

        self.assertFalse(mapping_is_complete(self.assessment))

    def test_panel_adjustment_is_used_without_mutating_the_original_snapshot(self):
        self.finish_sources()
        adjustment = PanelResultAdjustment(
            snapshot=self.snapshot,
            adjusted_score=Decimal('70'),
            reason='Moderation confirmed a calculation error.',
            revision=1,
            organization_id=self.org.id,
            created_by=self.owner,
            adjusted_by=self.owner,
        )
        adjustment.full_clean()
        adjustment.save()

        row = combined_assessment_results(self.assessment)[0]
        self.assertEqual(row['panel_score'], Decimal('70.000000'))
        self.assertIsNone(row['combined_score'])

        self.approve_moderation()
        self.assertEqual(
            combined_assessment_results(self.assessment)[0]['combined_score'], Decimal('76.00'))
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.score, Decimal('90'))

    def test_decision_only_panel_workflow_cannot_be_weighted(self):
        self.hearing.workflow = PanelHearing.Workflow.OUTCOME
        self.hearing.save()

        with self.assertRaises(ValidationError):
            self.assessment.full_clean()

    def test_separate_results_keep_nonnumeric_panel_outcomes_separate(self):
        self.assessment.result_policy = CombinedPresentationAssessment.ResultPolicy.SEPARATE
        self.assessment.save()
        self.hearing.workflow = PanelHearing.Workflow.OUTCOME
        self.hearing.save()
        group = self.session.groups.create(name='Outcome group')
        candidate = PanelCandidate.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=self.hearing,
            display_name='Outcome candidate',
        )
        mapping = CombinedPresentationMapping.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            assessment=self.assessment,
            group=group,
            candidate=candidate,
        )
        decision = PanelDecision.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=self.hearing,
            candidate=candidate,
            outcome=PanelDecision.Outcome.CONDITIONAL,
            rationale='Revise the demonstration.',
            conditions='Submit the revised demonstration.',
        )

        row = next(item for item in combined_assessment_results(self.assessment)
                   if item['mapping_id'] == mapping.pk)

        self.assertIsNone(row['panel_score'])
        self.assertEqual(row['panel_decision'].pk, decision.pk)
        self.assertIsNone(row['combined_score'])

    def test_weights_must_sum_to_one_hundred(self):
        self.assessment.peer_weight = Decimal('55')

        with self.assertRaises(ValidationError):
            self.assessment.full_clean()

    def test_mapping_rejects_a_group_from_another_classroom_session(self):
        other_session = AssessmentSession.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Other',
            service='classroom',
        )
        other_group = other_session.groups.create(name='Other group')
        mapping = CombinedPresentationMapping(
            organization_id=self.org.id,
            created_by=self.owner,
            assessment=self.assessment,
            group=other_group,
            candidate=self.candidate,
        )

        with self.assertRaises(ValidationError):
            mapping.full_clean()

    def test_result_calculation_rejects_an_unrelated_object(self):
        with self.assertRaises(TypeError):
            combined_assessment_results(object())
