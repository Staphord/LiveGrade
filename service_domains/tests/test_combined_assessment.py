from decimal import Decimal
from django.utils import timezone

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, EvaluationScore, PresentationTurn, RubricCategory
from service_domains.combined_assessment import (combined_assessment_results,
    record_combined_moderation)
from service_domains.models import (
    CombinedModerationDecision, CombinedPresentationAssessment, CombinedPresentationMapping, PanelCandidate,
    PanelCriterion, PanelDecision, PanelHearing, PanelResultSnapshot,
)


@override_settings(LIVEGRADE_LIVE_SERVICES=('classroom', 'panel'))
class CombinedAssessmentTests(TestCase):
    def setUp(self):
        self.org = make_org('Combined event org')
        self.owner = make_user('combined-owner')
        self.other = make_user('combined-other')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Peer event',
            service='classroom', status='live', ungraded_penalty=0)
        self.group = self.session.groups.create(name='Team One')
        self.category = self.session.rubric_categories.create(name='Presentation', weight=60,
            scope=RubricCategory.Scope.GROUP)
        self.session.rubric_categories.create(name='Individual contribution', weight=40,
            scope=RubricCategory.Scope.INDIVIDUAL)
        self.presenter = self.session.students.create(full_name='Presenter')
        evaluator = self.session.students.create(full_name='Peer')
        self.turn = PresentationTurn.objects.create(assessment_session=self.session,
            group=self.group, status=PresentationTurn.Status.CLOSED)
        evaluation = self.turn.evaluations.create(evaluator=evaluator)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=self.category,
            value=80, max_value=100)
        self.hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Panel event', status=PanelHearing.Status.REVIEW,
            workflow=PanelHearing.Workflow.SCORED)
        self.candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Team One')
        self.criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Quality')
        self.snapshot = PanelResultSnapshot.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, candidate=self.candidate,
            criterion=self.criterion, score=Decimal('90'), rater_count=2,
            aggregation=PanelHearing.Aggregation.MEAN)
        self.assessment = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Combined event',
            classroom_session=self.session, panel_hearing=self.hearing,
            result_policy=CombinedPresentationAssessment.ResultPolicy.WEIGHTED,
            peer_weight=Decimal('60'), panel_weight=Decimal('40'),
            moderation_threshold=Decimal('5'),
            minimum_peer_ratings=1, minimum_panel_raters=2)
        CombinedPresentationMapping.objects.create(organization_id=self.org.id,
            created_by=self.owner, assessment=self.assessment, group=self.group,
            candidate=self.candidate)

    def test_numeric_results_are_normalized_combined_and_source_scores_remain_visible(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])
        row = combined_assessment_results(self.assessment)[0]
        self.assertEqual(row['peer_score'], Decimal('80.000000'))
        self.assertEqual(row['panel_score'], Decimal('90.000000'))
        self.assertTrue(row['moderation_required'])
        self.assertIsNone(row['combined_score'])
        record_combined_moderation(self.assessment.mappings.get(),
            CombinedModerationDecision.Decision.APPROVE,
            'The difference reflects different assessment perspectives.',
            organization=self.org.as_claim(), user=self.owner)
        row = combined_assessment_results(self.assessment)[0]
        self.assertEqual(row['combined_score'], Decimal('84.00'))
        self.assertEqual(row['peer_rater_count'], 1)
        self.assertEqual(row['panel_rater_count'], 2)

    def test_missing_coverage_never_becomes_zero_or_a_combined_mark(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])
        self.assessment.minimum_peer_ratings = 2
        self.assessment.save()
        row = combined_assessment_results(self.assessment)[0]
        self.assertEqual(row['peer_score'], Decimal('80.000000'))
        self.assertFalse(row['peer_covered'])
        self.assertIsNone(row['combined_score'])

    def test_weighted_mark_waits_for_source_finality(self):
        row = combined_assessment_results(self.assessment)[0]
        self.assertFalse(row['sources_final'])
        self.assertIsNone(row['combined_score'])

    def test_added_source_entry_after_publish_blocks_weighted_result_until_remapped(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])
        self.assessment.moderation_threshold = Decimal('20')
        self.assessment.save()
        PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, display_name='Late candidate')
        row = combined_assessment_results(self.assessment)[0]
        self.assertFalse(row['mapping_complete'])
        self.assertIsNone(row['combined_score'])

    def test_panel_adjustment_is_used_without_mutating_original_snapshot(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])
        from service_domains.models import PanelResultAdjustment
        adjustment = PanelResultAdjustment(snapshot=self.snapshot, adjusted_score=Decimal('70'),
            reason='Moderation confirmed a calculation error.', revision=1,
            organization_id=self.org.id, created_by=self.owner, adjusted_by=self.owner)
        adjustment.full_clean()
        adjustment.save()
        row = combined_assessment_results(self.assessment)[0]
        self.assertEqual(row['panel_score'], Decimal('70.000000'))
        self.assertIsNone(row['combined_score'])
        record_combined_moderation(self.assessment.mappings.get(),
            CombinedModerationDecision.Decision.APPROVE,
            'The difference reflects different assessment perspectives.',
            organization=self.org.as_claim(), user=self.owner)
        row = combined_assessment_results(self.assessment)[0]
        self.assertEqual(row['combined_score'], Decimal('76.00'))
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.score, Decimal('90'))

    def test_organizer_can_record_reasoned_moderation_from_the_results_page(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        sign_in(self.client, self.owner, self.org)
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'moderate', 'mapping_id': self.assessment.mappings.get().pk,
            'decision': 'approve', 'reason': 'The criteria assess different aspects of the same presentation.'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.assessment.mappings.get().moderation_decisions.count(), 1)
        self.assertEqual(combined_assessment_results(self.assessment)[0]['combined_score'], Decimal('84.00'))

    def test_decision_only_panel_workflow_cannot_be_weighted(self):
        self.hearing.workflow = PanelHearing.Workflow.OUTCOME
        self.hearing.save()
        self.assessment.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.assessment.full_clean()

    def test_separate_results_include_nonnumeric_panel_outcomes(self):
        self.assessment.result_policy = CombinedPresentationAssessment.ResultPolicy.SEPARATE
        self.assessment.save()
        self.hearing.workflow = PanelHearing.Workflow.OUTCOME
        self.hearing.save()
        group = self.session.groups.create(name='Outcome group')
        candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Outcome candidate')
        mapping = CombinedPresentationMapping.objects.create(organization_id=self.org.id,
            created_by=self.owner, assessment=self.assessment, group=group, candidate=candidate)
        decision = PanelDecision.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, candidate=candidate,
            outcome=PanelDecision.Outcome.CONDITIONAL, rationale='Revise the demonstration.',
            conditions='Submit the revised demonstration.')
        row = next(item for item in combined_assessment_results(self.assessment)
            if item['mapping_id'] == mapping.pk)
        self.assertIsNone(row['panel_score'])
        self.assertEqual(row['panel_decision'].pk, decision.pk)
        self.assertIsNone(row['combined_score'])

    def test_weights_must_sum_to_100(self):
        self.assessment.peer_weight = Decimal('55')
        with self.assertRaises(ValidationError):
            self.assessment.full_clean()

    def test_mapping_rejects_cross_assessment_rows(self):
        other_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other', service='classroom')
        other_group = other_session.groups.create(name='Other group')
        mapping = CombinedPresentationMapping(organization_id=self.org.id,
            created_by=self.owner, assessment=self.assessment, group=other_group,
            candidate=self.candidate)
        with self.assertRaises(ValidationError):
            mapping.full_clean()

    def test_organizer_detail_is_owner_and_organization_scoped(self):
        sign_in(self.client, self.owner, self.org)
        response = self.client.get(reverse('combined_assessment_detail', args=[self.assessment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Team One')
        sign_in(self.client, self.other, self.org)
        self.assertEqual(self.client.get(reverse('combined_assessment_detail',
            args=[self.assessment.pk])).status_code, 404)
        second_org = make_org('Second combined org')
        sign_in(self.client, self.owner, second_org)
        self.assertEqual(self.client.get(reverse('combined_assessment_detail',
            args=[self.assessment.pk])).status_code, 404)

    def test_pause_control_only_appears_for_a_matching_active_pair(self):
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        sign_in(self.client, self.owner, self.org)

        response = self.client.get(reverse('combined_assessment_detail', args=[self.assessment.pk]))
        self.assertContains(response, 'No presentation is active.')
        self.assertNotContains(response, 'Pause active scoring slot')

        self.session.status = AssessmentSession.Status.LIVE
        self.session.save()
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.active_candidate = self.candidate
        self.hearing.save()
        PresentationTurn.objects.create(assessment_session=self.session,
            group=self.group, status=PresentationTurn.Status.ACTIVE)
        response = self.client.get(reverse('combined_assessment_detail', args=[self.assessment.pk]))
        self.assertContains(response, 'Pause active scoring slot')
        self.assertTrue(response.context['paired_slot_active'])

    def test_pause_refuses_to_change_sources_when_active_entries_do_not_match(self):
        other_candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Other entry')
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        self.session.status = AssessmentSession.Status.LIVE
        self.session.save()
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.active_candidate = other_candidate
        self.hearing.save()
        active_turn = PresentationTurn.objects.create(assessment_session=self.session,
            group=self.group, status=PresentationTurn.Status.ACTIVE)
        sign_in(self.client, self.owner, self.org)

        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'pause_entry'})

        self.assertEqual(response.status_code, 302)
        active_turn.refresh_from_db()
        self.hearing.refresh_from_db()
        self.assertEqual(active_turn.status, PresentationTurn.Status.ACTIVE)
        self.assertEqual(self.hearing.active_candidate_id, other_candidate.pk)
        self.assertEqual(list(response.wsgi_request._messages)[0].message,
            'The peer and Panel active entries do not match. Check both assessments before pausing.')

    def test_combined_assessment_history_lists_only_the_current_owners_events(self):
        sign_in(self.client, self.owner, self.org)
        response = self.client.get(reverse('combined_assessment_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Combined event')
        sign_in(self.client, self.other, self.org)
        response = self.client.get(reverse('combined_assessment_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['assessments'].count(), 0)

    def test_setup_from_classroom_creates_exact_group_candidate_mapping(self):
        self.assessment.delete()
        # Automatic setup follows an explicit source-group link; matching display
        # names alone are not sufficient to establish identity.
        self.candidate.source_group = self.group
        self.candidate.save(update_fields=['source_group'])
        sign_in(self.client, self.owner, self.org)
        response = self.client.post(reverse('combined_assessment_create'), {
            'name': 'Presentation event', 'classroom_session': self.session.pk,
            'panel_hearing': self.hearing.pk, 'result_policy': 'separate',
            'peer_weight': '50', 'panel_weight': '50',
            'minimum_peer_ratings': '2', 'minimum_panel_raters': '2'})
        self.assertEqual(response.status_code, 302)
        created = CombinedPresentationAssessment.objects.get(name='Presentation event')
        self.assertEqual(created.mappings.count(), 1)
        self.assertEqual(created.mappings.get().group_id, self.group.pk)
        self.assertEqual(created.mappings.get().candidate_id, self.candidate.pk)

    def test_unmatched_names_can_be_mapped_by_the_organizer_before_publish(self):
        self.assessment.delete()
        self.candidate.display_name = 'Presentation A'
        self.candidate.save()
        sign_in(self.client, self.owner, self.org)
        response = self.client.post(reverse('combined_assessment_create'), {
            'name': 'Manual mapping event', 'classroom_session': self.session.pk,
            'panel_hearing': self.hearing.pk, 'result_policy': 'separate',
            'minimum_peer_ratings': '1', 'minimum_panel_raters': '1'})
        self.assertEqual(response.status_code, 302)
        created = CombinedPresentationAssessment.objects.get(name='Manual mapping event')
        self.assertFalse(created.mappings.exists())
        response = self.client.post(reverse('combined_assessment_detail', args=[created.pk]), {
            'action': 'add_mapping', 'group_id': self.group.pk, 'candidate_id': self.candidate.pk})
        self.assertEqual(response.status_code, 302)
        created.refresh_from_db()
        self.assertEqual(created.mappings.count(), 1)
        response = self.client.post(reverse('combined_assessment_detail', args=[created.pk]), {
            'action': 'publish'})
        created.refresh_from_db()
        self.assertEqual(created.status, CombinedPresentationAssessment.Status.ACTIVE)

    def test_weighted_setup_requires_a_declared_moderation_threshold(self):
        sign_in(self.client, self.owner, self.org)
        response = self.client.post(reverse('combined_assessment_create'), {
            'name': 'Missing moderation rule', 'classroom_session': self.session.pk,
            'panel_hearing': self.hearing.pk, 'result_policy': 'weighted',
            'peer_weight': '60', 'panel_weight': '40',
            'minimum_peer_ratings': '1', 'minimum_panel_raters': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Set the difference that will require a reasoned review.')
        self.assertFalse(CombinedPresentationAssessment.objects.filter(
            name='Missing moderation rule').exists())

    def test_published_policy_cannot_be_edited(self):
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        self.assessment.peer_weight = Decimal('70')
        self.assessment.panel_weight = Decimal('30')
        with self.assertRaises(ValidationError):
            self.assessment.full_clean()
        with self.assertRaises(ValidationError):
            self.assessment.save()

    def test_paired_control_activates_peer_turn_and_panel_candidate_together(self):
        group = self.session.groups.create(name='Team Two')
        candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Team Two')
        mapping = CombinedPresentationMapping.objects.create(organization_id=self.org.id,
            created_by=self.owner, assessment=self.assessment, group=group, candidate=candidate)
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.save()
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        sign_in(self.client, self.owner, self.org)
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'activate_entry', 'mapping_id': mapping.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.session.presentation_turns.filter(status='active').get().group_id, group.pk)
        self.hearing.refresh_from_db()
        self.assertEqual(self.hearing.active_candidate_id, candidate.pk)
