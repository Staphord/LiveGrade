"""Preview-first import of a reusable Team and Training cohort roster."""
from collections import defaultdict

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max

from assessments.sheets import UNREADABLE, cell, clean, key, map_headers, read_rows, workbook_bytes

from .models import TrainingParticipant, TrainingSeries

MAX_ROWS = 500
LIMITS = {'name': 150, 'reference': 40, 'group': 100}
HEADERS = {
    'name': {'name', 'full name', 'learner', 'learner name', 'participant', 'participant name'},
    'reference': {'id', 'student id', 'learner id', 'employee id', 'reference', 'learner reference'},
    'group': {'group', 'group name', 'team', 'team name', 'cohort group'},
}


def sample_training_roster():
    return workbook_bytes(
        ['Name', 'Student ID', 'Team'],
        [['Amina Yusuf', 'EMP-001', 'Orchid'],
         ['Juma Mwakalinga', 'EMP-002', 'Orchid'],
         ['Neema Said', 'EMP-003', 'Baobab']],
        {'A': 28, 'B': 20, 'C': 24})


def parse_training_roster(upload):
    """Return normalized rows and row-level issues without changing the roster."""
    rows = read_rows(upload)
    result = {'fatal': None, 'participants': []}
    if rows is None:
        result['fatal'] = UNREADABLE
        return result
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows:
        result['fatal'] = 'The file is empty.'
        return result
    if len(rows) - 1 > MAX_ROWS:
        result['fatal'] = f'That file has more than {MAX_ROWS} learners. Split it and import in parts.'
        return result
    mapping = map_headers(rows[0], HEADERS)
    if 'name' not in mapping or 'group' not in mapping:
        result['fatal'] = 'The first row needs Name and Team (or Group) column headers.'
        return result

    for number, row in enumerate(rows[1:], start=2):
        participant = {
            'row': number,
            'display_name': clean(cell(row, mapping, 'name')),
            'reference': clean(cell(row, mapping, 'reference')),
            'group_name': clean(cell(row, mapping, 'group')),
            'issues': [], 'warnings': [], 'state': 'new',
        }
        if not participant['display_name'] and not participant['reference'] and not participant['group_name']:
            continue
        if not participant['display_name']:
            participant['issues'].append('Name is required.')
        if not participant['group_name']:
            participant['issues'].append('Team or group is required.')
        for key_name, label in (('display_name', 'Name'), ('reference', 'Reference'),
                                ('group_name', 'Team or group')):
            value = participant[key_name]
            limit = LIMITS['name' if key_name == 'display_name' else
                           'reference' if key_name == 'reference' else 'group']
            if len(value) > limit:
                participant['issues'].append(f'{label} is longer than {limit} characters.')
        result['participants'].append(participant)

    references = defaultdict(list)
    names = defaultdict(list)
    for participant in result['participants']:
        if participant['reference']:
            references[key(participant['reference'])].append(participant)
        if participant['display_name']:
            names[key(participant['display_name'])].append(participant)
    for duplicates in references.values():
        if len(duplicates) > 1:
            for participant in duplicates:
                participant['issues'].append('This reference appears more than once in the file.')
    for same_name in names.values():
        if len(same_name) > 1:
            if all(participant['reference'] for participant in same_name):
                for participant in same_name:
                    participant['warnings'].append(
                        'This name appears more than once; use Student ID when creating a demo.')
            else:
                for participant in same_name:
                    participant['issues'].append(
                        'This name is repeated. Add a unique reference for each learner, then choose Student ID when creating a demo.')
    return result


def plan_training_roster_import(series, parsed, *, organization, user):
    """Identify safe additions and duplicates before the organizer confirms."""
    scoped_series = (TrainingSeries.objects.for_organization(organization).owned_by(user)
        .filter(pk=series.pk, active=True).first())
    if scoped_series is None:
        raise PermissionDenied
    existing = list(TrainingParticipant.objects.for_organization(organization).owned_by(user)
        .filter(series=scoped_series).order_by('order', 'pk'))
    by_reference = {key(row.reference): row for row in existing if row.reference}
    by_name_group = {(key(row.display_name), key(row.group_name)): row for row in existing}
    by_name = defaultdict(list)
    for row in existing:
        by_name[key(row.display_name)].append(row)
    rows = []
    for source in parsed.get('participants', []):
        item = {**source, 'issues': list(source.get('issues', [])),
                'warnings': list(source.get('warnings', [])), 'state': 'new'}
        reference_key = key(item['reference'])
        name_key = key(item['display_name'])
        exact = by_name_group.get((name_key, key(item['group_name']))) if name_key else None
        existing_ref = by_reference.get(reference_key) if reference_key else None
        if existing_ref:
            item['state'] = 'already added'
            if (key(existing_ref.display_name), key(existing_ref.group_name)) == (
                    name_key, key(item['group_name'])):
                item['warnings'].append('This learner is already in the series; this row will be skipped.')
            else:
                item['issues'].append(
                    'This reference already belongs to a different learner or team in the series.')
        elif exact and not item['issues']:
            item['state'] = 'already added'
            item['warnings'].append('This learner is already in this team; this row will be skipped.')
        elif not reference_key and name_key and by_name.get(name_key):
            item['issues'].append(
                'A learner with this name is already in the series. Add a unique reference to distinguish them.')
        item['ready'] = not item['issues'] and item['state'] == 'new'
        rows.append(item)
    ready = [row for row in rows if row['ready']]
    return {
        'participants': rows,
        'summary': {'rows': len(rows), 'ready': len(ready),
                    'skipped': len(rows) - len(ready),
                    'teams': len({key(row['group_name']) for row in ready})},
    }


@transaction.atomic
def apply_training_roster_import(series, parsed, *, organization, user):
    """Re-plan under a series lock and atomically add only valid new learners."""
    locked_series = (TrainingSeries.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=series.pk, active=True).first())
    if locked_series is None:
        raise PermissionDenied
    plan = plan_training_roster_import(locked_series, parsed,
        organization=organization, user=user)
    next_order = (TrainingParticipant.objects.for_organization(organization).owned_by(user)
        .filter(series=locked_series).aggregate(value=Max('order'))['value'] or 0)
    additions = []
    for row in plan['participants']:
        if not row['ready']:
            continue
        next_order += 1
        participant = TrainingParticipant(
            series=locked_series, display_name=row['display_name'],
            reference=row['reference'], group_name=row['group_name'], order=next_order,
            organization_id=locked_series.organization_id, created_by=user)
        participant.full_clean()
        additions.append(participant)
    try:
        TrainingParticipant.objects.bulk_create(additions)
    except IntegrityError as exc:
        raise ValidationError('The series roster changed while importing. Review the preview and try again.') from exc
    return {'created': len(additions), 'skipped': plan['summary']['skipped'],
            'teams': plan['summary']['teams']}
