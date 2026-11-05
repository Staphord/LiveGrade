"""Read and validate a batch of classroom poll questions from an Excel file."""
from collections import OrderedDict

from .sheets import UNREADABLE, cell, clean, map_headers, read_rows, workbook_bytes

MAX_QUESTIONS = 100
MAX_OPTIONS = 50
MAX_ROWS = MAX_QUESTIONS * MAX_OPTIONS

HEADERS = {
    'question': {'question', 'poll question'},
    'type': {'type', 'question type', 'answer type'},
    'option': {'option', 'answer option', 'choice', 'answer'},
}

TYPE_ALIASES = {
    'single choice': False, 'single': False, 'one answer': False,
    'multiple choice': True, 'multiple': True, 'select all': True,
}

SAMPLE_HEADERS = ['Question', 'Question Type', 'Answer Option']
SAMPLE_ROWS = [
    ['When should we hold the presentation?', 'Single choice', 'Monday morning'],
    ['When should we hold the presentation?', 'Single choice', 'Wednesday afternoon'],
    ['When should we hold the presentation?', 'Single choice', 'Friday morning'],
    ['Which topics should we include?', 'Multiple choice', 'Project progress'],
    ['Which topics should we include?', 'Multiple choice', 'Challenges and solutions'],
    ['Which topics should we include?', 'Multiple choice', 'Next steps'],
]


def sample_workbook():
    return workbook_bytes(SAMPLE_HEADERS, SAMPLE_ROWS, {'A': 42, 'B': 20, 'C': 38})


def parse_questions(uploaded_file):
    """Return grouped questions and row-level problems without writing anything."""
    result = {'fatal': None, 'questions': [], 'errors': []}
    rows = read_rows(uploaded_file)
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result

    mapping = map_headers(rows[0], HEADERS)
    missing = [label for field, label in (
        ('question', 'Question'), ('type', 'Question Type'), ('option', 'Answer Option'))
        if field not in mapping]
    if missing:
        result['fatal'] = (f'Could not find the {", ".join(missing)} column(s). '
                           'Use the sample file as a guide.')
        return result
    if len(rows) - 1 > MAX_ROWS:
        result['fatal'] = f'The file can contain at most {MAX_ROWS} answer-option rows.'
        return result

    questions = OrderedDict()
    for number, row in enumerate(rows[1:], start=2):
        question = clean(cell(row, mapping, 'question'))
        type_label = clean(cell(row, mapping, 'type')).casefold()
        option = clean(cell(row, mapping, 'option'))
        if not question:
            result['errors'].append((number, 'Enter the question.'))
            continue
        if len(question) > 250:
            result['errors'].append((number, 'Keep each question within 250 characters.'))
            continue
        if type_label not in TYPE_ALIASES:
            result['errors'].append((number, 'Use Single choice or Multiple choice for the question type.'))
            continue
        if not option:
            result['errors'].append((number, 'Enter an answer option.'))
            continue
        if len(option) > 250:
            result['errors'].append((number, 'Keep each answer option within 250 characters.'))
            continue

        allow_multiple = TYPE_ALIASES[type_label]
        key = question.casefold()
        entry = questions.get(key)
        if entry is None:
            if len(questions) >= MAX_QUESTIONS:
                result['errors'].append((number, f'A file can contain at most {MAX_QUESTIONS} questions.'))
                continue
            entry = {'question': question, 'type': 'Multiple choice' if allow_multiple else 'Single choice',
                     'allow_multiple': allow_multiple, 'options': [], 'row': number}
            questions[key] = entry
        elif entry['allow_multiple'] != allow_multiple:
            result['errors'].append((number, 'Use the same question type for every option of a question.'))
            continue
        if option.casefold() in {existing.casefold() for existing in entry['options']}:
            result['errors'].append((number, 'Answer options must be different within a question.'))
            continue
        entry['options'].append(option)

    for entry in questions.values():
        if len(entry['options']) < 2:
            result['errors'].append((entry['row'], 'Add at least two answer options for this question.'))
        elif len(entry['options']) > MAX_OPTIONS:
            result['errors'].append((entry['row'], f'Use no more than {MAX_OPTIONS} options for one question.'))
        result['questions'].append(entry)

    if not result['questions'] and not result['errors']:
        result['fatal'] = 'No questions were found in the file.'
    return result
