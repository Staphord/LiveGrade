import re
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.http import Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.access import active_organization, can_oversee, in_public_workspace, lecturer_required
from accounts.context import CLASSROOM_WALKTHROUGHS_KEY
from accounts.models import User
from portal.stepper import build_steps

from ..duplication import duplicate_session
from ..excel import export_results
from ..reports import export_results_csv
from ..level_descriptors import format_descriptors
from ..forms import (
    AssessmentSessionForm, DuplicateSessionForm, GroupImportForm, PresentationGroupForm,
    RubricCategoryForm, RubricImportForm, StudentForm, TemplateChoiceForm,
)
from ..models import (
    TOTAL_WEIGHT, AssessmentSession, Evaluation, GroupMembership, ParticipationRecord,
    PresentationGroup, PresentationTurn, RubricCategory, Student,
)
from ..session_templates import blueprint, save_draft_from_template, starters_for, template_queryset
from ..setup_rules import (
    annotate_groups, closed_reason, group_delete_lock, group_members_lock, is_live,
    live_rubric_lock, record_change, student_delete_lock,
)
from ..pagination import PAGE_SIZE, PAGE_SIZES, paginate
from ..pending_imports import pending_group_import, pending_rubric_import
from ..qr import join_url, qr_png
from ..weight_pie import weight_pie
from ..realtime import broadcast_session_event
from ..scoring import group_score_percents, group_vote_counts, session_results, turn_vote_counts
from ..transfers import TransferError, eligible_recipients, transfer_session
from ..turns import (
    activate_turn, adjust_timer, ensure_turn_progressed, open_voting, pause_timer, resume_timer,
)
from ..turns import close_turn as close_turn_service

__all__ = [
    'assessment_dashboard', 'assessment_session_list', 'assessment_session_create', 'assessment_session_edit',
    'assessment_session_detail', 'assessment_session_duplicate',
    'assessment_session_delete', 'assessment_session_bulk_delete', 'assessment_live_state', 'roster', 'roster_search', 'roster_edit',
    'roster_delete',
    'rubric', 'rubric_edit', 'rubric_delete', 'groups', 'group_edit',
    'group_members', 'group_delete',
    'go_live', 'activate_group', 'set_transition_gap', 'set_default_turn_seconds',
    'set_default_presentation_seconds', 'set_designated_next_group', 'toggle_pause', 'toggle_joining',
    'adjust_turn_timer', 'close_turn', 'close_session',
    'toggle_public_results',
    'participation', 'participation_refresh', 'results', 'results_refresh',
    'results_export', 'results_export_csv', 'session_qr', 'session_transfer',
    'assessment_walkthrough_start', 'assessment_walkthrough_dismiss',
]


def _session(request, pk, oversight=False, lock=False):
    """The session, if it belongs to this lecturer in the active organization.

    Both conditions, every time: a session is private to the person who created
    it, so another lecturer - or an admin - in the same organization gets the
    same 404 as for an id that does not exist, which confirms nothing.

    ``oversight=True`` is for the few read-only pages (results, participation)
    and the hand-over, which somebody DevPerf has given the oversee permission
    may use on any session in the organization. It is opted into per view, never
    the default, so a new page is private to its owner until somebody decides
    otherwise. The organization still applies, and so does the service: a session of
    another service is as absent here as a session of another organization.
    """
    sessions = AssessmentSession.objects.for_request(request).for_service(getattr(request, 'service', None))
    if not (oversight and can_oversee(request)):
        sessions = sessions.owned_by(request.user)
    if lock:
        sessions = sessions.select_for_update()
    return get_object_or_404(sessions, pk=pk)


def _closed(request, session):
    """A redirect to the console, with the reason, when the session is closed and so a
    read-only record; otherwise ``None``."""
    reason = closed_reason(session)
    if reason is None:
        return None
    messages.error(request, reason)
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
def assessment_dashboard(request):
    """Classroom overview: a compact, data-backed summary of owned sessions."""
    if getattr(request, 'service', None) != 'classroom':
        raise Http404
    if request.GET.get('scope') == 'all' or request.GET.get('q') or request.GET.get('status'):
        return assessment_session_list(request)
    sessions = (AssessmentSession.objects.for_request(request)
                .for_service('classroom').owned_by(request.user))
    counts = sessions.aggregate(
        total=models.Count('pk'),
        live_count=models.Count('pk', filter=models.Q(status=AssessmentSession.Status.LIVE)),
        draft_count=models.Count('pk', filter=models.Q(status=AssessmentSession.Status.DRAFT)),
        closed_count=models.Count('pk', filter=models.Q(status=AssessmentSession.Status.CLOSED)),
    )
    live_sessions_qs = sessions.filter(status=AssessmentSession.Status.LIVE)
    counts['student_count'] = Student.objects.filter(assessment_session__in=sessions).count()
    counts['group_count'] = PresentationGroup.objects.filter(
        assessment_session__in=sessions, is_practice=False).count()
    counts['live_joined_count'] = ParticipationRecord.objects.filter(
        assessment_session__in=live_sessions_qs).count()
    counts['live_roster_count'] = Student.objects.filter(assessment_session__in=live_sessions_qs).count()

    stage_distribution = []
    stage_colors = {
        'live': 'var(--success)',
        'draft': '#d28a08',
        'closed': 'var(--text-subtle)',
    }
    pie_segments = []
    cumulative_sessions = 0
    for key, label, count in (
        ('live', 'Live now', counts['live_count']),
        ('draft', 'In setup', counts['draft_count']),
        ('closed', 'Completed', counts['closed_count']),
    ):
        segment_start = cumulative_sessions * 360 / counts['total'] if counts['total'] else 0
        cumulative_sessions += count
        segment_end = cumulative_sessions * 360 / counts['total'] if counts['total'] else 0
        stage_distribution.append({
            'key': key,
            'label': label,
            'count': count,
            'percentage': round(count * 100 / counts['total'], 1) if counts['total'] else 0,
        })
        if count:
            pie_segments.append(
                f'{stage_colors[key]} {segment_start:.3f}deg {segment_end:.3f}deg')
    stage_pie_background = (
        f"conic-gradient({', '.join(pie_segments)})" if pie_segments else ''
    )

    live_sessions = list(
        live_sessions_qs.select_related('created_by')
        .annotate(student_count=models.Count('students', distinct=True),
                  group_count=models.Count('groups', filter=models.Q(groups__is_practice=False), distinct=True),
                  live_joined_count=models.Count('participation_records', distinct=True))
        .order_by('-created_at', '-pk')[:3])
    if live_sessions:
        active_turns = {
            turn.assessment_session_id: turn.group
            for turn in PresentationTurn.objects.filter(
                assessment_session_id__in=[session.pk for session in live_sessions],
                status=PresentationTurn.Status.ACTIVE).select_related('group')
        }
        for session in live_sessions:
            session.live_active_group = active_turns.get(session.pk)

    recent_sessions = list(
        sessions.select_related('created_by')
        .annotate(student_count=models.Count('students', distinct=True),
                  group_count=models.Count('groups', filter=models.Q(groups__is_practice=False), distinct=True),
                  live_joined_count=models.Count('participation_records', distinct=True))
        .order_by('-created_at', '-pk')[:5])

    roster_sessions = list(
        sessions.select_related('created_by')
        .annotate(student_count=models.Count('students', distinct=True))
        .order_by('-student_count', '-created_at', '-pk')[:5])
    largest_roster = max((session.student_count for session in roster_sessions), default=0)
    for session in roster_sessions:
        session.roster_bar_percent = round(session.student_count * 100 / largest_roster, 1) if largest_roster else 0

    return render(request, 'assessments/session_dashboard.html', {
        'dashboard_stats': counts,
        'stage_distribution': stage_distribution,
        'stage_pie_background': stage_pie_background,
        'live_sessions': live_sessions,
        'recent_sessions': recent_sessions,
        'roster_sessions': roster_sessions,
        'largest_roster': largest_roster,
        'can_oversee': can_oversee(request),
    })


