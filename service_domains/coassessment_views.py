"""HTTP views and controllers for the Co-Assessment service.

Manages setup wizards, live presentation console, mobile peer scorecards,
examiner evaluation sheet, variance moderation, and auditorium projector.
"""

import csv
from decimal import Decimal
from functools import wraps
import hashlib
import re
import secrets

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from accounts.access import lecturer_required
from assessments.qr import qr_png
from assessments.pagination import PAGE_SIZE, PAGE_SIZES, paginate
from .coassessment_forms import (
    CoAssessmentPolicyForm,
    CoAssessmentCriterionForm,
    CoAssessmentGroupForm,
    CoAssessmentParticipantForm,
    CoAssessmentExaminerForm,
    CoAssessmentMemberForm,
    CoAssessmentModerationForm,
)
from .coassessment_operations import (
    activate_stage_turn,
    compute_session_results,
    compute_turn_results,
    conclude_stage_turn,
    delete_coassessment_sessions,
    get_coassessment_live_state,
    next_display_order,
    pause_stage_turn,
    record_chair_moderation,
    release_session_results,
    roster_sample_workbook,
    sign_off_session,
    submit_examiner_scorecard,
    submit_peer_scorecard,
    update_stage_timers,
)
from . import embedded_import
from .coassessment_import import (
    apply_examiners, apply_roster, apply_rubric, examiner_sample, parse_examiners, parse_roster,
    parse_rubric, plan_examiners, plan_roster, plan_rubric, rubric_sample,
)
from .forms import SpreadsheetImportForm
from .tasks import send_coassessment_examiner_invitation_email
from portal.stepper import build_steps
from .models import (
    CoAssessmentSession,
    CoAssessmentCriterion,
    CoAssessmentGroup,
    CoAssessmentParticipant,
    CoAssessmentExaminer,
    CoAssessmentTurn,
    CoAssessmentScorecard,
    CoAssessmentModeration,
)


_WIZARD_STEPS = (
    ('policy', 'Session & Weights', 'coassessment_setup_policy'),
    ('rubric', 'Shared Rubric', 'coassessment_setup_rubric'),
    ('roster', 'Presenting Groups', 'coassessment_setup_roster'),
    ('examiners', 'Panel Examiners', 'coassessment_setup_examiners'),
    ('review', 'Review', 'coassessment_setup_review'),
)


def _wizard_steps(session, active):
    """Stepper steps for the co-assessment setup wizard. Before the session
    is saved only the first step is open."""
    done = {
        'policy': session is not None,
        'rubric': session is not None and session.is_rubric_balanced(),
        'roster': session is not None and session.groups.exists(),
        'examiners': session is not None and session.examiners.exists(),
        'review': session is not None and session.status != CoAssessmentSession.Status.DRAFT,
    }
    return build_steps([
        {'key': key, 'label': label, 'done': done[key], 'optional': key == 'examiners',
         'final': key == 'review',
         **({'href': reverse(url_name, args=[session.pk])} if session is not None else {})}
        for key, label, url_name in _WIZARD_STEPS
    ], active, locked_after_first=session is None)


def _coassessment_owner(view):
    """Restricts access to authenticated organizers within the co-assessment service."""
    authenticated = lecturer_required(view)

    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'coassessment':
            raise Http404
        return authenticated(request, *args, **kwargs)
    return scoped


def _get_owned_session(request, session_id):
    """Safely retrieves a session scoped to the active organization and owner."""
    return (
        CoAssessmentSession.objects.for_organization(request.organization)
        .owned_by(request.user)
        .filter(pk=session_id)
        .first()
    )


# ==============================================================================
# 1. ORGANIZER DASHBOARD & TABLE LIST VIEW
# ==============================================================================

