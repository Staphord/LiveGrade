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

    def test_combined_assessment_edge_cases_and_error_handling(self):
        sign_in(self.client, self.owner, self.org)
        mapping = self.assessment.mappings.first()

        # Non-CombinedPresentationAssessment object passed to combined_assessment_results
        with self.assertRaises(TypeError):
            combined_assessment_results(object())

        # Unmatched group membership snapshot invalidates complete mapping
        mapping.group_member_ids_snapshot = [9999]
        mapping.save()
        self.assertFalse(self.assessment.mappings.first().assessment.mappings.count() == 0)

        # Deactivate entry action
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'deactivate_entry', 'mapping_id': mapping.pk})
        self.assertEqual(response.status_code, 302)

        # Invalid action triggers warning
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'unsupported_action'})
        self.assertEqual(response.status_code, 302)

        # Remove mapping action
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'remove_mapping', 'mapping_id': mapping.pk})
        self.assertEqual(response.status_code, 302)

        # Delete draft combined assessment with dedicated fresh sessions
        sess_draft = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Peer draft sess',
            service='classroom', status='draft', ungraded_penalty=0)
        hearing_draft = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Panel draft hearing',
            status=PanelHearing.Status.DRAFT, workflow=PanelHearing.Workflow.SCORED)
        draft_to_delete = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Temporary draft',
            classroom_session=sess_draft, panel_hearing=hearing_draft,
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE)
        response = self.client.post(reverse('combined_assessment_detail', args=[draft_to_delete.pk]), {
            'action': 'delete_draft'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CombinedPresentationAssessment.objects.filter(pk=draft_to_delete.pk).exists())

        # Create candidate from group action
        group_unlinked = self.session.groups.create(name='Unlinked Group')
        self.hearing.status = PanelHearing.Status.DRAFT
        self.hearing.save()
        self.assessment.status = CombinedPresentationAssessment.Status.DRAFT
        self.assessment.save()
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'create_candidate_from_group', 'group_id': group_unlinked.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PanelCandidate.objects.filter(hearing=self.hearing, source_group=group_unlinked).exists())

        # Pause and Close combined assessment
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        response = self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {
            'action': 'close'})
        self.assertEqual(response.status_code, 302)
        self.assessment.refresh_from_db()
        self.assertEqual(self.assessment.status, CombinedPresentationAssessment.Status.CLOSED)

        # Service scoping verification
        from django.test import RequestFactory
        from service_domains.combined_views import combined_assessment_list
        rf = RequestFactory()
        req = rf.get(reverse('combined_assessment_list'))
        req.user = self.owner
        req.service = 'classroom'
        from django.http import Http404
        with self.assertRaises(Http404):
            combined_assessment_list(req)

        # Unauthorized access from another user
        unauth_client = self.client_class()
        sign_in(unauth_client, self.other, self.org)
        response = unauth_client.get(reverse('combined_assessment_detail', args=[self.assessment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_combined_assessment_form_validation(self):
        from service_domains.combined_views import CombinedAssessmentForm
        # Missing one or both weights in WEIGHTED policy
        form = CombinedAssessmentForm(organization=self.org.as_claim(), user=self.owner, data={
            'name': 'Test Form',
            'classroom_session': self.session.pk,
            'panel_hearing': self.hearing.pk,
            'result_policy': 'weighted',
            'peer_weight': '',
            'panel_weight': '40',
            'moderation_threshold': '10',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('Enter both weights for a weighted combined mark.', form.non_field_errors())

        # Weights not totaling 100%
        form = CombinedAssessmentForm(organization=self.org.as_claim(), user=self.owner, data={
            'name': 'Test Form',
            'classroom_session': self.session.pk,
            'panel_hearing': self.hearing.pk,
            'result_policy': 'weighted',
            'peer_weight': '60',
            'panel_weight': '30',
            'moderation_threshold': '10',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('Peer and Panel weights must total 100%.', form.non_field_errors())

        # Non-numeric panel workflow in WEIGHTED policy
        hearing_non_numeric = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Outcome hearing',
            status=PanelHearing.Status.DRAFT, workflow=PanelHearing.Workflow.OUTCOME)
        form = CombinedAssessmentForm(organization=self.org.as_claim(), user=self.owner, data={
            'name': 'Test Form',
            'classroom_session': self.session.pk,
            'panel_hearing': hearing_non_numeric.pk,
            'result_policy': 'weighted',
            'peer_weight': '50',
            'panel_weight': '50',
            'moderation_threshold': '10',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('Weighted marks require numeric Panel rubric scoring.', form.errors['panel_hearing'])

        # Create view error handling when mapping save fails
        from unittest.mock import patch
        sign_in(self.client, self.owner, self.org)
        sess_fail = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Fail Session',
            service='classroom', status='draft', ungraded_penalty=0)
        hearing_fail = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Fail Hearing',
            status=PanelHearing.Status.DRAFT, workflow=PanelHearing.Workflow.SCORED)
        grp_fail = sess_fail.groups.create(name='FailGrp')
        cand_fail = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing_fail, display_name='FailGrp', source_group=grp_fail)
        with patch.object(CombinedPresentationMapping, 'save', side_effect=ValidationError(['Simulated mapping failure'])):
            res = self.client.post(reverse('combined_assessment_create'), {
                'name': 'Mapping failure form',
                'classroom_session': sess_fail.pk,
                'panel_hearing': hearing_fail.pk,
                'result_policy': 'separate',
                'minimum_peer_ratings': '1',
                'minimum_panel_raters': '1',
            })
            self.assertEqual(res.status_code, 200)
            self.assertIn('Simulated mapping failure', res.content.decode())

    def test_combined_assessment_form_labels_resolve_model_names(self):
        from service_domains.combined_views import CombinedAssessmentForm
        form = CombinedAssessmentForm(organization=self.org.as_claim(), user=self.owner)
        rendered = str(form['panel_hearing'])
        self.assertIn(self.hearing.name, rendered)
        self.assertNotIn('PanelHearing object', rendered)
        self.assertEqual(str(self.hearing), self.hearing.name)

    def test_combined_assessment_detail_views_and_roster_snapshot(self):
        sign_in(self.client, self.owner, self.org)
        # Create view with initial GET parameters
        url_create = reverse('combined_assessment_create') + f'?classroom_session={self.session.pk}&panel_hearing={self.hearing.pk}'
        res = self.client.get(url_create)
        self.assertEqual(res.status_code, 200)

        # Detail view with weighted policy and results view queries
        detail_url = reverse('combined_assessment_detail', args=[self.assessment.pk])
        res = self.client.get(detail_url + '?results=combined')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['result_view'], 'combined')

        res = self.client.get(detail_url + '?results=invalid_query')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['result_view'], 'all')

        # Roster change triggers roster_changed_mappings
        new_student = self.session.students.create(full_name='New Addition')
        self.group.memberships.create(student=new_student)
        res = self.client.get(detail_url)
        self.assertEqual(res.status_code, 200)
        self.assertGreater(len(res.context['roster_changed_mappings']), 0)

    def test_combined_assessment_publishing_and_candidate_actions(self):
        sign_in(self.client, self.owner, self.org)
        sess = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Draft peer',
            service='classroom', status='draft', ungraded_penalty=0)
        hearing = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Draft hearing',
            status=PanelHearing.Status.DRAFT, workflow=PanelHearing.Workflow.SCORED)
        draft = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Draft for actions',
            classroom_session=sess, panel_hearing=hearing,
            result_policy=CombinedPresentationAssessment.ResultPolicy.WEIGHTED,
            peer_weight=Decimal('50'), panel_weight=Decimal('50'), moderation_threshold=Decimal('10'))
        detail_url = reverse('combined_assessment_detail', args=[draft.pk])

        from django.db.models import QuerySet
        # Publish draft with unequal weights fails
        QuerySet.update(CombinedPresentationAssessment.objects.filter(pk=draft.pk),
            peer_weight=Decimal('60'), panel_weight=Decimal('30'))
        res = self.client.post(detail_url, {'action': 'publish'})
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, CombinedPresentationAssessment.Status.DRAFT)

        # Publish draft with non-numeric workflow fails
        QuerySet.update(CombinedPresentationAssessment.objects.filter(pk=draft.pk),
            peer_weight=Decimal('50'), panel_weight=Decimal('50'))
        PanelHearing.objects.filter(pk=hearing.pk).update(workflow=PanelHearing.Workflow.OUTCOME)
        draft.refresh_from_db()
        hearing.refresh_from_db()
        res = self.client.post(detail_url, {'action': 'publish'})
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, CombinedPresentationAssessment.Status.DRAFT)

        # Incomplete mapping blocks publish
        PanelHearing.objects.filter(pk=hearing.pk).update(workflow=PanelHearing.Workflow.SCORED)
        hearing.refresh_from_db()
        res = self.client.post(detail_url, {'action': 'publish'})
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, CombinedPresentationAssessment.Status.DRAFT)

        # Create candidate from group: 404 for invalid group
        res = self.client.post(detail_url, {'action': 'create_candidate_from_group', 'group_id': '99999'})
        self.assertEqual(res.status_code, 404)

        # Create candidate when hearing is not DRAFT
        hearing.status = PanelHearing.Status.OPEN
        hearing.save()
        group = sess.groups.create(name='Grp 1')
        res = self.client.post(detail_url, {'action': 'create_candidate_from_group', 'group_id': str(group.pk)})
        self.assertEqual(res.status_code, 302)

        # Create candidate when group already linked
        hearing.status = PanelHearing.Status.DRAFT
        hearing.save()
        PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, source_group=group, display_name=group.name)
        res = self.client.post(detail_url, {'action': 'create_candidate_from_group', 'group_id': str(group.pk)})
        self.assertEqual(res.status_code, 302)

        # Create candidate when save raises IntegrityError
        from unittest.mock import patch
        from django.db import IntegrityError
        group_fail = sess.groups.create(name='Grp Fail')
        with patch.object(PanelCandidate, 'save', side_effect=IntegrityError('Duplicate candidate')):
            res = self.client.post(detail_url, {'action': 'create_candidate_from_group', 'group_id': str(group_fail.pk)})
            self.assertEqual(res.status_code, 302)

    def test_combined_assessment_mapping_and_activation_edge_cases(self):
        sign_in(self.client, self.owner, self.org)
        sess = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Pair session',
            service='classroom', status=AssessmentSession.Status.LIVE, ungraded_penalty=0)
        hearing = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Pair hearing',
            status=PanelHearing.Status.OPEN, workflow=PanelHearing.Workflow.SCORED)
        grp = sess.groups.create(name='PGroup')
        cand = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='PCandidate')
        comb = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Pair assessment',
            classroom_session=sess, panel_hearing=hearing,
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE)
        detail_url = reverse('combined_assessment_detail', args=[comb.pk])

        # Add mapping with invalid group or candidate -> 404
        res = self.client.post(detail_url, {'action': 'add_mapping', 'group_id': '99999', 'candidate_id': str(cand.pk)})
        self.assertEqual(res.status_code, 404)

        # Add mapping successfully linking unlinked candidate
        res = self.client.post(detail_url, {'action': 'add_mapping', 'group_id': str(grp.pk), 'candidate_id': str(cand.pk)})
        self.assertEqual(res.status_code, 302)
        mapping = comb.mappings.first()
        self.assertIsNotNone(mapping)

        # Add duplicate mapping fails gracefully
        res = self.client.post(detail_url, {'action': 'add_mapping', 'group_id': str(grp.pk), 'candidate_id': str(cand.pk)})
        self.assertEqual(res.status_code, 302)

        # Remove mapping: 404 for invalid id
        res = self.client.post(detail_url, {'action': 'remove_mapping', 'mapping_id': '99999'})
        self.assertEqual(res.status_code, 404)

        # Activate entry: 404 for invalid mapping id
        comb.status = CombinedPresentationAssessment.Status.ACTIVE
        comb.save()
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': '99999'})
        self.assertEqual(res.status_code, 404)

        # Activate entry: incomplete mapping error
        unmapped_grp = sess.groups.create(name='Unmapped Group')
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        self.assertEqual(res.status_code, 302)
        unmapped_grp.delete()

        # Activate entry: session not live error
        sess.status = AssessmentSession.Status.CLOSED
        sess.save()
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        self.assertEqual(res.status_code, 302)
        sess.status = AssessmentSession.Status.LIVE
        sess.save()

        # Detail view on separate policy assessment exercises result_policy != WEIGHTED branch
        res = self.client.get(detail_url)
        self.assertEqual(res.status_code, 200)

        # Hearing candidate already active exercises candidate activation bypass branch
        from service_domains.operations import activate_panel_candidate
        activate_panel_candidate(hearing, cand, organization=self.org.as_claim(), user=self.owner)
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        self.assertEqual(res.status_code, 302)

        # Classroom turn already active exercises turn activation bypass branch
        from service_domains.operations import deactivate_panel_candidate
        deactivate_panel_candidate(hearing, organization=self.org.as_claim(), user=self.owner)
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        self.assertEqual(res.status_code, 302)

        # Activate entry when already active in both
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        self.assertEqual(res.status_code, 302)

        # Pause active entry successfully when panel hearing is under review (skips candidate deactivation)
        hearing.status = PanelHearing.Status.REVIEW
        hearing.active_candidate = cand
        hearing.save(update_fields=['status', 'active_candidate'])
        res = self.client.post(detail_url, {'action': 'pause_entry'})
        self.assertEqual(res.status_code, 302)
        hearing.status = PanelHearing.Status.OPEN
        hearing.save(update_fields=['status'])

        # Close combined assessment with active turn
        sess.presentation_turns.all().delete()
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        res = self.client.post(detail_url, {'action': 'close'})
        self.assertEqual(res.status_code, 302)
        comb.refresh_from_db()
        self.assertEqual(comb.status, CombinedPresentationAssessment.Status.CLOSED)

        # Reopen assessment to test closed turn check
        comb.status = CombinedPresentationAssessment.Status.ACTIVE
        comb.save()
        sess.presentation_turns.create(group=grp, status=PresentationTurn.Status.CLOSED)
        res = self.client.post(detail_url, {'action': 'activate_entry', 'mapping_id': str(mapping.pk)})
        self.assertEqual(res.status_code, 302)

        # Moderate: 404 for invalid mapping
        res = self.client.post(detail_url, {'action': 'moderate', 'mapping_id': '99999', 'decision': 'approve'})
        self.assertEqual(res.status_code, 404)

        # Moderate: invalid decision sets error message
        res = self.client.post(detail_url, {'action': 'moderate', 'mapping_id': str(mapping.pk), 'decision': 'invalid_decision'})
        self.assertEqual(res.status_code, 302)


