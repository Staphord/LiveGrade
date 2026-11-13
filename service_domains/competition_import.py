"""Preview and apply Competition track and entry spreadsheets."""
from collections import defaultdict
import zipfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
import openpyxl

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import Competition, CompetitionEntry, CompetitionTrack


MAX_ROWS = 500
MAX_PHYSICAL_ROWS = 2000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
TOO_MANY_PHYSICAL_ROWS = object()
LIMITS = {'track': 100, 'entry': 160, 'reference': 80}

HEADERS = {
    'track': {'track', 'track name', 'category', 'division'},
    'entry': {'entry', 'entry name', 'name', 'project', 'team'},
    'reference': {'reference', 'ref', 'code', 'entry id'},
}

SAMPLE_HEADERS = ['Track', 'Entry', 'Reference']
SAMPLE_ROWS = [
    ['Open Track', 'Solar Microgrid', 'ENT-01'],
    ['Open Track', 'Clean Water Sensor', 'ENT-02'],
]


def sample_competition_entries():
    return workbook_bytes(SAMPLE_HEADERS, SAMPLE_ROWS, {'A': 24, 'B': 32, 'C': 20})


def _read_bounded_rows(uploaded_file):
    """Read a bounded XLSX file without trusting archive metadata."""
    if getattr(uploaded_file, 'size', 0) > MAX_UPLOAD_BYTES:
        return None
    try:
        with zipfile.ZipFile(uploaded_file) as archive:
            files = archive.infolist()
            if len(files) > 1000 or sum(item.file_size for item in files) > MAX_UNCOMPRESSED_BYTES:
                return None
        uploaded_file.seek(0)
        workbook = openpyxl.load_workbook(uploaded_file, read_only=True, data_only=True)
        try:
            worksheet = workbook.worksheets[0]
            if (worksheet.max_row or 0) > MAX_PHYSICAL_ROWS:
                return TOO_MANY_PHYSICAL_ROWS
            return [[item.value for item in row]
                    for row in worksheet.iter_rows(max_row=MAX_PHYSICAL_ROWS)]
        finally:
            workbook.close()
    except Exception:
        return None


def parse_competition_entries(uploaded_file):
    """Return normalized spreadsheet rows and row-numbered errors without writes."""
    result = {'fatal': None, 'rows': [], 'errors': []}
    rows = _read_bounded_rows(uploaded_file)
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    if rows is TOO_MANY_PHYSICAL_ROWS:
        result['fatal'] = (f'That file has more than {MAX_PHYSICAL_ROWS} spreadsheet rows. '
                           'Remove blank rows and try again.')
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result
    if len(rows) - 1 > MAX_ROWS:
        result['fatal'] = (f'That file has more than {MAX_ROWS} entry rows. '
                           'Split it and import in parts.')
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'entry' not in mapping or 'track' not in mapping:
        result['fatal'] = ('Could not find the Track and Entry columns. '
                           'The first row must include Track and Entry headers.')
        return result

    seen_names = defaultdict(list)
    seen_refs = defaultdict(list)
    for number, source in enumerate(rows[1:], start=2):
        track = clean(cell(source, mapping, 'track'))
        entry = clean(cell(source, mapping, 'entry'))
        reference = clean(cell(source, mapping, 'reference'))
        if not track and not entry and not reference:
            continue
        row = {
            'row': number,
            'track': track,
            'name': entry,
            'reference': reference,
            'errors': [],
        }
        if not track:
            row['errors'].append('Track name is required.')
        if not entry:
            row['errors'].append('Entry name is required.')
        for field, value in (('track', track), ('entry', entry), ('reference', reference)):
            if len(value) > LIMITS[field]:
                row['errors'].append(f'{field.title()} is longer than {LIMITS[field]} characters.')
        result['rows'].append(row)
        if track and entry:
            seen_names[(key(track), key(entry))].append(row)
        if track and reference:
            seen_refs[(key(track), key(reference))].append(row)

    for duplicate_rows in seen_names.values():
        if len(duplicate_rows) > 1:
            row_numbers = ', '.join(str(row['row']) for row in duplicate_rows)
            message = f'This track/entry pair is repeated in rows {row_numbers}.'
            for row in duplicate_rows:
                row['errors'].append(message)

    for duplicate_rows in seen_refs.values():
        if len(duplicate_rows) > 1:
            row_numbers = ', '.join(str(row['row']) for row in duplicate_rows)
            message = f'This track/reference pair is repeated in rows {row_numbers}.'
            for row in duplicate_rows:
                row['errors'].append(message)

    result['errors'] = [(row['row'], message) for row in result['rows'] for message in row['errors']]
    if not result['rows'] and not result['errors']:
        result['fatal'] = 'The file has no entry rows.'
    return result