@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_dashboard(request):
    """Primary dashboard listing key metrics, live stages, and recent events."""
    sessions = list(
        CoAssessmentSession.objects.for_organization(request.organization)
        .owned_by(request.user)
        .annotate(
            group_count=Count('groups', distinct=True),
            criterion_count=Count('criteria', distinct=True),
            examiner_count=Count('examiners', distinct=True),
        )
        .order_by('-created_at', '-pk')
    )

    counts = {
        'total': len(sessions),
        'active': sum(s.status == CoAssessmentSession.Status.ACTIVE for s in sessions),
        'draft': sum(s.status == CoAssessmentSession.Status.DRAFT for s in sessions),
        'review': sum(s.status == CoAssessmentSession.Status.REVIEW for s in sessions),
        'signed_off': sum(s.status == CoAssessmentSession.Status.SIGNED_OFF for s in sessions),
        'released': sum(s.status == CoAssessmentSession.Status.RELEASED for s in sessions),
        'total_groups': sum(s.group_count for s in sessions),
        'total_criteria': sum(s.criterion_count for s in sessions),
    }

    attention_sessions = [
        s for s in sessions
        if s.status in (
            CoAssessmentSession.Status.ACTIVE,
            CoAssessmentSession.Status.REVIEW,
            CoAssessmentSession.Status.SIGNED_OFF,
        )
    ]

    stage_distribution = []
    stage_colors = {
        'draft': '#d28a08',
        'active': 'var(--success)',
        'review': 'var(--accent)',
        'signed_off': 'var(--primary)',
        'released': 'var(--text-muted)',
    }
    pie_segments = []
    cumulative = 0
    stage_definitions = (
        ('draft', 'Draft', counts['draft']),
        ('active', 'In progress', counts['active']),
        ('review', 'Review & moderation', counts['review']),
        ('signed_off', 'Signed off', counts['signed_off']),
        ('released', 'Results released', counts['released']),
    )
    for key, label, count in stage_definitions:
        start_deg = cumulative * 360 / counts['total'] if counts['total'] else 0
        cumulative += count
        end_deg = cumulative * 360 / counts['total'] if counts['total'] else 0
        stage_distribution.append({
            'key': key,
            'label': label,
            'count': count,
            'percentage': round(count * 100 / counts['total'], 1) if counts['total'] else 0,
        })
        if count:
            pie_segments.append(f'{stage_colors[key]} {start_deg:.3f}deg {end_deg:.3f}deg')
    stage_pie_background = (
        f"conic-gradient({', '.join(pie_segments)})" if pie_segments else ''
    )

    largest_sessions = sorted(
        sessions,
        key=lambda s: (-s.group_count, -s.created_at.timestamp() if s.created_at else 0, s.pk)
    )[:5]
    largest_group_count = max((s.group_count for s in largest_sessions), default=0)
    for s in largest_sessions:
        s.group_bar_percent = round(s.group_count * 100 / largest_group_count, 1) if largest_group_count else 0

    recent_sessions = sessions[:5]

    context = {
        'dashboard_stats': counts,
        'attention_sessions': attention_sessions,
        'stage_distribution': stage_distribution,
        'stage_pie_background': stage_pie_background,
        'largest_sessions': largest_sessions,
        'largest_group_count': largest_group_count,
        'recent_sessions': recent_sessions,
        'active_tab': 'dashboard',
    }
    return render(request, 'coassessment/dashboard.html', context)


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_table(request):
    """Full table list view with debounced search, status filter, and pagination."""
    visible = (
        CoAssessmentSession.objects.for_organization(request.organization)
        .owned_by(request.user)
    )
    counts = visible.aggregate(
        total=Count('pk'),
        draft=Count('pk', filter=Q(status=CoAssessmentSession.Status.DRAFT)),
        active=Count('pk', filter=Q(status=CoAssessmentSession.Status.ACTIVE)),
        review=Count('pk', filter=Q(status=CoAssessmentSession.Status.REVIEW)),
        signed_off=Count('pk', filter=Q(status=CoAssessmentSession.Status.SIGNED_OFF)),
        released=Count('pk', filter=Q(status=CoAssessmentSession.Status.RELEASED)),
    )
    search_query = request.GET.get('q', '').strip()[:100]
    status_filter = request.GET.get('status', '')
    if status_filter not in CoAssessmentSession.Status.values:
        status_filter = ''
    filtered = visible
    if search_query:
        filtered = filtered.filter(name__icontains=search_query)
    if status_filter:
        filtered = filtered.filter(status=status_filter)

    queryset = (
        filtered
        .annotate(
            group_count=Count('groups', distinct=True),
            criterion_count=Count('criteria', distinct=True),
            examiner_count=Count('examiners', distinct=True),
        )
        .order_by('-created_at', '-pk')
    )

    paginator, page = paginate(request, queryset)

    query_params = QueryDict('', mutable=True)
    if search_query:
        query_params['q'] = search_query
    if status_filter:
        query_params['status'] = status_filter
    query_params['per_page'] = str(page.paginator.per_page)

    context = {
        'sessions': page,
        'page_obj': page,
        'paginator': paginator,
        'per_page': page.paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'status_counts': counts,
        'status_filter': status_filter,
        'search_query': search_query,
        'querystring': query_params.urlencode(),
        'filtered_count': paginator.count,
    }

    template = (
        'service_domains/_partials/coassessment_results.html'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest'
        else 'coassessment/coassessment_table.html'
    )
    response = render(request, template, context)
    patch_vary_headers(response, ['X-Requested-With'])
    return response


