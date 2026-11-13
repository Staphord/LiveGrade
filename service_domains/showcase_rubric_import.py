"""Preview and apply a Showcase judge rubric from a spreadsheet.

The preview is read-only. Apply rechecks the plan and replaces the draft
event's rubric in one transaction, because a rubric must total 100%.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import ShowcaseJudgeCriterion
from .operations import replace_showcase_judge_criteria
from .showcase_import import (
    MAX_UPLOAD_BYTES, TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows,
)

MAX_CRITERIA = 50
MAX_CHECKLIST_ITEMS = 20
Scale = ShowcaseJudgeCriterion.Scale

HEADERS = {
    'name': {'criterion', 'criterion name', 'name'},
    'description': {'description', 'what to assess', 'guidance'},
    'weight': {'weight', 'weight (%)', 'criterion weight'},
    'scale': {'scale', 'scoring scale'},
    'max_points': {'max points', 'maximum points', 'max score'},
    'checklist': {'checklist', 'checklist items'},
}
SCALES = {
    'points': Scale.POINTS, 'stars': Scale.STARS, 'five stars': Scale.STARS,
    'pass fail': Scale.PASS_FAIL, 'pass or fail': Scale.PASS_FAIL,
    'pass/fail': Scale.PASS_FAIL, 'checklist': Scale.CHECKLIST,
}


def sample_workbook():
    return workbook_bytes(
        ['Criterion', 'Description', 'Weight', 'Scale', 'Max points', 'Checklist items'], [
            ['Innovation', 'Originality of the idea', 40, 'Points', 100, ''],
            ['Presentation', 'Clarity of the pitch', 30, 'Stars', '', ''],
            ['Safety', 'Meets the safety requirements', 30, 'Checklist', '',
             'Guards fitted\nMaterials labelled'],
        ], {'A': 24, 'B': 40, 'C': 10, 'D': 14, 'E': 12, 'F': 40})


def _weight(raw, row):
    try:
        weight = Decimal(str(raw))
        if (not weight.is_finite() or not Decimal('0') <= weight <= Decimal('100')
                or weight.as_tuple().exponent < -2):
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        row['errors'].append('Weight must be a number from 0 to 100 with at most two decimal places.')
        return '0'
    return str(weight)


def _max_points(raw, row):
    if raw in (None, ''):
        return '100'
    try:
        points = Decimal(str(raw))
        if (not points.is_finite() or not Decimal('0.01') <= points <= Decimal('1000')
                or points.as_tuple().exponent < -2):
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        row['errors'].append('Max points must be a number from 0.01 to 1000 with at most two decimal places.')
        return '100'
    return str(points)


def _check_scale(row, scale, raw_max, checklist_text):
    """Scale-specific fields; fills `max_points` and `checklist_items`."""
    row['max_points'] = '100'
    row['checklist_items'] = []
    if scale == Scale.POINTS:
        row['max_points'] = _max_points(raw_max, row)
    elif raw_max not in (None, ''):
        row['errors'].append('Max points apply only to the Points scale.')
    items = [line.strip() for line in checklist_text.splitlines() if line.strip()]
    if scale == Scale.CHECKLIST:
        if not items or len(items) > MAX_CHECKLIST_ITEMS or any(len(item) > 240 for item in items):
            row['errors'].append(
                f'A checklist needs 1 to {MAX_CHECKLIST_ITEMS} items of 240 characters or fewer.')
        row['checklist_items'] = items
    elif items:
        row['errors'].append('Checklist items apply only to the Checklist scale.')


def parse_rubric(uploaded_file):
    result = {'fatal': None, 'rows': [], 'errors': []}
    if getattr(uploaded_file, 'size', 0) > MAX_UPLOAD_BYTES:
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
    if len(rows) - 1 > MAX_CRITERIA:
        result['fatal'] = f'That file has more than {MAX_CRITERIA} criteria.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'name' not in mapping or 'weight' not in mapping:
        result['fatal'] = 'The first row must include Criterion and Weight headers.'
        return result

    seen = defaultdict(list)
    for number, source in enumerate(rows[1:], start=2):
        name = clean(cell(source, mapping, 'name'))
        description = clean(cell(source, mapping, 'description'))
        raw_checklist = cell(source, mapping, 'checklist')
        checklist_text = str(raw_checklist).strip() if raw_checklist not in (None, '') else ''
        row = {'row': number, 'name': name, 'description': description, 'errors': []}
        row['weight'] = _weight(cell(source, mapping, 'weight'), row)
        raw_scale = clean(cell(source, mapping, 'scale'))
        scale = SCALES.get(key(raw_scale)) if raw_scale else Scale.POINTS
        if scale is None:
            row['errors'].append('Scale must be Points, Stars, Pass or fail, or Checklist.')
            scale = Scale.POINTS
        row['scale'] = scale
        _check_scale(row, scale, cell(source, mapping, 'max_points'), checklist_text)
        if not name:
            row['errors'].append('Criterion name is required.')
        if len(name) > 120:
            row['errors'].append('Criterion name is longer than 120 characters.')
        if len(description) > 1000:
            row['errors'].append('Description is longer than 1000 characters.')
        if any(value.startswith('=') for value in (name, description, checklist_text)):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        result['rows'].append(row)
        if name:
            seen[key(name)].append(row)
    for duplicates in seen.values():
        if len(duplicates) > 1:
            numbers = ', '.join(str(row['row']) for row in duplicates)
            for row in duplicates:
                row['errors'].append(f'This criterion is repeated in rows {numbers}.')
    result['errors'] = [(row['row'], message) for row in result['rows'] for message in row['errors']]
    return result


def plan_rubric(event, parsed):
    """Describe what an apply would do: replace the rubric with these rows."""
    if parsed.get('fatal'):
        return {'rows': [], 'errors': [(None, parsed['fatal'])], 'criteria_to_create': 0,
                'total_weight': Decimal('0'), 'replaces': 0, 'ok': False}
    rows = [{**row, 'errors': list(row['errors'])} for row in parsed['rows']]
    errors = list(parsed['errors'])
    total = sum((Decimal(row['weight']) for row in rows), Decimal('0'))
    if not rows:
        errors.append((None, 'The file has no criteria rows.'))
    elif total != 100:
        errors.append((None, f'Criterion weights must total exactly 100%; this file totals {total}%.'))
    return {'rows': rows, 'errors': errors, 'criteria_to_create': len(rows),
            'total_weight': total, 'replaces': event.judge_criteria.count(), 'ok': not errors}


def apply_rubric_import(event, parsed, *, organization, user):
    """Replace the draft rubric with the reviewed rows; stale plans write nothing."""
    plan = plan_rubric(event, parsed)
    if not plan['ok']:
        raise ValidationError('The rubric import has errors. Review the preview before applying it.')
    return replace_showcase_judge_criteria(event, [{
        'name': row['name'], 'description': row['description'], 'weight': row['weight'],
        'scale': row['scale'], 'max_points': row['max_points'],
        'checklist_items': row['checklist_items'],
    } for row in plan['rows']], organization=organization, user=user)
