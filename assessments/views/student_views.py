import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import OperationalError
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from ..forms import JoinForm
from ..models import AssessmentSession, Evaluation, ParticipationRecord, PresentationTurn, RubricCategory
from ..privacy import (
    NOTICE_SHOWN_SESSION_KEY, active_privacy_notice, notice_seen_key,
)
from ..qr import join_url, qr_png
from ..realtime import broadcast_session_event
from ..services import (
    already_submitted, eligible_to_evaluate, match_student, record_join,
    student_from_request, submit_evaluation,
)
from ..scoring import group_score_percents
from ..turns import ensure_turn_progressed

__all__ = [
    'join', 'privacy_notice', 'evaluate', 'submitted', 'check_turn_expiry', 'grading_ping',
    'join_screen', 'join_screen_state', 'join_screen_qr',
]


def _get_session(session_uuid):
    return get_object_or_404(AssessmentSession, uuid=session_uuid)


def join(request, session_uuid):
    session = _get_session(session_uuid)
    existing = student_from_request(request, session)
    if existing:
        return redirect('student_evaluate', session_uuid=session.uuid)

    error = None
    if request.method == 'POST':
        if session.joining_locked:
            return render(request, 'assessments/student/join.html', {
                'session': session, 'form': JoinForm(), 'joining_closed': True,
                'field_label': 'your full name' if session.identify_by == AssessmentSession.IdentifyBy.FULL_NAME else 'your student ID',
            })
        form = JoinForm(request.POST)
        if form.is_valid():
            student = match_student(session, form.cleaned_data['identifier'])
            if student is None:
                error = ("We couldn't find you on the roster. Check what you typed, "
                          "or ask to be added.")
            else:
                record_join(request, student)
                broadcast_session_event(session, 'participation.updated', {
                    'joined_count': session.participation_records.count(),
                })
                return redirect('student_evaluate', session_uuid=session.uuid)
    else:
        form = JoinForm()

    field_label = 'your full name' if session.identify_by == AssessmentSession.IdentifyBy.FULL_NAME else 'your student ID'
    if error or form.errors:
        form.fields['identifier'].widget.attrs['aria-invalid'] = 'true'
        form.fields['identifier'].widget.attrs['aria-describedby'] = 'identifier-help identifier-errors'
    return render(request, 'assessments/student/join.html', {
        'session': session, 'form': form, 'error': error, 'field_label': field_label,
        'joining_closed': session.joining_locked,
    })


@never_cache
@require_http_methods(['GET', 'POST'])
def privacy_notice(request, session_uuid):
    """Show the current service notice and record the exact version delivered."""
    session = _get_session(session_uuid)
    student = student_from_request(request, session)
    if student is None:
        return redirect('student_join', session_uuid=session.uuid)
    notice = active_privacy_notice(session)
    if notice is None:
        return render(request, 'assessments/student/privacy_unavailable.html',
                      {'session': session}, status=503)

    participation, _ = ParticipationRecord.objects.get_or_create(
        assessment_session=session, student=student)
    seen = request.session.get(NOTICE_SHOWN_SESSION_KEY, {})
    if not isinstance(seen, dict):
        seen = {}
    marker = notice_seen_key(session, student)
    if request.method == 'POST':
        if (request.POST.get('action') != 'continue'
                or seen.get(marker) != notice.pk):
            return render(request, 'assessments/student/privacy_notice.html', {
                'session': session, 'notice': notice,
                'error': 'Review the notice on this page before continuing.',
            }, status=400)
        ParticipationRecord.objects.filter(pk=participation.pk).update(privacy_notice=notice)
        seen.pop(marker, None)
        request.session[NOTICE_SHOWN_SESSION_KEY] = seen
        return redirect('student_evaluate', session_uuid=session.uuid)

    if participation.privacy_notice_id == notice.pk:
        return redirect('student_evaluate', session_uuid=session.uuid)
    seen[marker] = notice.pk
    request.session[NOTICE_SHOWN_SESSION_KEY] = seen
    return render(request, 'assessments/student/privacy_notice.html', {
        'session': session, 'notice': notice,
    })


