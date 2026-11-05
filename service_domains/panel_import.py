"""Preview and apply bounded Panel candidate spreadsheets."""
from collections import defaultdict
from datetime import time
import zipfile

import openpyxl
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import PanelAuditEvent, PanelCandidate, PanelHearing


MAX_ROWS = 500
MAX_PHYSICAL_ROWS = 2000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
TOO_MANY_PHYSICAL_ROWS = object()
LIMITS = {'candidate': 160, 'reference': 80}
HEADERS = {
    'candidate': {'candidate', 'candidate name', 'entrant', 'entrant name', 'name'},
    'reference': {'reference', 'candidate id', 'candidate reference', 'number'},
    'scheduled_time': {'scheduled time', 'presentation time', 'time'},
}


def sample_workbook():
    return workbook_bytes(['Candidate', 'Reference', 'Scheduled time'],
        [['Example candidate', 'C-001', '09:00'], ['Another candidate', 'C-002', '09:15']],
        {'A': 32, 'B': 22, 'C': 20})


def _read_bounded_rows(uploaded_file):
    if getattr(uploaded_file, 'size', 0) > MAX_UPLOAD_BYTES:
        return None
    try:
        with zipfile.ZipFile(uploaded_file) as archive:
            files = archive.infolist()
            if len(files) > 1000 or sum(item.file_size for item in files) > MAX_UNCOMPRESSED_BYTES:
                return None
        uploaded_file.seek(0)
        workbook = openpyxl.load_workbook(uploaded_file, read_only=True, data_only=False)
        try:
            worksheet = workbook.worksheets[0]
            if (worksheet.max_row or 0) > MAX_PHYSICAL_ROWS:
                return TOO_MANY_PHYSICAL_ROWS
            return [[cell.value for cell in row]
                    for row in worksheet.iter_rows(max_row=MAX_PHYSICAL_ROWS)]
        finally:
            workbook.close()
    except Exception:
        return None


def parse_candidates(uploaded_file):
    result = {'fatal': None, 'rows': [], 'errors': []}
    rows = _read_bounded_rows(uploaded_file)
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    if rows is TOO_MANY_PHYSICAL_ROWS:
        result['fatal'] = f'That file has more than {MAX_PHYSICAL_ROWS} spreadsheet rows. Remove blank rows and try again.'
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result
    if len(rows) - 1 > MAX_ROWS:
        result['fatal'] = f'That file has more than {MAX_ROWS} candidate rows. Split it and import in parts.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'candidate' not in mapping:
        result['fatal'] = 'Could not find the Candidate column. The first row must include a Candidate header.'
        return result

    seen = defaultdict(list)
    seen_references = defaultdict(list)
    for number, source in enumerate(rows[1:], start=2):
        candidate = clean(cell(source, mapping, 'candidate'))
        reference = clean(cell(source, mapping, 'reference'))
        raw_time = cell(source, mapping, 'scheduled_time')
        scheduled_time = None
        if isinstance(raw_time, time):
            scheduled_time = raw_time.replace(tzinfo=None)
        elif raw_time not in (None, ''):
            try:
                scheduled_time = time.fromisoformat(str(raw_time).strip())
            except (TypeError, ValueError):
                scheduled_time = None
        if not candidate and not reference and raw_time in (None, ''):
            continue
        row = {'row': number, 'display_name': candidate, 'reference': reference,
               'scheduled_time': scheduled_time, 'errors': []}
        if not candidate:
            row['errors'].append('Candidate name is required.')
        if raw_time not in (None, '') and scheduled_time is None:
            row['errors'].append('Scheduled time must be a valid time such as 09:30.')
        for field, value in (('candidate', candidate), ('reference', reference)):
            if len(value) > LIMITS[field]:
                row['errors'].append(f'{field.title()} is longer than {LIMITS[field]} characters.')
        if candidate.startswith('=') or reference.startswith('='):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        result['rows'].append(row)
        if candidate:
            seen[key(candidate)].append(row)
        if reference:
            seen_references[key(reference)].append(row)

    for duplicates in seen.values():
        if len(duplicates) > 1:
            numbers = ', '.join(str(row['row']) for row in duplicates)
            message = f'This candidate name is repeated in rows {numbers}.'
            for row in duplicates:
                row['errors'].append(message)
    for duplicates in seen_references.values():
        if len(duplicates) > 1:
            numbers = ', '.join(str(row['row']) for row in duplicates)
            message = f'This candidate reference is repeated in rows {numbers}.'
            for row in duplicates:
                row['errors'].append(message)
    result['errors'] = [(row['row'], message) for row in result['rows'] for message in row['errors']]
    if not result['rows'] and not result['errors']:
        result['fatal'] = 'The file has no candidate rows.'
    return result


def plan_candidates(hearing, parsed):
    if parsed.get('fatal'):
        return {'rows': [], 'errors': [(None, parsed['fatal'])], 'candidates_to_create': 0, 'ok': False}
    names = {key(item.display_name) for item in hearing.candidates.all()}
    references = {key(item.reference) for item in hearing.candidates.exclude(reference='')}
    rows = []
    errors = list(parsed['errors'])
    for source in parsed['rows']:
        row = {**source, 'errors': list(source['errors'])}
        name_key = key(row['display_name'])
        reference_key = key(row['reference'])
        if row['display_name'] and name_key in names:
            row['errors'].append('A candidate with this name already exists in the hearing.')
        if row['reference'] and reference_key in references:
            row['errors'].append('A candidate with this reference already exists in the hearing.')
        rows.append(row)
    errors.extend((row['row'], message) for row in rows for message in row['errors']
        if (row['row'], message) not in errors)
    count = sum(bool(row['display_name']) and not row['errors'] for row in rows)
    return {'rows': rows, 'errors': errors, 'candidates_to_create': count, 'ok': not errors}


@transaction.atomic
def apply_candidate_import(hearing, parsed, *, organization, user):
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.DRAFT:
        raise ValidationError('Candidates can only be imported while the hearing is a draft.')
    plan = plan_candidates(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The candidate import has errors. Review the preview before applying it.')
    order = locked.candidates.aggregate(Max('order'))['order__max']
    order = order + 1 if order is not None else 0
    created = []
    for row in plan['rows']:
        candidate = PanelCandidate(hearing=locked, display_name=row['display_name'],
            reference=row['reference'], order=order, scheduled_time=row['scheduled_time'],
            organization_id=locked.organization_id, created_by=locked.created_by)
        candidate.full_clean()
        candidate.save(force_insert=True)
        event = PanelAuditEvent(hearing=locked, candidate=candidate,
            action=PanelAuditEvent.Action.CANDIDATE_ADDED,
            reason='Candidate added from a reviewed spreadsheet import.', actor_user=user,
            organization_id=locked.organization_id, created_by=user)
        event.full_clean()
        event.save(force_insert=True)
        created.append(candidate)
        order += 1
    return created
