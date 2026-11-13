"""Preview-first import of judging criteria across a competition's tracks."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import Competition, CompetitionCriterion, CompetitionRound, CompetitionTrack
from .showcase_import import MAX_UPLOAD_BYTES, TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows

MAX_ROWS = 200
COLUMNS = ['Track', 'Criterion', 'Weight', 'Description']
HEADERS = {
    'track': {'track', 'track name', 'category'},
    'name': {'criterion', 'criterion name', 'name'},
    'weight': {'weight', 'weight (%)', 'criterion weight'},
    'description': {'description', 'what to assess', 'guidance'},
}


def sample_workbook(track_names):
    track = track_names[0] if track_names else 'Main track'
    return workbook_bytes(COLUMNS, [
        [track, 'Innovation', 40, 'How original the idea is'],
        [track, 'Delivery', 35, 'Clarity and confidence of the pitch'],
        [track, 'Impact', 25, 'Benefit to the people it serves'],
    ], {'A': 22, 'B': 24, 'C': 10, 'D': 48})


def parse_criteria(upload):
    result = {'fatal': None, 'rows': []}
    if getattr(upload, 'size', 0) > MAX_UPLOAD_BYTES:
        result['fatal'] = 'The spreadsheet must be 10 MB or smaller.'
        return result
    rows = _read_bounded_rows(upload)
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
        result['fatal'] = f'That file has more than {MAX_ROWS} criteria.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if not {'track', 'name', 'weight'} <= set(mapping):
        result['fatal'] = 'The first row must include Track, Criterion and Weight headers.'
        return result
    for number, source in enumerate(rows[1:], start=2):
        row = {'row': number, 'track': clean(cell(source, mapping, 'track')),
               'name': clean(cell(source, mapping, 'name')),
               'description': clean(cell(source, mapping, 'description')), 'errors': []}
        try:
            weight = Decimal(str(cell(source, mapping, 'weight')))
            if (not weight.is_finite() or not Decimal('0') < weight <= Decimal('100')
                    or weight.as_tuple().exponent < -2):
                raise InvalidOperation
            row['weight'] = str(weight)
        except (InvalidOperation, TypeError, ValueError):
            row['weight'] = '0'
            row['errors'].append('Weight must be a number above 0 and up to 100, with at most two decimal places.')
        if not row['track']:
            row['errors'].append('Track is required.')
        if not row['name']:
            row['errors'].append('Criterion name is required.')
        if len(row['name']) > 120:
            row['errors'].append('Criterion name is longer than 120 characters.')
        if len(row['description']) > 1000:
            row['errors'].append('Description is longer than 1000 characters.')
        if any(value.startswith('=') for value in (row['track'], row['name'], row['description'])):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        result['rows'].append(row)
    return result


def plan_criteria(competition, parsed):
    if parsed.get('fatal'):
        return {'columns': COLUMNS, 'rows': [], 'errors': [(None, parsed['fatal'])], 'ok': False,
                'summary': ''}
    tracks = {key(track.name): track for track in competition.tracks.all()}
    taken = defaultdict(set)
    weight_used = defaultdict(Decimal)
    for track_id, name, weight in CompetitionCriterion.objects.filter(
            track__competition=competition).values_list('track_id', 'name', 'weight'):
        taken[track_id].add(key(name))
        weight_used[track_id] += weight
    seen = {}
    rows = []
    for source in parsed['rows']:
        errors = list(source['errors'])
        track = tracks.get(key(source['track'])) if source['track'] else None
        if source['track'] and track is None:
            errors.append(f'There is no track called "{source["track"]}" in this competition.')
        if track is not None and source['name']:
            name_key = key(source['name'])
            if name_key in taken[track.pk]:
                errors.append('This track already has a criterion with that name.')
            if (track.pk, name_key) in seen:
                errors.append(f'This criterion is repeated from row {seen[(track.pk, name_key)]}.')
            seen.setdefault((track.pk, name_key), source['row'])
        if track is not None and not errors:
            weight_used[track.pk] += Decimal(source['weight'])
        rows.append({**source, 'errors': errors, 'track_id': track.pk if track else None,
                     'cells': [source['track'] or '-', source['name'] or '-',
                               f"{source['weight']}%", source['description'] or '-']})
    errors = [(row['row'], message) for row in rows for message in row['errors']]
    for track in tracks.values():
        if weight_used[track.pk] > 100:
            errors.append((None, f'Criteria for {track.name} would total {weight_used[track.pk]}%; '
                                 'a track can have at most 100%.'))
    if not rows:
        errors.append((None, 'The file has no criteria rows.'))
    return {'columns': COLUMNS, 'rows': rows, 'errors': errors, 'ok': not errors,
            'summary': f'{len(rows)} criteri{"on" if len(rows) == 1 else "a"} will be added. '
                       'Each track must total exactly 100% before the competition starts.'}


@transaction.atomic
def apply_criteria(competition, parsed, *, organization, user):
    locked = (Competition.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=competition.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != Competition.Status.DRAFT or CompetitionRound.objects.filter(
            competition=locked).exists():
        raise ValidationError('Criteria can only be imported before the competition starts.')
    plan = plan_criteria(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The criteria import has errors. Review the preview before applying it.')
    tops = dict(CompetitionCriterion.objects.filter(track__competition=locked).values_list(
        'track_id').annotate(top=Max('order')))
    created = []
    for row in plan['rows']:
        track = CompetitionTrack.objects.get(pk=row['track_id'], competition=locked)
        order = tops.get(track.pk, -1) + 1
        tops[track.pk] = order
        criterion = CompetitionCriterion(track=track, name=row['name'], description=row['description'],
            weight=Decimal(row['weight']), order=order, organization_id=locked.organization_id,
            created_by=user)
        criterion.full_clean()
        criterion.save(force_insert=True)
        created.append(criterion)
    return created
