from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.models import TrainingParticipant, TrainingSeries
from service_domains.training_roster_import import (
    apply_training_roster_import, parse_training_roster,
    plan_training_roster_import, sample_training_roster,
)


def upload_workbook(data=None):
    contents = (sample_training_roster() if data is None else data)
    if isinstance(contents, BytesIO):
        contents = contents.getvalue()
    return SimpleUploadedFile('cohort.xlsx', contents,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class TrainingRosterImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Training roster import')
        self.other_org = make_org('Other training roster')
        self.owner = make_user('training-roster-owner')
        self.other = make_user('training-roster-other')
        self.series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='New staff cohort')

    def test_sample_is_parsed_and_adds_roster_only_after_revalidation(self):
        parsed = parse_training_roster(upload_workbook())
        self.assertIsNone(parsed['fatal'])
        plan = plan_training_roster_import(self.series, parsed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(plan['summary'], {'rows': 3, 'ready': 3, 'skipped': 0, 'teams': 2})
        self.assertEqual(self.series.participants.count(), 0)
        applied = apply_training_roster_import(self.series, parsed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(applied, {'created': 3, 'skipped': 0, 'teams': 2})
        self.assertEqual(list(self.series.participants.order_by('order').values_list(
            'display_name', 'reference', 'group_name')), [
                ('Amina Yusuf', 'EMP-001', 'Orchid'),
                ('Juma Mwakalinga', 'EMP-002', 'Orchid'),
                ('Neema Said', 'EMP-003', 'Baobab'),
            ])

    def test_repeated_references_are_rejected_and_duplicate_existing_rows_are_skipped(self):
        duplicate_file = workbook_bytes(['Name', 'Student ID', 'Team'], [
            ['Amina Yusuf', 'EMP-001', 'Orchid'],
            ['Amina Y.', 'emp-001', 'Orchid'],
            ['Juma Mwakalinga', 'EMP-002', 'Orchid'],
        ])
        parsed = parse_training_roster(upload_workbook(duplicate_file))
        self.assertTrue(all(parsed['participants'][index]['issues'] for index in (0, 1)))
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=self.series, display_name='Juma Mwakalinga', reference='EMP-002',
            group_name='Orchid')
        plan = plan_training_roster_import(self.series, parsed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(plan['summary']['ready'], 0)
        self.assertIn('already in the series', plan['participants'][2]['warnings'][0])
        applied = apply_training_roster_import(self.series, parsed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(applied['created'], 0)
        self.assertEqual(self.series.participants.count(), 1)

    def test_missing_headers_and_empty_roster_fail_preview(self):
        missing = parse_training_roster(upload_workbook(
            workbook_bytes(['Learner'], [['Amina']])) )
        self.assertIn('Name and Team', missing['fatal'])
        empty = parse_training_roster(upload_workbook(workbook_bytes(['Name', 'Team'], [])))
        self.assertEqual(empty['participants'], [])
        self.assertFalse(empty['fatal'])

    def test_ambiguous_repeated_names_require_references_and_id_based_join(self):
        workbook = workbook_bytes(['Name', 'Student ID', 'Team'], [
            ['Amina Yusuf', '', 'Orchid'], ['Amina Yusuf', 'EMP-002', 'Baobab'],
        ])
        parsed = parse_training_roster(upload_workbook(workbook))
        self.assertTrue(all(row['issues'] for row in parsed['participants']))
        resolved = parse_training_roster(upload_workbook(workbook_bytes(
            ['Name', 'Student ID', 'Team'], [
                ['Amina Yusuf', 'EMP-001', 'Orchid'],
                ['Amina Yusuf', 'EMP-002', 'Baobab'],
            ])))
        self.assertTrue(all(not row['issues'] for row in resolved['participants']))
        self.assertTrue(all(any('Student ID' in warning for warning in row['warnings'])
                            for row in resolved['participants']))

    def test_owner_and_organization_scope_is_required_for_preview_and_apply(self):
        parsed = parse_training_roster(upload_workbook())
        with self.assertRaises(PermissionDenied):
            plan_training_roster_import(self.series, parsed,
                organization=self.org.as_claim(), user=self.other)
        with self.assertRaises(PermissionDenied):
            apply_training_roster_import(self.series, parsed,
                organization=self.other_org.as_claim(), user=self.owner)
        self.assertEqual(self.series.participants.count(), 0)

    def test_authenticated_preview_apply_and_sample_routes(self):
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            sample = self.client.get(reverse('training_series_roster_sample', args=(self.series.pk,)))
            self.assertEqual(sample.status_code, 200)
            response = self.client.post(reverse('training_series_roster_import', args=(self.series.pk,)),
                {'file': upload_workbook()})
            self.assertEqual(response.status_code, 302)
            preview_url = reverse('training_series_roster_preview', args=(self.series.pk,))
            preview = self.client.get(preview_url)
            self.assertEqual(preview.status_code, 200)
            self.assertContains(preview, 'Ready to add')
            self.assertContains(preview, 'Amina Yusuf')
            applied = self.client.post(preview_url, {'action': 'apply'})
            self.assertEqual(applied.status_code, 302)
        self.assertEqual(self.series.participants.count(), 3)
