"""One spreadsheet for groups and students together.

One row is one group: its name, then its members in one cell separated by commas,
optionally their IDs in the next column in the same order, and optionally a topic
and a presentation location. Students are created from the file, so there is no
separate roster to import first.

Three steps, so nothing is written by surprise:

1. ``parse_groups`` reads the file into plain data (safe to keep in the session).
2. ``plan_import`` compares it with what the session already holds and says, per
   group, what would happen and what is wrong. It writes nothing.
3. ``apply_import`` runs the plan again - the roster may have changed since the
   preview - and writes every group that has no problem, in one transaction.
   A group with a problem is skipped whole, never half-added.
"""
from collections import defaultdict

from django.db import models, transaction

from .models import AssessmentSession, GroupMembership, PresentationGroup, Student
from .sheets import (
    UNREADABLE, cell, clean, key, map_headers, read_rows, split_people, workbook_bytes,
)

MAX_ROWS = 500

HEADERS = {
    'group': {'group', 'group name', 'group no', 'group number', 'team', 'team name'},
    'names': {'name', 'names', 'member', 'members', 'student', 'students', 'student name',
              'student names', 'full name', 'full names'},
    'ids': {'student id', 'student ids', 'id', 'ids', 'reg no', 'reg number', 'registration number',
            'registration numbers', 'student number', 'student numbers'},
    'topic': {'topic', 'project', 'project title', 'project topic'},
    'location': {'presentation location', 'location', 'room', 'venue'},
}

LIMITS = {'group': 100, 'topic': 200, 'location': 120, 'name': 150, 'id': 40}

SAMPLE_HEADERS = ['Group Name', 'Name', 'Student ID', 'Topic', 'Presentation Location']
SAMPLE_ROWS = [
    ['Aurora', 'Ann Lee, Bo Ng, Cy Oh', 'A101, A102, A103', 'Smart campus map', 'Room 1'],
    ['Meridian', 'Di Park, Ed Roy', 'B201, B202', 'Library kiosk app', 'Room 1'],
    ['Horizon', 'Flo Kim, Gus Ito, Hal Yu', 'C301, C302, C303', '', ''],
]


def sample_workbook():
    return workbook_bytes(SAMPLE_HEADERS, SAMPLE_ROWS, {'A': 18, 'B': 44, 'C': 26, 'D': 30, 'E': 24})


# ---------------------------------------------------------------- 1. read ----

def parse_groups(uploaded_file):
    """``{'fatal': str|None, 'groups': [...], 'problems': [(row, message)]}``."""
    result = {'fatal': None, 'groups': [], 'problems': []}
    rows = read_rows(uploaded_file)
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result
    if len(rows) - 1 > MAX_ROWS:
        result['fatal'] = f'That file has more than {MAX_ROWS} rows. Split it and import in parts.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'group' not in mapping or 'names' not in mapping:
        result['fatal'] = ('Could not find the Group and Name columns. The first row must have '
                           'headers like "Group Name" and "Name".')
        return result

    by_group = {}
    for number, row in enumerate(rows[1:], start=2):
        group_name = clean(cell(row, mapping, 'group'))
        names = split_people(cell(row, mapping, 'names'))
        ids = split_people(cell(row, mapping, 'ids'))
        topic = clean(cell(row, mapping, 'topic'))
        location = clean(cell(row, mapping, 'location'))
        if not group_name:
            if names or ids:
                result['problems'].append((number, 'Members are listed but the group name is empty.'))
            continue
        entry = by_group.get(key(group_name))
        if entry is None:
            entry = {'name': group_name, 'topic': '', 'location': '', 'rows': [], 'members': [],
                     'issues': [], 'warnings': []}
            by_group[key(group_name)] = entry
            result['groups'].append(entry)
        entry['rows'].append(number)
        entry['topic'] = entry['topic'] or topic
        entry['location'] = entry['location'] or location
        _add_members(entry, number, names, ids)

    for entry in result['groups']:
        _check_lengths(entry)
        if not entry['members']:
            entry['warnings'].append('No members listed: the group will be created empty.')
    return result


