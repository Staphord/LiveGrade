"""Preview-first import of a Team series' competency criteria."""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import TrainingCompetency, TrainingSeries
from .showcase_import import MAX_UPLOAD_BYTES, TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows

MAX_ROWS = 100
COLUMNS = ['Competency', 'Description', 'Pass score']
HEADERS = {
    'name': {'competency', 'competency name', 'name', 'skill'},
    'description': {'description', 'what to assess', 'guidance'},
    'passing_score': {'pass score', 'passing score', 'pass mark', 'threshold'},
}


def sample_workbook():
    return workbook_bytes(COLUMNS, [
        ['Incident response', 'Follows the escalation steps without prompting', 70],
        ['Clear communication', 'Keeps the team informed during the drill', 60],
    ], {'A': 26, 'B': 52, 'C': 12})


def parse_competencies(upload):
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
        result['fatal'] = f'That file has more than {MAX_ROWS} competencies.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'name' not in mapping or 'passing_score' not in mapping:
        result['fatal'] = 'The first row must include Competency and Pass score headers.'
        return result
    for number, source in enumerate(rows[1:], start=2):
        name = clean(cell(source, mapping, 'name'))
        description = clean(cell(source, mapping, 'description'))
        row = {'row': number, 'name': name, 'description': description, 'errors': []}
        try:
            score = Decimal(str(cell(source, mapping, 'passing_score')))
            if (not score.is_finite() or not Decimal('0') <= score <= Decimal('100')
                    or score.as_tuple().exponent < -2):
                raise InvalidOperation
            row['passing_score'] = str(score)
        except (InvalidOperation, TypeError, ValueError):
            row['passing_score'] = '0'
            row['errors'].append('Pass score must be a number from 0 to 100 with at most two decimal places.')
        if not name:
            row['errors'].append('Competency name is required.')
        if len(name) > 120:
            row['errors'].append('Competency name is longer than 120 characters.')
        if len(description) > 1000:
            row['errors'].append('Description is longer than 1000 characters.')
        if any(value.startswith('=') for value in (name, description)):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        result['rows'].append(row)
    return result


def plan_competencies(series, parsed):
    if parsed.get('fatal'):
        return {'columns': COLUMNS, 'rows': [], 'errors': [(None, parsed['fatal'])], 'ok': False,
                'summary': ''}
    existing = {key(name) for name in series.competencies.values_list('name', flat=True)}
    seen = {}
    rows = []
    for source in parsed['rows']:
        errors = list(source['errors'])
        name_key = key(source['name'])
        if source['name'] and name_key in existing:
            errors.append('A competency with this name already exists in the series.')
        if source['name'] and name_key in seen:
            errors.append(f'This competency is repeated from row {seen[name_key]}.')
        seen.setdefault(name_key, source['row'])
        rows.append({'row': source['row'], 'errors': errors, 'name': source['name'],
                     'description': source['description'], 'passing_score': source['passing_score'],
                     'cells': [source['name'] or '-', source['description'] or '-',
                               f"{source['passing_score']}%"]})
    errors = [(row['row'], message) for row in rows for message in row['errors']]
    if not rows:
        errors.append((None, 'The file has no competency rows.'))
    return {'columns': COLUMNS, 'rows': rows, 'errors': errors, 'ok': not errors,
            'summary': f'{len(rows)} competenc{"y" if len(rows) == 1 else "ies"} will be added. '
                       'Existing competencies stay unchanged.'}


@transaction.atomic
def apply_competencies(series, parsed, *, organization, user):
    locked = (TrainingSeries.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=series.pk).first())
    if locked is None:
        raise PermissionDenied
    plan = plan_competencies(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The competency import has errors. Review the preview before applying it.')
    order = (locked.competencies.aggregate(top=Max('order'))['top'] or -1) + 1
    created = []
    for row in plan['rows']:
        competency = TrainingCompetency(series=locked, name=row['name'], description=row['description'],
            passing_score=Decimal(row['passing_score']), order=order,
            organization_id=locked.organization_id, created_by=locked.created_by)
        competency.full_clean()
        competency.save(force_insert=True)
        created.append(competency)
        order += 1
    return created
