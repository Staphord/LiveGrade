"""Bounded, preview-first import of panel scoring criteria from Excel."""
from collections import defaultdict

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from assessments.level_descriptors import parse_descriptors
from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import PanelAuditEvent, PanelCriterion, PanelCriterionLevel, PanelHearing
from .panel_import import MAX_ROWS, TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows


HEADERS = {
    'name': {'criterion', 'criterion name', 'name'},
    'description': {'description', 'what is assessed', 'guidance'},
    'levels': {'score guidance', 'score levels', 'level guidance', 'anchors'},
}
MAX_LEVEL_TEXT = 1200


def sample_workbook():
    return workbook_bytes(['Criterion', 'Description', 'Score guidance'], [
        ['Subject knowledge', 'Accuracy and depth of understanding',
         '0 | Not demonstrated\n50 | Partly demonstrated\n100 | Fully demonstrated'],
        ['Communication', 'Clear structure and audience engagement', ''],
    ], {'A': 28, 'B': 52, 'C': 62})


def parse_rubric(uploaded_file):
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
        result['fatal'] = f'That file has more than {MAX_ROWS} criteria. Split it and import in parts.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'name' not in mapping:
        result['fatal'] = 'Could not find the Criterion column. The first row must include a Criterion header.'
        return result

    seen = defaultdict(list)
    for number, source in enumerate(rows[1:], start=2):
        name = clean(cell(source, mapping, 'name'))
        description = clean(cell(source, mapping, 'description'))
        levels_text = clean(cell(source, mapping, 'levels'))
        if not name and not description and not levels_text:
            continue
        row = {'row': number, 'name': name, 'description': description,
               'levels_text': levels_text, 'levels': [], 'errors': []}
        if not name:
            row['errors'].append('Criterion name is required.')
        if len(name) > 120:
            row['errors'].append('Criterion name is longer than 120 characters.')
        if len(description) > 1000:
            row['errors'].append('Description is longer than 1000 characters.')
        if len(levels_text) > MAX_LEVEL_TEXT:
            row['errors'].append(f'Score guidance is longer than {MAX_LEVEL_TEXT} characters.')
        if any(value.startswith('=') for value in (name, description, levels_text)):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        if levels_text:
            descriptors, descriptor_errors = parse_descriptors(levels_text, 100)
            row['levels'] = [{'score': str(item['score']), 'description': item['description']}
                             for item in descriptors]
            row['errors'].extend(descriptor_errors)
        result['rows'].append(row)
        if name:
            seen[key(name)].append(row)
    for duplicates in seen.values():
        if len(duplicates) > 1:
            numbers = ', '.join(str(row['row']) for row in duplicates)
            for row in duplicates:
                row['errors'].append(f'This criterion is repeated in rows {numbers}.')
    result['errors'] = [(row['row'], message) for row in result['rows'] for message in row['errors']]
    if not result['rows'] and not result['errors']:
        result['fatal'] = 'The file has no criteria rows.'
    return result


def plan_rubric(hearing, parsed):
    if parsed.get('fatal'):
        return {'rows': [], 'errors': [(None, parsed['fatal'])], 'criteria_to_create': 0, 'ok': False}
    names = {key(name) for name in hearing.criteria.values_list('name', flat=True)}
    planned, errors = [], list(parsed['errors'])
    for source in parsed['rows']:
        row = {**source, 'errors': list(source['errors'])}
        if row['name'] and key(row['name']) in names:
            row['errors'].append('A criterion with this name already exists in the hearing.')
        planned.append(row)
    errors.extend((row['row'], message) for row in planned for message in row['errors']
                  if (row['row'], message) not in errors)
    count = sum(bool(row['name']) and not row['errors'] for row in planned)
    return {'rows': planned, 'errors': errors, 'criteria_to_create': count, 'ok': not errors}


@transaction.atomic
def apply_rubric_import(hearing, parsed, *, organization, user):
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.DRAFT:
        raise ValidationError('Rubric criteria can only be imported while the hearing is a draft.')
    plan = plan_rubric(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The rubric import has errors. Review the preview before applying it.')
    panels = list(locked.panels.all())
    order = max((item.order for item in locked.criteria.all()), default=-1) + 1
    created = []
    for row in plan['rows']:
        criterion = PanelCriterion(hearing=locked, name=row['name'],
            description=row['description'], order=order,
            organization_id=locked.organization_id, created_by=locked.created_by)
        criterion.full_clean()
        criterion.save(force_insert=True)
        criterion.panels.add(*panels)
        levels = [PanelCriterionLevel(criterion=criterion, score=item['score'],
            description=item['description'], organization_id=locked.organization_id,
            created_by=locked.created_by) for item in row['levels']]
        for level in levels:
            level.full_clean()
        PanelCriterionLevel.objects.bulk_create(levels)
        event = PanelAuditEvent(hearing=locked, action=PanelAuditEvent.Action.CRITERION_ADDED,
            reason='Scoring criterion added from a reviewed spreadsheet import.', actor_user=user,
            organization_id=locked.organization_id, created_by=user)
        event.full_clean()
        event.save(force_insert=True)
        created.append(criterion)
        order += 1
    return created