@lecturer_required
def assessment_session_list(request):
    if getattr(request, 'service', None) != 'classroom':
        raise Http404
    # "All sessions in the organization" is for people DevPerf has given the
    # oversee permission; for everybody else the parameter does nothing.
    show_all = request.GET.get('scope') == 'all' and can_oversee(request)
    visible = (AssessmentSession.objects.for_request(request)
               .for_service('classroom'))
    if not show_all:
        visible = visible.owned_by(request.user)

    counts = visible.aggregate(
        total=models.Count('pk'),
        live=models.Count('pk', filter=models.Q(status=AssessmentSession.Status.LIVE)),
        draft=models.Count('pk', filter=models.Q(status=AssessmentSession.Status.DRAFT)),
        closed=models.Count('pk', filter=models.Q(status=AssessmentSession.Status.CLOSED)),
    )
    search_query = request.GET.get('q', '').strip()[:100]
    status_filter = request.GET.get('status', '')
    if status_filter not in AssessmentSession.Status.values:
        status_filter = ''
    filtered = visible
    if search_query:
        search = models.Q(name__icontains=search_query)
        if show_all:
            search |= (models.Q(created_by__username__icontains=search_query)
                       | models.Q(created_by__first_name__icontains=search_query)
                       | models.Q(created_by__last_name__icontains=search_query))
        filtered = filtered.filter(search)
    if status_filter:
        filtered = filtered.filter(status=status_filter)

    status_order = models.Case(
        models.When(status=AssessmentSession.Status.LIVE, then=0),
        models.When(status=AssessmentSession.Status.DRAFT, then=1),
        default=2,
        output_field=models.IntegerField(),
    )
    queryset = (filtered.select_related('created_by')
        .annotate(student_count=models.Count('students', distinct=True),
                  group_count=models.Count('groups', filter=models.Q(groups__is_practice=False), distinct=True),
                  live_joined_count=models.Count('participation_records', distinct=True))
        .order_by(status_order, '-created_at', '-pk'))
    paginator, page = paginate(request, queryset)
    sessions = list(page.object_list)
    for session in sessions:
        session.is_mine = session.created_by_id == request.user.pk

    live_ids = [s.pk for s in sessions if s.status == AssessmentSession.Status.LIVE]
    if live_ids:
        active_turns = {
            t.assessment_session_id: t.group
            for t in PresentationTurn.objects.filter(
                assessment_session_id__in=live_ids, status=PresentationTurn.Status.ACTIVE
            ).select_related('group')
        }
        for session in sessions:
            if session.status != AssessmentSession.Status.LIVE:
                continue
            session.live_active_group = active_turns.get(session.pk)

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_params['per_page'] = str(page.paginator.per_page)
    if show_all:
        query_params['scope'] = 'all'
    else:
        query_params.pop('scope', None)

    context = {
        'sessions': page, 'page_obj': page, 'paginator': paginator,
        'show_all': show_all, 'can_oversee': can_oversee(request),
        'per_page': page.paginator.per_page, 'page_sizes': PAGE_SIZES,
        'status_counts': counts, 'status_filter': status_filter,
        'search_query': search_query, 'querystring': query_params.urlencode(),
        'filtered_count': paginator.count,
    }
    template = 'assessments/_session_list_results.html'
    if request.headers.get('x-requested-with') != 'XMLHttpRequest':
        template = 'assessments/session_list.html'
    response = render(request, template, context)
    patch_vary_headers(response, ['X-Requested-With'])
    return response


@lecturer_required
@require_POST
def assessment_walkthrough_start(request):
    """Start or resume a first-session walkthrough for this org in this service."""
    if getattr(request, 'service', None) != 'classroom':
        raise Http404
    organization = active_organization(request)
    existing = (AssessmentSession.objects.for_request(request).for_service('classroom')
                .owned_by(request.user).filter(status=AssessmentSession.Status.DRAFT)
                .order_by('-created_at', '-pk').first())
    walkthroughs = request.session.get(CLASSROOM_WALKTHROUGHS_KEY, {})
    if not isinstance(walkthroughs, dict):
        walkthroughs = {}
    walkthroughs[str(organization['id'])] = {'session_id': existing.pk if existing else None}
    request.session[CLASSROOM_WALKTHROUGHS_KEY] = walkthroughs
    if existing:
        return redirect('assessment_groups', pk=existing.pk)
    return redirect('assessment_session_create')


@lecturer_required
@require_POST
def assessment_walkthrough_dismiss(request):
    if getattr(request, 'service', None) != 'classroom':
        raise Http404
    organization = active_organization(request)
    walkthroughs = request.session.get(CLASSROOM_WALKTHROUGHS_KEY, {})
    if isinstance(walkthroughs, dict):
        walkthroughs.pop(str(organization['id']), None)
    else:
        walkthroughs = {}
    request.session[CLASSROOM_WALKTHROUGHS_KEY] = walkthroughs
    return redirect('assessment_session_list')


@lecturer_required
@require_http_methods(['GET', 'POST'])
def assessment_session_create(request):
    """Choose a blank setup, a service starter, or an owned configuration snapshot."""
    organization = active_organization(request)
    service = getattr(request, 'service', None)
    # Team can use the shared setup flow to build its first session template.
    # Other services have their own assessment and result workflows.
    if service not in ('classroom', 'team'):
        raise Http404
    saved = list(template_queryset(request).prefetch_related('rubric_categories__level_descriptors'))
    starters = starters_for(service)
    selection = (request.POST if request.method == 'POST' else request.GET).get('template', '')
    picker = TemplateChoiceForm({'template': selection}, saved_templates=saved, starters=starters)
    selected = None
    if picker.is_valid() and selection:
        candidates = {'saved:' + str(item.pk): item for item in saved}
        candidates.update({'starter:' + item.key: item for item in starters})
        selected = candidates[selection]
    settings, categories = blueprint(selected) if selected else ({}, [])
    instance = AssessmentSession(name=selected.name if selected else '', organization_id=organization['id'], service=service,
                                 created_by=request.user, **settings)
    form = AssessmentSessionForm(request.POST if request.method == 'POST' else None, instance=instance)
    if request.method == 'POST' and picker.is_valid() and form.is_valid():
        try:
            session = save_draft_from_template(form.save(commit=False), selected)
        except ValidationError as error:
            form.add_error(None, ' '.join(error.messages))
        else:
            walkthroughs = request.session.get(CLASSROOM_WALKTHROUGHS_KEY, {})
            guide = walkthroughs.get(str(organization['id'])) if isinstance(walkthroughs, dict) else None
            if (service == 'classroom' and isinstance(guide, dict)
                    and guide.get('session_id') is None):
                walkthroughs[str(organization['id'])] = {'session_id': session.pk}
                request.session[CLASSROOM_WALKTHROUGHS_KEY] = walkthroughs
            messages.success(request, f'"{session.name}" created - add your groups next.')
            return redirect('assessment_groups', pk=session.pk)
    return render(request, 'assessments/session_setup.html', {
        'session': None, 'active_step': 'basics', 'form': form, 'template_picker': picker,
        'selected_template': selected, 'template_selection': selection,
        'template_categories': categories,
        'stepper_steps': _setup_stepper(request, None, 'basics'),
    })


@lecturer_required
@require_http_methods(['GET', 'POST'])
def assessment_session_duplicate(request, pk):
    source = _session(request, pk)
    if request.method == 'POST':
        form = DuplicateSessionForm(request.POST)
        if form.is_valid():
            scoped = (AssessmentSession.objects.for_request(request)
                      .for_service(getattr(request, 'service', None)).owned_by(request.user))
            try:
                duplicate = duplicate_session(scoped, source.pk, owner=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, f'"{duplicate.name}" created as a new draft. Review its setup before going live.')
                return redirect('assessment_groups', pk=duplicate.pk)
    else:
        form = DuplicateSessionForm(initial={'name': f'{source.name[:143]} (copy)'})
    return render(request, 'assessments/_session_duplicate_modal.html', {
        'session': source, 'form': form,
    })


BASICS_LABELS = {'name': 'name', 'identify_by': 'how students join', 'ungraded_penalty': 'penalty per group not graded', 'instructions': 'evaluator notice'}