@never_cache
def evaluate(request, session_uuid):
    session = _get_session(session_uuid)
    as_json = 'application/json' in request.headers.get('Accept', '')
    student = student_from_request(request, session)
    if student is None:
        if as_json:
            return JsonResponse({'status': 'join_required', 'message': 'Join the session again to continue.',
                'next_url': reverse('student_join', args=[session.uuid])}, status=403)
        return redirect('student_join', session_uuid=session.uuid)
    if session.status != AssessmentSession.Status.CLOSED:
        notice = active_privacy_notice(session)
        if notice is None:
            if as_json:
                return JsonResponse({'status': 'privacy_notice_unavailable'}, status=503)
            return render(request, 'assessments/student/privacy_unavailable.html',
                          {'session': session}, status=503)
        participation = ParticipationRecord.objects.filter(
            assessment_session=session, student=student).first()
        if participation is None or participation.privacy_notice_id != notice.pk:
            next_url = reverse('student_privacy_notice', args=[session.uuid])
            if as_json:
                return JsonResponse({'status': 'privacy_notice_required', 'next_url': next_url}, status=428)
            return redirect('student_privacy_notice', session_uuid=session.uuid)
    if session.status == AssessmentSession.Status.LIVE:
        ensure_turn_progressed(session)
    turn = session.active_turn()
    eligible = eligible_to_evaluate(student, turn)
    already_done = turn is not None and already_submitted(student, turn)
    error = None
    key = request.POST.get('submission_key', '') if request.method == 'POST' else ''
    if request.method == 'POST':
        posted_turn_id = request.POST.get('turn_id')
        post_turn = turn
        try:
            if posted_turn_id:
                post_turn = PresentationTurn.objects.filter(pk=int(posted_turn_id), assessment_session=session).first()
            if (key or as_json) and not posted_turn_id:
                raise ValidationError('A retry must identify the original presentation turn.', code='invalid')
            if as_json and not key:
                raise ValidationError('A submission key is required.', code='invalid')
            if post_turn is None:
                raise ValidationError("Time ran out before your evaluation was submitted, so it wasn't counted.", code='expired')
            # Old HTML clients retain their existing already-submitted screen.
            if not key and already_submitted(student, post_turn):
                receipt = None
            else:
                if not key and (post_turn.status != PresentationTurn.Status.ACTIVE or (post_turn.voting_ends_at is not None and post_turn.voting_ends_at <= timezone.now())):
                    raise ValidationError("Time ran out before your evaluation was submitted, so it wasn't counted.", code='expired')
                group_categories = session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP)
                individual_categories = list(session.rubric_categories.filter(scope=RubricCategory.Scope.INDIVIDUAL))
                group_scores = {cat.pk: _parse_decimal(request.POST.get(f'group_{cat.pk}')) for cat in group_categories}
                individual_scores = {}
                if individual_categories:
                    for member in post_turn.group.memberships.select_related('student'):
                        individual_scores[member.student_id] = {
                            cat.pk: _parse_decimal(request.POST.get(f'ind_{member.student_id}_{cat.pk}'))
                            for cat in individual_categories}
                receipt = submit_evaluation(post_turn, student, group_scores, individual_scores, key=key)
            if receipt is not None:
                if not receipt.was_replayed:
                    broadcast_session_event(session, 'evaluation.submitted', {
                        'group_id': post_turn.group_id, 'student_id': student.pk})
                if as_json:
                    return JsonResponse({'status': 'accepted', 'replayed': receipt.was_replayed,
                        'next_url': reverse('student_submitted', args=[session.uuid])},
                        status=200 if receipt.was_replayed else 201)
                return redirect('student_submitted', session_uuid=session.uuid)
        except (ValidationError, InvalidOperation, TypeError, ValueError) as exc:
            error = exc.messages[0] if hasattr(exc, 'messages') and exc.messages else 'Invalid submission.'
            if as_json:
                code = getattr(exc, 'code', None) or 'invalid'
                status = 410 if code == 'expired' else 409 if code in ('conflict', 'paused', 'not_open', 'ineligible', 'already_submitted') else 400
                return JsonResponse({'status': 'rejected', 'code': code, 'message': error}, status=status)
        except OperationalError:
            error = 'The connection could not be confirmed. Keep your scores and retry.'
            if as_json:
                return JsonResponse({'status': 'retry', 'message': error}, status=503)
    if as_json:
        return JsonResponse({'status': session.status, 'turn_id': turn.pk if turn else None,
            'voting_open': bool(turn and turn.is_voting_open), 'voting_paused': session.voting_paused,
            'already_submitted': already_done, 'csrf_token': get_token(request)})
    if session.status == AssessmentSession.Status.CLOSED:
        return render(request, 'assessments/student/closed.html', {'session': session, 'student': student})

    categories = session.rubric_categories.prefetch_related('level_descriptors')
    group_categories = categories.filter(scope=RubricCategory.Scope.GROUP)
    individual_categories = categories.filter(scope=RubricCategory.Scope.INDIVIDUAL)
    teammates = list(turn.group.memberships.all()) if turn else []

    all_groups = list(session.groups.all().order_by('order', 'id').prefetch_related('memberships__student'))
    closed_group_ids = set(session.presentation_turns.filter(status=PresentationTurn.Status.CLOSED).values_list('group_id', flat=True))
    completed_groups = [g for g in all_groups if g.pk in closed_group_ids]
    active_group = turn.group if turn else None
    next_group = session.pending_next_group or session.get_effective_next_group()

    # Determine remaining queue in order
    pending_groups = [
        g for g in all_groups
        if g.pk not in closed_group_ids and (not active_group or g.pk != active_group.pk)
    ]
    # If next_group is designated and in pending_groups, put it at the front of upcoming list
    if next_group and next_group in pending_groups:
        upcoming_groups = [next_group] + [g for g in pending_groups if g.pk != next_group.pk]
    else:
        upcoming_groups = pending_groups

    total_groups_count = len(all_groups)
    completed_count = len(completed_groups)
    current_index = completed_count + (1 if active_group else 0)

    return render(request, 'assessments/student/evaluate.html', {
        'session': session, 'student': student, 'turn': turn,
        'submission_key': key or str(uuid.uuid4()),
        'eligible': eligible, 'already_done': already_done, 'error': error,
        'group_categories': group_categories, 'individual_categories': individual_categories,
        'teammates': teammates,
        'pending_transition_at': session.pending_transition_at if turn is None else None,
        'next_group': next_group,
        'completed_groups': completed_groups,
        'upcoming_groups': upcoming_groups,
        'all_groups': all_groups,
        'total_groups_count': total_groups_count,
        'completed_count': completed_count,
        'current_index': current_index,
    })


