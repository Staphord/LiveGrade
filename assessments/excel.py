"""Roster import and results export (Section 15 of the spec).

Per-row validation rather than all-or-nothing: one malformed row shouldn't
block importing the other 29 students, but the lecturer needs to know which
row failed and why.
"""

from io import BytesIO

import openpyxl

from .models import GroupMembership, PresentationGroup, Student

# Header aliases accepted, matched case-insensitively after stripping.
COLUMN_ALIASES = {
    'full_name': {'name', 'full name', 'student name', 'fullname'},
    'student_id': {'registration number', 'reg no', 'reg number', 'student id',
                    'id', 'student_id', 'regno'},
    'email': {'email', 'e-mail'},
    'programme': {'programme', 'program', 'class', 'programme/class'},
}

GROUP_COLUMN_ALIASES = {
    'group': {'group', 'group name', 'group no', 'group number', 'team', 'team name'},
    'student': {'student', 'student id', 'student name', 'name', 'registration number',
                'reg no', 'reg number', 'id', 'member'},
}


def _map_headers(header_row):
    mapping = {}
    for index, cell in enumerate(header_row):
        label = (str(cell.value).strip().lower() if cell.value else '')
        for field, aliases in COLUMN_ALIASES.items():
            if label in aliases:
                mapping[field] = index
    return mapping


UNREADABLE = 'That file could not be read as an Excel workbook.'


def _open_rows(uploaded_file):
    """The sheet's rows, or None when the file is not a readable workbook.

    A corrupt or mislabelled upload raises a different exception from each
    layer (zip, XML, openpyxl); all of them mean the same thing to the person
    who chose the file.
    """
    try:
        workbook = openpyxl.load_workbook(uploaded_file, read_only=True, data_only=True)
        return list(workbook.active.iter_rows())
    except Exception:
        return None


def import_roster(assessment_session, uploaded_file):
    """Returns {'created': int, 'errors': [(row_number, message), ...]}."""
    rows = _open_rows(uploaded_file)
    if rows is None:
        return {'created': 0, 'errors': [(0, UNREADABLE)]}
    if not rows:
        return {'created': 0, 'errors': [(0, 'The file is empty.')]}

    mapping = _map_headers(rows[0])
    if 'full_name' not in mapping and 'student_id' not in mapping:
        return {'created': 0, 'errors': [(1, 'Could not find a name or student ID column. '
                                              'Expected headers like "Name" and "Registration Number".')]}

    created = 0
    errors = []
    for row_number, row in enumerate(rows[1:], start=2):
        values = {field: (row[idx].value if idx < len(row) else None)
                  for field, idx in mapping.items()}
        full_name = str(values.get('full_name') or '').strip()
        student_id = str(values.get('student_id') or '').strip()
        if not full_name and not student_id:
            continue  # blank row, skip silently
        student = Student(
            assessment_session=assessment_session,
            full_name=full_name,
            student_id=student_id,
            email=str(values.get('email') or '').strip(),
            programme=str(values.get('programme') or '').strip(),
        )
        try:
            student.full_clean()
        except Exception as exc:
            errors.append((row_number, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)))
            continue
        student.save()
        created += 1

    return {'created': created, 'errors': errors}


def _map_group_headers(header_row):
    mapping = {}
    for index, cell in enumerate(header_row):
        label = (str(cell.value).strip().lower() if cell.value else '')
        for field, aliases in GROUP_COLUMN_ALIASES.items():
            if label in aliases:
                mapping[field] = index
    return mapping


def _find_student(assessment_session, identifier, roster_by_id, roster_by_name):
    """Match a typed value against the roster already uploaded for this
    session — by student ID first, then full name, whichever exists — so a
    lecturer's groups sheet works whether it lists IDs or names.
    """
    identifier = (identifier or '').strip()
    if not identifier:
        return None
    return roster_by_id.get(identifier.lower()) or roster_by_name.get(identifier.lower())


