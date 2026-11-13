"""Rubric from a spreadsheet, and the "edit as a table" apply path.

Both turn rows into categories through ``rubric_rules``, so a spreadsheet and the
table are held to the same standard. Importing is two steps like the groups import:
``parse_rubric`` reads the file into plain strings (safe to keep in the session),
``check_import`` judges them against the session, ``apply_import`` writes.
"""
from django.db import models, transaction

from . import rubric_rules as rules
from .level_descriptors import format_descriptors
from .models import EvaluationScore, LevelDescriptor, RubricCategory
from .setup_rules import category_core_lock
from .sheets import UNREADABLE, cell, cell_text, map_headers, read_rows, workbook_bytes

HEADERS = {
    'name': {'category', 'category name', 'criterion', 'criteria', 'name'},
    'description': {'description', 'guidance', 'details'},
    'scope': {'scope', 'type', 'level', 'applies to'},
    'weight': {'weight', 'weight %', 'weight (%)', '% weight', 'weighting'},
    'level_descriptors': {'level descriptors', 'score level guidance', 'descriptors'},
}

SAMPLE_HEADERS = ['Category', 'Description', 'Scope', 'Weight %', 'Level descriptors']
SAMPLE_ROWS = [
    ['Technical depth', 'Quality and difficulty of the solution', 'Group', 30,
     '0 | Not demonstrated\n15 | Partly demonstrated\n30 | Fully demonstrated'],
    ['Presentation', 'Clarity and delivery', 'Group', 30, '0 | Unclear\n30 | Clear and well structured'],
    ['Teamwork', "This student's contribution to the group", 'Individual', 40, '0 | No clear contribution\n40 | Strong individual contribution'],
]


def sample_workbook():
    return workbook_bytes(SAMPLE_HEADERS, SAMPLE_ROWS,
                         {'A': 24, 'B': 46, 'C': 14, 'D': 12, 'E': 54})


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
    missing = [label for field, label in (('name', 'Category'), ('weight', 'Weight %')) if field not in mapping]
    if missing:
        result['fatal'] = (f'Could not find the {", ".join(missing)} column(s). The first row must '
                           f'have headers like "Category", "Scope" and "Weight %".')
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
        return {'rows': rows, 'errors': errors, 'totals': {}, 'total': None, 'ok': False}
    combined = list(rows)
    if mode == 'add':
        combined = [_existing_row(c) for c in session.rubric_categories.all()] + rows
    verdict = rules.check_rubric(combined, strict=True)
    return {'rows': rows, 'errors': verdict['errors'], 'totals': verdict['totals'],
            'total': verdict['total'], 'ok': not verdict['errors']}


def _existing_row(category):
    return {'row': None, 'id': category.pk, 'name': category.name, 'description': category.description,
            'scope': category.scope, 'weight': category.weight,
            'level_descriptors': format_descriptors(category.level_descriptors.all())}


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
        levels = row['level_descriptors']
        category = RubricCategory.objects.create(
            assessment_session=session, name=row['name'], description=row['description'],
            scope=row['scope'], weight=row['weight'], order=order)
        LevelDescriptor.objects.bulk_create([
            LevelDescriptor(rubric_category=category, **level) for level in levels])
    return len(verdict['rows'])


def check_table(rows_raw, session, strict):
    """Judge the "edit as a table" submission.

    ``rows_raw`` are dicts with an optional ``id``. Returns the same shape as
    ``check_import`` plus ``'removed'`` (existing categories the table dropped).
    """
    existing = {c.pk: c for c in session.rubric_categories.all()}
    scored_ids = set(EvaluationScore.objects.filter(
        rubric_category__assessment_session=session).values_list('rubric_category_id', flat=True))
    errors, rows = [], []
    for index, raw in enumerate(rows_raw, start=1):
        row, problems = rules.normalise_row(raw, index)
        errors.extend((index, message) for message in problems)
        category = existing.get(row['id']) if row['id'] is not None else None
        if row['id'] is not None and category is None:
            errors.append((index, 'That category no longer exists. Reload the page.'))
        elif category is not None and not problems:
            if raw.get('level_descriptors') is None:
                row['level_descriptors'] = [
                    {'score': level.score, 'description': level.description}
                    for level in category.level_descriptors.all()]
            if row['scope'] != category.scope:
                lock = category_core_lock(category)
                if lock:
                    errors.append((index, lock))
            old_levels = [(level.score, level.description)
                          for level in category.level_descriptors.all()]
            new_levels = [(level['score'], level['description'])
                          for level in row['level_descriptors']]
            if category.pk in scored_ids and old_levels != new_levels:
                errors.append((index, 'Score level guidance cannot change after this criterion has been scored.'))
        rows.append(row)
    removed = [c for pk, c in existing.items() if pk not in {r['id'] for r in rows}]
    if strict and not rows:
        errors.append((None, 'A live session needs at least one rubric category.'))
    if errors:
        return {'rows': rows, 'errors': errors, 'totals': {}, 'total': None, 'ok': False, 'removed': removed}
    verdict = rules.check_rubric(rows, strict=strict)
    return {'rows': rows, 'errors': verdict['errors'], 'totals': verdict['totals'],
            'total': verdict['total'], 'ok': not verdict['errors'], 'removed': removed}


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
            category = RubricCategory.objects.create(
                assessment_session=session, name=row['name'], description=row['description'],
                scope=row['scope'], weight=row['weight'], order=order)
            LevelDescriptor.objects.bulk_create([
                LevelDescriptor(rubric_category=category, **level)
                for level in row['level_descriptors']])
            added += 1
            continue
        fields = {'name': row['name'], 'description': row['description'], 'scope': row['scope'],
                  'weight': row['weight']}
        if any(getattr(category, f) != v for f, v in fields.items()):
            for field, value in fields.items():
                setattr(category, field, value)
            category.save()
            changed += 1
        new_levels = row['level_descriptors']
        old_levels = [(level.score, level.description)
                      for level in category.level_descriptors.all()]
        canonical_new = [(level['score'], level['description']) for level in new_levels]
        if old_levels != canonical_new:
            category.level_descriptors.all().delete()
            LevelDescriptor.objects.bulk_create([
                LevelDescriptor(rubric_category=category, **level) for level in new_levels])
            changed += 1
    for category in verdict['removed']:
        category.delete()
    return changed, added, len(verdict['removed'])
