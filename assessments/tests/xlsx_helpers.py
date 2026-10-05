"""Small helpers for tests that upload spreadsheets."""
from io import BytesIO

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

GROUP_HEADERS = ['Group Name', 'Name', 'Student ID', 'Topic', 'Presentation Location']


def xlsx_buffer(*rows, percent_columns=()):
    """An in-memory workbook holding ``rows`` (the first is the header)."""
    book = openpyxl.Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(list(row))
    for column in percent_columns:
        for cell in sheet[column][1:]:
            cell.number_format = '0%'
    buffer = BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


def xlsx_upload(*rows, name='groups.xlsx', **kwargs):
    return SimpleUploadedFile(name, xlsx_buffer(*rows, **kwargs).read(), content_type=XLSX)
