"""Setup views: editing a session after go-live, the spreadsheet imports, and bulk delete.

Every view here is the lecturer's own session only (``_session``), refuses a closed
session, and asks ``setup_rules`` before changing anything that grading depends on.
Imports are two steps - upload shows a preview, a second press applies it - so a
file never changes a session without being looked at first.
"""
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.access import lecturer_required

from .. import group_import, rubric_import
from ..pending_imports import GROUP_KEY, RUBRIC_KEY
from ..forms import GroupImportForm, RubricImportForm
from ..models import EvaluationScore
from ..setup_rules import is_live, live_rubric_lock, record_change, rubric_structure_lock
from .lecturer_views import _closed, _session, _setup_context, delete_groups

__all__ = [
    'assessment_setup', 'group_import_view', 'group_import_preview', 'group_import_sample',
    'groups_bulk_delete', 'rubric_import_view', 'rubric_import_preview', 'rubric_import_sample',
    'rubric_table', 'rubric_bulk_delete',
]

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
STEPS = ('basics', 'groups', 'rubric')


def _download(buffer, filename):
    response = HttpResponse(buffer.getvalue(), content_type=XLSX)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _upload_errors(request, form):
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, error)


# ----------------------------------------------------------------- setup ----

@lecturer_required
def assessment_setup(request, pk):
    """The setup screen for a draft or a live session (a closed one has none)."""
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    step = request.GET.get('step')
    return render(request, 'assessments/session_setup.html',
                  _setup_context(request, session, step if step in STEPS else 'groups'))


# ---------------------------------------------------- groups + students ----