def _add_members(entry, row_number, names, ids):
    if ids and len(ids) != len(names):
        entry['issues'].append(
            f'Row {row_number} lists {len(names)} name(s) but {len(ids)} ID(s); they must '
            f'line up one to one.')
        ids = []
    seen = {(key(m['name']), key(m['student_id'])) for m in entry['members']}
    for index, name in enumerate(names):
        student_id = ids[index] if ids else ''
        marker = (key(name), key(student_id))
        if marker in seen:
            entry['warnings'].append(f'"{name}" is listed twice; counted once.')
            continue
        seen.add(marker)
        entry['members'].append({'name': name, 'student_id': student_id})


def _check_lengths(entry):
    for label, value, limit in (('Group name', entry['name'], LIMITS['group']),
                                ('Topic', entry['topic'], LIMITS['topic']),
                                ('Location', entry['location'], LIMITS['location'])):
        if len(value) > limit:
            entry['issues'].append(f'{label} is longer than {limit} characters.')
    for member in entry['members']:
        if len(member['name']) > LIMITS['name']:
            entry['issues'].append(f'"{member["name"][:30]}..." is longer than {LIMITS["name"]} characters.')
        if len(member['student_id']) > LIMITS['id']:
            entry['issues'].append(f'ID "{member["student_id"][:20]}..." is longer than {LIMITS["id"]} characters.')


# --------------------------------------------------------------- 2. plan ----

def plan_import(session, parsed):
    """What importing ``parsed`` into ``session`` would do. Writes nothing."""
    roster = list(session.students.all())
    by_id = {key(s.student_id): s for s in roster if s.student_id}
    by_name = defaultdict(list)
    for student in roster:
        by_name[key(student.full_name)].append(student)
    existing = {key(g.name): g for g in session.groups.all()}
    placed = {m.student_id: m.group for m in
              GroupMembership.objects.filter(group__assessment_session=session).select_related('group')}
    needs_id = session.identify_by == AssessmentSession.IdentifyBy.STUDENT_ID

    groups = []
    seen_in = defaultdict(set)
    new_names = defaultdict(set)
    for source in parsed['groups']:
        entry = {**source, 'issues': list(source['issues']), 'warnings': list(source['warnings']),
                 'members': [], 'group': existing.get(key(source['name']))}
        group = entry['group']
        entry['is_new'] = group is None
        for raw in source['members']:
            member = _match_member(raw, by_id, by_name, entry)
            student = member['student']
            identity = ('s', student.pk) if student else ('i', key(raw['student_id'])) if raw['student_id'] \
                else ('n', key(raw['name']))
            seen_in[identity].add(source['name'])
            member['identity'] = identity
            if student is not None:
                home = placed.get(student.pk)
                if home is not None and group is not None and home.pk == group.pk:
                    member['state'] = 'member'
                elif home is not None:
                    entry['issues'].append(f'{raw["name"]} is already in "{home.name}".')
            elif needs_id and not raw['student_id']:
                entry['issues'].append(
                    f'{raw["name"]} has no Student ID, and this session identifies students by ID.')
            if student is None and not needs_id:
                new_names[key(raw['name'])].add(identity)
            entry['members'].append(member)
        if group is not None:
            _check_existing_group(entry, group)
        groups.append(entry)

    _flag_repeats(groups, seen_in)
    if not needs_id:
        _flag_same_names(groups, new_names, by_name)
    for entry in groups:
        entry['ready'] = not entry['issues']
    return {'groups': groups, 'problems': parsed['problems'], 'summary': _summary(groups)}