def _coassessment_bulk_delete_redirect(request):
    """Return to the filtered table list view, preserving search and pagination parameters."""
    params = QueryDict('', mutable=True)
    query = (request.POST.get('q') or '').strip()[:100]
    status_filter = request.POST.get('status', '')
    if query:
        params['q'] = query
    if status_filter in CoAssessmentSession.Status.values:
        params['status'] = status_filter
    try:
        per_page = int(request.POST.get('per_page', PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE
    params['per_page'] = str(per_page)
    url = reverse('coassessment_table')
    return redirect(f'{url}?{params.urlencode()}' if params else url)


@require_POST
@never_cache
@_coassessment_owner
def coassessment_delete(request, session_id):
    """Deletes an owned co-assessment session."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404
    name = session.name
    delete_coassessment_sessions([session])
    messages.success(request, f'"{name}" deleted.')
    return redirect('coassessment_table')


@require_POST
@never_cache
@_coassessment_owner
def coassessment_bulk_delete(request):
    """Deletes an explicitly selected set of owned co-assessment sessions."""
    raw_ids = request.POST.getlist('session_ids')
    if (not raw_ids or len(raw_ids) > max(PAGE_SIZES)
            or any(not re.fullmatch(r'[1-9][0-9]*', value)
                   or len(value) > 19
                   or (len(value) == 19 and value > '9223372036854775807')
                   for value in raw_ids)):
        messages.error(request, 'Select one or more valid co-assessments to delete.')
        return _coassessment_bulk_delete_redirect(request)

    session_ids = {int(value) for value in raw_ids}
    owned_sessions = (
        CoAssessmentSession.objects.for_organization(request.organization)
        .owned_by(request.user)
    )
    with transaction.atomic():
        selected = list(owned_sessions.select_for_update().filter(pk__in=session_ids))
        if len(selected) != len(session_ids):
            messages.error(request, 'One or more selected co-assessments are no longer available to delete.')
            return _coassessment_bulk_delete_redirect(request)
        count = len(selected)
        delete_coassessment_sessions(selected)

    messages.success(request, f'{count} co-assessment{"s" if count != 1 else ""} deleted.')
    return _coassessment_bulk_delete_redirect(request)



# ==============================================================================
# 2. CREATION WIZARD (STEPS 1 - 4)
# ==============================================================================

@_coassessment_owner
@require_http_methods(['GET', 'POST'])
def coassessment_create_step1(request, session_id=None):
    """Step 1: Set event metadata, role weights, thresholds, and timers."""
    session = None
    if session_id:
        session = _get_owned_session(request, session_id)
        if not session:
            raise Http404

    if request.method == 'POST':
        form = CoAssessmentPolicyForm(request.POST, instance=session)
        if form.is_valid():
            saved_session = form.save(commit=False)
            if not session:
                saved_session.organization_id = request.organization['id']
                saved_session.created_by = request.user
            saved_session.save()
            messages.success(request, f'Co-assessment "{saved_session.name}" saved.')
            return redirect('coassessment_setup_rubric', session_id=saved_session.pk)
    else:
        form = CoAssessmentPolicyForm(instance=session)

    return render(request, 'coassessment/wizard_step_policy.html', {
        'form': form, 'session': session, 'stepper_steps': _wizard_steps(session, 'policy'),
    })


def _import_state(request, kind, session, plan):
    """Preview, token and one-time errors for an embedded import card."""
    result, token = embedded_import.load_preview(request, kind, session, plan)
    return {
        'plan': result, 'token': token,
        'errors': embedded_import.take_errors(request, kind, session),
        'form': SpreadsheetImportForm(auto_id=f'id_coassessment_{kind}_%s'),
    }


def _import_back(session, url_name):
    return lambda: redirect(url_name, session_id=session.pk)


def _xlsx_response(workbook, filename):
    response = HttpResponse(
        workbook.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['Cache-Control'] = 'private, no-store'
    return response


def _owned_or_404(request, session_id):
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404
    return session


@_coassessment_owner
@require_http_methods(['GET', 'POST'])
def coassessment_setup_rubric(request, session_id):
    """Step 2: Define shared rubric criteria (weights summing to 100%)."""
    session = _owned_or_404(request, session_id)
    action = request.POST.get('action') if request.method == 'POST' else ''
    editing = None
    form = None

    if action in ('add_criterion', 'update_criterion'):
        if action == 'update_criterion':
            editing = session.criteria.filter(pk=request.POST.get('criterion_id') or 0).first()
            if editing is None:
                messages.error(request, 'That criterion no longer exists.')
                return redirect('coassessment_setup_rubric', session_id=session.pk)
        form = CoAssessmentCriterionForm(request.POST, instance=editing, session=session)
        if form.is_valid():
            crit = form.save(commit=False)
            if editing is None:
                crit.organization_id = session.organization_id
                crit.created_by = request.user
                crit.session = session
                crit.order = next_display_order(session.criteria.all())
            crit.save()
            verb = 'updated' if editing else 'added'
            messages.success(request, f'Criterion "{crit.name}" {verb}.')
            return redirect('coassessment_setup_rubric', session_id=session.pk)
    elif action == 'delete_criterion':
        crit = session.criteria.filter(pk=request.POST.get('criterion_id') or 0).first()
        if crit:
            crit.delete()
            messages.success(request, 'Criterion removed.')
        return redirect('coassessment_setup_rubric', session_id=session.pk)
    else:
        edit_id = request.GET.get('edit', '')
        if edit_id.isdigit():
            editing = session.criteria.filter(pk=int(edit_id)).first()

    criteria = list(session.criteria.all().order_by('order', 'pk'))
    total_weight = sum((c.weight for c in criteria), Decimal('0'))
    return render(request, 'coassessment/wizard_step_rubric.html', {
        'session': session,
        'stepper_steps': _wizard_steps(session, 'rubric'),
        'criteria': criteria,
        'total_weight': total_weight,
        'remaining_weight': Decimal('100.00') - total_weight,
        'is_balanced': total_weight == Decimal('100.00'),
        'form': form or CoAssessmentCriterionForm(instance=editing, session=session),
        'editing': editing,
        'import_state': _import_state(request, 'rubric', session, plan_rubric),
    })


@_coassessment_owner
@require_POST
@never_cache
def coassessment_rubric_import(request, session_id):
    """Preview, apply or cancel a rubric spreadsheet; returns to the Rubric step."""
    session = _owned_or_404(request, session_id)
    return embedded_import.handle_post(
        request, kind='rubric', owner=session, form_class=SpreadsheetImportForm,
        parse=parse_rubric, plan=plan_rubric, apply=apply_rubric,
        success='Rubric imported.', back=_import_back(session, 'coassessment_setup_rubric'))


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_rubric_sample(request, session_id):
    _owned_or_404(request, session_id)
    return _xlsx_response(rubric_sample(), 'coassessment-rubric-sample.xlsx')


@_coassessment_owner
@require_http_methods(['GET', 'POST'])
def coassessment_setup_roster(request, session_id):
    """Step 3: Add presenting groups and students by spreadsheet or by hand."""
    session = _owned_or_404(request, session_id)
    group_form = CoAssessmentGroupForm()
    action = request.POST.get('action') if request.method == 'POST' else ''

    if action == 'add_group':
        group_form = CoAssessmentGroupForm(request.POST)
        if group_form.is_valid():
            grp = group_form.save(commit=False)
            grp.organization_id = session.organization_id
            grp.created_by = request.user
            grp.session = session
            grp.order = next_display_order(session.groups.all())
            grp.save()
            messages.success(request, f'Team "{grp.name}" added to lineup.')
            return redirect('coassessment_setup_roster', session_id=session.pk)

    elif action == 'update_group':
        target_group = session.groups.filter(pk=request.POST.get('group_id') or 0).first()
        if target_group is None:
            messages.error(request, 'That team no longer exists.')
        else:
            edit_form = CoAssessmentGroupForm(request.POST, instance=target_group)
            if edit_form.is_valid():
                edit_form.save()
                messages.success(request, f'Team "{target_group.name}" updated.')
            else:
                messages.error(request, ' '.join(
                    error for errors in edit_form.errors.values() for error in errors))
        return redirect('coassessment_setup_roster', session_id=session.pk)

    elif action == 'add_member':
        target_group = session.groups.filter(pk=request.POST.get('group_id') or 0).first()
        member_form = CoAssessmentMemberForm(request.POST, session=session)
        if target_group is None:
            messages.error(request, 'Choose a team to add the student to.')
        elif member_form.is_valid():
            member = member_form.save(commit=False)
            member.organization_id = session.organization_id
            member.created_by = request.user
            member.session = session
            member.group = target_group
            member.save()
            messages.success(request, f'{member.full_name} added to {target_group.name}.')
        else:
            messages.error(request, ' '.join(
                error for errors in member_form.errors.values() for error in errors))
        return redirect('coassessment_setup_roster', session_id=session.pk)

    elif action == 'remove_member':
        member = session.participants.filter(pk=request.POST.get('participant_id') or 0).first()
        if member:
            member.delete()
            messages.success(request, 'Student removed from the roster.')
        return redirect('coassessment_setup_roster', session_id=session.pk)

    elif action == 'delete_group':
        grp = session.groups.filter(pk=request.POST.get('group_id') or 0).first()
        if grp:
            grp.delete()
            messages.success(request, 'Team removed from lineup.')
        return redirect('coassessment_setup_roster', session_id=session.pk)

    groups = list(session.groups.prefetch_related('members').order_by('order', 'pk'))
    return render(request, 'coassessment/wizard_step_roster.html', {
        'session': session,
        'stepper_steps': _wizard_steps(session, 'roster'),
        'groups': groups,
        'student_count': sum(len(grp.members.all()) for grp in groups),
        'group_form': group_form,
        'member_form': CoAssessmentMemberForm(),
        'import_state': _import_state(request, 'roster', session, plan_roster),
    })


@_coassessment_owner
@require_POST
@never_cache
def coassessment_roster_import_apply(request, session_id):
    """Preview, apply or cancel a roster spreadsheet; returns to the Groups step."""
    session = _owned_or_404(request, session_id)
    return embedded_import.handle_post(
        request, kind='roster', owner=session, form_class=SpreadsheetImportForm,
        parse=parse_roster, plan=plan_roster, apply=apply_roster,
        success='Roster imported.', back=_import_back(session, 'coassessment_setup_roster'))


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_roster_sample(request, session_id):
    """Downloads the sample roster workbook."""
    _owned_or_404(request, session_id)
    return _xlsx_response(roster_sample_workbook(), 'coassessment-roster-sample.xlsx')


def _queue_examiner_invitation(request, examiner):
    """Queues the examiner invitation email after the transaction commits."""
    origin = request.build_absolute_uri('/').rstrip('/')

    def enqueue():
        try:
            send_coassessment_examiner_invitation_email.delay(examiner.pk, origin)
        except Exception:
            pass

    transaction.on_commit(enqueue)


@_coassessment_owner
@require_http_methods(['GET', 'POST'])
def coassessment_setup_examiners(request, session_id):
    """Step 4: Invite panel examiners and manage their private scoring links."""
    session = _owned_or_404(request, session_id)

    examiners = list(session.examiners.all().order_by('display_name'))
    for ex in examiners:
        if not ex.invite_token:
            raw_token = secrets.token_urlsafe(32)
            ex.invite_token = raw_token
            ex.token_digest = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
            ex.save(update_fields=['invite_token', 'token_digest'])

    examiner_form = CoAssessmentExaminerForm()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'invite_examiner':
            examiner_form = CoAssessmentExaminerForm(request.POST)
            if examiner_form.is_valid():
                ex = examiner_form.save(commit=False)
                ex.organization_id = session.organization_id
                ex.created_by = request.user
                ex.session = session
                raw_token = secrets.token_urlsafe(32)
                ex.invite_token = raw_token
                ex.token_digest = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
                ex.save()
                _queue_examiner_invitation(request, ex)
                messages.success(request, f'Examiner {ex.display_name} invited. The email with their private link is queued for {ex.email}.')
                return redirect('coassessment_setup_examiners', session_id=session.pk)

        elif action == 'resend_invitation':
            ex = session.examiners.filter(pk=request.POST.get('examiner_id') or 0).first()
            if ex:
                _queue_examiner_invitation(request, ex)
                messages.success(request, f'Invitation email queued again for {ex.email}. The link is unchanged.')
            return redirect('coassessment_setup_examiners', session_id=session.pk)

        elif action == 'rotate_token':
            ex = session.examiners.filter(pk=request.POST.get('examiner_id') or 0).first()
            if ex:
                raw_token = secrets.token_urlsafe(32)
                ex.invite_token = raw_token
                ex.token_digest = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
                ex.save(update_fields=['invite_token', 'token_digest'])
                _queue_examiner_invitation(request, ex)
                messages.success(request, f'Access link regenerated for {ex.display_name}. The new email is queued for {ex.email}.')
            return redirect('coassessment_setup_examiners', session_id=session.pk)

        elif action == 'delete_examiner':
            ex = session.examiners.filter(pk=request.POST.get('examiner_id') or 0).first()
            if ex:
                ex.delete()
                messages.success(request, 'Examiner invitation revoked.')
            return redirect('coassessment_setup_examiners', session_id=session.pk)

    return render(request, 'coassessment/wizard_step_examiners.html', {
        'session': session,
        'stepper_steps': _wizard_steps(session, 'examiners'),
        'examiners': examiners,
        'form': examiner_form,
        'import_state': _import_state(request, 'examiners', session, plan_examiners),
    })


@_coassessment_owner
@require_POST
@never_cache
def coassessment_examiner_import(request, session_id):
    """Preview, apply or cancel an examiner spreadsheet; invites go out on apply."""
    session = _owned_or_404(request, session_id)

    def apply(owner, parsed, *, organization, user):
        for examiner in apply_examiners(owner, parsed, organization=organization, user=user):
            _queue_examiner_invitation(request, examiner)

    return embedded_import.handle_post(
        request, kind='examiners', owner=session, form_class=SpreadsheetImportForm,
        parse=parse_examiners, plan=plan_examiners, apply=apply,
        success='Examiners invited. Each gets an email with a private link.',
        back=_import_back(session, 'coassessment_setup_examiners'))


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_examiner_sample(request, session_id):
    _owned_or_404(request, session_id)
    return _xlsx_response(examiner_sample(), 'coassessment-examiners-sample.xlsx')


def _readiness(session):
    """What must be true before going live; `blocking` items stop publishing."""
    total = session.rubric_total_weight()
    teams = session.groups.count()
    examiners = session.examiners.count()
    needed = session.minimum_panel_evaluators
    return [
        {'label': 'Rubric totals 100%', 'ok': total == Decimal('100.00'), 'blocking': True,
         'detail': f'Currently {total}%.', 'url': 'coassessment_setup_rubric'},
        {'label': 'At least one presenting team', 'ok': teams > 0, 'blocking': True,
         'detail': f'{teams} team{"" if teams == 1 else "s"} in the lineup.',
         'url': 'coassessment_setup_roster'},
        {'label': f'{needed} panel examiner{"" if needed == 1 else "s"} invited', 'ok': examiners >= needed,
         'blocking': False, 'detail': f'{examiners} invited. You can still invite more while live.',
         'url': 'coassessment_setup_examiners'},
    ]


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_setup_review(request, session_id):
    """Step 5: Read-only review; going live happens here, running it in the console."""
    session = _owned_or_404(request, session_id)
    checks = _readiness(session)
    return render(request, 'coassessment/wizard_step_review.html', {
        'session': session,
        'stepper_steps': _wizard_steps(session, 'review'),
        'checks': checks,
        'can_publish': all(item['ok'] for item in checks if item['blocking']),
        'is_draft': session.status == CoAssessmentSession.Status.DRAFT,
        'team_count': session.groups.count(),
        'student_count': session.participants.count(),
        'criteria_count': session.criteria.count(),
        'examiner_count': session.examiners.count(),
    })


@_coassessment_owner
@require_POST
def coassessment_publish(request, session_id):
    """Validates full readiness and transitions the session to active live state."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    if session.status != CoAssessmentSession.Status.DRAFT:
        return redirect('coassessment_live_console', session_id=session.pk)

    total_weight = sum((c.weight for c in session.criteria.all()), Decimal('0'))
    if total_weight != Decimal('100.00'):
        messages.error(request, 'Rubric criteria weights must total exactly 100% before going live.')
        return redirect('coassessment_setup_rubric', session_id=session.pk)

    if not session.groups.exists():
        messages.error(request, 'At least one presenting team is required to go live.')
        return redirect('coassessment_setup_roster', session_id=session.pk)

    session.status = CoAssessmentSession.Status.ACTIVE
    session.save(update_fields=['status', 'updated_at'])
    messages.success(request, f'"{session.name}" is now live. Open the stage console to begin presentations.')
    return redirect('coassessment_live_console', session_id=session.pk)


# ==============================================================================
# 3. ORGANIZER LIVE CONSOLE & STAGE HUD
# ==============================================================================

@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_live_console(request, session_id):
    """The master control HUD for orchestrating presentations and tracking quorums."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).select_related('group').first()
    live_state = get_coassessment_live_state(session)

    # Lineup of all groups with their latest turn and rater counts
    groups = list(session.groups.prefetch_related('members').order_by('order', 'pk'))
    group_lineup = []
    for grp in groups:
        latest_turn = session.turns.filter(group=grp).order_by('-pk').first()
        panel_raters = 0
        peer_raters = 0
        turn_status = 'queued'
        if latest_turn:
            panel_raters = latest_turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PANEL).count()
            peer_raters = latest_turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PEER).count()
            if latest_turn.status == CoAssessmentTurn.Status.ACTIVE:
                turn_status = 'active'
            elif latest_turn.status == CoAssessmentTurn.Status.CLOSED:
                turn_status = 'closed'
        group_lineup.append({
            'group': grp,
            'latest_turn': latest_turn,
            'panel_raters': panel_raters,
            'peer_raters': peer_raters,
            'turn_status': turn_status,
        })

    return render(request, 'coassessment/live_console.html', {
        'session': session,
        'public_screen_url': reverse('coassessment_public_screen', kwargs={'code': session.public_join_code}),
        'join_url': request.build_absolute_uri(
            reverse('coassessment_peer_join', kwargs={'code': session.public_join_code})),
        'is_live': session.status == CoAssessmentSession.Status.ACTIVE,
        'server_now_ms': int(timezone.now().timestamp() * 1000),
        'active_turn': active_turn,
        'live_state': live_state,
        'group_lineup': group_lineup,
        'total_turns': len(groups),
        'presentation_minutes': session.presentation_duration_seconds // 60,
        'qa_minutes': session.qa_duration_seconds // 60,
        'panel_count': live_state['panel_count'],
        'panel_total': live_state['panel_total'],
        'panel_percent': live_state['panel_percent'],
        'panel_quorum_met': live_state['panel_quorum_met'],
        'peer_count': live_state['peer_count'],
        'peer_total': live_state['peer_total'],
        'peer_percent': live_state['peer_percent'],
        'peer_quorum_met': live_state['peer_quorum_met'],
    })


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_live_state(request, session_id):
    """JSON API endpoint returning real-time timer and quorum data for live polling."""
    session = _get_owned_session(request, session_id)
    if not session:
        return JsonResponse({'error': 'Not found'}, status=404)

    return JsonResponse(get_coassessment_live_state(session))


@_coassessment_owner
@require_POST
def coassessment_update_timers(request, session_id):
    """Saves the presentation and Q&A clock lengths from the live console."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404
    try:
        update_stage_timers(
            session,
            request.POST.get('presentation_minutes'),
            request.POST.get('qa_minutes'),
        )
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': '; '.join(exc.messages)}, status=400)
    return JsonResponse({
        'ok': True,
        'presentation_minutes': session.presentation_duration_seconds // 60,
        'qa_minutes': session.qa_duration_seconds // 60,
    })


def _stage_closed_response(request, session):
    """Stage actions only make sense while the session is live."""
    if session.status == CoAssessmentSession.Status.ACTIVE:
        return None
    messages.error(request, 'The stage is only open while the session is live.')
    return redirect('coassessment_live_console', session_id=session.pk)


@_coassessment_owner
@require_POST
def coassessment_activate_turn(request, session_id, group_id):
    """Calls a specific group to the stage."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404
    group = get_object_or_404(session.groups, pk=group_id)
    closed = _stage_closed_response(request, session)
    if closed:
        return closed

    try:
        activate_stage_turn(session, group, user=request.user)
        messages.success(request, f'Team "{group.name}" is now on stage.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))

    return redirect('coassessment_live_console', session_id=session.pk)


@_coassessment_owner
@require_POST
def coassessment_advance_turn(request, session_id):
    """Atomically locks the active turn and calls the next group in lineup."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    closed = _stage_closed_response(request, session)
    if closed:
        return closed

    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
    if active_turn:
        conclude_stage_turn(session, user=request.user)

    # Find next group in queue that has not presented
    presented_group_ids = session.turns.filter(
        status=CoAssessmentTurn.Status.CLOSED
    ).values_list('group_id', flat=True)
    next_group = session.groups.exclude(pk__in=presented_group_ids).order_by('order', 'pk').first()

    if next_group:
        activate_stage_turn(session, next_group, user=request.user)
        messages.success(request, f'Turn locked. Next team "{next_group.name}" is on stage.')
    else:
        session.status = CoAssessmentSession.Status.REVIEW
        session.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'All scheduled teams have completed their presentations. Entering review stage.')
        return redirect('coassessment_results', session_id=session.pk)

    return redirect('coassessment_live_console', session_id=session.pk)


@_coassessment_owner
@require_POST
def coassessment_pause_turn(request, session_id):
    """Pauses the current active presentation."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    closed = _stage_closed_response(request, session)
    if closed:
        return closed

    turn = pause_stage_turn(session, user=request.user)
    if turn is not None:
        messages.info(request, 'Presentation paused.' if turn.is_paused else 'Presentation resumed.')
    return redirect('coassessment_live_console', session_id=session.pk)


# ==============================================================================
# 4. RESULTS, MODERATION & SIGN-OFF
# ==============================================================================

@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_results(request, session_id):
    """Rollup gradebook displaying comparative marks, discrepancy flags, and audit log."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    rows = compute_session_results(session)
    moderation_pending_count = sum(1 for r in rows if r['moderation_required'] and not r['latest_moderation'])

    total_groups = session.groups.count()
    closed_turns_count = session.turns.filter(status=CoAssessmentTurn.Status.CLOSED).count()
    all_groups_presented = (total_groups > 0 and closed_turns_count >= total_groups)

    can_sign_off = (
        moderation_pending_count == 0
        and all_groups_presented
        and session.status in (CoAssessmentSession.Status.ACTIVE, CoAssessmentSession.Status.REVIEW)
    )

    return render(request, 'coassessment/results_moderation.html', {
        'session': session,
        'rows': rows,
        'moderation_pending_count': moderation_pending_count,
        'all_groups_presented': all_groups_presented,
        'closed_turns_count': closed_turns_count,
        'total_groups': total_groups,
        'can_sign_off': can_sign_off,
    })


@_coassessment_owner
@require_POST
def coassessment_moderate(request, session_id, turn_id):
    """Records an audited chair moderation decision for a flagged presentation."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    turn = get_object_or_404(session.turns, pk=turn_id)
    action = request.POST.get('action')
    final_score = request.POST.get('final_score')
    justification = request.POST.get('justification')

    try:
        record_chair_moderation(turn, action, final_score, justification, user=request.user)
        messages.success(request, f'Moderation decision logged for team "{turn.group.name}".')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))

    return redirect('coassessment_results', session_id=session.pk)


@_coassessment_owner
@require_POST
def coassessment_sign_off(request, session_id):
    """Locks marks under formal chair sign-off."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    try:
        sign_off_session(session, user=request.user)
        messages.success(request, 'Co-assessment signed off successfully.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))

    return redirect('coassessment_results', session_id=session.pk)


@_coassessment_owner
@require_POST
def coassessment_release(request, session_id):
    """Releases marks to the public leaderboard and student view."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    try:
        release_session_results(session, user=request.user)
        messages.success(request, 'Results released to public leaderboard.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))

    return redirect('coassessment_results', session_id=session.pk)


@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_export_csv(request, session_id):
    """Exports full comparative gradebook to CSV for external LMS sync."""
    session = _get_owned_session(request, session_id)
    if not session:
        raise Http404

    rows = compute_session_results(session)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="coassessment_{session.pk}_results.csv"'

    has_ind = session.individual_criteria().exists()
    writer = csv.writer(response)
    if has_ind:
        writer.writerow([
            'Team', 'Topic', 'Student Name', 'Student ID',
            'Weighted Group Mark', 'Weighted Individual Mark', 'Final Grade',
            'Team Panel Score', 'Team Peer Score', 'Discrepancy', 'Moderated'
        ])
        for r in rows:
            if r['member_results']:
                for m in r['member_results']:
                    writer.writerow([
                        r['group_name'],
                        r['group_topic'],
                        m['full_name'],
                        m['student_id'],
                        m['group_score'],
                        m['individual_score'],
                        m['final_score'],
                        r['panel_score'] or '',
                        r['peer_score'] or '',
                        r['discrepancy'] or '',
                        'Yes' if r['latest_moderation'] else 'No',
                    ])
            else:
                writer.writerow([
                    r['group_name'],
                    r['group_topic'],
                    'Entire Team',
                    '',
                    r['group_mark'] or r['final_mark'] or '',
                    '0.00',
                    r['final_mark'] or '',
                    r['panel_score'] or '',
                    r['peer_score'] or '',
                    r['discrepancy'] or '',
                    'Yes' if r['latest_moderation'] else 'No',
                ])
    else:
        writer.writerow([
            'Order', 'Team', 'Topic', 'Panel Score', 'Panel Raters',
            'Peer Score', 'Peer Raters', 'Discrepancy', 'Combined Score', 'Final Mark', 'Moderated'
        ])
        for r in rows:
            writer.writerow([
                r.get('order', ''),
                r['group_name'],
                r['group_topic'],
                r['panel_score'] or '',
                r['panel_raters'],
                r['peer_score'] or '',
                r['peer_raters'],
                r['discrepancy'] or '',
                r['combined_score'] or '',
                r['final_mark'] or '',
                'Yes' if r['latest_moderation'] else 'No',
            ])
    return response


# ==============================================================================
# 5. PUBLIC AUDITORIUM STAGE PROJECTOR DISPLAY
# ==============================================================================

@_coassessment_owner
@require_http_methods(['GET'])
def coassessment_stage_display(request, session_id):
    """Owner shortcut to the public screen of this session."""
    session = _owned_or_404(request, session_id)
    return redirect('coassessment_public_screen', code=session.public_join_code)


def _session_by_code(code):
    return get_object_or_404(CoAssessmentSession, public_join_code=code.upper())


@require_http_methods(['GET'])
def coassessment_public_screen(request, code):
    """Venue screen: join QR and PIN, the team on stage, its clock, and released standings.

    Public by the join code, which already admits peers, so it shows nothing a
    peer cannot see.
    """
    session = _session_by_code(code)
    released = session.status == CoAssessmentSession.Status.RELEASED
    active_turn = (session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE)
                   .select_related('group').first())
    rows = compute_session_results(session) if released else []
    rows.sort(key=lambda row: row['final_mark'] or Decimal('0'), reverse=True)
    response = render(request, 'coassessment/stage_display.html', {
        'session': session,
        'active_turn': active_turn,
        'is_released': released,
        'leaderboard_rows': rows,
        'join_url': request.build_absolute_uri(
            reverse('coassessment_peer_join', kwargs={'code': session.public_join_code})),
        'state_url': reverse('coassessment_public_state', kwargs={'code': session.public_join_code}),
        'qr_url': reverse('coassessment_public_qr', kwargs={'code': session.public_join_code}),
    })
    response['Cache-Control'] = 'no-store'
    return response


PUBLIC_STATE_FIELDS = (
    'session_status', 'has_active_turn', 'active_turn_id', 'active_group_name', 'active_group_topic',
    'timer_paused', 'phase', 'presentation_remaining_seconds', 'qa_remaining_seconds')


@require_http_methods(['GET'])
def coassessment_public_state(request, code):
    """What the venue screen may know: the team on stage and its clock, nothing else."""
    session = _session_by_code(code)
    state = get_coassessment_live_state(session)
    response = JsonResponse({field: state[field] for field in PUBLIC_STATE_FIELDS})
    response['Cache-Control'] = 'no-store'
    return response


@require_http_methods(['GET'])
def coassessment_public_qr(request, code):
    """The peer join link as a QR code image for the venue screen."""
    session = _session_by_code(code)
    join = request.build_absolute_uri(
        reverse('coassessment_peer_join', kwargs={'code': session.public_join_code}))
    response = HttpResponse(qr_png(join), content_type='image/png')
    response['Cache-Control'] = 'no-store'
    return response


# ==============================================================================
# 6. STUDENT PEER MOBILE PORTAL
# ==============================================================================

@require_http_methods(['GET', 'POST'])
def coassessment_peer_join(request, code):
    """Peer scan entry door: identifies student against session roster."""
    session = get_object_or_404(CoAssessmentSession, public_join_code=code.upper())

    if request.method == 'POST':
        identifier = request.POST.get('identifier', '').strip()
        participant = session.participants.filter(student_id__iexact=identifier).first()
        if not participant:
            participant = session.participants.filter(full_name__iexact=identifier).first()

        if not participant:
            messages.error(request, 'Student ID or name was not found on the session roster.')
        else:
            request.session[f'co_peer_{session.pk}'] = participant.pk
            return redirect('coassessment_peer_scorecard', session_id=session.pk)

    return render(request, 'coassessment/peer_join.html', {'session': session})


@require_http_methods(['GET'])
def coassessment_peer_scorecard(request, session_id):
    """Student peer mobile scorecard with self-voting prevention."""
    session = get_object_or_404(CoAssessmentSession, pk=session_id)
    participant_id = request.session.get(f'co_peer_{session.pk}')
    participant = session.participants.filter(pk=participant_id).first() if participant_id else None

    if not participant:
        return redirect('coassessment_peer_join', code=session.public_join_code)

    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).select_related('group').first()
    criteria = list(session.criteria.all().order_by('order', 'pk'))
    group_criteria = [c for c in criteria if c.scope == CoAssessmentCriterion.Scope.GROUP]
    individual_criteria = [c for c in criteria if c.scope == CoAssessmentCriterion.Scope.INDIVIDUAL]
    presenting_members = list(active_turn.group.members.all().order_by('full_name', 'pk')) if active_turn else []

    is_self_group = bool(active_turn and participant.group_id == active_turn.group_id)
    already_submitted = False
    if active_turn:
        already_submitted = active_turn.scorecards.filter(
            evaluator_tier=CoAssessmentScorecard.Tier.PEER,
            peer_evaluator=participant,
        ).exists()

    return render(request, 'coassessment/peer_scorecard.html', {
        'session': session,
        'participant': participant,
        'active_turn': active_turn,
        'criteria': criteria,
        'group_criteria': group_criteria,
        'individual_criteria': individual_criteria,
        'presenting_members': presenting_members,
        'has_individual_criteria': bool(individual_criteria),
        'is_self_group': is_self_group,
        'already_submitted': already_submitted,
    })


def _unscored_items(session, members, group_scores, individual_scores):
    """Names of every criterion (per presenter for individual ones) the rater left unscored."""
    missing = [c.name for c in session.criteria.filter(scope=CoAssessmentCriterion.Scope.GROUP).order_by('order', 'pk')
               if c.pk not in group_scores]
    for crit in session.criteria.filter(scope=CoAssessmentCriterion.Scope.INDIVIDUAL).order_by('order', 'pk'):
        missing.extend('%s for %s' % (crit.name, mem.full_name) for mem in members
                       if (crit.pk, mem.pk) not in individual_scores)
    return missing


@require_POST
def coassessment_peer_submit(request, session_id):
    """Processes student peer rating submission."""
    session = get_object_or_404(CoAssessmentSession, pk=session_id)
    participant_id = request.session.get(f'co_peer_{session.pk}')
    participant = session.participants.filter(pk=participant_id).first() if participant_id else None

    if not participant:
        return redirect('coassessment_peer_join', code=session.public_join_code)

    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
    if not active_turn:
        messages.error(request, 'No presentation is currently active.')
        return redirect('coassessment_peer_scorecard', session_id=session.pk)

    group_scores = {}
    for crit in session.criteria.filter(scope=CoAssessmentCriterion.Scope.GROUP):
        val = request.POST.get(f'criterion_{crit.pk}')
        if val is not None and val != '':
            group_scores[crit.pk] = val

    individual_scores = {}
    members = list(active_turn.group.members.all())
    for crit in session.criteria.filter(scope=CoAssessmentCriterion.Scope.INDIVIDUAL):
        for mem in members:
            val = request.POST.get(f'individual_criterion_{mem.pk}_{crit.pk}') or request.POST.get(f'criterion_{crit.pk}_{mem.pk}')
            if val is not None and val != '':
                individual_scores[(crit.pk, mem.pk)] = val

    missing = _unscored_items(session, members, group_scores, individual_scores)
    if missing:
        messages.error(request, 'Almost there. Please score: %s.' % ', '.join(missing))
        return redirect('coassessment_peer_scorecard', session_id=session.pk)

    feedback = request.POST.get('qualitative_feedback', '')
    try:
        submit_peer_scorecard(active_turn, participant, group_scores, feedback, individual_scores=individual_scores)
        messages.success(request, 'Your peer evaluation has been submitted.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))

    return redirect('coassessment_peer_scorecard', session_id=session.pk)


# ==============================================================================
# 7. PANEL EXAMINER PORTAL
# ==============================================================================

@require_http_methods(['GET', 'POST'])
def coassessment_examiner_portal(request, token):
    """Panel examiner authenticated sheet with calibrated level descriptors."""
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    examiner = get_object_or_404(CoAssessmentExaminer, token_digest=digest)
    session = examiner.session

    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).select_related('group').first()
    criteria = list(session.criteria.prefetch_related('descriptors').order_by('order', 'pk'))
    presenting_members = list(active_turn.group.members.all().order_by('full_name', 'pk')) if active_turn else []

    existing_scorecard = None
    existing_scores = {}
    existing_ind_scores = {}
    already_submitted = False
    if active_turn:
        existing_scorecard = active_turn.scorecards.filter(
            evaluator_tier=CoAssessmentScorecard.Tier.PANEL,
            examiner_evaluator=examiner,
        ).prefetch_related('values').first()
        if existing_scorecard:
            already_submitted = True
            for s in existing_scorecard.values.all():
                if s.target_participant_id is None:
                    existing_scores[s.criterion_id] = float(s.score)
                else:
                    existing_ind_scores[(s.criterion_id, s.target_participant_id)] = float(s.score)

    if request.method == 'POST' and active_turn:
        group_scores = {}
        for crit in session.criteria.filter(scope=CoAssessmentCriterion.Scope.GROUP):
            val = request.POST.get(f'criterion_{crit.pk}')
            if val is not None and val != '':
                group_scores[crit.pk] = val

        individual_scores = {}
        for crit in session.criteria.filter(scope=CoAssessmentCriterion.Scope.INDIVIDUAL):
            for mem in presenting_members:
                val = request.POST.get(f'individual_criterion_{mem.pk}_{crit.pk}') or request.POST.get(f'criterion_{crit.pk}_{mem.pk}')
                if val is not None and val != '':
                    individual_scores[(crit.pk, mem.pk)] = val

        feedback = request.POST.get('qualitative_feedback', '')
        try:
            submit_examiner_scorecard(active_turn, examiner, group_scores, feedback, individual_scores=individual_scores)
            messages.success(request, f'Examiner marks recorded for "{active_turn.group.name}".')
            return redirect('coassessment_examiner_portal', token=token)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))

    group_criteria = [c for c in criteria if c.scope == CoAssessmentCriterion.Scope.GROUP]
    individual_criteria = [c for c in criteria if c.scope == CoAssessmentCriterion.Scope.INDIVIDUAL]

    for crit in group_criteria:
        crit.existing_score = existing_scores.get(crit.pk, 75.0)

    member_data = []
    for mem in presenting_members:
        mem_criteria = []
        for crit in individual_criteria:
            existing_val = existing_ind_scores.get((crit.pk, mem.pk), 75.0)
            mem_criteria.append({
                'criterion': crit,
                'existing_score': existing_val,
            })
        member_data.append({
            'member': mem,
            'criteria': mem_criteria,
        })

    return render(request, 'coassessment/examiner_scorecard.html', {
        'session': session,
        'examiner': examiner,
        'active_turn': active_turn,
        'criteria': criteria,
        'group_criteria': group_criteria,
        'individual_criteria': individual_criteria,
        'presenting_members': presenting_members,
        'member_data': member_data,
        'has_individual_criteria': bool(individual_criteria),
        'already_submitted': already_submitted,
        'existing_feedback': existing_scorecard.qualitative_feedback if existing_scorecard else '',
    })


@require_http_methods(['GET'])
def coassessment_public_results(request, code):
    """Public read-only presentation standings and certified results view."""
    session = get_object_or_404(CoAssessmentSession, public_join_code=code.upper())
    is_released = (session.status == CoAssessmentSession.Status.RELEASED)
    rows = []
    if is_released:
        raw_rows = compute_session_results(session)
        rows = sorted(
            raw_rows,
            key=lambda r: (r['final_mark'] is not None, r['final_mark'] or Decimal('0')),
            reverse=True,
        )
    return render(request, 'coassessment/public_results.html', {
        'session': session,
        'is_released': is_released,
        'rows': rows,
    })

