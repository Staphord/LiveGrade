"""The rules a rubric must satisfy, in one place.

The spreadsheet import, the "edit as a table" form and the checks before going
live all go through here, so a rubric is judged the same way however it arrives.
Rows are plain dicts: ``row`` (the line it came from, or None), ``id`` (an existing
category, or None), ``name``, ``description``, ``scope``, ``weight``. A weight is the category's share of the
100-point final score, so a rubric's weights, group and individual together, are
judged as one pool of ``TOTAL_WEIGHT``.
"""
from decimal import Decimal, InvalidOperation

from .models import TOTAL_WEIGHT, RubricCategory
from .sheets import cell_text, clean, key

MAX_ROWS = 50

SCOPE_ALIASES = {
    'group': RubricCategory.Scope.GROUP, 'team': RubricCategory.Scope.GROUP,
    'project': RubricCategory.Scope.GROUP, 'group / project': RubricCategory.Scope.GROUP,
    'individual': RubricCategory.Scope.INDIVIDUAL, 'student': RubricCategory.Scope.INDIVIDUAL,
    'person': RubricCategory.Scope.INDIVIDUAL, 'individual student': RubricCategory.Scope.INDIVIDUAL,
}
SCOPE_LABEL = {RubricCategory.Scope.GROUP: 'Group', RubricCategory.Scope.INDIVIDUAL: 'Individual'}

NAME_MAX, DESCRIPTION_MAX = 150, 255


def pct(value):
    """A weight without trailing zeros: 60.00 -> '60', 12.50 -> '12.5'."""
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _number(value, label):
    """``(Decimal, None)`` or ``(None, message)``. Accepts '30', 30, '30%', '12.5'."""
    text = cell_text(value).strip().rstrip('%').strip().replace(',', '.')
    if not text:
        return None, f'{label} is empty.'
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, f'{label} "{cell_text(value)}" is not a number.'
    if not number.is_finite():
        return None, f'{label} "{cell_text(value)}" is not a number.'
    if number != number.quantize(Decimal('0.01')):
        return None, f'{label} can have at most 2 decimal places.'
    return number, None


def normalise_row(raw, row_number=None):
    """``(row, errors)``: ``raw`` cleaned and typed; ``errors`` are messages."""
    errors = []
    name = clean(raw.get('name'))
    description = clean(raw.get('description'))
    if not name:
        errors.append('Category name is empty.')
    elif len(name) > NAME_MAX:
        errors.append(f'Category name is longer than {NAME_MAX} characters.')
    if len(description) > DESCRIPTION_MAX:
        errors.append(f'Description is longer than {DESCRIPTION_MAX} characters.')

    scope_text = key(raw.get('scope')) or RubricCategory.Scope.GROUP
    scope = SCOPE_ALIASES.get(scope_text)
    if scope is None:
        errors.append(f'Scope "{clean(raw.get("scope"))}" must be Group or Individual.')

    weight, problem = _number(raw.get('weight'), 'Weight')
    if problem:
        errors.append(problem)
    elif weight <= 0 or weight > TOTAL_WEIGHT:
        errors.append(f'Weight must be more than 0 and at most {pct(TOTAL_WEIGHT)}.')
        weight = None

    row = {'row': row_number, 'id': raw.get('id'), 'name': name, 'description': description,
           'scope': scope, 'weight': weight}
    return row, errors


def check_rubric(rows, strict):
    """Judge a whole rubric.

    ``rows`` are already normalised and error-free field by field. ``strict`` is the
    go-live standard: the weights of every category, group and individual together,
    must add up to exactly ``TOTAL_WEIGHT``. Not strict (a draft being built up) only
    forbids going over.

    Returns ``{'errors': [(row, message)], 'totals': {scope: Decimal}, 'total': Decimal}``.
    """
    errors = []
    if len(rows) > MAX_ROWS:
        errors.append((None, f'A rubric can have at most {MAX_ROWS} categories.'))
    seen = {}
    totals = {scope: Decimal('0') for scope in SCOPE_LABEL}
    for row in rows:
        marker = (row['scope'], key(row['name']))
        if marker in seen:
            errors.append((row['row'], f'"{row["name"]}" appears twice in {SCOPE_LABEL[row["scope"]].lower()} categories.'))
        seen[marker] = True
        totals[row['scope']] += row['weight']
    total = sum(totals.values(), Decimal('0'))
    if total > TOTAL_WEIGHT:
        errors.append((None, f'Weights add up to {pct(total)}%, which is over {pct(TOTAL_WEIGHT)}% '
                             f'by {pct(total - TOTAL_WEIGHT)}%.'))
    elif strict and total < TOTAL_WEIGHT:
        errors.append((None, f'Weights add up to {pct(total)}%; they must add up to '
                             f'{pct(TOTAL_WEIGHT)}% (short by {pct(TOTAL_WEIGHT - total)}%).'))
    return {'errors': errors, 'totals': totals, 'total': total}