@lecturer_required
@require_POST
def group_import_view(request, pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    form = GroupImportForm(request.POST, request.FILES)
    if not form.is_valid():
        _upload_errors(request, form)
        return redirect('assessment_groups', pk=session.pk)
    parsed = group_import.parse_groups(form.cleaned_data['file'])
    if parsed['fatal']:
        messages.error(request, parsed['fatal'])
        return redirect('assessment_groups', pk=session.pk)
    request.session[GROUP_KEY] = {'session': session.pk, 'parsed': parsed}
    return redirect('assessment_groups', pk=session.pk)


@lecturer_required
@require_http_methods(['GET', 'POST'])
def group_import_preview(request, pk):
    """Apply or drop the waiting file. The review itself is the window on the groups step."""
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    stash = request.session.get(GROUP_KEY)
    if not stash or stash['session'] != session.pk:
        messages.info(request, 'Choose a file to import first.')
        return redirect('assessment_groups', pk=session.pk)
    if request.method == 'GET':
        return redirect('assessment_groups', pk=session.pk)

    request.session.pop(GROUP_KEY, None)
    if request.POST.get('action') != 'apply':
        return redirect('assessment_groups', pk=session.pk)
    counts = group_import.apply_import(session, stash['parsed'])
    if counts['groups_created'] or counts['members_added'] or counts['groups_updated']:
        record_change(session, request.user,
                      f'Imported {counts["groups_created"]} group(s) and '
                      f'{counts["students_created"]} student(s) from a file.')
        messages.success(
            request, f'Imported {counts["groups_created"]} new group(s) and '
                     f'{counts["students_created"]} new student(s); '
                     f'{counts["members_added"]} member(s) placed in groups.')
    else:
        messages.info(request, 'Nothing new to import.')
    for name in counts['skipped'][:10]:
        messages.warning(request, f'Skipped "{name}": it had problems in the review.')
    return redirect('assessment_groups', pk=session.pk)


@lecturer_required
def group_import_sample(request, pk):
    _session(request, pk)
    return _download(group_import.sample_workbook(), 'groups-and-students-sample.xlsx')


@lecturer_required
@require_POST
def groups_bulk_delete(request, pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    ids = [int(value) for value in request.POST.getlist('group_ids') if value.isdigit()]
    groups = list(session.groups.filter(pk__in=ids))
    if not groups:
        messages.error(request, 'Select at least one group to delete.')
    else:
        delete_groups(request, session, groups)
    return redirect('assessment_groups', pk=session.pk)


# ----------------------------------------------------------------- rubric ----

def _rubric_structure_blocked(request, session):
    lock = rubric_structure_lock(session)
    if lock:
        messages.error(request, lock)
        return redirect('assessment_rubric', pk=session.pk)
    return None


@lecturer_required
@require_POST
def rubric_bulk_delete(request, pk):
    """Remove the selected rubric categories in one go (one, several or all).

    Held to the same rules as removing one: a live session's rubric is edited as a
    table so its weights keep adding up to 100, and once votes exist no category may
    be removed. All or nothing.
    """
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    lock = live_rubric_lock(session) or rubric_structure_lock(session)
    if lock:
        messages.error(request, lock)
        return redirect('assessment_rubric', pk=session.pk)
    ids = [int(value) for value in request.POST.getlist('category_ids') if value.isdigit()]
    categories = list(session.rubric_categories.filter(pk__in=ids))
    if not categories:
        messages.error(request, 'Select at least one rubric category to delete.')
        return redirect('assessment_rubric', pk=session.pk)
    names = ', '.join(f'"{c.name}"' for c in categories[:5]) + (
        f' and {len(categories) - 5} more' if len(categories) > 5 else '')
    session.rubric_categories.filter(pk__in=[c.pk for c in categories]).delete()
    record_change(session, request.user, f'Deleted {len(categories)} rubric categor'
                  f'{"y" if len(categories) == 1 else "ies"}: {names}.', notify='rubric.changed')
    messages.success(request, f'Deleted {len(categories)} rubric categor{"y" if len(categories) == 1 else "ies"}.')
    return redirect('assessment_rubric', pk=session.pk)


@lecturer_required
@require_POST
def rubric_import_view(request, pk):
    session = _session(request, pk)
    blocked = _closed(request, session) or _rubric_structure_blocked(request, session)
    if blocked:
        return blocked
    form = RubricImportForm(request.POST, request.FILES)
    if not form.is_valid():
        _upload_errors(request, form)
        return redirect('assessment_rubric', pk=session.pk)
    parsed = rubric_import.parse_rubric(form.cleaned_data['file'])
    if parsed['fatal']:
        messages.error(request, parsed['fatal'])
        return redirect('assessment_rubric', pk=session.pk)
    request.session[RUBRIC_KEY] = {
        'session': session.pk, 'rows': parsed['rows'], 'mode': form.cleaned_data['mode']}
    return redirect('assessment_rubric', pk=session.pk)


@lecturer_required
@require_http_methods(['GET', 'POST'])
def rubric_import_preview(request, pk):
    """Apply or drop the waiting file. The review itself is the window on the rubric step."""
    session = _session(request, pk)
    blocked = _closed(request, session) or _rubric_structure_blocked(request, session)
    if blocked:
        return blocked
    stash = request.session.get(RUBRIC_KEY)
    if not stash or stash['session'] != session.pk:
        messages.info(request, 'Choose a file to import first.')
        return redirect('assessment_rubric', pk=session.pk)
    if request.method == 'GET':
        return redirect('assessment_rubric', pk=session.pk)

    request.session.pop(RUBRIC_KEY, None)
    if request.POST.get('action') != 'apply':
        return redirect('assessment_rubric', pk=session.pk)
    if not rubric_import.check_import(stash['rows'], session, stash['mode'])['ok']:
        messages.error(request, 'The rubric has problems, so nothing was imported.')
        return redirect('assessment_rubric', pk=session.pk)
    created = rubric_import.apply_import(session, stash['rows'], stash['mode'])
    record_change(session, request.user, f'Imported {created} rubric category(ies) from a file.',
                  notify='rubric.changed')
    messages.success(request, f'Imported {created} rubric categor{"y" if created == 1 else "ies"}.')
    return redirect('assessment_rubric', pk=session.pk)


@lecturer_required
def rubric_import_sample(request, pk):
    _session(request, pk)
    return _download(rubric_import.sample_workbook(), 'rubric-sample.xlsx')


FIELDS = ('id', 'name', 'description', 'scope', 'weight')


def _table_rows_from_post(post):
    """The submitted rows, in order, as plain strings (so a rejected form re-renders as typed).

    ``None`` when the submission is not one of our forms: an unreadable form must
    never be taken for "delete every category".
    """
    try:
        total = min(int(post['rows-total']), 200)
    except (KeyError, ValueError):
        return None
    rows = []
    for index in range(total):
        row = {field: (post.get(f'rows-{index}-{field}') or '').strip() for field in FIELDS}
        if not any(row[f] for f in FIELDS if f != 'scope') and not row['id']:
            continue
        row['id'] = int(row['id']) if row['id'].isdigit() else None
        rows.append(row)
    return rows


@lecturer_required
@require_http_methods(['GET', 'POST'])
def rubric_table(request, pk):
    """Edit the whole rubric as one table, so the weights are judged together."""
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked

    if request.method == 'POST':
        rows = _table_rows_from_post(request.POST)
        if rows is None:
            messages.error(request, 'That form was not understood, so nothing was changed. Reload the page and try again.')
            return redirect('assessment_rubric_table', pk=session.pk)
        strict = is_live(session)
        verdict = rubric_import.check_table(rows, session, strict)
        changes_structure = bool(verdict['removed']) or any(r['id'] is None for r in verdict['rows'])
        lock = rubric_structure_lock(session) if changes_structure else None
        if verdict['ok'] and not lock:
            changed, added, removed = rubric_import.apply_table(session, verdict)
            record_change(session, request.user,
                          f'Edited the rubric ({changed} changed, {added} added, {removed} removed).',
                          notify='rubric.changed')
            messages.success(request, 'Rubric saved.')
            return redirect('assessment_rubric', pk=session.pk)
        errors = list(verdict['errors']) + ([(None, lock)] if lock else [])
    else:
        rows = [{'id': c.pk, 'name': c.name, 'description': c.description, 'scope': c.scope,
                 'weight': f'{c.weight:f}'.rstrip('0').rstrip('.')}
                for c in session.rubric_categories.all()]
        errors = []
    scored = set(EvaluationScore.objects.filter(
        rubric_category__assessment_session=session).values_list('rubric_category_id', flat=True))
    problem_rows = {number for number, _ in errors if number}
    for number, row in enumerate(rows, start=1):
        row['core_locked'] = row.get('id') in scored
        row['problem'] = number in problem_rows
    return render(request, 'assessments/rubric_table.html', {
        'session': session, 'rows': rows, 'errors': errors, 'strict': is_live(session),
        'live_mode': is_live(session), 'structure_lock': rubric_structure_lock(session)})
