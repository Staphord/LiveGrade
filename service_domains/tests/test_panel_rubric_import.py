"""Unit and edge-case verification for panel rubric spreadsheet import."""
import io
import openpyxl
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from accounts.testing import make_org, make_user
from service_domains.models import PanelHearing, PanelCriterion
from service_domains.panel_rubric_import import (
    sample_workbook, parse_rubric, plan_rubric, apply_rubric_import, MAX_LEVEL_TEXT
)


class PanelRubricImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel Org')
        self.user = make_user('panel-chair')
        self.other_user = make_user('other-chair')
        self.hearing = PanelHearing.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            name='Senior Defense',
            status=PanelHearing.Status.DRAFT
        )

    def _build_xlsx(self, headers, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(headers)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    def test_sample_workbook_generation(self):
        raw_io = sample_workbook()
        self.assertGreater(raw_io.getbuffer().nbytes, 0)
        parsed = parse_rubric(raw_io)
        self.assertIsNone(parsed['fatal'])
        self.assertEqual(len(parsed['rows']), 2)

    def test_parse_rubric_fatal_errors(self):
        # Empty file
        empty_buf = io.BytesIO()
        res = parse_rubric(empty_buf)
        self.assertIsNotNone(res['fatal'])

        # Missing criterion header
        buf = self._build_xlsx(['Description', 'Weight'], [['Test', 10]])
        res = parse_rubric(buf)
        self.assertIn('Could not find the Criterion column', res['fatal'])

        # Exceeds max physical rows
        buf = self._build_xlsx(['Criterion'], [['Row %d' % i] for i in range(505)])
        res = parse_rubric(buf)
        self.assertIn('That file has more than', res['fatal'])

        # File exceeding 10 MB limit
        large_file = io.BytesIO(b'x')
        large_file.size = 11 * 1024 * 1024
        res = parse_rubric(large_file)
        self.assertIn('10 MB or smaller', res['fatal'])

        # Only header with no criteria rows
        buf = self._build_xlsx(['Criterion', 'Description'], [])
        res = parse_rubric(buf)
        self.assertIn('The file has no criteria rows', res['fatal'])

        # Mock physical rows exceeded and empty rows
        from unittest.mock import patch
        from service_domains.panel_import import TOO_MANY_PHYSICAL_ROWS
        with patch('service_domains.panel_rubric_import._read_bounded_rows', return_value=TOO_MANY_PHYSICAL_ROWS):
            res = parse_rubric(io.BytesIO(b'dummy'))
            self.assertIn('physical row limit', res['fatal'])

        with patch('service_domains.panel_rubric_import._read_bounded_rows', return_value=[]):
            res = parse_rubric(io.BytesIO(b'dummy'))
            self.assertIn('The file is empty', res['fatal'])

        # Data with empty row where only weight is present (so name/desc/levels empty, triggering line 63)
        buf = self._build_xlsx(['Criterion', 'Description', 'Weight'], [['', '', '50'], ['Crit1', 'Desc', '10']])
        res = parse_rubric(buf)
        self.assertEqual(len(res['rows']), 1)

        # All empty data rows
        buf = self._build_xlsx(['Criterion', 'Description'], [['', ''], [None, None]])
        res = parse_rubric(buf)
        self.assertIn('The file has no criteria rows', res['fatal'])

        # Plan with fatal error
        res_plan = plan_rubric(self.hearing, {'fatal': 'Some fatal error'})
        self.assertFalse(res_plan['ok'])

        # Applying a plan with errors
        org_dict = {'id': self.org.pk, 'name': self.org.name}
        with self.assertRaises(ValidationError):
            apply_rubric_import(self.hearing, {'fatal': 'Some error'}, organization=org_dict, user=self.user)

    def test_parse_rubric_row_errors(self):
        # Duplicate names, formula cell, invalid weight, excessive length
        rows = [
            ['Valid Name', 'Valid Desc', '0 | Bad\n100 | Good', '50.50'],
            ['Valid Name', 'Duplicate', '', '10'],
            ['=HYPERLINK("bad")', 'Formula', '', '10'],
            ['Invalid Weight', 'Desc', '', '9999'],
            ['', 'Missing name', '', '10'],
            ['Long Name ' * 15, 'Desc', '', '10'],
            ['Desc Too Long', 'x' * 1050, '', '10'],
            ['Guidance Too Long', 'Desc', 'x' * (MAX_LEVEL_TEXT + 10), '10'],
        ]
        buf = self._build_xlsx(['Criterion', 'Description', 'Score guidance', 'Weight'], rows)
        res = parse_rubric(buf)
        self.assertGreater(len(res['errors']), 0)
        error_messages = [msg for _, msg in res['errors']]
        self.assertTrue(any('repeated in rows' in m for m in error_messages))
        self.assertTrue(any('Formula cells are not accepted' in m for m in error_messages))
        self.assertTrue(any('Weight must be a number' in m for m in error_messages))
        self.assertTrue(any('Criterion name is required' in m for m in error_messages))

    def test_plan_and_apply_rubric_import(self):
        rows = [
            ['Methodology', 'Sound experimental design', '0 | Flawed\n100 | Robust', '40'],
            ['Delivery', 'Clarity and conciseness', '', '60'],
        ]
        buf = self._build_xlsx(['Criterion', 'Description', 'Score guidance', 'Weight'], rows)
        parsed = parse_rubric(buf)
        self.assertEqual(len(parsed['errors']), 0)

        plan = plan_rubric(self.hearing, parsed)
        self.assertTrue(plan['ok'])
        self.assertEqual(plan['criteria_to_create'], 2)

        org_dict = {'id': self.org.pk, 'name': self.org.name}
        # Apply import
        created = apply_rubric_import(self.hearing, parsed, organization=org_dict, user=self.user)
        self.assertEqual(len(created), 2)
        self.assertEqual(self.hearing.criteria.count(), 2)
        methodology = self.hearing.criteria.get(name='Methodology')
        self.assertEqual(methodology.weight, Decimal('40'))
        self.assertEqual(methodology.levels.count(), 2)

        # Duplicate collision on re-plan
        plan2 = plan_rubric(self.hearing, parsed)
        self.assertFalse(plan2['ok'])
        self.assertTrue(any('already exists in the hearing' in msg for _, msg in plan2['errors']))

        # Apply error on non-draft status
        self.hearing.status = PanelHearing.Status.OPEN
        self.hearing.save()
        with self.assertRaises(ValidationError):
            apply_rubric_import(self.hearing, parsed, organization=org_dict, user=self.user)

        # Apply error on unauthorized user
        with self.assertRaises(PermissionDenied):
            apply_rubric_import(self.hearing, parsed, organization=org_dict, user=self.other_user)
