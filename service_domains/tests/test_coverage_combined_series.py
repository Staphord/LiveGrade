"""Combined assessments and Team series housekeeping at their edges."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, PresentationTurn
from service_domains import team_series
from service_domains.combined_assessment import mapping_is_complete, record_combined_moderation
from service_domains.models import (
    CombinedModerationDecision, CombinedPresentationAssessment, PanelCandidate, PanelHearing)
from service_domains.tests import test_combined_assessment as combined


@override_settings(LIVEGRADE_LIVE_SERVICES=('classroom', 'panel'))
class CombinedEdgeTests(TestCase):
    setUp = combined.CombinedAssessmentTests.setUp

    def finish_sources(self):
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        self.hearing.status = PanelHearing.Status.SIGNED_OFF
        self.hearing.signed_off_at = timezone.now()
        self.hearing.save(update_fields=['status', 'signed_off_at'])

    def moderate(self, user=None):
        return record_combined_moderation(
            self.assessment.mappings.get(), CombinedModerationDecision.Decision.APPROVE, 'Because',
            organization=self.org.as_claim(), user=user or self.owner)

    def test_only_the_owner_can_moderate(self):
        with self.assertRaises(PermissionDenied):
            self.moderate(self.other)

    def test_moderation_waits_for_final_sources_and_a_large_enough_gap(self):
        with self.assertRaisesMessage(ValidationError, 'final'):
            self.moderate()
        self.finish_sources()
        self.assessment.moderation_threshold = Decimal('50')
        self.assessment.save()
        with self.assertRaisesMessage(ValidationError, 'threshold'):
            self.moderate()

    def test_moderation_applies_only_to_weighted_marks(self):
        separate = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Separate',
            classroom_session=AssessmentSession.objects.create(
                organization_id=self.org.id, created_by=self.owner, name='Other peers', service='classroom'),
            panel_hearing=PanelHearing.objects.create(
                organization_id=self.org.id, created_by=self.owner, name='Other panel',
                workflow=PanelHearing.Workflow.SCORED),
            result_policy=CombinedPresentationAssessment.ResultPolicy.SEPARATE)
        group = separate.classroom_session.groups.create(name='Solo')
        candidate = PanelCandidate.objects.create(
            organization_id=self.org.id, created_by=self.owner, hearing=separate.panel_hearing, display_name='Solo')
        mapping = separate.mappings.create(
            organization_id=self.org.id, created_by=self.owner, group=group, candidate=candidate)
        with self.assertRaisesMessage(ValidationError, 'weighted'):
            record_combined_moderation(mapping, CombinedModerationDecision.Decision.APPROVE, 'x',
                                       organization=self.org.as_claim(), user=self.owner)

    def test_a_panel_entry_linked_to_another_group_makes_the_mapping_incomplete(self):
        self.assertTrue(mapping_is_complete(self.assessment))
        practice = self.session.groups.create(name='Practice', is_practice=True)
        PanelCandidate.objects.filter(pk=self.candidate.pk).update(source_group=practice)
        self.assertFalse(mapping_is_complete(CombinedPresentationAssessment.objects.get(pk=self.assessment.pk)))

    def test_pausing_a_matched_pair_also_pauses_the_open_panel(self):
        self.assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        self.assessment.save()
        self.session.status = AssessmentSession.Status.LIVE
        self.session.save()
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.active_candidate = self.candidate
        self.hearing.save()
        PresentationTurn.objects.create(assessment_session=self.session, group=self.group,
                                        status=PresentationTurn.Status.ACTIVE)
        sign_in(self.client, self.owner, self.org)
        self.client.post(reverse('combined_assessment_detail', args=[self.assessment.pk]), {'action': 'pause_entry'})
        self.hearing.refresh_from_db()
        self.assertIsNone(self.hearing.active_candidate_id)

    def test_a_failed_bulk_delete_keeps_the_list_filters(self):
        sign_in(self.client, self.owner, self.org)
        response = self.client.post(reverse('combined_assessment_bulk_delete'), {
            'q': 'event', 'status': 'draft', 'policy': 'weighted', 'per_page': '25'})
        for part in ('q=event', 'status=draft', 'policy=weighted', 'per_page=25'):
            self.assertIn(part, response['Location'])


class SeriesHousekeepingTests(TestCase):
    def test_organization_identity_is_read_from_any_shape(self):
        self.assertEqual(team_series.organization_id(SimpleNamespace(pk=4)), 4)
        self.assertEqual(team_series.organization_id({'id': 5}), 5)
        self.assertEqual(team_series.organization_id(6), 6)

    def test_a_repeated_session_is_exported_once(self):
        session = AssessmentSession(pk=1, name='S')
        with patch.object(team_series, '_record', return_value={}) as record, \
                patch.object(AssessmentSession._meta, 'related_objects', []):
            exported = team_series._assessment_cascade_records([session, session])
        self.assertEqual(len(exported), 1)
        record.assert_called_once()

    def test_forged_or_expired_export_tokens_are_refused(self):
        self.assertFalse(team_series.verify_deletion_export_token('forged', {'id': 1}, SimpleNamespace(pk=1), [1]))

    def test_search_and_page_size_survive_a_failed_deletion(self):
        query = team_series.deletion_redirect_query
        self.assertEqual(query({'q': ' Leadership ', 'per_page': 'x'}).split('&')[0], 'q=Leadership')
        self.assertNotIn('q=', query({'per_page': '999999'}))

    def test_unresolvable_result_revisions_stop_the_deletion(self):
        remaining = MagicMock()
        remaining.exists.return_value = True
        remaining.filter.return_value.distinct.return_value.exists.return_value = False
        results = MagicMock()
        results.objects.filter.return_value = remaining
        with patch.multiple(
                team_series, _series_for_selection=MagicMock(return_value=[SimpleNamespace(pk=1)]),
                TrainingSeriesSession=MagicMock(), TrainingCompetencyResult=results,
                TrainingCertificate=MagicMock(), TrainingLearnerReportLink=MagicMock(),
                TrainingFeedback=MagicMock()):
            with self.assertRaises(RuntimeError):
                team_series.delete_series_selection({'id': 1}, SimpleNamespace(pk=1), [1])