def submitted(request, session_uuid):
    session = _get_session(session_uuid)
    student = student_from_request(request, session)
    if student is None:
        return redirect('student_join', session_uuid=session.uuid)
    last_was_practice = (Evaluation.objects.filter(evaluator=student,
        presentation_turn__assessment_session=session).order_by('-submitted_at', '-pk')
        .values_list('presentation_turn__group__is_practice', flat=True).first())
    return render(request, 'assessments/student/submitted.html', {
        'session': session, 'student': student, 'is_practice': bool(last_was_practice)})


@require_POST
def check_turn_expiry(request, session_uuid):
    """Called by a client's own countdown the instant it hits zero, so the
    close-and-advance happens right then rather than waiting for the next
    scheduled Celery tick (which may never come if no worker is running) or
    for someone to reload the page. Harmless to call repeatedly - it's a
    thin, idempotent wrapper around `ensure_turn_progressed`, and every
    resulting state change is broadcast over the websocket exactly as if a
    lecturer or the scheduled task had triggered it, so every connected
    client updates the same way regardless of who happened to call this."""
    session = _get_session(session_uuid)
    if session.status == AssessmentSession.Status.LIVE:
        ensure_turn_progressed(session)
    return JsonResponse({'ok': True})


@require_POST
def grading_ping(request, session_uuid):
    """Heartbeat fired every few seconds by an eligible student's open
    evaluate form, purely so the lecturer's Participation tab can show a
    "grading now" indicator *before* the vote is submitted. Nothing is
    persisted - this only broadcasts over the session's websocket group, the
    same fire-and-forget shape as a chat app's typing indicator, and a
    missed or stale ping just means the lecturer's UI stops showing it a few
    seconds later (see the sweep in participation.html)."""
    session = _get_session(session_uuid)
    student = student_from_request(request, session)
    if student is None:
        return JsonResponse({'ok': False}, status=403)

    turn = session.active_turn()
    if turn is None or not eligible_to_evaluate(student, turn) or already_submitted(student, turn):
        return JsonResponse({'ok': False})

    ParticipationRecord.objects.filter(
        assessment_session=session, student=student
    ).update(last_seen_at=timezone.now())
    cache.set(f'assessment-grading:{session.uuid}:{student.pk}', True, timeout=15)

    try:
        answered = int(request.POST.get('answered', 0))
        total = int(request.POST.get('total', 0))
    except (TypeError, ValueError):
        answered, total = 0, 0

    broadcast_session_event(session, 'grading.progress', {
        'student_id': student.pk,
        'group_id': turn.group_id,
        'answered': answered,
        'total': total,
    })
    return JsonResponse({'ok': True})


