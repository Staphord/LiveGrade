"""Team series housekeeping at its edges."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from assessments.models import AssessmentSession
from service_domains import team_series

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
