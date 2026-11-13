from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from accounts.testing import make_org, make_user
from assessments.sheets import workbook_bytes
from service_domains.models import ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit
from service_domains.showcase_import import (
    MAX_PHYSICAL_ROWS, MAX_ROWS, MAX_UNCOMPRESSED_BYTES, MAX_UPLOAD_BYTES,
    TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows, apply_import, parse_exhibits,
    plan_import, sample_workbook,
)
from service_domains.operations import (
    MAX_SHOWCASE_JUDGE_ASSIGNMENTS, replace_showcase_judge_assignments,
    issue_showcase_judge_invitation, replace_showcase_invited_judge_assignments,
    showcase_judge_coverage,
)


class ShowcaseImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase import org')
        self.other_org = make_org('Other Showcase org')
        self.owner = make_user('showcase-import-owner')
        self.other = make_user('showcase-import-other')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Expo')
        self.category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Science', order=0)
        ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, category=self.category, name='Existing filter', order=0)

    def upload(self, headers, rows, filename='exhibits.xlsx'):
        workbook = workbook_bytes(headers, rows)
        return SimpleUploadedFile(filename, workbook.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_sample_and_valid_workbook_parse_and_preview_without_writes(self):
        parsed = parse_exhibits(sample_workbook())
        self.assertIsNone(parsed['fatal'])
        self.assertEqual(len(parsed['rows']), 2)
        self.assertEqual(parsed['rows'][0]['row'], 2)
        before = (ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count())
        plan = plan_import(self.event, parsed)
        self.assertTrue(plan['ok'])
        self.assertEqual(plan['categories_to_create'], ['Engineering', 'Community'])
        self.assertEqual(plan['exhibits_to_create'], 2)
        self.assertEqual((ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count()), before)

    def test_import_reuses_existing_categories_and_creates_each_new_category_once(self):
        parsed = parse_exhibits(self.upload(
            ['Category', 'Exhibit', 'Description'],
            [['Science', 'Seedling monitor', 'Uses soil moisture'],
             ['New category', 'Solar oven', 'Uses reflected light'],
             ['New category', 'Wind vane', 'Shows wind direction']]))
        plan = plan_import(self.event, parsed)
        self.assertTrue(plan['ok'])
        created = apply_import(self.event, parsed, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(created), 3)
        self.assertEqual(ShowcaseCategory.objects.filter(event=self.event).count(), 2)
        self.assertEqual(ShowcaseExhibit.objects.get(name='Seedling monitor').category, self.category)
        self.assertEqual(ShowcaseExhibit.objects.filter(category__name='New category').count(), 2)
        self.assertEqual([item.order for item in created], [1, 2, 3])

    def test_parse_reports_missing_names_lengths_and_duplicate_pairs_by_row(self):
        parsed = parse_exhibits(self.upload(
            ['Category', 'Exhibit', 'Description'],
            [['Science', '', 'Missing exhibit'],
             ['Science', 'Same exhibit', ''],
             ['Science', ' same EXHIBIT ', ''],
             ['x' * 101, 'Long category', ''],
             ['', 'Long description', 'd' * 2001]]))
        self.assertIsNone(parsed['fatal'])
        errors = '\n'.join(message for _, message in parsed['errors'])
        self.assertIn('Exhibit name is required', errors)
        self.assertIn('repeated in rows 3, 4', errors)
        self.assertIn('Category is longer than 100', errors)
        self.assertIn('Description is longer than 2000', errors)
        plan = plan_import(self.event, parsed)
        self.assertFalse(plan['ok'])

    def test_parse_rejects_unreadable_empty_missing_header_and_over_limit(self):
        unreadable = SimpleUploadedFile('wrong.xlsx', b'not an xlsx')
        self.assertIsNotNone(parse_exhibits(unreadable)['fatal'])
        empty = self.upload(['Category', 'Exhibit'], [])
        self.assertEqual(parse_exhibits(empty)['fatal'], 'The file has no exhibit rows.')
        missing = self.upload(['Category', 'Artifact'], [['Science', 'A']])
        self.assertIn('Exhibit column', parse_exhibits(missing)['fatal'])
        oversized = self.upload(['Exhibit'], [[f'Entry {number}'] for number in range(MAX_ROWS + 1)])
        self.assertIn('more than', parse_exhibits(oversized)['fatal'])
        empty_workbook = self.upload(['Category', 'Exhibit'], [], filename='empty.xlsx')
        self.assertEqual(parse_exhibits(empty_workbook)['fatal'], 'The file has no exhibit rows.')

    def test_empty_sheet_and_blank_rows_are_skipped(self):
        upload = self.upload(['Category', 'Exhibit'], [])
        with patch('service_domains.showcase_import._read_bounded_rows', return_value=[]):
            self.assertEqual(parse_exhibits(upload)['fatal'], 'The file is empty.')
        with patch('service_domains.showcase_import._read_bounded_rows', return_value=[['Category', 'Exhibit'], [' ', ' ']]):
            parsed = parse_exhibits(upload)
        self.assertEqual(parsed['fatal'], 'The file has no exhibit rows.')
        self.assertEqual(parsed['rows'], [])
        self.assertFalse(plan_import(self.event, {'fatal': 'bad file', 'rows': [], 'errors': []})['ok'])

    def test_bounded_reader_rejects_large_upload_zip_and_physical_row_counts(self):
        too_large = SimpleUploadedFile('large.xlsx', b'x' * (MAX_UPLOAD_BYTES + 1))
        self.assertIsNone(_read_bounded_rows(too_large))

        def archive_mock(infos):
            archive = MagicMock()
            archive.infolist.return_value = infos
            context = MagicMock()
            context.__enter__.return_value = archive
            return context

        upload = SimpleUploadedFile('fake.xlsx', b'x')
        with patch('service_domains.showcase_import.zipfile.ZipFile',
                   return_value=archive_mock([SimpleNamespace(file_size=MAX_UNCOMPRESSED_BYTES + 1)])):
            self.assertIsNone(_read_bounded_rows(upload))
        with patch('service_domains.showcase_import.zipfile.ZipFile',
                   return_value=archive_mock([SimpleNamespace(file_size=0)] * 1001)):
            self.assertIsNone(_read_bounded_rows(upload))

        worksheet = MagicMock()
        worksheet.max_row = MAX_PHYSICAL_ROWS + 1
        workbook = MagicMock()
        workbook.worksheets = [worksheet]
        with patch('service_domains.showcase_import.zipfile.ZipFile',
                   return_value=archive_mock([])), \
             patch('service_domains.showcase_import.openpyxl.load_workbook', return_value=workbook):
            self.assertIs(_read_bounded_rows(upload), TOO_MANY_PHYSICAL_ROWS)
        workbook.close.assert_called_once()
        with patch('service_domains.showcase_import._read_bounded_rows',
                   return_value=TOO_MANY_PHYSICAL_ROWS):
            self.assertIn('spreadsheet rows', parse_exhibits(upload)['fatal'])

    def test_fatal_and_stale_previews_never_write_partial_rows(self):
        malformed = parse_exhibits(self.upload(['Category', 'Exhibit'], [['Science', '']]))
        with self.assertRaises(ValidationError):
            apply_import(self.event, malformed, organization=self.org.as_claim(), user=self.owner)

        parsed = parse_exhibits(self.upload(['Category', 'Exhibit'], [['New', 'Fresh entry']]))
        self.assertTrue(plan_import(self.event, parsed)['ok'])
        category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='New')
        ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, category=category, name='Fresh entry')
        before = (ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count())
        with self.assertRaises(ValidationError):
            apply_import(self.event, parsed, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual((ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count()), before)

    def test_import_requires_owner_organization_and_draft_event(self):
        parsed = parse_exhibits(self.upload(['Category', 'Exhibit'], [['New', 'Fresh entry']]))
        with self.assertRaises(PermissionDenied):
            apply_import(self.event, parsed, organization=self.other_org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            apply_import(self.event, parsed, organization=self.org.as_claim(), user=self.other)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status',))
        with self.assertRaisesMessage(ValidationError, 'draft'):
            apply_import(self.event, parsed, organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(ShowcaseCategory.objects.filter(name='New').exists())
        self.assertFalse(ShowcaseExhibit.objects.filter(name='Fresh entry').exists())

    def test_judge_assignment_replace_is_owner_scoped_and_coverage_reports_gaps(self):
        second = ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, name='Wind turbine', order=1)
        first_exhibit = self.event.exhibits.first()
        first, _ = issue_showcase_judge_invitation(self.event, 'judge-a@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        second_judge, _ = issue_showcase_judge_invitation(self.event, 'judge-b@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        created = replace_showcase_invited_judge_assignments(self.event,
            {first.pk: [first_exhibit.pk, second.pk], second_judge.pk: [second.pk]},
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(created), 3)
        report = showcase_judge_coverage(self.event, minimum_judges=2)
        self.assertEqual(report['exhibit_count'], 2)
        self.assertEqual(report['covered_exhibit_count'], 1)
        self.assertEqual(report['uncovered_exhibit_count'], 1)
        self.assertEqual(report['missing_assignments'], 1)
        self.assertEqual([row['assigned_count'] for row in report['exhibits']], [1, 2])

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status',))
        third, _ = issue_showcase_judge_invitation(self.event, 'judge-c@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {first.pk: [first_exhibit.pk, second.pk], second_judge.pk: [second.pk],
             third.pk: [first_exhibit.pk]},
            organization=self.org.as_claim(), user=self.owner)
        report = showcase_judge_coverage(self.event, minimum_judges=2)
        self.assertEqual(report['covered_exhibit_count'], 2)
        self.assertEqual(report['uncovered_exhibit_count'], 0)
        self.assertEqual(report['missing_assignments'], 0)

    def test_judge_assignment_rejects_wrong_scope_invalid_inputs_and_closed_event(self):
        exhibit = self.event.exhibits.first()
        with self.assertRaises(PermissionDenied):
            replace_showcase_judge_assignments(self.event, {}, organization=self.other_org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            replace_showcase_judge_assignments(self.event, {}, organization=self.org.as_claim(), user=self.other)
        for invalid in (None, []):
            with self.assertRaises(ValidationError):
                replace_showcase_judge_assignments(self.event, invalid,
                    organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            replace_showcase_judge_assignments(self.event, {999999: ['Judge']},
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            replace_showcase_judge_assignments(self.event, {True: ['Judge']},
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            replace_showcase_judge_assignments(self.event, {exhibit.pk: 'Judge'},
                organization=self.org.as_claim(), user=self.owner)
        for labels in ([None], ['  '], ['x' * 161]):
            with self.assertRaises(ValidationError):
                replace_showcase_judge_assignments(self.event, {exhibit.pk: labels},
                    organization=self.org.as_claim(), user=self.owner)
        too_many = [f'Judge {index}' for index in range(MAX_SHOWCASE_JUDGE_ASSIGNMENTS + 1)]
        with self.assertRaisesMessage(ValidationError, 'cannot have more than'):
            replace_showcase_judge_assignments(self.event, {exhibit.pk: too_many},
                organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.CLOSED
        self.event.save(update_fields=('status',))
        with self.assertRaisesMessage(ValidationError, 'locked'):
            replace_showcase_judge_assignments(self.event, {},
                organization=self.org.as_claim(), user=self.owner)

    def test_judge_coverage_requires_threshold_from_one_to_fifty(self):
        for threshold in (-1, 51, True, '1'):
            with self.assertRaises(ValidationError):
                showcase_judge_coverage(self.event, minimum_judges=threshold)

    def test_judge_assignment_model_rejects_cross_event_blank_label_and_closed_event(self):
        from service_domains.models import ShowcaseJudgeAssignment

        other_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other Expo')
        other_exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=other_event, name='Other entry')
        assignment = ShowcaseJudgeAssignment(organization_id=self.org.id, created_by=self.owner,
            event=self.event, exhibit=other_exhibit, judge_label='Judge A')
        with self.assertRaises(ValidationError):
            assignment.full_clean()

        assignment.exhibit = self.event.exhibits.first()
        assignment.judge_label = ' '
        with self.assertRaises(ValidationError):
            assignment.full_clean()

        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status',))
        assignment.judge_label = 'Judge A'
        with self.assertRaises(ValidationError):
            assignment.full_clean()

    def test_judge_assignment_rejects_non_numeric_exhibit_keys(self):
        for key_value in (None, 'not-an-id'):
            with self.assertRaises(ValidationError):
                replace_showcase_judge_assignments(self.event, {key_value: ['Judge']},
                    organization=self.org.as_claim(), user=self.owner)
