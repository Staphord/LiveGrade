"""Spreadsheet import edge cases: odd cells, wide sheets, and entries added too late."""
import io
from datetime import time

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from accounts.testing import make_org, make_user
from assessments import poll_import
from assessments.sheets import workbook_bytes
from service_domains.competition_import import apply_competition_entry_import
from service_domains.models import Competition
from service_domains.panel_import import parse_candidates


def upload(headers, rows):
    return workbook_bytes(headers, rows)


class PanelCandidateCellTests(SimpleTestCase):
    def test_time_cells_and_bad_times_are_read(self):
        result = parse_candidates(upload(
            ['Candidate', 'Reference', 'Scheduled time'],
            [['Ada', 'A1', time(9, 30)], ['Bo', 'B1', 'nine thirty'], ['Cy', 'C1', '10:15']]))
        ada, bo, cy = result['rows']
        self.assertEqual(ada['scheduled_time'], time(9, 30))
        self.assertIsNone(bo['scheduled_time'])
        self.assertTrue(any('valid time' in message for message in bo['errors']))
        self.assertEqual(cy['scheduled_time'], time(10, 15))

    def test_sheets_that_are_too_wide_are_unreadable(self):
        headers = ['Candidate'] + [f'Extra {n}' for n in range(101)]
        result = parse_candidates(upload(headers, [['Ada'] + [''] * 101]))
        self.assertTrue(result['fatal'])


class PollQuestionCellTests(SimpleTestCase):
    def test_unbalanced_quotes_in_options_do_not_crash(self):
        parsed = poll_import.parse_questions(upload(
            ['Question', 'Question Type', 'Answer Options'],
            [['Where?', 'Single choice', '"unterminated, Hall']]))
        self.assertIn('questions', parsed)


class LateEntryImportTests(TestCase):
    def test_entries_cannot_be_imported_once_the_competition_has_started(self):
        org, owner = make_org('Late import org'), make_user('late-import-owner')
        competition = Competition.objects.create(
            organization_id=org.pk, created_by=owner, name='Started', status=Competition.Status.ACTIVE)
        with self.assertRaises(ValidationError):
            apply_competition_entry_import(competition, {'rows': []}, organization=org.as_claim(), user=owner)
