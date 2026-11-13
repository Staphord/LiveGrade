from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.models import TrainingParticipant, TrainingSeries
from service_domains.training_roster_import import (
    UNREADABLE,
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

    def test_parse_training_roster_edge_cases_and_limits(self):
        corrupt = SimpleUploadedFile('bad.xlsx', b'not a valid zip file', content_type='application/octet-stream')
        self.assertEqual(parse_training_roster(corrupt)['fatal'], UNREADABLE)

        empty_upload = upload_workbook(workbook_bytes(['', ''], [['', '']]))
        self.assertEqual(parse_training_roster(empty_upload)['fatal'], 'The file is empty.')

        too_many = workbook_bytes(['Name', 'Team'], [['Learner', 'Team']] * 5001)
        self.assertIn('Split it and import in parts', parse_training_roster(upload_workbook(too_many))['fatal'])

        blank_and_invalid_rows = workbook_bytes(['Name', 'Student ID', 'Team', 'Notes'], [
            ['', '', '', 'Unmapped note row'],
            ['', 'EMP-010', 'Team A', ''],
            ['Learner Two', '', '', ''],
            ['A' * 200, 'B' * 100, 'C' * 200, ''],
        ])
        parsed = parse_training_roster(upload_workbook(blank_and_invalid_rows))
        self.assertEqual(len(parsed['participants']), 3)
        self.assertIn('Name is required.', parsed['participants'][0]['issues'])
        self.assertIn('Team or group is required.', parsed['participants'][1]['issues'])
        self.assertTrue(any('longer than' in issue for issue in parsed['participants'][2]['issues']))

    def test_plan_training_roster_edge_cases_matching_existing(self):
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=self.series, display_name='Existing Name', reference='EMP-999', group_name='Existing Group')
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=self.series, display_name='No Ref Learner', reference='', group_name='Team Alpha')

        diff_learner_same_ref = workbook_bytes(['Name', 'Student ID', 'Team'], [
            ['Different Name', 'EMP-999', 'Other Group'],
            ['No Ref Learner', '', 'Team Alpha'],
        ])
        parsed = parse_training_roster(upload_workbook(diff_learner_same_ref))
        plan = plan_training_roster_import(self.series, parsed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIn('belongs to a different learner', plan['participants'][0]['issues'][0])
        self.assertIn('already in this team', plan['participants'][1]['warnings'][0])

        diff_team_same_name_no_ref = workbook_bytes(['Name', 'Student ID', 'Team'], [
            ['No Ref Learner', '', 'Team Beta'],
        ])
        parsed_diff = parse_training_roster(upload_workbook(diff_team_same_name_no_ref))
        plan_diff = plan_training_roster_import(self.series, parsed_diff,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIn('already in the series', plan_diff['participants'][0]['issues'][0])

    def test_apply_training_roster_integrity_error_handling(self):
        parsed = parse_training_roster(upload_workbook())
        with patch('service_domains.training_roster_import.TrainingParticipant.objects.bulk_create',
                   side_effect=IntegrityError('Duplicate simulated')):
            with self.assertRaises(ValidationError):
                apply_training_roster_import(self.series, parsed,
                    organization=self.org.as_claim(), user=self.owner)

    def test_import_and_preview_views_edge_cases_and_cancellation(self):
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            invalid_post = self.client.post(
                reverse('training_series_roster_import', args=(self.series.pk,)), {})
            self.assertEqual(invalid_post.status_code, 302)

            fatal_file = SimpleUploadedFile('corrupt.xlsx', b'garbage', content_type='application/octet-stream')
            fatal_post = self.client.post(
                reverse('training_series_roster_import', args=(self.series.pk,)), {'file': fatal_file})
            self.assertEqual(fatal_post.status_code, 302)

            with patch('service_domains.views.cache.set', side_effect=RuntimeError('Cache down')):
                cache_down = self.client.post(
                    reverse('training_series_roster_import', args=(self.series.pk,)),
                    {'file': upload_workbook()})
                self.assertEqual(cache_down.status_code, 302)

            preview_url = reverse('training_series_roster_preview', args=(self.series.pk,))
            no_token_preview = self.client.get(preview_url)
            self.assertEqual(no_token_preview.status_code, 302)

            self.client.post(reverse('training_series_roster_import', args=(self.series.pk,)),
                             {'file': upload_workbook()})
            cancel_resp = self.client.post(preview_url, {'action': 'cancel'})
            self.assertEqual(cancel_resp.status_code, 302)

            self.client.post(reverse('training_series_roster_import', args=(self.series.pk,)),
                             {'file': upload_workbook()})
            invalid_action = self.client.post(preview_url, {'action': 'unknown'})
            self.assertEqual(invalid_action.status_code, 404)

            self.client.post(reverse('training_series_roster_import', args=(self.series.pk,)),
                             {'file': upload_workbook()})
            with patch('service_domains.views.apply_training_roster_import',
                       side_effect=ValidationError('Simulated apply error')):
                err_resp = self.client.post(preview_url, {'action': 'apply'})
                self.assertEqual(err_resp.status_code, 400)
                self.assertContains(err_resp, 'Simulated apply error', status_code=400)