@lecturer_required
def assessment_session_edit(request, pk):
    """Edit a session's name, how students join, and the penalty for groups not graded.

    All three stay editable while the session is live. Students already in are not
    affected by a change to how they join (they are recognised by their record),
    and a changed penalty rewrites the results of every student, including past
    groups, because results are always worked out from the current setup. Each live
    change is logged for the change history. A closed session is a read-only record.
    """
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked

    if request.method == 'POST':
        form = AssessmentSessionForm(request.POST, instance=session)
        if form.is_valid():
            changed = list(form.changed_data)
            form.save()
            if changed:
                record_change(session, request.user,
                              'Changed the ' + ', '.join(BASICS_LABELS[f] for f in changed) + '.')
            messages.success(request, f'"{session.name}" updated.')
            # nosemgrep: python.django.security.django-using-request-post-after-is-valid.django-using-request-post-after-is-valid (compared with a fixed word, never used as an address)
            if request.POST.get('next') == 'setup':
                return redirect(reverse('assessment_setup', args=[session.pk]) + '?step=basics')
            return redirect('assessment_session_detail', pk=session.pk)
    else:
        form = AssessmentSessionForm(instance=session)

    if is_live(session):
        return render(request, 'assessments/session_setup.html',
                      {**_setup_context(request, session, 'basics'), 'form': form, 'editing': True})
    return render(request, 'assessments/session_setup.html', {
        'session': session, 'active_step': 'basics', 'form': form, 'editing': True,
        'stepper_steps': _setup_stepper(request, session, 'basics'),
    })


def _live_context(session):
    """Everything the live control panel needs to render, computed fresh
    from the database. Shared between the full-page view and the JSON
    `assessment_live_state` endpoint the panel polls after a websocket
    event, so the two never drift apart."""
    active_turn = session.active_turn()
    pending_groups = session.groups.exclude(
        turns__status__in=[PresentationTurn.Status.ACTIVE]).order_by('order', 'id')
    presented_group_ids = set(
        session.presentation_turns.filter(status=PresentationTurn.Status.CLOSED)
        .values_list('group_id', flat=True))
    joined_count = session.participation_records.count()
    voted_count = Evaluation.objects.filter(
        presentation_turn__group__is_practice=False,
        presentation_turn__assessment_session=session, target_student__isnull=True
    ).values('evaluator').distinct().count()
    next_group = session.get_effective_next_group()

    group_count = session.groups.count()
    presented_count = len(presented_group_ids)
    groups_remaining = max(group_count - presented_count - (1 if active_turn else 0), 0)

    # Effective next-up group: honors lecturer designation if set, else falls back to order
    next_up_group = next_group

    # Recent activity feed + a rough "typical turn length" - both read
    # straight off closed turns, so the panel shows real session history
    # instead of static setup numbers once things start moving.
    closed_turns = list(
        session.presentation_turns.filter(status=PresentationTurn.Status.CLOSED)
        .exclude(closed_at__isnull=True).select_related('group')
        .order_by('-closed_at')[:5])
    recent_activity = []
    durations = []
    recent_votes = turn_vote_counts([turn.pk for turn in closed_turns])
    for turn in closed_turns:
        votes = recent_votes.get(turn.pk, 0)
        if turn.opened_at and turn.closed_at:
            durations.append((turn.closed_at - turn.opened_at).total_seconds())
        recent_activity.append({'group': turn.group, 'closed_at': turn.closed_at, 'votes': votes, 'turn_id': turn.pk})
    avg_turn_seconds = int(sum(durations) / len(durations)) if durations else None

    # Once a session is closed its "running for" clock has to stop ticking
    # against wall-clock `now()` - that would just keep growing forever on
    # a summary page viewed hours or days later - so it's measured against
    # `closed_at` instead, giving the actual total session duration.
    first_turn = session.presentation_turns.order_by('opened_at').first()
    elapsed_seconds = None
    if first_turn and first_turn.opened_at:
        end = session.closed_at if session.status == AssessmentSession.Status.CLOSED else timezone.now()
        elapsed_seconds = int(((end or timezone.now()) - first_turn.opened_at).total_seconds())

    # Provisional live rankings - group score percent computed off whatever
    # votes have been submitted *so far*, not just after a turn closes, so
    # the panel can show "who's winning" the way a Kahoot leaderboard does
    # mid-session rather than only once everything is final. Groups with no
    # votes yet sort to the bottom instead of being dropped, so the full
    # roster of groups is always visible here.
    from ..leaderboard import group_standings
    group_rankings = group_standings(session, active_group_id=active_turn.group_id if active_turn else None)
    has_any_scores = any(row['percent'] is not None for row in group_rankings)

    # The closed-session summary reuses the Participation tab's own status
    # breakdown (full/partial/none) so the two screens never tell a
    # lecturer two different stories - only worth computing once grading
    # has actually stopped, not on every live poll.
    participation_counts = None
    if session.status == AssessmentSession.Status.CLOSED:
        counts = _participation_rows(session)['counts']
        participation_counts = {
            'full_count': counts['full'], 'partial_count': counts['partial'], 'none_count': counts['none'],
        }

    # The closed-session "Presentation order" timeline reuses the same
    # activity-feed component as the live tab's "Last presentations", but
    # tells a different, final story: every group (not capped at 5),
    # ordered by how they actually placed rather than by who happened to
    # close last, with each row's final score and time-on-stage standing
    # in for the live version's raw vote count + wall-clock timestamp.
    presentation_timeline = None
    if session.status == AssessmentSession.Status.CLOSED:
        rank_by_group_id = {row['group'].pk: row for row in group_rankings}
        all_closed_turns = list(
            session.presentation_turns.filter(status=PresentationTurn.Status.CLOSED, group__is_practice=False)
            .exclude(closed_at__isnull=True).select_related('group'))
        timeline = []
        timeline_votes = turn_vote_counts([turn.pk for turn in all_closed_turns])
        for turn in all_closed_turns:
            votes = timeline_votes.get(turn.pk, 0)
            duration_label = None
            if turn.opened_at and turn.closed_at:
                secs = int((turn.closed_at - turn.opened_at).total_seconds())
                duration_label = f'{secs // 60}m {secs % 60}s' if secs >= 60 else f'{secs}s'
            rank_row = rank_by_group_id.get(turn.group_id)
            timeline.append({
                'group': turn.group, 'votes': votes, 'turn_id': turn.pk,
                'duration_label': duration_label,
                'rank': rank_row['rank'] if rank_row else None,
                'percent': rank_row['percent'] if rank_row else None,
            })
        timeline.sort(key=lambda item: (item['rank'] is None, item['rank'] or 0))
        presentation_timeline = timeline

    participation_rows = _participation_rows(session)
    joined_students = [row for row in participation_rows['rows'] if row['joined']]

    return {
        'session': session,
        'active_turn': active_turn,
        'next_group': next_group,
        'next_up_group': next_up_group,
        'groups': session.groups.all().order_by('order', 'id'),
        'presented_group_ids': presented_group_ids,
        'student_count': session.students.count(),
        'group_count': group_count,
        'presented_count': presented_count,
        'groups_remaining': groups_remaining,
        'rubric_count': session.rubric_categories.count(),
        'joined_count': joined_count,
        'voted_count': voted_count,
        'recent_activity': recent_activity,
        'avg_turn_seconds': avg_turn_seconds,
        'elapsed_seconds': elapsed_seconds,
        'group_rankings': group_rankings,
        'group_points_max': session.group_weight_total(),
        'has_any_scores': has_any_scores,
        'participation_counts': participation_counts,
        'presentation_timeline': presentation_timeline,
        'joined_students': joined_students,
        'joining_locked': session.joining_locked,
        'designated_next_group': session.designated_next_group,
    }


@lecturer_required
def assessment_session_detail(request, pk):
    session = _session(request, pk)
    if session.status == AssessmentSession.Status.DRAFT:
        # A draft session has no live console to show yet - render the same
        # merged setup screen the groups/rubric steps render, opened
        # on whichever step isn't done yet.
        active_step = 'groups'
        if session.groups.exists():
            active_step = 'rubric'
        if session.rubric_categories.exists():
            active_step = 'go-live' if session.can_go_live() else 'rubric'
        return render(request, 'assessments/session_setup.html',
                      _setup_context(request, session, active_step))
    if session.status == AssessmentSession.Status.LIVE:
        # Same self-healing as the student side: catch up an expired timer
        # right on page load rather than only ever on the next Celery tick.
        ensure_turn_progressed(session)
    context = _live_context(session)
    context['join_url'] = join_url(request, session)
    return render(request, 'assessments/session_detail.html', context)