def import_groups(assessment_session, uploaded_file):
    """Import group assignments, verified against the roster already in this
    session (Student.objects for this session — never created here).

    Expected columns: Group, and a Student column (ID or name). A group is
    only created once *every* member on its rows checks out; a group with
    even one problem row is flagged and skipped entirely rather than
    created half-populated. Two kinds of problem, both caught here:

    - the student isn't on the roster at all;
    - the student is already assigned elsewhere — to a different group
      already in the database, or to a different group earlier in this
      same file — since a student belongs to exactly one group per
      session (GroupMembership enforces this at the model level too; this
      catches it earlier, with a message that names the conflicting group).

    Returns:
        {
          'groups_created': int, 'members_added': int,
          'flagged': [{'group': name, 'issues': [str, ...]}],
          'row_errors': [(row_number, message)],
        }
    """
    rows = _open_rows(uploaded_file)
    if rows is None:
        return {'groups_created': 0, 'members_added': 0, 'flagged': [],
                'row_errors': [(0, UNREADABLE)]}
    if not rows:
        return {'groups_created': 0, 'members_added': 0, 'flagged': [], 'row_errors': [(0, 'The file is empty.')]}

    mapping = _map_group_headers(rows[0])
    if 'group' not in mapping or 'student' not in mapping:
        return {'groups_created': 0, 'members_added': 0, 'flagged': [], 'row_errors': [
            (1, 'Could not find a "Group" column and a "Student" (ID or name) column.')]}

    roster_by_id = {s.student_id.strip().lower(): s
                    for s in assessment_session.students.all() if s.student_id}
    roster_by_name = {s.full_name.strip().lower(): s
                       for s in assessment_session.students.all() if s.full_name}

    # A student already in a group in the database, from before this import.
    existing_group_name_by_student = {
        m.student_id: m.group.name
        for m in GroupMembership.objects
            .filter(group__assessment_session=assessment_session)
            .select_related('group')
    }

    # group_name -> {'matched': [Student, ...], 'issues': [str, ...]}
    by_group = {}
    row_errors = []
    for row_number, row in enumerate(rows[1:], start=2):
        group_cell = row[mapping['group']].value if mapping['group'] < len(row) else None
        student_cell = row[mapping['student']].value if mapping['student'] < len(row) else None
        group_name = str(group_cell or '').strip()
        identifier = str(student_cell or '').strip()
        if not group_name and not identifier:
            continue  # blank row, skip silently
        if not group_name:
            row_errors.append((row_number, 'Missing group name.'))
            continue
        if not identifier:
            row_errors.append((row_number, f'Missing student for group "{group_name}".'))
            continue

        entry = by_group.setdefault(group_name, {'matched': [], 'issues': []})
        student = _find_student(assessment_session, identifier, roster_by_id, roster_by_name)
        if student is None:
            entry['issues'].append(f'"{identifier}" is not on the roster')
            continue

        existing_group = existing_group_name_by_student.get(student.pk)
        if existing_group and existing_group != group_name:
            entry['issues'].append(f'{student} is already in "{existing_group}"')
            continue

        entry['matched'].append(student)

    # A student listed under two different groups within this same file —
    # every group they appear in is invalid, not just the second one found.
    group_names_by_student = {}
    for group_name, entry in by_group.items():
        for student in entry['matched']:
            group_names_by_student.setdefault(student.pk, set()).add(group_name)
    for student_pk, group_names in group_names_by_student.items():
        if len(group_names) < 2:
            continue
        student = next(s for entry in by_group.values() for s in entry['matched'] if s.pk == student_pk)
        others = sorted(group_names)
        for group_name in group_names:
            other_names = ', '.join(f'"{n}"' for n in others if n != group_name)
            by_group[group_name]['issues'].append(
                f'{student} is also listed under {other_names} in this file')

    groups_created = 0
    members_added = 0
    flagged = []
    for group_name, entry in by_group.items():
        if entry['issues']:
            flagged.append({'group': group_name, 'issues': entry['issues']})
            continue

        existing_group = PresentationGroup.objects.filter(
            assessment_session=assessment_session, name=group_name).first()
        if existing_group and existing_group.max_size is not None:
            # Re-importing into a group a lecturer already capped — new
            # members here must still fit, same as the manual "add" path.
            incoming_new = [s for s in entry['matched'] if s.pk not in existing_group.member_ids()]
            room = existing_group.room_left()
            if len(incoming_new) > room:
                flagged.append({'group': group_name, 'issues': [
                    f'"{group_name}" allows {existing_group.max_size} member(s) and already has '
                    f'{existing_group.member_count()} — only {room} more would fit, but '
                    f'{len(incoming_new)} new student(s) were listed']})
                continue

        group, created = PresentationGroup.objects.get_or_create(
            assessment_session=assessment_session, name=group_name,
            defaults={'order': assessment_session.groups.count()})
        if created:
            groups_created += 1
        for student in entry['matched']:
            _, added = GroupMembership.objects.get_or_create(group=group, student=student)
            if added:
                members_added += 1

    return {
        'groups_created': groups_created, 'members_added': members_added,
        'flagged': flagged, 'row_errors': row_errors,
    }


PARTICIPATION_STATUS_LABELS = {
    'full': 'Fully graded', 'partial': 'Partially graded', 'none': 'Did not participate',
}


def export_results(assessment_session, results, participation_rows):
    """Returns an in-memory .xlsx workbook of participation + results.

    `participation_rows` is the same unpaginated per-student row list the
    Participation page itself renders (`_participation_rows`), so this
    sheet always matches what a lecturer sees there instead of carrying
    its own, weaker notion of "participation" — the four-status detail
    (status/graded/eligible/progress) used to live only in a since-removed
    Participation-tab export; it's centralized here now."""
    workbook = openpyxl.Workbook()

    participation_sheet = workbook.active
    participation_sheet.title = 'Participation'
    participation_sheet.append([
        'Student', 'Student ID', 'Group', 'Joined', 'Participation Status',
        'Groups Graded', 'Groups Eligible', 'Grading Progress %',
    ])
    for row in participation_rows:
        student = row['student']
        participation_sheet.append([
            student.full_name, student.student_id,
            row['own_group'].name if row['own_group'] else '',
            'Yes' if row['joined'] else 'No',
            PARTICIPATION_STATUS_LABELS.get(row['status'], row['status']),
            row['graded_count'], row['eligible_count'], row['progress_pct'],
        ])

    results_sheet = workbook.create_sheet('Results')
    results_sheet.append(['Student', 'Group', 'Group %', 'Individual %', 'Penalty %', 'Final %'])
    for row in results['student_rows']:
        results_sheet.append([
            row['student'].full_name, row['group'].name,
            float(row['group_percent']) if row['group_percent'] is not None else '',
            float(row['individual_percent']) if row['individual_percent'] is not None else '',
            row.get('penalty', 0),
            float(row['final_percent']) if row['final_percent'] is not None else '',
        ])

    group_sheet = workbook.create_sheet('Group scores')
    group_sheet.append(['Group', 'Group %'])
    for row in results['group_rows']:
        group_sheet.append([row['group'].name,
                             float(row['percent']) if row['percent'] is not None else ''])

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
