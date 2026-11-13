from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.models import Competition, CompetitionTrack, CompetitionEntry, CompetitionRound, CompetitionRoundEntry, PanelHearing
from service_domains.panel_rubric_import import parse_rubric, apply_rubric_import


class ReadinessAdditionsTests(TestCase):
    def setUp(self):
        self.org = make_org('Readiness additions')
        self.owner = make_user('readiness-additions-owner')

    def test_rubric_import_preserves_weights_and_rejects_invalid_weights(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id, created_by=self.owner, name='Assessment')
        parsed = parse_rubric(workbook_bytes(['Criterion', 'Weight'], [['Evidence', 40], ['Delivery', 60]]))
        created = apply_rubric_import(hearing, parsed, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual([item.weight for item in created], [40, 60])
        invalid = parse_rubric(workbook_bytes(['Criterion', 'Weight'], [['Evidence', -1]]))
        self.assertTrue(invalid['errors'])

    def test_competition_csv_is_owner_scoped_and_safe_for_spreadsheets(self):
        event = Competition.objects.create(organization_id=self.org.id, created_by=self.owner,
            name='Event', status=Competition.Status.COMPLETE)
        track = CompetitionTrack.objects.create(organization_id=self.org.id, created_by=self.owner,
            competition=event, name='Track')
        entry = CompetitionEntry.objects.create(organization_id=self.org.id, created_by=self.owner,
            track=track, name='=formula')
        round_obj = CompetitionRound.objects.create(organization_id=self.org.id, created_by=self.owner,
            competition=event, name='Final', order=1)
        CompetitionRoundEntry.objects.create(organization_id=self.org.id, created_by=self.owner,
            round=round_obj, entry=entry, track=track, score=85)
        url = reverse('competition_results_export', args=(event.pk,))
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "'=formula")
            self.assertIn('no-store', response['Cache-Control'])
            sign_in(self.client, make_user('other-export-owner'), self.org)
            self.assertEqual(self.client.get(url).status_code, 404)