@lecturer_required
def assessment_live_state(request, pk):
    """JSON, fetched by the control panel after a websocket event instead
    of reloading the whole page - only the pieces that actually changed get
    re-rendered client-side, so a lecturer's in-progress duration-picker
    selection, scroll position, etc. survive every update."""
    session = _session(request, pk)
    if session.status == AssessmentSession.Status.LIVE:
        ensure_turn_progressed(session)
    context = _live_context(session)

    active_turn = context['active_turn']
    return JsonResponse({
        'public_show_scores': session.public_leaderboard_show_scores,
        'status': session.status,
        'voting_paused': session.voting_paused,
        'active_turn': {
            'group_id': active_turn.group_id,
            'voting_ends_at': active_turn.voting_ends_at.isoformat() if active_turn.voting_ends_at else None,
            'paused_remaining_seconds': active_turn.paused_remaining_seconds,
        } if active_turn else None,
        'hero_html': render_to_string('assessments/_partials/live_hero.html', context, request=request),
        'stats_html': render_to_string('assessments/_partials/live_stats.html', context, request=request),
        'groups_summary_html': render_to_string('assessments/_partials/live_groups_summary.html', context, request=request),
        'groups_html': render_to_string('assessments/_partials/live_groups.html', context, request=request),
        'rankings_html': render_to_string('assessments/_partials/live_rankings.html', context, request=request),
        'activity_html': render_to_string('assessments/_partials/live_activity.html', context, request=request),
        'participation_html': render_to_string('assessments/_partials/live_participation.html', context, request=request),
        'roster_html': render_to_string('assessments/_partials/live_roster.html', context, request=request),
        'pause_html': render_to_string('assessments/_partials/live_pause_button.html', context, request=request),
        'qr_html': render_to_string('assessments/_partials/live_qr.html', {**context, 'join_url': join_url(request, session)}, request=request),
    })


@lecturer_required
@require_POST
def assessment_session_delete(request, pk):
    session = _session(request, pk)
    name = session.name
    session.delete()
    messages.success(request, f'"{name}" deleted.')
    return redirect('assessment_session_list')


@lecturer_required
@require_POST
def assessment_session_bulk_delete(request):
    """Delete an explicitly selected set of the current lecturer's sessions."""
    if getattr(request, 'service', None) != 'classroom':
        raise Http404

    raw_ids = request.POST.getlist('session_ids')
    if (not raw_ids or len(raw_ids) > max(PAGE_SIZES)
            or any(not re.fullmatch(r'[1-9][0-9]*', value)
                   or len(value) > 19
                   or (len(value) == 19 and value > '9223372036854775807')
                   for value in raw_ids)):
        messages.error(request, 'Select one or more valid sessions to delete.')
        return _session_bulk_delete_redirect(request)

    session_ids = {int(value) for value in raw_ids}
    owned_sessions = (AssessmentSession.objects.for_request(request)
                      .for_service('classroom').owned_by(request.user))
    with transaction.atomic():
        selected = list(owned_sessions.select_for_update().filter(pk__in=session_ids))
        if len(selected) != len(session_ids):
            messages.error(request, 'One or more selected sessions are no longer available to delete.')
            return _session_bulk_delete_redirect(request)
        count = len(selected)
        owned_sessions.filter(pk__in=session_ids).delete()

    messages.success(request, f'{count} session{"s" if count != 1 else ""} deleted.')
    return _session_bulk_delete_redirect(request)


