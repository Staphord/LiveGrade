"""Parse and render owner-defined score guidance for rubric criteria."""
from decimal import Decimal, InvalidOperation


MAX_DESCRIPTOR_TEXT = 4000
MAX_LEVEL_DESCRIPTION = 500
MAX_LEVELS_PER_CRITERION = 12


def score_text(value):
    text = format(Decimal(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def format_descriptors(rows):
    return '\n'.join(
        f'{score_text(row.get("score") if isinstance(row, dict) else row.score)} | '
        f'{row.get("description") if isinstance(row, dict) else row.description}'
        for row in rows)


def parse_descriptors(value, maximum):
    """Return ``(levels, errors)`` from lines formatted ``point value | guidance``.

    Levels are owner-defined anchors on the existing continuous score scale; this
    parser does not add, remove or round the score control's available values.
    """
    text = '' if value is None else str(value)
    levels, errors, seen = [], [], set()
    if len(text) > MAX_DESCRIPTOR_TEXT:
        return [], [f'Descriptor text is longer than {MAX_DESCRIPTOR_TEXT} characters.']
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if '|' not in line:
            errors.append(f'Descriptor line {number} must use "point value | guidance".')
            continue
        raw_score, description = line.split('|', 1)
        raw_score = raw_score.strip()
        description = description.strip()
        try:
            score = Decimal(raw_score)
        except (InvalidOperation, ValueError):
            errors.append(f'Descriptor line {number} has an invalid point value.')
            continue
        if not score.is_finite() or score < 0 or score > maximum:
            errors.append(f'Descriptor line {number} must be between 0 and {score_text(maximum)} points.')
            continue
        try:
            exact_score = score.quantize(Decimal('0.01'))
        except InvalidOperation:
            errors.append(f'Descriptor line {number} can have at most 2 decimal places.')
            continue
        if score != exact_score:
            errors.append(f'Descriptor line {number} can have at most 2 decimal places.')
            continue
        if exact_score in seen:
            errors.append(f'Descriptor line {number} repeats point value {score_text(exact_score)}.')
            continue
        if not description:
            errors.append(f'Descriptor line {number} needs guidance after the point value.')
            continue
        if len(description) > MAX_LEVEL_DESCRIPTION:
            errors.append(f'Descriptor line {number} guidance is longer than {MAX_LEVEL_DESCRIPTION} characters.')
            continue
        seen.add(exact_score)
        levels.append({'score': exact_score, 'description': description})
    if len(levels) > MAX_LEVELS_PER_CRITERION:
        errors.append(f'A criterion can have at most {MAX_LEVELS_PER_CRITERION} score descriptors.')
    levels.sort(key=lambda item: item['score'])
    return levels, errors
