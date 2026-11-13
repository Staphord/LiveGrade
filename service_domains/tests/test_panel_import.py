import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import zipfile

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.models import PanelAuditEvent, PanelCandidate, PanelHearing
from service_domains.panel_import import (
    MAX_PHYSICAL_ROWS, MAX_ROWS, MAX_UNCOMPRESSED_BYTES, MAX_UPLOAD_BYTES,
    TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows, apply_candidate_import,
    parse_candidates, plan_candidates, sample_workbook,
)
from service_domains.views import PANEL_CANDIDATE_IMPORT_TOKEN_SALT


class PanelCandidateImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel candidate import org')
        self.other_org = make_org('Other Panel import org')
        self.owner = make_user('panel-import-owner')
        self.other = make_user('panel-import-other')
        self.hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Candidate import hearing')
        PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, display_name='Existing candidate', reference='EX-1', order=0)

    def upload(self, headers, rows, filename='candidates.xlsx'):
        workbook = workbook_bytes(headers, rows)
        return SimpleUploadedFile(filename, workbook.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_sample_and_valid_workbook_preview_without_writes_then_apply_in_file_order(self):
        parsed = parse_candidates(sample_workbook())
        self.assertIsNone(parsed['fatal'])
        self.assertEqual(len(parsed['rows']), 2)
        before = PanelCandidate.objects.count()
        plan = plan_candidates(self.hearing, parsed)
        self.assertTrue(plan['ok'])
        self.assertEqual(plan['candidates_to_create'], 2)
        self.assertEqual(PanelCandidate.objects.count(), before)
        created = apply_candidate_import(self.hearing, parsed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual([row.display_name for row in created],
            ['Example candidate', 'Another candidate'])
        self.assertEqual([row.order for row in created], [1, 2])
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.CANDIDATE_ADDED).count(), 2)

    def test_parser_reports_required_lengths_formula_and_duplicates_by_row(self):
        parsed = parse_candidates(self.upload(['Candidate', 'Reference'], [
            ['  ', 'REF-1'], ['Same name', 'DUP'], [' same NAME ', 'DUP'],
            ['x' * 161, 'R-2'], ['Formula', '=1+2'],
        ]))
        self.assertIsNone(parsed['fatal'])
        messages = '\n'.join(message for _, message in parsed['errors'])
        self.assertIn('Candidate name is required', messages)
        self.assertIn('name is repeated in rows 3, 4', messages)
        self.assertIn('reference is repeated in rows 3, 4', messages)
        self.assertIn('Candidate is longer than 160', messages)
        self.assertIn('Formula cells are not accepted', messages)

    def test_parser_rejects_unreadable_empty_missing_header_and_row_limit(self):
        unreadable = SimpleUploadedFile('bad.xlsx', b'not a workbook')
        self.assertIsNotNone(parse_candidates(unreadable)['fatal'])
        self.assertEqual(parse_candidates(self.upload(['Candidate'], []))['fatal'],
            'The file has no candidate rows.')
        missing = parse_candidates(self.upload(['Entrant id'], [['E-1']]))
        self.assertIn('Candidate column', missing['fatal'])
        oversized = parse_candidates(self.upload(['Candidate'],
            [[f'Candidate {number}'] for number in range(MAX_ROWS + 1)]))
        self.assertIn('more than', oversized['fatal'])
        with patch('service_domains.panel_import._read_bounded_rows', return_value=[]):
            self.assertEqual(parse_candidates(self.upload(['Candidate'], []))['fatal'],
                'The file is empty.')
        with patch('service_domains.panel_import._read_bounded_rows',
                   return_value=[['Candidate'], ['  ']]):
            self.assertEqual(parse_candidates(self.upload(['Candidate'], []))['fatal'],
                'The file has no candidate rows.')
        with patch('service_domains.panel_import._read_bounded_rows',
                   return_value=TOO_MANY_PHYSICAL_ROWS):
            self.assertIn('more than', parse_candidates(self.upload(['Candidate'], []))['fatal'])

    def test_bounded_reader_rejects_size_zip_limits_and_physical_rows(self):
        too_large = SimpleUploadedFile('large.xlsx', b'x' * (MAX_UPLOAD_BYTES + 1))
        self.assertIsNone(_read_bounded_rows(too_large))

        def archive_mock(infos):
            archive = MagicMock()
            archive.infolist.return_value = infos
            context = MagicMock()
            context.__enter__.return_value = archive
            return context

        upload = SimpleUploadedFile('fake.xlsx', b'x')
        with patch('service_domains.panel_import.zipfile.ZipFile',
                   return_value=archive_mock([SimpleNamespace(file_size=MAX_UNCOMPRESSED_BYTES + 1)])):
            self.assertIsNone(_read_bounded_rows(upload))
        with patch('service_domains.panel_import.zipfile.ZipFile',
                   return_value=archive_mock([SimpleNamespace(file_size=0)] * 1001)):
            self.assertIsNone(_read_bounded_rows(upload))
        worksheet = MagicMock()
        worksheet.max_row = MAX_PHYSICAL_ROWS + 1
        workbook = MagicMock()
        workbook.worksheets = [worksheet]
        with patch('service_domains.panel_import.zipfile.ZipFile',
                   return_value=archive_mock([])), \
             patch('service_domains.panel_import.openpyxl.load_workbook', return_value=workbook):
            self.assertIs(_read_bounded_rows(upload), TOO_MANY_PHYSICAL_ROWS)

    def test_preview_detects_existing_duplicate_names_and_references(self):
        parsed = parse_candidates(self.upload(['Candidate', 'Reference'], [
            ['Existing candidate', 'NEW'], ['New candidate', 'EX-1'],
        ]))
        plan = plan_candidates(self.hearing, parsed)
        self.assertFalse(plan['ok'])
        errors = '\n'.join(message for _, message in plan['errors'])
        self.assertIn('name already exists', errors)
        self.assertIn('reference already exists', errors)

    def test_apply_rechecks_scope_status_and_invalid_preview_atomically(self):
        parsed = parse_candidates(self.upload(['Candidate'], [['New candidate']]))
        before = PanelCandidate.objects.count()
        with self.assertRaises(PermissionDenied):
            apply_candidate_import(self.hearing, parsed,
                organization=self.other_org.as_claim(), user=self.owner)
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.save(update_fields=('status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'only be imported while'):
            apply_candidate_import(self.hearing, parsed,
                organization=self.org.as_claim(), user=self.owner)
        self.hearing.status = PanelHearing.Status.DRAFT
        self.hearing.save(update_fields=('status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'has errors'):
            apply_candidate_import(self.hearing, {'fatal': 'invalid', 'rows': [], 'errors': []},
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(PanelCandidate.objects.count(), before)

    def test_apply_replans_stale_preview_before_any_insert(self):
        parsed = parse_candidates(self.upload(['Candidate'], [['New candidate']]))
        self.assertTrue(plan_candidates(self.hearing, parsed)['ok'])
        PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, display_name='New candidate')
        before = PanelCandidate.objects.count()
        with self.assertRaisesMessage(ValidationError, 'has errors'):
            apply_candidate_import(self.hearing, parsed,
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(PanelCandidate.objects.count(), before)


class PanelCandidateImportViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel import view org')
        self.owner = make_user('panel-import-view-owner')
        sign_in(self.client, self.owner, self.org)
        self.hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Preview candidates')
        self.url = reverse('panel_candidate_import', args=(self.hearing.pk,))

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def test_upload_preview_signed_apply_and_sample_download(self):
        workbook = workbook_bytes(['Candidate', 'Reference'], [['Imported', 'I-1']])
        upload = SimpleUploadedFile('candidates.xlsx', workbook.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response = self.request('post', self.url, {'file': upload})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1 candidate ready.')
        token = re.search(rb'name="preview_token" value="([^"]+)"', response.content).group(1).decode()
        applied = self.request('post', self.url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(applied.status_code, 302)
        self.assertTrue(PanelCandidate.objects.filter(hearing=self.hearing,
            display_name='Imported', reference='I-1').exists())
        sample = self.request('get', reverse('panel_candidate_import_sample', args=(self.hearing.pk,)))
        self.assertEqual(sample.status_code, 200)
        self.assertEqual(sample['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_invalid_upload_expired_preview_and_foreign_scope_fail_safely(self):
        invalid_upload = SimpleUploadedFile('bad.csv', b'candidate')
        response = self.request('post', self.url, {'file': invalid_upload})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, '.xlsx', status_code=400)
        expired = self.request('post', self.url, {'action': 'apply', 'preview_token': 'invalid'})
        self.assertEqual(expired.status_code, 400)
        sign_in(self.client, make_user('other-panel-import-user'), self.org)
        forbidden = self.request('get', self.url)
        self.assertEqual(forbidden.status_code, 404)

    def test_upload_size_preview_errors_and_invalid_apply_payloads(self):
        oversized = SimpleUploadedFile('large.xlsx', b'x' * (MAX_UPLOAD_BYTES + 1))
        too_large = self.request('post', self.url, {'file': oversized})
        self.assertEqual(too_large.status_code, 400)
        self.assertContains(too_large, '10 MB or smaller', status_code=400)

        workbook = workbook_bytes(['Candidate'], [['Already exists']])
        PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, display_name='Already exists')
        duplicate_upload = SimpleUploadedFile('duplicate.xlsx', workbook.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        invalid_preview = self.request('post', self.url, {'file': duplicate_upload})
        self.assertEqual(invalid_preview.status_code, 200)
        self.assertContains(invalid_preview, 'Nothing has been added')
        self.assertNotContains(invalid_preview, 'name="preview_token"')

        missing_token = self.request('post', self.url, {'action': 'apply'})
        self.assertEqual(missing_token.status_code, 400)
        oversized_token = self.request('post', self.url,
            {'action': 'apply', 'preview_token': 'x' * 150_001})
        self.assertEqual(oversized_token.status_code, 404)

    def test_signed_preview_identity_and_apply_failures_are_denied_or_reported(self):
        base = {'hearing': self.hearing.pk, 'organization': self.org.id,
                'user': self.owner.pk, 'parsed': {'fatal': None, 'rows': [], 'errors': []}}
        wrong_owner = signing.dumps({**base, 'user': 999999},
            salt=PANEL_CANDIDATE_IMPORT_TOKEN_SALT)
        self.assertEqual(self.request('post', self.url, {
            'action': 'apply', 'preview_token': wrong_owner}).status_code, 404)

        token = signing.dumps(base, salt=PANEL_CANDIDATE_IMPORT_TOKEN_SALT)
        with patch('service_domains.views.apply_candidate_import',
                   side_effect=PermissionDenied):
            denied = self.request('post', self.url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(denied.status_code, 404)
        with patch('service_domains.views.apply_candidate_import',
                   side_effect=ValidationError('stale import')):
            stale = self.request('post', self.url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(stale.status_code, 400)
        self.assertContains(stale, 'stale import', status_code=400)
