"""Parse and preview bounded Panel examiner-email spreadsheets."""
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponse
from django.utils import timezone

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .panel_import import MAX_ROWS, _read_bounded_rows, TOO_MANY_PHYSICAL_ROWS
from .models import PanelInvitation


HEADERS = {'email': {'email', 'examiner email', 'email address', 'examiner'}}


def parse_examiner_emails(uploaded_file):
    result = {'fatal': None, 'rows': [], 'errors': []}
    if getattr(uploaded_file, 'size', 0) > 10 * 1024 * 1024:
        result['fatal'] = 'The spreadsheet must be 10 MB or smaller.'
        return result
    rows = _read_bounded_rows(uploaded_file)
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    if rows is TOO_MANY_PHYSICAL_ROWS:
        result['fatal'] = 'The worksheet exceeds the physical row limit.'
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result
    if len(rows) - 1 > MAX_ROWS:
        result['fatal'] = f'That file has more than {MAX_ROWS} examiner rows. Split it and import in parts.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'email' not in mapping:
        result['fatal'] = 'Could not find the Email column. The first row must include an Email header.'
        return result
    seen = defaultdict(list)
    for number, source in enumerate(rows[1:], start=2):
        email = clean(cell(source, mapping, 'email'))
        if not email:
            continue
        item = {'row': number, 'email': email, 'errors': []}
        if len(email) > 254:
            item['errors'].append('Email address is longer than 254 characters.')
        else:
            try:
                validate_email(email)
            except ValidationError:
                item['errors'].append('Enter a valid examiner email address.')
        result['rows'].append(item)
        seen[key(email)].append(item)
    for duplicates in seen.values():
        if len(duplicates) > 1:
            numbers = ', '.join(str(item['row']) for item in duplicates)
            message = f'This examiner email is repeated in rows {numbers}.'
            for item in duplicates:
                item['errors'].append(message)
    result['errors'] = [(item['row'], message) for item in result['rows']
        for message in item['errors']]
    if not result['rows'] and not result['errors']:
        result['fatal'] = 'The file has no examiner email rows.'
    return result


def plan_examiner_import(hearing, parsed):
    if parsed.get('fatal'):
        return {'rows': [], 'errors': [(None, parsed['fatal'])], 'invitations_to_create': 0,
                'ok': False}
    active_emails = {key(email) for email in PanelInvitation.objects.filter(hearing=hearing,
        revoked_at__isnull=True, declined_at__isnull=True, accepted_at__isnull=True,
        expires_at__gt=timezone.now()
        ).values_list('email', flat=True)}
    planned = []
    errors = list(parsed['errors'])
    for source in parsed['rows']:
        row = {**source, 'errors': list(source['errors'])}
        if key(row['email']) in active_emails:
            row['errors'].append('An active invitation already exists for this email address.')
        planned.append(row)
    errors.extend((row['row'], message) for row in planned for message in row['errors']
        if (row['row'], message) not in errors)
    count = sum(not row['errors'] for row in planned)
    return {'rows': planned, 'errors': errors, 'invitations_to_create': count, 'ok': not errors}


def sample_workbook():
    return workbook_bytes(['Email'], [['examiner@example.org']], {'A': 36})