def _session_bulk_delete_redirect(request):
    """Return to the current filtered view, with a valid page-size and scope."""
    params = QueryDict('', mutable=True)
    query = (request.POST.get('q') or '').strip()[:100]
    status_filter = request.POST.get('status', '')
    if query:
        params['q'] = query
    if status_filter in AssessmentSession.Status.values:
        params['status'] = status_filter
    try:
        per_page = int(request.POST.get('per_page', PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE
    params['per_page'] = str(per_page)
    if request.POST.get('scope') == 'all' and can_oversee(request):
        params['scope'] = 'all'
    url = reverse('assessment_session_list')
    return redirect(f'{url}?{params.urlencode()}' if params else url)


# ---------------------------------------------------------------- Roster ----

def _roster_context(request, session):
    """Shared between the full page and the JSON search endpoint it calls
    while the lecturer types - same split as `_participation_context`
    above, so filtering the roster updates the table silently instead of
    reloading the whole page (and stealing focus from the search box)."""
    students = session.students.all().order_by('full_name')
    query = (request.GET.get('q') or '').strip()
    if query:
        students = students.filter(
            models.Q(full_name__icontains=query) | models.Q(student_id__icontains=query)
            | models.Q(email__icontains=query) | models.Q(programme__icontains=query))

    paginator, page = paginate(request, students)
    programme_count = (
        session.students.exclude(programme='').exclude(programme__isnull=True)
        .values('programme').distinct().count())
    return {
        'session': session, 'page_obj': page, 'paginator': paginator, 'students': page.object_list,
        'total': paginator.count, 'query': query, 'roster_total': session.students.count(),
        'programme_count': programme_count,
        'per_page': paginator.per_page, 'page_sizes': PAGE_SIZES,
    }



def _setup_stepper(request, session, active_step, groups_done=None, rubric_done=None, live_mode=False):
    """Steps for the shared stepper on the classroom setup screen. Completion
    is read from the session unless the caller already computed it."""
    if session is not None:
        if groups_done is None:
            groups_done = session.groups.exists()
        if rubric_done is None:
            rubric_done = session.rubric_weight_total() == TOTAL_WEIGHT
    people = 'team members' if request.service == 'team' else 'students'
    specs = [
        {'key': 'basics', 'label': 'Basics', 'desc': 'Name, join & penalty', 'done': session is not None},
        {'key': 'groups', 'label': f'Groups & {people}', 'desc': 'Import or build groups', 'done': groups_done},
        {'key': 'rubric', 'label': 'Rubric', 'desc': 'Categories & weights', 'done': rubric_done},
    ]
    if not live_mode:
        can_go_live = session is not None and session.can_go_live()
        specs.append({'key': 'go-live', 'label': 'Go live', 'desc': 'Show QR, start grading',
                      'final': True, 'icon': 'bi-rocket-takeoff', 'locked': not can_go_live,
                      'disabled': session is None})
    for spec in specs:
        if session is not None:
            spec['href'] = f"#{spec['key']}-section"
    return build_steps(specs, active_step, locked_after_first=session is None)


def _setup_context(request, session, active_step, roster_form=None, rubric_form=None, group_form=None):
    """Everything the merged create → groups and students → rubric → go-live
    screen needs, built from the same context-builders each standalone step
    already used (`_roster_context`, the rubric aggregates, `_groups_context`)
    so the sections keep behaving exactly as they did on their own pages.
    Each section's own form can be overridden with an in-progress (possibly
    invalid) instance from a POST handler; the others get a fresh blank form.

    The same screen serves a live session (``live_mode``): then it also carries
    the log of edits made since going live.

    Each form gets a distinct `auto_id` prefix - with all three forms live
    on one page at once, Django's default `id_%s` would collide wherever
    field names match across forms (e.g. both the rubric and group forms
    have a `name` field), which the standalone pages never had to worry
    about.
    """
    context = {'session': session, 'active_step': active_step}

    context.update(_roster_context(request, session))
    context['form'] = roster_form or StudentForm(auto_id='id_roster_%s')
    context['basics_form'] = AssessmentSessionForm(instance=session, auto_id='id_basics_%s')

    all_categories = list(session.rubric_categories.prefetch_related('level_descriptors'))
    for category in all_categories:
        category.level_descriptors_text = format_descriptors(category.level_descriptors.all())
    group_categories = [category for category in all_categories
                        if category.scope == RubricCategory.Scope.GROUP]
    individual_categories = [category for category in all_categories
                             if category.scope == RubricCategory.Scope.INDIVIDUAL]
    group_weight_total = sum((c.weight for c in group_categories), Decimal('0'))
    individual_weight_total = sum((c.weight for c in individual_categories), Decimal('0'))
    weight_total = group_weight_total + individual_weight_total
    context.update({
        'rubric_form': rubric_form or RubricCategoryForm(assessment_session=session, auto_id='id_rubric_%s'),
        'group_categories': group_categories,
        'individual_categories': individual_categories,
        'group_weight_total': group_weight_total,
        'individual_weight_total': individual_weight_total,
        'weight_total': weight_total,
        'weight_remaining': TOTAL_WEIGHT - weight_total,
        'weight_pie': weight_pie(all_categories),
    })

    context.update(_groups_context(session))
    context['group_form'] = group_form or PresentationGroupForm(auto_id='id_group_%s')
    context['group_import_form'] = GroupImportForm(auto_id='id_group_import_%s')
    context['rubric_import_form'] = RubricImportForm(auto_id='id_rubric_import_%s')
    context['live_mode'] = is_live(session)
    context['recent_changes'] = list(session.changes.select_related('by_user')[:8]) if is_live(session) else []
    context['rubric_live_lock'] = live_rubric_lock(session)
    context['import_plan'] = pending_group_import(request, session) if active_step == 'groups' else None
    context['rubric_review'] = pending_rubric_import(request, session) if active_step == 'rubric' else None

    rubric_ready = weight_total == TOTAL_WEIGHT
    context.update({
        'step_rubric_done': rubric_ready,
        'step_groups_done': session.groups.exists(),
        'stepper_steps': _setup_stepper(request, session, active_step, session.groups.exists(),
                                        rubric_ready, context['live_mode']),
    })
    return context


@lecturer_required

def roster(request, pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    form = None
    if request.method == 'POST':
        form = StudentForm(request.POST, auto_id='id_roster_%s')
        if form.is_valid():
            student = form.save(commit=False)
            student.assessment_session = session
            try:
                student.full_clean()
            except Exception as exc:
                for msg in getattr(exc, 'messages', [str(exc)]):
                    messages.error(request, msg)
            else:
                student.save()
                record_change(session, request.user, f'Added student {student}.')
                messages.success(request, f'{student.full_name or student.student_id} added.')
                return redirect('assessment_roster', pk=session.pk)

    return render(request, 'assessments/session_setup.html',
                  {**_setup_context(request, session, 'groups', roster_form=form), 'students_open': True})


@lecturer_required
def roster_search(request, pk):
    """Polled from the search box's debounce, not a page reload - returns
    just the table+pager fragment and the "N students…" count text so the
    lecturer's cursor and scroll position never leave the search field."""
    session = _session(request, pk)
    context = _roster_context(request, session)
    return JsonResponse({
        'table_html': render_to_string('assessments/_partials/roster_table.html', context, request=request),
        'count_html': render_to_string('assessments/_partials/roster_count.html', context, request=request),
    })


@lecturer_required
@require_POST
def roster_edit(request, pk, student_pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    student = get_object_or_404(Student, pk=student_pk, assessment_session=session)
    form = StudentForm(request.POST, instance=student)
    if form.is_valid():
        try:
            student = form.save(commit=False)
            student.full_clean()
        except Exception as exc:
            for msg in getattr(exc, 'messages', [str(exc)]):
                messages.error(request, msg)
        else:
            student.save()
            record_change(session, request.user, f'Edited student {student}.')
            messages.success(request, f'{student.full_name or student.student_id} updated.')
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect('assessment_roster', pk=session.pk)


@lecturer_required
@require_POST
def roster_delete(request, pk, student_pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    student = get_object_or_404(Student, pk=student_pk, assessment_session=session)
    reason = student_delete_lock(student)
    if reason:
        messages.error(request, reason)
        return redirect('assessment_roster', pk=session.pk)
    name = str(student)
    student.delete()
    record_change(session, request.user, f'Removed student {name}.')
    messages.success(request, 'Student removed.')
    return redirect('assessment_roster', pk=session.pk)


# ---------------------------------------------------------------- Rubric ----

@lecturer_required
def rubric(request, pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    form = None
    if request.method == 'POST':
        lock = live_rubric_lock(session)
        if lock:
            messages.error(request, lock)
            return redirect('assessment_rubric', pk=session.pk)
        form = RubricCategoryForm(request.POST, assessment_session=session, auto_id='id_rubric_%s')
        if form.is_valid():
            category = form.save()
            messages.success(request, f'"{category.name}" added to the rubric.')
            return redirect('assessment_rubric', pk=session.pk)

    return render(request, 'assessments/session_setup.html',
                  _setup_context(request, session, 'rubric', rubric_form=form))


@lecturer_required
@require_POST
def rubric_edit(request, pk, category_pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    lock = live_rubric_lock(session)
    if lock:
        messages.error(request, lock)
        return redirect('assessment_rubric', pk=session.pk)
    category = get_object_or_404(RubricCategory, pk=category_pk, assessment_session=session)
    form = RubricCategoryForm(request.POST, instance=category, assessment_session=session,
                              auto_id='id_rubric_%s')
    if form.is_valid():
        form.save()
        messages.success(request, f'"{category.name}" updated.')
        return redirect('assessment_rubric', pk=session.pk)
    # Show the edit form again with what was typed and each problem under its own
    # field, rather than clearing it and reporting the errors in a toast.
    context = _setup_context(request, session, 'rubric', rubric_form=form)
    context['editing_category'] = RubricCategory.objects.get(pk=category.pk)
    return render(request, 'assessments/session_setup.html', context)


@lecturer_required
@require_POST
def rubric_delete(request, pk, category_pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    lock = live_rubric_lock(session)
    if lock:
        messages.error(request, lock)
        return redirect('assessment_rubric', pk=session.pk)
    category = get_object_or_404(RubricCategory, pk=category_pk, assessment_session=session)
    category.delete()
    messages.success(request, 'Rubric category removed.')
    return redirect('assessment_rubric', pk=session.pk)


# ---------------------------------------------------------------- Groups ----

def _unassigned_students(session):
    """Students with no membership in any group in this session - the only
    students it's ever valid to offer for a new assignment, whether that's
    the create-group form, the per-group "add" dropdown, or an import."""
    return session.students.exclude(
        group_memberships__group__assessment_session=session).order_by('full_name')


@lecturer_required
def groups(request, pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    form = None
    if request.method == 'POST':
        form = PresentationGroupForm(request.POST, auto_id='id_group_%s')
        if form.is_valid():
            eligible_ids = set(_unassigned_students(session).values_list('pk', flat=True))
            member_ids = [int(pk_) for pk_ in request.POST.getlist('members') if pk_.isdigit()]
            invalid_ids = set(member_ids) - eligible_ids
            max_size = form.cleaned_data.get('max_size')
            if invalid_ids:
                messages.error(
                    request,
                    'One or more selected students are no longer unassigned - '
                    'reload and try again.')
            elif max_size is not None and len(member_ids) > max_size:
                messages.error(
                    request,
                    f'You selected {len(member_ids)} member(s) but set the group '
                    f'size to {max_size} - pick a smaller selection or raise the limit.')
            else:
                group = form.save(commit=False)
                group.assessment_session = session
                # Every group defaults to order=0, so without this a new
                # group ties with the others on the ordering field and
                # falls back to whatever order the database happens to
                # return them in - which is how a new group ended up
                # appearing next to the first one instead of at the end.
                last_order = session.groups.aggregate(models.Max('order'))['order__max'] or 0
                group.order = last_order + 1
                group.save()
                for student_id in member_ids:
                    GroupMembership.objects.create(group=group, student_id=student_id)
                record_change(session, request.user, f'Added group "{group.name}".')
                messages.success(
                    request,
                    f'"{group.name}" created with {len(member_ids)} member(s).'
                    if member_ids else f'"{group.name}" created.')
                return redirect('assessment_groups', pk=session.pk)

    return render(request, 'assessments/session_setup.html',
                  _setup_context(request, session, 'groups', group_form=form))


def _groups_context(session):
    """Shared by the full-page view and the drag/drop AJAX patch below -
    both need the same groups/ungrouped/full-count trio to render their
    respective templates (full page vs. just the affected fragments)."""
    # Members and their students come in with the groups (3 queries in all), so the
    # card's counts and member rows read from memory instead of querying per group.
    groups_list = list(session.groups.all().order_by('order', 'id')
                       .prefetch_related('memberships__student'))
    annotate_groups(session, groups_list)
    return {
        'session': session,
        'groups': groups_list,
        'ungrouped_students': _unassigned_students(session),
        'full_group_count': sum(1 for group in groups_list if group.is_full()),
        'live_mode': is_live(session),
    }


@lecturer_required
@require_POST
def group_members(request, pk, group_pk):
    session = _session(request, pk)
    group = get_object_or_404(PresentationGroup, pk=group_pk, assessment_session=session)
    action = request.POST.get('action')
    student_id = request.POST.get('student_id')
    student = get_object_or_404(Student, pk=student_id, assessment_session=session)
    error = closed_reason(session) or group_members_lock(group)
    if error is not None:
        messages.error(request, error)
    elif action == 'add':
        membership = GroupMembership(group=group, student=student)
        try:
            membership.full_clean()
        except ValidationError as exc:
            error = '; '.join(exc.messages)
            messages.error(request, error)
        else:
            membership.save()
            record_change(session, request.user, f'Added {student} to "{group.name}".')
    elif action == 'remove':
        GroupMembership.objects.filter(group=group, student=student).delete()
        record_change(session, request.user, f'Removed {student} from "{group.name}".')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # Drag-and-drop reassignment - reply with just the fragments that
        # changed (this group's card, the unassigned pool, the stat strip)
        # so the page can patch itself in place instead of reloading.
        context = _groups_context(session)
        # The card needs the lock details `_groups_context` worked out, so use its
        # copy of the group. The page shows `error` as a toast now, so the queued
        # message is dropped rather than showing again on the next page load.
        list(messages.get_messages(request))
        card_group = next(g for g in context['groups'] if g.pk == group.pk)
        context['recent_changes'] = list(session.changes.select_related('by_user')[:8]) if is_live(session) else []
        return JsonResponse({
            'ok': error is None,
            'error': error,
            'group_id': group.pk,
            'changes_html': render_to_string(
                'assessments/_partials/live_recent_changes.html', context, request=request),
            'group_html': render_to_string(
                'assessments/_partials/group_card.html', {**context, 'group': card_group}, request=request),
            'unassigned_html': render_to_string(
                'assessments/_partials/group_unassigned_pool.html', context, request=request),
            'stats_html': render_to_string(
                'assessments/_partials/group_stats.html', context, request=request),
        })
    return redirect('assessment_groups', pk=session.pk)


@lecturer_required
@require_POST
@transaction.atomic
def group_edit(request, pk, group_pk):
    session = _session(request, pk, lock=True)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    group = get_object_or_404(PresentationGroup, pk=group_pk, assessment_session=session)
    form = PresentationGroupForm(request.POST, instance=group)
    if form.is_valid():
        form.save()
        record_change(session, request.user, f'Edited group "{group.name}".')
        messages.success(request, f'"{group.name}" updated.')
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect('assessment_groups', pk=session.pk)


@lecturer_required
@require_POST
def group_delete(request, pk, group_pk):
    session = _session(request, pk)
    blocked = _closed(request, session)
    if blocked:
        return blocked
    group = get_object_or_404(PresentationGroup, pk=group_pk, assessment_session=session)
    delete_groups(request, session, [group])
    return redirect('assessment_groups', pk=session.pk)


def delete_groups(request, session, groups):
    """Delete groups together with their students, skipping any that must stay.

    Deleting a group deletes the students in it: they were added for that group.
    A group stays when it has presented, is presenting, or any of its students has
    already graded (removing them would erase those grades); each skipped group
    says why. All or nothing for the deletions that go ahead.
    """
    deleted, students_removed, skipped = [], 0, []
    with transaction.atomic():
        for group in groups:
            reason = group_delete_lock(group)
            if reason:
                skipped.append(reason)
                continue
            student_ids = list(group.memberships.values_list('student_id', flat=True))
            students_removed += Student.objects.filter(
                pk__in=student_ids, assessment_session=session).delete()[1].get('assessments.Student', 0)
            deleted.append(group.name)
            group.delete()
    if deleted:
        names = ', '.join(f'"{n}"' for n in deleted[:5]) + (f' and {len(deleted) - 5} more' if len(deleted) > 5 else '')
        record_change(session, request.user, f'Deleted {len(deleted)} group(s): {names}.')
        messages.success(
            request, f'Deleted {len(deleted)} group{"s" if len(deleted) != 1 else ""} '
                     f'and {students_removed} student{"s" if students_removed != 1 else ""}.')
    for reason in skipped[:5]:
        messages.warning(request, reason)
    if len(skipped) > 5:
        messages.warning(request, f'...and {len(skipped) - 5} more group(s) could not be deleted.')
    return len(deleted)


# ------------------------------------------------------------ Live control --

@lecturer_required
@require_POST
def go_live(request, pk):
    session = _session(request, pk)
    if not session.can_go_live():
        messages.error(request, 'Add rubric categories whose weights add up to exactly 100% '
                                 'and at least one group before going live.')
        return redirect('assessment_session_detail', pk=session.pk)
    session.status = AssessmentSession.Status.LIVE
    session.save(update_fields=['status'])
    messages.success(request, 'Session is now live. Share the QR code with students.')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def activate_group(request, pk, group_pk):
    session = _session(request, pk)
    group = get_object_or_404(PresentationGroup, pk=group_pk, assessment_session=session)

    duration_raw = (request.POST.get('duration_seconds') or '').strip()
    duration_seconds = int(duration_raw) if duration_raw.isdigit() and int(duration_raw) > 0 else None

    gap_raw = (request.POST.get('gap_seconds') or '').strip()
    gap_seconds = int(gap_raw) if gap_raw.isdigit() else 0

    presentation_raw = (request.POST.get('presentation_seconds') or '').strip()
    presentation_seconds = int(presentation_raw) if presentation_raw.isdigit() and int(presentation_raw) > 0 else None
    activate_turn(session, group, duration_seconds=duration_seconds, gap_seconds=gap_seconds,
                 presentation_seconds=presentation_seconds)

    if duration_seconds:
        messages.success(
            request,
            f'"{group.name}" is now presenting - voting closes automatically '
            f'in {duration_seconds // 60}m {duration_seconds % 60:02d}s.')
    else:
        messages.success(request, f'"{group.name}" is now presenting.')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def set_default_turn_seconds(request, pk):
    """Persists the lecturer's chosen voting timer as the session default,
    the same instant-save pattern already used for the transition gap -
    so picking 60s here sticks for every group activated from now on,
    not just whichever one happens to be submitted next.
    """
    session = _session(request, pk)
    duration_raw = (request.POST.get('duration_seconds') or '').strip()
    duration_seconds = int(duration_raw) if duration_raw.isdigit() and int(duration_raw) > 0 else None
    session.default_turn_seconds = duration_seconds
    session.save(update_fields=['default_turn_seconds'])
    broadcast_session_event(session, 'turn_duration.updated', {'duration_seconds': duration_seconds})
    return JsonResponse({'ok': True, 'duration_seconds': duration_seconds})


@lecturer_required
@require_POST
def set_default_presentation_seconds(request, pk):
    """Persists the lecturer's chosen presentation-phase timer. Informational
    only — it does not auto-advance the session; it gives the lecturer a visible
    cue on screen for when to open voting.
    """
    session = _session(request, pk)
    duration_raw = (request.POST.get('presentation_seconds') or '').strip()
    presentation_seconds = int(duration_raw) if duration_raw.isdigit() and int(duration_raw) > 0 else None
    session.default_presentation_seconds = presentation_seconds
    session.save(update_fields=['default_presentation_seconds'])
    broadcast_session_event(session, 'presentation_duration.updated', {'presentation_seconds': presentation_seconds})
    return JsonResponse({'ok': True, 'presentation_seconds': presentation_seconds})



@lecturer_required
@require_POST
def set_transition_gap(request, pk):
    """Persists the lecturer's chosen gap-between-groups the instant they
    pick it - not just the next time they happen to click "activate group".

    This matters because the gap governs the *currently running* auto-
    advance chain too: a lecturer who changes it mid-session, while some
    group is already presenting, expects the very next automatic transition
    to honour the new value, not silently keep using whatever was in effect
    when the current group was activated.
    """
    session = _session(request, pk)
    gap_raw = (request.POST.get('gap_seconds') or '').strip()
    gap_seconds = int(gap_raw) if gap_raw.isdigit() else 0
    session.transition_gap_seconds = gap_seconds
    session.save(update_fields=['transition_gap_seconds'])
    broadcast_session_event(session, 'transition_gap.updated', {'gap_seconds': gap_seconds})
    return JsonResponse({'ok': True, 'gap_seconds': gap_seconds})


@lecturer_required
@require_POST
def toggle_pause(request, pk):
    session = _session(request, pk)
    session.voting_paused = not session.voting_paused
    session.save(update_fields=['voting_paused'])

    # The countdown on whatever turn is active must not keep running
    # unseen while voting is paused (it would auto-advance mid-pause) -
    # freeze it on pause, restore the exact remaining time on resume.
    if session.voting_paused:
        pause_timer(session)
    else:
        resume_timer(session)

    broadcast_session_event(session, 'voting.paused', {'paused': session.voting_paused})
    messages.success(request, 'Voting paused.' if session.voting_paused else 'Voting resumed.')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def toggle_joining(request, pk):
    session = _session(request, pk)
    session.joining_locked = not session.joining_locked
    session.save(update_fields=['joining_locked'])
    broadcast_session_event(session, 'joining.locked', {
        'locked': session.joining_locked,
        'joined_count': session.participation_records.count(),
    })
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'ok': True,
            'locked': session.joining_locked,
            'joined_count': session.participation_records.count(),
        })
    messages.success(
        request,
        'New joining is locked.' if session.joining_locked else 'New joining is open.')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def set_designated_next_group(request, pk):
    """Sets or clears the lecturer's explicitly chosen next group."""
    session = _session(request, pk)
    group_pk_raw = (request.POST.get('group_id') or '').strip()
    if not group_pk_raw:
        session.designated_next_group = None
        session.save(update_fields=['designated_next_group'])
        effective = session.get_effective_next_group()
        broadcast_session_event(session, 'lineup.updated', {
            'next_group_id': effective.pk if effective else None,
            'next_group_name': effective.name if effective else '',
        })
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True, 'cleared': True, 'effective_next': effective.name if effective else ''})
        messages.success(request, 'Upcoming queue order reset to default.')
        return redirect('assessment_session_detail', pk=session.pk)

    group = get_object_or_404(PresentationGroup, pk=group_pk_raw, assessment_session=session)
    session.designated_next_group = group
    session.save(update_fields=['designated_next_group'])
    broadcast_session_event(session, 'lineup.updated', {
        'next_group_id': group.pk,
        'next_group_name': group.name,
    })
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'group_id': group.pk, 'group_name': group.name})
    messages.success(request, f'"{group.name}" designated as next up.')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def adjust_turn_timer(request, pk):
    session = _session(request, pk)
    turn = session.active_turn()
    delta_raw = (request.POST.get('delta_seconds') or '').strip()
    try:
        delta_seconds = int(delta_raw)
    except ValueError:
        messages.error(request, 'Invalid time adjustment.')
        return redirect('assessment_session_detail', pk=session.pk)

    if turn is None:
        messages.error(request, 'No group is currently presenting.')
        return redirect('assessment_session_detail', pk=session.pk)

    try:
        result = adjust_timer(turn, delta_seconds)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        if result is None:
            messages.success(request, 'Timer ended - voting closed.')
        else:
            messages.success(
                request,
                f'Timer {"extended" if delta_seconds > 0 else "reduced"} '
                f'by {abs(delta_seconds)}s.')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def close_turn(request, pk):
    session = _session(request, pk)
    turn = session.active_turn()
    if turn:
        close_turn_service(turn)
        messages.success(request, f'Voting closed for "{turn.group.name}".')
    return redirect('assessment_session_detail', pk=session.pk)


@lecturer_required
@require_POST
def close_session(request, pk):
    from django.utils import timezone

    session = _session(request, pk)
    session.presentation_turns.filter(status=PresentationTurn.Status.ACTIVE).update(
        status=PresentationTurn.Status.CLOSED, closed_at=timezone.now())
    session.status = AssessmentSession.Status.CLOSED
    session.closed_at = timezone.now()
    session.save(update_fields=['status', 'closed_at'])
    broadcast_session_event(session, 'session.closed', {})
    messages.success(request, 'Session closed. Results are ready.')
    return redirect('assessment_results', pk=session.pk)


@lecturer_required
@require_POST
def toggle_public_results(request, pk):
    """Explicitly publish/revoke the aggregate group scores on the public room screen."""
    from django.db import transaction

    release = request.POST.get('action') == 'release'
    if request.POST.get('action') not in {'release', 'revoke'}:
        raise Http404
    with transaction.atomic():
        locked = _session(request, pk, lock=True)
        if locked.status != AssessmentSession.Status.CLOSED:
            messages.error(request, 'Close the session before releasing a public leaderboard.')
            return redirect('assessment_results', pk=locked.pk)
        if release and not locked.public_results_released_at:
            groups = list(locked.groups.filter(is_practice=False).order_by('order', 'pk'))
            scores = group_score_percents(locked, groups)
            if not groups or any(scores.get(group.pk) is None for group in groups):
                messages.error(request,
                    'Complete scoring for every presentation group before release.')
                return redirect('assessment_results', pk=locked.pk)
        elif not release and not locked.public_leaderboard_show_scores:
            messages.info(request, 'Public scores are already hidden.')
            return redirect('assessment_results', pk=locked.pk)
        elif release and locked.public_leaderboard_show_scores:
            messages.info(request, 'Public scores are already visible.')
            return redirect('assessment_results', pk=locked.pk)
        if release:
            locked.public_results_released_at = timezone.now()
            locked.public_results_released_by = request.user
            summary = 'Released aggregate group scores on the public join screen.'
        else:
            locked.public_results_released_at = None
            locked.public_results_released_by = None
            summary = 'Revoked aggregate group scores from the public join screen.'
        locked.public_leaderboard_show_scores = release
        locked.save(update_fields=(
            'public_results_released_at', 'public_results_released_by', 'public_leaderboard_show_scores'))
        from ..models import SessionChange
        SessionChange.objects.create(session=locked, by_user=request.user, summary=summary)
        transaction.on_commit(lambda: broadcast_session_event(locked, 'public.results.changed', {}))
    messages.success(request, 'Public leaderboard released.' if release
                     else 'Public scores hidden; rankings remain visible.')
    return redirect('assessment_results', pk=locked.pk)


# --------------------------------------------------------- Reporting views --

PARTICIPATION_STATUSES = {'full', 'partial', 'none'}


def _participation_rows(session):
    """The unpaginated per-student participation rows plus their status
    counts - shared by `_participation_context` (which paginates them for
    the page/refresh endpoint) and `results_export` (whose Participation
    sheet needs the full, unpaginated set)."""
    voted_turn_ids_by_student = {}
    for evaluation in Evaluation.objects.filter(
            presentation_turn__assessment_session=session, target_student__isnull=True,
            presentation_turn__group__is_practice=False
    ).values('evaluator_id', 'presentation_turn__group_id'):
        voted_turn_ids_by_student.setdefault(evaluation['evaluator_id'], set()).add(
            evaluation['presentation_turn__group_id'])

    groups = list(session.groups.filter(is_practice=False).order_by('order', 'id'))
    all_group_ids = {g.pk for g in groups}
    groups_by_id = {g.pk: g for g in groups}
    # One query for every membership instead of one per group (and, in the
    # template, one per card per group). Each group then answers
    # `member_ids()` from memory with the same set it would have fetched.
    member_ids_by_group = {group.pk: set() for group in groups}
    for group_id, student_id in GroupMembership.objects.filter(
            group__assessment_session=session, group__is_practice=False).values_list('group_id', 'student_id'):
        member_ids_by_group[group_id].add(student_id)
    own_group_id_by_student = {}
    for group in groups:
        group.member_ids = lambda ids=member_ids_by_group[group.pk]: ids
        for student_id in member_ids_by_group[group.pk]:
            own_group_id_by_student[student_id] = group.pk

    joined_ids = set(session.participation_records.values_list('student_id', flat=True))
    students = session.students.all().order_by('full_name')
    active_turn = session.active_turn()
    if active_turn and active_turn.group.is_practice:
        active_turn = None
    active_group_id = active_turn.group_id if active_turn else None
    rows = []
    counts = {'full': 0, 'partial': 0, 'none': 0}
    for student in students:
        voted_groups = voted_turn_ids_by_student.get(student.pk, set())
        own_group_id = own_group_id_by_student.get(student.pk)
        eligible_group_ids = all_group_ids - ({own_group_id} if own_group_id else set())
        graded_count = len(voted_groups & eligible_group_ids)
        eligible_count = len(eligible_group_ids)
        if eligible_count == 0 or graded_count >= eligible_count:
            status = 'full'
        elif graded_count == 0:
            status = 'none'
        else:
            status = 'partial'
        counts[status] += 1
        progress_pct = 100 if eligible_count == 0 else round((graded_count / eligible_count) * 100)
        rows.append({
            'student': student,
            'joined': student.pk in joined_ids,
            'voted_groups': voted_groups,
            'status': status,
            'graded_count': graded_count,
            'eligible_count': eligible_count,
            'progress_pct': progress_pct,
            # The team banner needs to show even before "Group breakdown" is
            # expanded, and "is this student's own group the one presenting
            # right now" drives the card's live "Presenting" state below.
            'own_group': groups_by_id.get(own_group_id),
            'is_presenting_now': own_group_id is not None and own_group_id == active_group_id,
        })

    return {
        'rows': rows, 'counts': counts, 'groups': groups,
        'joined_count': len(joined_ids), 'roster_count': students.count(),
        'active_group_id': active_group_id,
        'active_group_name': active_turn.group.name if active_turn else None,
    }


def _participation_context(request, session):
    """Shared between the full page and the JSON refresh endpoint it polls
    while the session is live - same split as `_live_context` above, so a
    lecturer who leaves this tab open on a second screen during a session
    sees it update on its own instead of needing a manual reload."""
    base = _participation_rows(session)
    rows = base['rows']
    counts = base['counts']

    status_filter = request.GET.get('status')
    if status_filter in PARTICIPATION_STATUSES:
        rows = [row for row in rows if row['status'] == status_filter]

    # How the page lists people, nothing more: by presenting group in the order the groups
    # present, students without a group last, each group's students still A-Z. The export
    # builds from `_participation_rows` and keeps its own order.
    position = {group.pk: index for index, group in enumerate(base['groups'])}
    rows = sorted(rows, key=lambda row: position[row['own_group'].pk] if row['own_group'] else len(position))

    paginator, page = paginate(request, rows)
    return {
        'session': session, 'groups': base['groups'],
        'page_obj': page, 'paginator': paginator, 'rows': page.object_list,
        'total': paginator.count, 'status_filter': status_filter,
        'joined_count': base['joined_count'], 'roster_count': base['roster_count'],
        'full_count': counts['full'], 'partial_count': counts['partial'], 'none_count': counts['none'],
        'active_group_id': base['active_group_id'],
        'active_group_name': base['active_group_name'],
    }


@lecturer_required
def participation(request, pk):
    session = _session(request, pk, oversight=True)
    context = _participation_context(request, session)
    context['is_owner'] = session.created_by_id == request.user.pk
    return render(request, 'assessments/participation.html', context)


@lecturer_required
def participation_refresh(request, pk):
    """Polled every few seconds while the session is live, so this table -
    plausibly open on a second screen during the session - updates itself
    the same silent way the Live control tab already does, rather than
    only ever refreshing on a manual reload."""
    session = _session(request, pk, oversight=True)
    context = _participation_context(request, session)
    return JsonResponse({
        'status': session.status,
        'cards_html': render_to_string('assessments/_partials/participation_cards.html', context, request=request),
        'stats_html': render_to_string('assessments/_partials/participation_stats.html', context, request=request),
    })


RESULTS_SORT_KEYS = {
    'student': lambda row: (row['student'].full_name or row['student'].student_id or '').lower(),
    'group': lambda row: (row['group'].name or '').lower(),
    'group_percent': lambda row: row['group_percent'] if row['group_percent'] is not None else Decimal('-1'),
    'individual_percent': lambda row: row['individual_percent'] if row['individual_percent'] is not None else Decimal('-1'),
    'penalty': lambda row: row['penalty'],
    'final_percent': lambda row: row['final_percent'],
}


def _results_context(request, session):
    """Shared between the full page and the JSON refresh endpoint it polls
    while the session is live - same split as `_participation_context`, so a
    lecturer who leaves this tab open on a second screen sees scores and
    charts update themselves the instant a new evaluation lands."""
    computed = session_results(session)

    # Sortable columns. Fresh page load leads with the highest scorer -
    # what a lecturer actually wants to eyeball first - while an explicit
    # click on a column header still toggles from whatever asc/desc it says.
    sort = request.GET.get('sort', 'final_percent')
    if 'dir' in request.GET:
        direction = request.GET['dir']
    else:
        direction = 'desc' if sort == 'final_percent' else 'asc'
    key_field = sort[1:] if sort.startswith('-') else sort
    reverse = direction == 'desc'
    sort_fn = RESULTS_SORT_KEYS.get(key_field, RESULTS_SORT_KEYS['final_percent'])
    student_rows = sorted(computed['student_rows'], key=sort_fn, reverse=reverse)

    # A coarse 0-100% distribution in 10-point buckets - the number a
    # lecturer actually wants when sanity-checking a grading run (any
    # outliers or no-shows), which the group-only bar chart doesn't show.
    buckets = [0] * 10
    for row in computed['student_rows']:
        pct = float(row['final_percent'])
        idx = min(int(pct // 10), 9)
        buckets[idx] += 1

    paginator, page = paginate(request, student_rows)
    return {
        'session': session, 'group_rows': computed['group_rows'],
        'page_obj': page, 'paginator': paginator, 'student_rows': page.object_list,
        'total': paginator.count, 'sort': key_field, 'dir': direction,
        'distribution_buckets': buckets,
    }


@lecturer_required
def results(request, pk):
    session = _session(request, pk, oversight=True)
    context = _results_context(request, session)
    context['is_owner'] = session.created_by_id == request.user.pk
    return render(request, 'assessments/results.html', context)


@lecturer_required
def results_refresh(request, pk):
    """Polled (and websocket-triggered) while the session is live, so the
    results table and both charts stay current as evaluations are submitted
    instead of only ever reflecting whatever the page looked like on load."""
    session = _session(request, pk, oversight=True)
    context = _results_context(request, session)
    return JsonResponse({
        'status': session.status,
        'rows_html': render_to_string('assessments/_partials/results_rows.html', context, request=request),
        'group_chart': {
            'labels': [row['group'].name for row in context['group_rows']],
            'values': [float(row['percent']) if row['percent'] is not None else 0 for row in context['group_rows']],
        },
        'distribution_buckets': context['distribution_buckets'],
    })


@lecturer_required
def results_export(request, pk):
    session = _session(request, pk)
    computed = session_results(session)
    participation_rows = _participation_rows(session)['rows']
    buffer = export_results(session, computed, participation_rows)
    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    # The session name is typed by a person and ends up in a response header:
    # a line break in it would make the header invalid (a 500 instead of a
    # download) and a quote would end the filename early.
    filename = ' '.join(re.sub(r'[\x00-\x1f\x7f"\\/]+', ' ', session.name).split()) or 'session'
    response['Content-Disposition'] = f'attachment; filename="{filename}-results.xlsx"'
    return response


@lecturer_required
def results_export_csv(request, pk):
    session = _session(request, pk)
    response = HttpResponse(export_results_csv(session, session_results(session)),
                            content_type='text/csv; charset=utf-8')
    filename = ' '.join(re.sub(r'[\x00-\x1f\x7f"\\/]+', ' ', session.name).split()) or 'session'
    response['Content-Disposition'] = f'attachment; filename="{filename}-results.csv"'
    return response


@lecturer_required
def session_qr(request, pk):
    session = _session(request, pk)
    return HttpResponse(qr_png(join_url(request, session)), content_type='image/png')


@lecturer_required
def session_transfer(request, pk):
    """Hand a session to another lecturer in the same organization.

    The owner's access ends the moment this succeeds - the session simply stops
    being theirs - so the page says so before it asks, and a successful transfer
    sends them back to their list rather than to a page they can no longer open.

    Somebody with oversight may hand over any session in the organization, not
    only their own; it is recorded with them as the person who did it.
    """
    if in_public_workspace(request):
        # Everybody in the shared workspace is a stranger to everybody else, so
        # there is nobody to hand a session to and no list to show.
        raise Http404
    session = _session(request, pk, oversight=True)
    recipients = list(eligible_recipients(session))
    is_owner = session.created_by_id == request.user.pk
    transfer_error = ''
    selected_owner = ''

    if request.method == 'POST':
        selected_owner = request.POST.get('new_owner', '')
        try:
            chosen = next(user for user in recipients if str(user.pk) == selected_owner)
        except StopIteration:
            transfer_error = 'Choose one of the lecturers in the list.'
        else:
            try:
                transfer_session(session, chosen, by_user=request.user)
            except TransferError as error:
                # Only if the recipient stopped being eligible between the list
                # being drawn and this request.
                transfer_error = str(error)
            else:
                # Former owners return to My sessions, where the handed-over
                # session is no longer listed. An overseer who transferred a
                # different person's session stays on the organization-wide list,
                # unless they took ownership themselves.
                show_all = can_oversee(request) and not is_owner and chosen.pk != request.user.pk
                back = reverse('assessment_session_list') + ('?scope=all' if show_all else '')
                messages.success(
                    request, f'"{session.name}" now belongs to {chosen.get_full_name() or chosen.username}.')
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    # Let the browser perform the redirect. If fetch follows
                    # this redirect, the list page consumes the message before
                    # the actual navigation can display it.
                    return JsonResponse({'redirect_url': back})
                return redirect(back)

    context = {
        'session': session, 'recipients': recipients, 'is_owner': is_owner,
        'transfer_error': transfer_error, 'selected_owner': selected_owner,
    }
    return render(request, 'assessments/_session_transfer_modal.html', context)
