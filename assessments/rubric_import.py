"""Rubric from a spreadsheet, and the "edit as a table" apply path.

Both turn rows into categories through ``rubric_rules``, so a spreadsheet and the
table are held to the same standard. Importing is two steps like the groups import:
``parse_rubric`` reads the file into plain strings (safe to keep in the session),
``check_import`` judges them against the session, ``apply_import`` writes.
"""
from django.db import models, transaction

from . import rubric_rules as rules
from .models import RubricCategory
from .setup_rules import category_core_lock
from .sheets import UNREADABLE, cell, cell_text, map_headers, read_rows, workbook_bytes

HEADERS = {
    'name': {'category', 'category name', 'criterion', 'criteria', 'name'},
    'description': {'description', 'guidance', 'details'},
    'scope': {'scope', 'type', 'level', 'applies to'},
    'max_points': {'max points', 'max', 'maximum', 'points', 'max score', 'out of'},
    'weight': {'weight', 'weight %', 'weight (%)', '% weight', 'weighting'},
}

SAMPLE_HEADERS = ['Category', 'Description', 'Scope', 'Max points', 'Weight %']
SAMPLE_ROWS = [
    ['Technical depth', 'Quality and difficulty of the solution', 'Group', 10, 30],
    ['Presentation', 'Clarity and delivery', 'Group', 10, 30],
    ['Teamwork', "This student's contribution to the group", 'Individual', 5, 40],
]


def sample_workbook():
    return workbook_bytes(SAMPLE_HEADERS, SAMPLE_ROWS, {'A': 24, 'B': 46, 'C': 14, 'D': 12, 'E': 12})


def parse_rubric(uploaded_file):
    """``{'fatal': str|None, 'rows': [raw row dicts of strings]}``."""
    result = {'fatal': None, 'rows': []}
    rows = read_rows(uploaded_file, percent_as_points=True)
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    missing = [label for field, label in (('name', 'Category'), ('max_points', 'Max points'),
                                          ('weight', 'Weight %')) if field not in mapping]
    if missing:
        result['fatal'] = (f'Could not find the {", ".join(missing)} column(s). The first row must '
                           f'have headers like "Category", "Scope", "Max points" and "Weight %".')
        return result
    if len(rows) - 1 > rules.MAX_ROWS:
        result['fatal'] = f'A rubric can have at most {rules.MAX_ROWS} categories.'
        return result
    for number, row in enumerate(rows[1:], start=2):
        result['rows'].append({
            'row': number,
            **{field: cell_text(cell(row, mapping, field)) for field in HEADERS}})
    return result


def check_import(raw_rows, session, mode):
    """Judge an import before writing anything.

    ``mode`` is ``'replace'`` or ``'add'``. Returns ``{'rows': [normalised],
    'errors': [(row, message)], 'totals': {...}, 'ok': bool}``.
    """
    errors, rows = [], []
    for raw in raw_rows:
        row, problems = rules.normalise_row(raw, raw['row'])
        errors.extend((raw['row'], message) for message in problems)
        rows.append(row)
    if errors:
        return {'rows': rows, 'errors': errors, 'totals': {}, 'ok': False}
    combined = list(rows)
    if mode == 'add':
        combined = [_existing_row(c) for c in session.rubric_categories.all()] + rows
    verdict = rules.check_rubric(combined, session, strict=True)
    return {'rows': rows, 'errors': verdict['errors'], 'totals': verdict['totals'],
            'ok': not verdict['errors']}


def _existing_row(category):
    return {'row': None, 'id': category.pk, 'name': category.name, 'description': category.description,
            'scope': category.scope, 'max_points': category.max_points, 'weight': category.weight}


@transaction.atomic
def apply_import(session, raw_rows, mode):
    """Write a checked import. Returns the number of categories created."""
    verdict = check_import(raw_rows, session, mode)
    if not verdict['ok']:
        raise ValueError('The rubric no longer passes its checks.')
    if mode == 'replace':
        session.rubric_categories.all().delete()
    order = session.rubric_categories.aggregate(models.Max('order'))['order__max'] or 0
    for row in verdict['rows']:
        order += 1
        RubricCategory.objects.create(
            assessment_session=session, name=row['name'], description=row['description'],
            scope=row['scope'], max_points=row['max_points'], weight=row['weight'], order=order)
    return len(verdict['rows'])


def check_table(rows_raw, session, strict):
    """Judge the "edit as a table" submission.

    ``rows_raw`` are dicts with an optional ``id``. Returns the same shape as
    ``check_import`` plus ``'removed'`` (existing categories the table dropped).
    """
    existing = {c.pk: c for c in session.rubric_categories.all()}
    errors, rows = [], []
    for index, raw in enumerate(rows_raw, start=1):
        row, problems = rules.normalise_row(raw, index)
        errors.extend((index, message) for message in problems)
        category = existing.get(row['id']) if row['id'] is not None else None
        if row['id'] is not None and category is None:
            errors.append((index, 'That category no longer exists. Reload the page.'))
        elif category is not None and not problems:
            if (row['scope'], row['max_points']) != (category.scope, category.max_points):
                lock = category_core_lock(category)
                if lock:
                    errors.append((index, lock))
        rows.append(row)
    removed = [c for pk, c in existing.items() if pk not in {r['id'] for r in rows}]
    if strict and not rows:
        errors.append((None, 'A live session needs at least one rubric category.'))
    if errors:
        return {'rows': rows, 'errors': errors, 'totals': {}, 'ok': False, 'removed': removed}
    verdict = rules.check_rubric(rows, session, strict=strict)
    return {'rows': rows, 'errors': verdict['errors'], 'totals': verdict['totals'],
            'ok': not verdict['errors'], 'removed': removed}


@transaction.atomic
def apply_table(session, verdict):
    """Write a checked table. Returns ``(changed, added, removed)`` counts."""
    existing = {c.pk: c for c in session.rubric_categories.all()}
    changed = added = 0
    order = session.rubric_categories.aggregate(models.Max('order'))['order__max'] or 0
    for row in verdict['rows']:
        category = existing.get(row['id']) if row['id'] is not None else None
        if category is None:
            order += 1
            RubricCategory.objects.create(
                assessment_session=session, name=row['name'], description=row['description'],
                scope=row['scope'], max_points=row['max_points'], weight=row['weight'], order=order)
            added += 1
            continue
        fields = {'name': row['name'], 'description': row['description'], 'scope': row['scope'],
                  'max_points': row['max_points'], 'weight': row['weight']}
        if any(getattr(category, f) != v for f, v in fields.items()):
            for field, value in fields.items():
                setattr(category, field, value)
            category.save()
            changed += 1
    for category in verdict['removed']:
        category.delete()
    return changed, added, len(verdict['removed'])