@never_cache
@require_http_methods(['GET'])
def join_screen(request, session_uuid):
    """A no-login page meant to be projected in the room: the join QR code
    plus the session's name and live status, nothing else. Reachable only
    by the session's UUID - the same "can't be guessed or enumerated"
    identifier the join flow already uses (`AssessmentSession.uuid`) -
    rather than a separate share token, since this is just another public
    view of the same join link the QR on the admin panel already encodes.
    """
    session = _get_session(session_uuid)
    if session.service != 'classroom':
        raise Http404
    response = render(request, 'assessments/student/join_screen.html', {
        'session': session, **_public_live_state(session),
    })
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


def _public_live_state(session):
    """Aggregate-only state for the projected room display; never names a student."""
    if session.status == AssessmentSession.Status.LIVE:
        ensure_turn_progressed(session)
    turn = session.active_turn()
    joined_count = session.participation_records.count()
    student_count = session.students.count()
    # One cache round trip for the whole roster instead of one per student.
    grading_now_count = sum(1 for flag in cache.get_many(
        [f'assessment-grading:{session.uuid}:{student_id}'
         for student_id in session.students.values_list('pk', flat=True)]).values() if flag)
    total_groups = session.groups.count()
    completed_groups = session.presentation_turns.filter(
        status=PresentationTurn.Status.CLOSED).values('group_id').distinct().count()
    all_groups_complete = (session.status == AssessmentSession.Status.LIVE and not turn
                           and not session.pending_next_group and total_groups > 0
                           and completed_groups >= total_groups)
    if turn:
        submitted_count = Evaluation.objects.filter(
            presentation_turn=turn, target_student__isnull=True,
            presentation_turn__group__is_practice=False).values('evaluator_id').distinct().count()
    elif all_groups_complete:
        submitted_count = Evaluation.objects.filter(
            presentation_turn__assessment_session=session, target_student__isnull=True,
            presentation_turn__group__is_practice=False).count()
    else:
        submitted_count = 0
    if session.status == AssessmentSession.Status.CLOSED:
        stage = 'Session complete'
    elif session.status != AssessmentSession.Status.LIVE:
        stage = 'Waiting for session to start'
    elif session.pending_next_group:
        stage = 'Transitioning to next group'
    elif all_groups_complete:
        stage = 'All presentations complete'
    elif turn and not turn.is_voting_open:
        stage = 'Presentation in progress'
    elif turn:
        stage = 'Voting open'
    else:
        stage = 'Waiting for presentation'

    next_group = session.pending_next_group or session.get_effective_next_group()
    current_index = None
    if turn:
        ordered_group_ids = list(session.groups.order_by('order', 'id').values_list('pk', flat=True))
        if turn.group_id in ordered_group_ids:
            current_index = ordered_group_ids.index(turn.group_id) + 1

    leaderboard = _public_group_leaderboard(session)
    return {
        'public_session_status': session.status,
        'public_stage': ('Practice - ' + stage) if turn and turn.group.is_practice else stage,
        'public_is_practice': bool(turn and turn.group.is_practice),
        'public_group_name': turn.group.name if turn else '',
        'public_group_topic': turn.group.topic if turn else '',
        'public_started_at': turn.opened_at.isoformat() if turn and turn.opened_at else '',
        'public_presentation_ends_at': turn.presentation_ends_at.isoformat() if turn and turn.presentation_ends_at else '',
        'public_ends_at': turn.voting_ends_at.isoformat() if turn and turn.voting_ends_at else '',
        'public_transition_ends_at': session.pending_transition_at.isoformat() if session.pending_transition_at else '',
        'public_next_group_name': next_group.name if next_group else '',
        'public_total_groups': total_groups,
        'public_completed_groups': completed_groups,
        'public_all_groups_complete': all_groups_complete,
        'public_current_index': current_index,
        'public_joined_count': joined_count,
        'public_student_count': student_count,
        'public_submitted_count': submitted_count,
        'public_grading_now_count': grading_now_count,
        'public_joining_locked': session.joining_locked,
        'public_leaderboard_released': session.status != AssessmentSession.Status.DRAFT,
        'public_show_scores': session.public_leaderboard_show_scores,
        'public_group_points_max': format(session.group_weight_total(), 'f'),
        'public_leaderboard_provisional': session.status != AssessmentSession.Status.CLOSED,
        'public_leaderboard': leaderboard,
    }