def _match_member(raw, by_id, by_name, entry):
    member = {**raw, 'state': 'new', 'student': None, 'fill_id': False}
    if raw['student_id']:
        student = by_id.get(key(raw['student_id']))
        if student is None:
            blank = [s for s in by_name.get(key(raw['name']), []) if not s.student_id]
            if len(blank) == 1:
                student, member['fill_id'] = blank[0], True
        member['student'] = student
    else:
        candidates = by_name.get(key(raw['name']), [])
        if len(candidates) == 1:
            member['student'] = candidates[0]
        elif len(candidates) > 1:
            entry['issues'].append(
                f'"{raw["name"]}" matches {len(candidates)} students already in the session; '
                f'give a Student ID to tell them apart.')
    if member['student'] is not None:
        member['state'] = 'matched'
    return member


def _check_existing_group(entry, group):
    if group.turns.exists():
        entry['issues'].append(f'"{group.name}" has presented or is presenting, so its members are fixed.')
        return
    joining = [m for m in entry['members'] if m['state'] != 'member']
    room = group.room_left()
    if room is not None and len(joining) > room:
        entry['issues'].append(
            f'"{group.name}" allows {group.max_size} member(s) and has {group.member_count()}; '
            f'only {room} more fit, but {len(joining)} were listed.')


def _flag_repeats(groups, seen_in):
    for entry in groups:
        for member in entry['members']:
            others = sorted(seen_in[member['identity']] - {entry['name']})
            if others:
                entry['issues'].append(
                    f'{member["name"]} is also listed under '
                    f'{", ".join(chr(34) + o + chr(34) for o in others)} in this file.')


def _flag_same_names(groups, new_names, by_name):
    """Joining by name needs every name to point at one person."""
    for entry in groups:
        for member in entry['members']:
            if member['student'] is not None:
                continue
            clash = len(new_names[key(member['name'])]) > 1 or bool(by_name.get(key(member['name'])))
            if clash:
                entry['issues'].append(
                    f'"{member["name"]}" would match more than one student when joining by name; '
                    f'make the names different, or switch the session to join by Student ID.')


def _summary(groups):
    ready = [g for g in groups if g['ready']]
    members = [m for g in ready for m in g['members'] if m['state'] != 'member']
    return {
        'groups': len(groups), 'ready': len(ready), 'skipped': len(groups) - len(ready),
        'new_groups': sum(1 for g in ready if g['is_new']),
        'new_students': sum(1 for m in members if m['student'] is None),
        'matched_students': sum(1 for m in members if m['student'] is not None),
    }


# -------------------------------------------------------------- 3. apply ----

@transaction.atomic
def apply_import(session, parsed):
    """Write every group the plan found no problem with. Returns the counts."""
    plan = plan_import(session, parsed)
    counts = {'groups_created': 0, 'groups_updated': 0, 'students_created': 0,
              'members_added': 0, 'skipped': [g['name'] for g in plan['groups'] if not g['ready']]}
    next_order = (session.groups.aggregate(models.Max('order'))['order__max'] or 0)
    for entry in plan['groups']:
        if not entry['ready']:
            continue
        group = entry['group']
        if group is None:
            next_order += 1
            group = PresentationGroup.objects.create(
                assessment_session=session, name=entry['name'], topic=entry['topic'],
                location=entry['location'], order=next_order)
            counts['groups_created'] += 1
        else:
            changed = [field for field in ('topic', 'location')
                       if entry[field] and entry[field] != getattr(group, field)]
            for field in changed:
                setattr(group, field, entry[field])
            if changed:
                group.save(update_fields=changed)
                counts['groups_updated'] += 1
        for member in entry['members']:
            if member['state'] == 'member':
                continue
            student = member['student']
            if student is None:
                student = Student.objects.create(
                    assessment_session=session, full_name=member['name'], student_id=member['student_id'])
                counts['students_created'] += 1
            elif member['fill_id']:
                student.student_id = member['student_id']
                student.save(update_fields=['student_id'])
            GroupMembership.objects.create(group=group, student=student)
            counts['members_added'] += 1
    return counts
