"""Regression checks for bounded public inputs and recovered Team live access."""
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch

import openpyxl
from django.test import SimpleTestCase

from assessments.sheets import read_rows


class BoundedWorkbookTests(SimpleTestCase):
    def test_rejects_archive_before_xml_parsing(self):
        stream = BytesIO()
        with ZipFile(stream, 'w') as archive:
            for number in range(1001):
                archive.writestr(str(number), '')
        stream.seek(0)
        with patch('assessments.sheets.openpyxl.load_workbook') as load:
            self.assertIsNone(read_rows(stream))
        load.assert_not_called()

    def test_rejects_sparse_excessive_dimensions(self):
        for address in ('A10001', 'CW1'):
            book = openpyxl.Workbook()
            book.active[address] = 'oversized'
            stream = BytesIO()
            book.save(stream)
            stream.seek(0)
            self.assertIsNone(read_rows(stream))