def _public_group_leaderboard(session):
    """Completed-group rankings; numeric scores never leave the server when hidden."""
    if session.service != 'classroom' or session.status == AssessmentSession.Status.DRAFT:
        return []
    from ..leaderboard import group_standings
    rows = []
    for standing in group_standings(session, completed_only=True):
        row = {'rank': standing['rank'], 'group_name': standing['group'].name}
        if session.public_leaderboard_show_scores:
            score = standing['percent']
            row['score'] = format(score, 'f') if score is not None else None
        rows.append(row)
    return rows


@never_cache
@require_http_methods(['GET'])
def join_screen_state(request, session_uuid):
    session = _get_session(session_uuid)
    if session.service != 'classroom':
        raise Http404
    response = JsonResponse(_public_live_state(session))
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@never_cache
@require_http_methods(['GET'])
def join_screen_qr(request, session_uuid):
    """PNG for the join screen above - a separate, unauthenticated
    counterpart to the admin-only `session_qr` (which requires login and
    organization scoping); both just wrap the same `qr_png(join_url(...))`.
    """
    session = _get_session(session_uuid)
    if session.service != 'classroom':
        raise Http404
    response = HttpResponse(qr_png(join_url(request, session)), content_type='image/png')
    response['Cache-Control'] = 'private, no-store'
    return response


def _parse_decimal(raw):
    if raw is None or str(raw).strip() == '':
        raise ValidationError('Please score every category before submitting.')
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise ValidationError('Scores must be numbers.')
