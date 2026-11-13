"""Preview-first spreadsheet imports for the Co-Assessment setup steps.

Rubric, presenting groups and panel examiners each supply `parse`, `plan` and
`apply` for `embedded_import.handle_post`. Parsing is read-only; apply rechecks
the plan so a stale preview writes nothing.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import secrets

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, workbook_bytes

from .coassessment_operations import apply_parsed_roster, parse_roster_spreadsheet
from .models import CoAssessmentCriterion, CoAssessmentExaminer, CoAssessmentSession
from .showcase_import import MAX_UPLOAD_BYTES, TOO_MANY_PHYSICAL_ROWS, _read_bounded_rows

MAX_CRITERIA = 50
MAX_GROUPS = 100
MAX_MEMBERS_PER_GROUP = 50
MAX_EXAMINERS = 100

RUBRIC_COLUMNS = ['Criterion', 'Scope', 'Weight', 'Description']
ROSTER_COLUMNS = ['Group', 'Topic', 'Students']
EXAMINER_COLUMNS = ['Name', 'Email', 'Chair']

RUBRIC_HEADERS = {
    'name': {'criterion', 'criterion name', 'name'},
    'scope': {'scope', 'scoring scope', 'applies to'},
    'weight': {'weight', 'weight (%)', 'criterion weight'},
    'description': {'description', 'guidance', 'what to assess'},
}
EXAMINER_HEADERS = {
    'name': {'name', 'examiner', 'full name', 'examiner name'},
    'email': {'email', 'email address'},
    'chair': {'chair', 'panel chair', 'is chair'},
}
SCOPES = {
    'group': CoAssessmentCriterion.Scope.GROUP, 'project': CoAssessmentCriterion.Scope.GROUP,
    'group project': CoAssessmentCriterion.Scope.GROUP, 'team': CoAssessmentCriterion.Scope.GROUP,
    'individual': CoAssessmentCriterion.Scope.INDIVIDUAL,
    'individual student': CoAssessmentCriterion.Scope.INDIVIDUAL,
    'student': CoAssessmentCriterion.Scope.INDIVIDUAL,
}
YES = {'yes', 'y', 'true', '1', 'chair'}
NO = {'', 'no', 'n', 'false', '0'}


def _read(upload, limit, noun):
    """Rows from a worksheet (header first), or (None, fatal message)."""
    if getattr(upload, 'size', 0) > MAX_UPLOAD_BYTES:
        return None, 'The spreadsheet must be 10 MB or smaller.'
    rows = _read_bounded_rows(upload)
    if rows is None:
        return None, UNREADABLE
    if rows is TOO_MANY_PHYSICAL_ROWS:
        return None, 'The worksheet exceeds the physical row limit.'
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        return None, 'The file is empty.'
    if len(rows) - 1 > limit:
        return None, f'That file has more than {limit} {noun}.'
    return rows, None


def _failed(columns, message):
    return {'columns': columns, 'rows': [], 'errors': [(None, message)], 'ok': False, 'summary': ''}


def _is_formula(*values):
    return any(str(value).startswith('=') for value in values)


# ---------------------------------------------------------------- rubric

def rubric_sample():
    return workbook_bytes(RUBRIC_COLUMNS, [
        ['Content', 'Group', 40, 'Accuracy and depth of the material'],
        ['Delivery', 'Group', 30, 'Clarity, pacing and use of visuals'],
        ['Individual contribution', 'Individual', 30, 'Each member speaks and answers questions'],
    ], {'A': 26, 'B': 14, 'C': 10, 'D': 50})


def parse_rubric(upload):
    result = {'fatal': None, 'rows': []}
    rows, fatal = _read(upload, MAX_CRITERIA, 'criteria')
    if fatal:
        result['fatal'] = fatal
        return result
    mapping = map_headers(rows[0], RUBRIC_HEADERS)
    if 'name' not in mapping or 'weight' not in mapping:
        result['fatal'] = 'The first row must include Criterion and Weight headers.'
        return result
    for number, source in enumerate(rows[1:], start=2):
        name = clean(cell(source, mapping, 'name'))
        description = clean(cell(source, mapping, 'description'))
        raw_scope = clean(cell(source, mapping, 'scope'))
        row = {'row': number, 'name': name, 'description': description, 'errors': []}
        scope = SCOPES.get(key(raw_scope)) if raw_scope else CoAssessmentCriterion.Scope.GROUP
        if scope is None:
            row['errors'].append('Scope must be Group or Individual.')
            scope = CoAssessmentCriterion.Scope.GROUP
        row['scope'] = scope
        try:
            weight = Decimal(str(cell(source, mapping, 'weight')))
            if (not weight.is_finite() or not Decimal('0.01') <= weight <= Decimal('100')
                    or weight.as_tuple().exponent < -2):
                raise InvalidOperation
            row['weight'] = str(weight)
        except (InvalidOperation, TypeError, ValueError):
            row['weight'] = '0'
            row['errors'].append('Weight must be a number from 0.01 to 100 with at most two decimal places.')
        if not name:
            row['errors'].append('Criterion name is required.')
        if len(name) > 140:
            row['errors'].append('Criterion name is longer than 140 characters.')
        if len(description) > 1000:
            row['errors'].append('Description is longer than 1000 characters.')
        if _is_formula(name, description):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        result['rows'].append(row)
    return result


def plan_rubric(session, parsed):
    """Describe a replace-the-rubric apply; the weights must total 100%."""
    if parsed.get('fatal'):
        return _failed(RUBRIC_COLUMNS, parsed['fatal'])
    seen = {}
    rows = []
    for source in parsed['rows']:
        errors = list(source['errors'])
        name_key = key(source['name'])
        if source['name'] and name_key in seen:
            errors.append(f'This criterion is repeated from row {seen[name_key]}.')
        seen.setdefault(name_key, source['row'])
        scope_label = CoAssessmentCriterion.Scope(source['scope']).label
        rows.append({**source, 'errors': errors,
                     'cells': [source['name'] or '-', scope_label, f"{source['weight']}%",
                               source['description'] or '-']})
    errors = [(row['row'], message) for row in rows for message in row['errors']]
    total = sum((Decimal(row['weight']) for row in rows), Decimal('0'))
    if not rows:
        errors.append((None, 'The file has no criteria rows.'))
    elif total != 100:
        errors.append((None, f'Criterion weights must total exactly 100%; this file totals {total}%.'))
    replaces = session.criteria.count()
    summary = f'{len(rows)} criteri{"on" if len(rows) == 1 else "a"} will replace the current rubric.'
    if replaces:
        summary += f' The {replaces} existing criteri{"on" if replaces == 1 else "a"} will be removed.'
    return {'columns': RUBRIC_COLUMNS, 'rows': rows, 'errors': errors, 'ok': not errors,
            'summary': summary}


@transaction.atomic
def apply_rubric(session, parsed, *, organization, user):
    locked = (CoAssessmentSession.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=session.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != CoAssessmentSession.Status.DRAFT:
        raise ValidationError('The rubric can only be replaced before the session goes live.')
    plan = plan_rubric(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The rubric import has errors. Review the preview before applying it.')
    locked.criteria.all().delete()
    created = []
    for order, row in enumerate(plan['rows'], start=1):
        criterion = CoAssessmentCriterion(
            session=locked, name=row['name'], description=row['description'], scope=row['scope'],
            weight=Decimal(row['weight']), order=order, organization_id=locked.organization_id,
            created_by=user)
        criterion.full_clean()
        criterion.save(force_insert=True)
        created.append(criterion)
    return created


# ---------------------------------------------------------------- roster

def parse_roster(upload):
    if getattr(upload, 'size', 0) > MAX_UPLOAD_BYTES:
        return {'fatal': 'The spreadsheet must be 10 MB or smaller.', 'groups': []}
    return parse_roster_spreadsheet(upload)


def plan_roster(session, parsed):
    """Describe a merge: new groups are added, same-named groups gain students."""
    if parsed.get('fatal'):
        return _failed(ROSTER_COLUMNS, parsed['fatal'])
    existing_groups = {key(name) for name in session.groups.values_list('name', flat=True)}
    seen_ids = {}
    rows = []
    for number, group in enumerate(parsed['groups'], start=2):
        errors = []
        if len(group['name']) > 140:
            errors.append('Group name is longer than 140 characters.')
        if len(group.get('topic', '')) > 255:
            errors.append('Topic is longer than 255 characters.')
        if len(group['members']) > MAX_MEMBERS_PER_GROUP:
            errors.append(f'A group can have at most {MAX_MEMBERS_PER_GROUP} students.')
        if _is_formula(group['name'], group.get('topic', ''),
                       *[member['full_name'] for member in group['members']]):
            errors.append('Formula cells are not accepted; replace them with plain text values.')
        for member in group['members']:
            student_id = member['student_id']
            if len(member['full_name']) > 150 or len(student_id) > 40:
                errors.append('A student name or ID is too long.')
            if student_id:
                other = seen_ids.setdefault(key(student_id), group['name'])
                if other != group['name']:
                    errors.append(f'Student ID {student_id} is also listed under {other}.')
        names = ', '.join(member['full_name'] for member in group['members']) or 'No students'
        note = 'Adds students to the existing group' if key(group['name']) in existing_groups else ''
        rows.append({'row': number, 'errors': errors, 'group': group,
                     'cells': [group['name'], group.get('topic') or '-', names if not note else f'{names} ({note})']})
    errors = [(row['row'], message) for row in rows for message in row['errors']]
    if not rows:
        errors.append((None, 'The file has no group rows.'))
    elif len(rows) > MAX_GROUPS:
        errors.append((None, f'That file has more than {MAX_GROUPS} groups.'))
    students = sum(len(row['group']['members']) for row in rows)
    return {'columns': ROSTER_COLUMNS, 'rows': rows, 'errors': errors, 'ok': not errors,
            'summary': f'{len(rows)} group{"" if len(rows) == 1 else "s"} and {students} '
                       f'student{"" if students == 1 else "s"} will be added. Existing groups stay unchanged.'}


@transaction.atomic
def apply_roster(session, parsed, *, organization, user):
    locked = (CoAssessmentSession.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=session.pk).first())
    if locked is None:
        raise PermissionDenied
    plan = plan_roster(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The roster import has errors. Review the preview before applying it.')
    apply_parsed_roster(locked, [row['group'] for row in plan['rows']], user=user)
    return len(plan['rows'])


# -------------------------------------------------------------- examiners

def examiner_sample():
    return workbook_bytes(EXAMINER_COLUMNS, [
        ['Dr Amara Okoye', 'amara.okoye@example.edu', 'Yes'],
        ['Prof Ben Carter', 'ben.carter@example.edu', 'No'],
    ], {'A': 26, 'B': 34, 'C': 10})


def parse_examiners(upload):
    result = {'fatal': None, 'rows': []}
    rows, fatal = _read(upload, MAX_EXAMINERS, 'examiners')
    if fatal:
        result['fatal'] = fatal
        return result
    mapping = map_headers(rows[0], EXAMINER_HEADERS)
    if 'name' not in mapping or 'email' not in mapping:
        result['fatal'] = 'The first row must include Name and Email headers.'
        return result
    for number, source in enumerate(rows[1:], start=2):
        name = clean(cell(source, mapping, 'name'))
        email = clean(cell(source, mapping, 'email')).lower()
        raw_chair = key(clean(cell(source, mapping, 'chair')))
        row = {'row': number, 'name': name, 'email': email, 'errors': []}
        row['is_chair'] = raw_chair in YES
        if raw_chair not in YES | NO:
            row['errors'].append('Chair must be Yes or No.')
        if not name:
            row['errors'].append('Examiner name is required.')
        if len(name) > 120:
            row['errors'].append('Examiner name is longer than 120 characters.')
        try:
            validate_email(email)
        except ValidationError:
            row['errors'].append('Enter a valid email address.')
        if _is_formula(name, email):
            row['errors'].append('Formula cells are not accepted; replace them with plain text values.')
        result['rows'].append(row)
    return result


def plan_examiners(session, parsed):
    if parsed.get('fatal'):
        return _failed(EXAMINER_COLUMNS, parsed['fatal'])
    existing = {key(email) for email in session.examiners.values_list('email', flat=True)}
    seen = {}
    rows = []
    for source in parsed['rows']:
        errors = list(source['errors'])
        email_key = key(source['email'])
        if source['email'] and email_key in existing:
            errors.append('This examiner is already invited to the session.')
        if source['email'] and email_key in seen:
            errors.append(f'This email is repeated from row {seen[email_key]}.')
        seen.setdefault(email_key, source['row'])
        rows.append({**source, 'errors': errors,
                     'cells': [source['name'] or '-', source['email'] or '-',
                               'Yes' if source['is_chair'] else 'No']})
    errors = [(row['row'], message) for row in rows for message in row['errors']]
    if not rows:
        errors.append((None, 'The file has no examiner rows.'))
    return {'columns': EXAMINER_COLUMNS, 'rows': rows, 'errors': errors, 'ok': not errors,
            'summary': f'{len(rows)} examiner{"" if len(rows) == 1 else "s"} will be invited by email.'}


@transaction.atomic
def apply_examiners(session, parsed, *, organization, user):
    """Invite the reviewed examiners; returns them so the caller can email links."""
    locked = (CoAssessmentSession.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=session.pk).first())
    if locked is None:
        raise PermissionDenied
    plan = plan_examiners(locked, parsed)
    if not plan['ok']:
        raise ValidationError('The examiner import has errors. Review the preview before applying it.')
    created = []
    for row in plan['rows']:
        token = secrets.token_urlsafe(32)
        examiner = CoAssessmentExaminer(
            session=locked, display_name=row['name'], email=row['email'], is_chair=row['is_chair'],
            invite_token=token, token_digest=hashlib.sha256(token.encode('utf-8')).hexdigest(),
            organization_id=locked.organization_id, created_by=user)
        examiner.full_clean()
        examiner.save(force_insert=True)
        created.append(examiner)
    return created