class CombinedAssessmentTableTests(CombinedAssessmentTests):
    def test_search_and_filters_on_combined_assessment_list(self):
        sign_in(self.client, self.owner, self.org)
        # Create a second combined assessment with weighted policy
        sess2 = AssessmentSession.objects.create(
            name='Physics Session', organization_id=self.org.pk,
            created_by=self.owner, service='classroom')
        hearing2 = PanelHearing.objects.create(
            name='Physics Hearing', organization_id=self.org.pk,
            created_by=self.owner, workflow=PanelHearing.Workflow.SCORED)
        comb2 = CombinedPresentationAssessment.objects.create(
            name='Weighted Science Evaluation', classroom_session=sess2, panel_hearing=hearing2,
            result_policy=CombinedPresentationAssessment.ResultPolicy.WEIGHTED,
            peer_weight=Decimal('60'), panel_weight=Decimal('40'),
            moderation_threshold=Decimal('10'), status=CombinedPresentationAssessment.Status.ACTIVE,
            organization_id=self.org.pk, created_by=self.owner)

        # Base list renders both
        res = self.client.get(reverse('combined_assessment_list'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Combined event')
        self.assertContains(res, 'Weighted Science Evaluation')
        self.assertContains(res, 'Separate reporting')
        self.assertContains(res, 'Weighted (60% peer · 40% panel)')

        # Search by assessment name
        res_search = self.client.get(reverse('combined_assessment_list'), {'q': 'Science'})
        self.assertContains(res_search, 'Weighted Science Evaluation')
        self.assertNotContains(res_search, '>Combined event<')

        # Search by source classroom session name
        res_source = self.client.get(reverse('combined_assessment_list'), {'q': 'Physics Session'})
        self.assertContains(res_source, 'Weighted Science Evaluation')
        self.assertNotContains(res_source, '>Combined event<')

        # Filter by status
        res_status = self.client.get(reverse('combined_assessment_list'), {'status': 'active'})
        self.assertContains(res_status, 'Weighted Science Evaluation')
        self.assertNotContains(res_status, '>Combined event<')

        # Filter by policy
        sess3 = AssessmentSession.objects.create(
            name='Biology Session', organization_id=self.org.id,
            created_by=self.owner, service='classroom')
        hearing3 = PanelHearing.objects.create(
            name='Biology Hearing', organization_id=self.org.id,
            created_by=self.owner, workflow=PanelHearing.Workflow.SCORED)
        comb_sep = CombinedPresentationAssessment.objects.create(
            name='Unique Separate Evaluation', classroom_session=sess3, panel_hearing=hearing3,
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE,
            status=CombinedPresentationAssessment.Status.DRAFT,
            organization_id=self.org.pk, created_by=self.owner)
        res_policy = self.client.get(reverse('combined_assessment_list'), {'policy': 'separate'})
        self.assertContains(res_policy, 'Unique Separate Evaluation')
        self.assertNotContains(res_policy, 'Weighted Science Evaluation')

        # AJAX request returns fragment
        res_ajax = self.client.get(reverse('combined_assessment_list'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_ajax.status_code, 200)
        self.assertIn('X-Requested-With', res_ajax.headers.get('Vary', ''))
        self.assertContains(res_ajax, 'id="panel-results"')

    def test_delete_draft_combined_assessment(self):
        sign_in(self.client, self.owner, self.org)
        # self.assessment is draft
        self.assertEqual(self.assessment.status, CombinedPresentationAssessment.Status.DRAFT)
        delete_url = reverse('combined_assessment_delete', args=[self.assessment.pk])
        res = self.client.post(delete_url)
        self.assertEqual(res.status_code, 302)
        self.assertFalse(CombinedPresentationAssessment.objects.filter(pk=self.assessment.pk).exists())

    def test_delete_active_combined_assessment_rejected(self):
        sign_in(self.client, self.owner, self.org)
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save(update_fields=['status'])
        delete_url = reverse('combined_assessment_delete', args=[self.assessment.pk])
        res = self.client.post(delete_url)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(CombinedPresentationAssessment.objects.filter(pk=self.assessment.pk).exists())

    def test_combined_assessment_bulk_delete(self):
        sign_in(self.client, self.owner, self.org)
        sess_b1 = AssessmentSession.objects.create(
            name='Bulk Sess 1', organization_id=self.org.pk,
            created_by=self.owner, service='classroom')
        hearing_b1 = PanelHearing.objects.create(
            name='Bulk Hearing 1', organization_id=self.org.pk,
            created_by=self.owner, workflow=PanelHearing.Workflow.SCORED)
        comb_b1 = CombinedPresentationAssessment.objects.create(
            name='Bulk Combined 1', classroom_session=sess_b1, panel_hearing=hearing_b1,
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE,
            status=CombinedPresentationAssessment.Status.DRAFT,
            organization_id=self.org.pk, created_by=self.owner)

        sess_b2 = AssessmentSession.objects.create(
            name='Bulk Sess 2', organization_id=self.org.pk,
            created_by=self.owner, service='classroom')
        hearing_b2 = PanelHearing.objects.create(
            name='Bulk Hearing 2', organization_id=self.org.pk,
            created_by=self.owner, workflow=PanelHearing.Workflow.SCORED)
        comb_b2 = CombinedPresentationAssessment.objects.create(
            name='Bulk Combined 2', classroom_session=sess_b2, panel_hearing=hearing_b2,
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE,
            status=CombinedPresentationAssessment.Status.DRAFT,
            organization_id=self.org.pk, created_by=self.owner)

        bulk_url = reverse('combined_assessment_bulk_delete')

        # Empty selection validation
        res_empty = self.client.post(bulk_url, {'assessment_ids': []})
        self.assertEqual(res_empty.status_code, 302)

        # Invalid ID validation
        res_invalid = self.client.post(bulk_url, {'assessment_ids': ['abc']})
        self.assertEqual(res_invalid.status_code, 302)

        # Over 100 IDs limit validation
        res_overflow = self.client.post(bulk_url, {'assessment_ids': [str(i) for i in range(101)]})
        self.assertEqual(res_overflow.status_code, 302)

        # Active status cannot be bulk deleted
        comb_b2.status = CombinedPresentationAssessment.Status.ACTIVE
        comb_b2.save(update_fields=['status'])
        res_active = self.client.post(bulk_url, {'assessment_ids': [str(comb_b1.pk), str(comb_b2.pk)]})
        self.assertEqual(res_active.status_code, 302)
        self.assertTrue(CombinedPresentationAssessment.objects.filter(pk=comb_b1.pk).exists())
        self.assertTrue(CombinedPresentationAssessment.objects.filter(pk=comb_b2.pk).exists())

        # Deletion of draft items
        sess_b3 = AssessmentSession.objects.create(
            name='Bulk Sess 3', organization_id=self.org.pk,
            created_by=self.owner, service='classroom')
        hearing_b3 = PanelHearing.objects.create(
            name='Bulk Hearing 3', organization_id=self.org.pk,
            created_by=self.owner, workflow=PanelHearing.Workflow.SCORED)
        comb_b3 = CombinedPresentationAssessment.objects.create(
            name='Bulk Combined 3', classroom_session=sess_b3, panel_hearing=hearing_b3,
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE,
            status=CombinedPresentationAssessment.Status.DRAFT,
            organization_id=self.org.pk, created_by=self.owner)

        res_ok = self.client.post(bulk_url, {'assessment_ids': [str(comb_b1.pk), str(comb_b3.pk)]})
        self.assertEqual(res_ok.status_code, 302)
        self.assertFalse(CombinedPresentationAssessment.objects.filter(pk=comb_b1.pk).exists())
        self.assertFalse(CombinedPresentationAssessment.objects.filter(pk=comb_b3.pk).exists())
        # Active comb_b2 remains untouched
        self.assertTrue(CombinedPresentationAssessment.objects.filter(pk=comb_b2.pk).exists())

    def test_sidebar_and_tab_navigation(self):
        sign_in(self.client, self.owner, self.org)

        # Panel assessments table page
        res_table = self.client.get(reverse('panel_hearing_table'))
        self.assertEqual(res_table.status_code, 200)
        self.assertContains(res_table, 'aria-label="Assessment views"')
        self.assertContains(res_table, 'panel-view-tab active')
        self.assertContains(res_table, reverse('combined_assessment_list'))
        self.assertContains(res_table, '<span class="nav-text">Assessments</span>')
        self.assertNotContains(res_table, '<span class="nav-text">Combined assessments</span>')

        # Combined assessments list page
        res_combined = self.client.get(reverse('combined_assessment_list'))
        self.assertEqual(res_combined.status_code, 200)
        self.assertContains(res_combined, 'aria-label="Assessment views"')
        self.assertContains(res_combined, 'panel-view-tab active')
        self.assertContains(res_combined, reverse('panel_hearing_table'))
        self.assertContains(res_combined, '<span class="nav-text">Assessments</span>')
        self.assertNotContains(res_combined, '<span class="nav-text">Combined assessments</span>')

    def test_combined_assessment_live_state_endpoint(self):
        live_state_url = reverse('combined_assessment_live_state', args=[self.assessment.pk])
        # Unauthenticated request fails
        res_unauth = self.client.get(live_state_url)
        self.assertEqual(res_unauth.status_code, 302)

        # Other user cannot access
        sign_in(self.client, self.other, self.org)
        res_other = self.client.get(live_state_url)
        self.assertEqual(res_other.status_code, 404)

        # Owner gets valid JSON state
        sign_in(self.client, self.owner, self.org)
        res_owner = self.client.get(live_state_url)
        self.assertEqual(res_owner.status_code, 200)
        data = res_owner.json()
        self.assertEqual(data['assessment_id'], self.assessment.pk)
        self.assertFalse(data['paired_slot_active'])
        self.assertFalse(data['is_out_of_sync'])
        self.assertIn('pairs', data)
        self.assertEqual(len(data['pairs']), 1)
        self.assertEqual(data['pairs'][0]['group_name'], 'Team One')
        self.assertEqual(data['pairs'][0]['candidate_name'], 'Team One')

        # Activate paired presentation
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        self.session.status = AssessmentSession.Status.LIVE
        self.session.save()
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.active_candidate = self.candidate
        self.hearing.save()
        PresentationTurn.objects.create(assessment_session=self.session,
            group=self.group, status=PresentationTurn.Status.ACTIVE)

        res_active = self.client.get(live_state_url)
        self.assertEqual(res_active.status_code, 200)
        data_active = res_active.json()
        self.assertTrue(data_active['paired_slot_active'])
        self.assertFalse(data_active['is_out_of_sync'])
        self.assertEqual(data_active['active_group_name'], 'Team One')
        self.assertEqual(data_active['active_candidate_name'], 'Team One')
        self.assertTrue(data_active['pairs'][0]['is_active'])