def plan_competition_entry_import(competition, parsed):
    """Describe the track reuse and entries an apply would create."""
    if parsed.get('fatal'):
        return {
            'rows': [],
            'errors': [(None, parsed['fatal'])],
            'tracks_to_create': [],
            'entries_to_create': 0,
            'ok': False,
        }
    tracks = {key(track.name): track for track in competition.tracks.all()}
    existing_entries = list(CompetitionEntry.objects.filter(
        track__competition=competition).select_related('track'))
    existing_names = {(key(entry.track.name), key(entry.name)) for entry in existing_entries}
    existing_refs = {(key(entry.track.name), key(entry.reference))
                     for entry in existing_entries if entry.reference}
    new_tracks = {}
    planned_rows = []
    errors = list(parsed['errors'])
    count = 0
    for source in parsed['rows']:
        row = {**source, 'errors': list(source['errors'])}
        track_key = key(row['track'])
        if row['track'] and row['name'] and (track_key, key(row['name'])) in existing_names:
            row['errors'].append('An entry with this name already exists in this track.')
        if row['track'] and row['reference'] and (track_key, key(row['reference'])) in existing_refs:
            row['errors'].append('An entry with this reference already exists in this track.')
        if row['track'] and track_key not in tracks and track_key not in new_tracks:
            new_tracks[track_key] = row['track']
        if row['track'] and row['name'] and not row['errors']:
            count += 1
        planned_rows.append(row)

    errors.extend((row['row'], message) for row in planned_rows for message in row['errors']
                  if (row['row'], message) not in errors)
    return {
        'rows': planned_rows,
        'errors': errors,
        'tracks_to_create': list(new_tracks.values()),
        'entries_to_create': count,
        'ok': not errors,
    }


@transaction.atomic
def apply_competition_entry_import(competition, parsed, *, organization, user):
    """Create the reviewed tracks and entries in one transaction."""
    locked_competition = (Competition.objects.for_organization(organization).owned_by(user)
                          .select_for_update().filter(pk=competition.pk).first())
    if locked_competition is None:
        raise PermissionDenied
    if locked_competition.status != Competition.Status.DRAFT or locked_competition.rounds.exists():
        raise ValidationError(
            'Entries can only be imported while the competition is a draft before rounds are created.')

    plan = plan_competition_entry_import(locked_competition, parsed)
    if not plan['ok']:
        raise ValidationError('The entry import has errors. Review the preview before applying it.')

    tracks = {key(track.name): track for track in locked_competition.tracks.all()}
    for track_name in plan['tracks_to_create']:
        track = CompetitionTrack(
            competition=locked_competition,
            name=track_name,
            organization_id=locked_competition.organization_id,
            created_by=locked_competition.created_by,
        )
        track.full_clean()
        track.save(force_insert=True)
        tracks[key(track_name)] = track

    next_orders = {}
    for track_key, track in tracks.items():
        max_order = track.entries.aggregate(Max('order'))['order__max']
        next_orders[track_key] = (max_order + 1) if max_order is not None else 0

    created = []
    for row in plan['rows']:
        track_key = key(row['track'])
        track = tracks[track_key]
        entry = CompetitionEntry(
            track=track,
            name=row['name'],
            reference=row['reference'],
            order=next_orders[track_key],
            organization_id=locked_competition.organization_id,
            created_by=locked_competition.created_by,
        )
        entry.full_clean()
        entry.save(force_insert=True)
        created.append(entry)
        next_orders[track_key] += 1
    return created



ENTRY_COLUMNS = ['Track', 'Entry', 'Reference']


def plan_entries(competition, parsed):
    """The entry plan in the shared import card's shape."""
    plan = plan_competition_entry_import(competition, parsed)
    rows = [{'row': row['row'], 'errors': row['errors'],
             'cells': [row['track'] or '-', row['name'] or '-', row['reference'] or '-']}
            for row in plan['rows']]
    tracks = len(plan['tracks_to_create'])
    count = plan['entries_to_create']
    return {
        'columns': ENTRY_COLUMNS, 'rows': rows, 'errors': plan['errors'], 'ok': plan['ok'],
        'summary': (f'{count} entr{"y" if count == 1 else "ies"} will be added'
                    + (f', with {tracks} new track{"" if tracks == 1 else "s"}.' if tracks else '.')),
    }
