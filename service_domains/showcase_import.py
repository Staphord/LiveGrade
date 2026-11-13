"""Preview and apply Showcase category/exhibit spreadsheets.

The preview is read-only. Apply rechecks the file plan against the event while
holding the event row lock, then creates categories and exhibits atomically.
"""
from collections import defaultdict
import zipfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
import openpyxl

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .models import ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit


MAX_ROWS = 500
MAX_PHYSICAL_ROWS = 2000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
TOO_MANY_PHYSICAL_ROWS = object()
LIMITS = {'category': 100, 'exhibit': 160, 'description': 2000}

HEADERS = {
    'category': {'category', 'category name', 'award category'},
    'exhibit': {'exhibit', 'exhibit name', 'project', 'project name', 'entry'},
    'description': {'description', 'summary', 'details'},
}

SAMPLE_HEADERS = ['Category', 'Exhibit', 'Description']
SAMPLE_ROWS = [
    ['Engineering', 'Solar water pump', 'A solar-powered irrigation prototype.'],
    ['Community', 'Low-cost water filter', 'A reusable filter designed for local materials.'],
]


def sample_workbook():
    return workbook_bytes(SAMPLE_HEADERS, SAMPLE_ROWS, {'A': 20, 'B': 30, 'C': 64})


def _read_bounded_rows(uploaded_file):
    """Read a small XLSX without trusting ZIP sizes or worksheet dimensions."""
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
            return [[cell.value for cell in row]
                    for row in worksheet.iter_rows(max_row=MAX_PHYSICAL_ROWS)]
        finally:
            workbook.close()
    except Exception:
        return None


def parse_exhibits(uploaded_file):
    """Return normalized spreadsheet rows and row-numbered errors without writes."""
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
        result['fatal'] = f'That file has more than {MAX_ROWS} exhibit rows. Split it and import in parts.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'exhibit' not in mapping:
        result['fatal'] = 'Could not find the Exhibit column. The first row must include an Exhibit header.'
        return result

    seen = defaultdict(list)
    for number, source in enumerate(rows[1:], start=2):
        category = clean(cell(source, mapping, 'category'))
        exhibit = clean(cell(source, mapping, 'exhibit'))
        description = clean(cell(source, mapping, 'description'))
        if not category and not exhibit and not description:
            continue
        row = {'row': number, 'category': category, 'name': exhibit,
               'description': description, 'errors': []}
        if not exhibit:
            row['errors'].append('Exhibit name is required.')
        for field, value in (('category', category), ('exhibit', exhibit), ('description', description)):
            if len(value) > LIMITS[field]:
                row['errors'].append(f'{field.title()} is longer than {LIMITS[field]} characters.')
        result['rows'].append(row)
        if exhibit:
            seen[(key(category), key(exhibit))].append(row)

    for duplicate_rows in seen.values():
        if len(duplicate_rows) > 1:
            row_numbers = ', '.join(str(row['row']) for row in duplicate_rows)
            message = f'This category/exhibit pair is repeated in rows {row_numbers}.'
            for row in duplicate_rows:
                row['errors'].append(message)
    result['errors'] = [(row['row'], message) for row in result['rows'] for message in row['errors']]
    if not result['rows'] and not result['errors']:
        result['fatal'] = 'The file has no exhibit rows.'
    return result


def plan_import(event, parsed):
    """Describe the category reuse and exhibits an apply would create."""
    if parsed.get('fatal'):
        return {'rows': [], 'errors': [(None, parsed['fatal'])], 'categories_to_create': [],
                'exhibits_to_create': 0, 'ok': False}
    categories = {key(category.name): category for category in event.categories.all()}
    exhibits = {(key(exhibit.category.name) if exhibit.category_id else '', key(exhibit.name))
                for exhibit in event.exhibits.select_related('category').all()}
    new_categories = {}
    planned_rows = []
    errors = list(parsed['errors'])
    count = 0
    for source in parsed['rows']:
        row = {**source, 'errors': list(source['errors'])}
        category_key = key(row['category'])
        pair = (category_key, key(row['name']))
        if row['name'] and pair in exhibits:
            row['errors'].append('An exhibit with this name already exists in this category.')
        if row['category'] and category_key not in categories and category_key not in new_categories:
            new_categories[category_key] = row['category']
        if row['name'] and not row['errors']:
            count += 1
        planned_rows.append(row)

    errors.extend((row['row'], message) for row in planned_rows for message in row['errors']
                  if (row['row'], message) not in errors)
    return {
        'rows': planned_rows,
        'errors': errors,
        'categories_to_create': list(new_categories.values()),
        'exhibits_to_create': count,
        'ok': not errors,
    }


@transaction.atomic
def apply_import(event, parsed, *, organization, user):
    """Create the reviewed exhibits in one transaction; stale/invalid plans write nothing."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
                    .select_for_update().filter(pk=event.pk).first())
    if locked_event is None:
        raise PermissionDenied
    if locked_event.status != ShowcaseEvent.Status.DRAFT:
        raise ValidationError('Exhibits can only be imported while the event is a draft.')

    plan = plan_import(locked_event, parsed)
    if not plan['ok']:
        raise ValidationError('The exhibit import has errors. Review the preview before applying it.')

    categories = {key(category.name): category for category in locked_event.categories.all()}
    next_category_order = locked_event.categories.aggregate(Max('order'))['order__max']
    next_category_order = (next_category_order + 1) if next_category_order is not None else 0
    next_exhibit_order = locked_event.exhibits.aggregate(Max('order'))['order__max']
    next_exhibit_order = (next_exhibit_order + 1) if next_exhibit_order is not None else 0

    for category_name in plan['categories_to_create']:
        category = ShowcaseCategory(event=locked_event, name=category_name,
            organization_id=locked_event.organization_id, created_by=locked_event.created_by,
            order=next_category_order)
        category.full_clean()
        category.save(force_insert=True)
        categories[key(category_name)] = category
        next_category_order += 1

    created = []
    for row in plan['rows']:
        category = categories.get(key(row['category'])) if row['category'] else None
        exhibit = ShowcaseExhibit(event=locked_event, category=category, name=row['name'],
            description=row['description'], organization_id=locked_event.organization_id,
            created_by=locked_event.created_by, order=next_exhibit_order)
        exhibit.full_clean()
        exhibit.save(force_insert=True)
        created.append(exhibit)
        next_exhibit_order += 1
    return created
