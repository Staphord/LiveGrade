from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import JoinForm
from ..models import AssessmentSession, Evaluation, ParticipationRecord, PresentationTurn, RubricCategory
from ..qr import join_url, qr_png
from ..realtime import broadcast_session_event
from ..services import (
    already_submitted, eligible_to_evaluate, match_student, record_join,
    student_from_request, submit_evaluation,
)
from ..turns import ensure_turn_progressed

__all__ = [
    'join', 'evaluate', 'submitted', 'check_turn_expiry', 'grading_ping',
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
                'session': session, 'form': JoinForm(),
                'error': 'Joining is closed for this live session. Please ask your lecturer for help.',
                'field_label': 'your full name' if session.identify_by == AssessmentSession.IdentifyBy.FULL_NAME else 'your student ID',
            })
        form = JoinForm(request.POST)
        if form.is_valid():
            student = match_student(session, form.cleaned_data['identifier'])
            if student is None:
                error = ("We couldn't find you on the roster. Check what you typed, "
                          "or ask your lecturer to add you.")
            else:
                record_join(request, student)
                broadcast_session_event(session, 'participation.updated', {
                    'joined_count': session.participation_records.count(),
                })
                return redirect('student_evaluate', session_uuid=session.uuid)
    else:
        form = JoinForm()

    field_label = 'your full name' if session.identify_by == AssessmentSession.IdentifyBy.FULL_NAME else 'your student ID'
    return render(request, 'assessments/student/join.html', {
        'session': session, 'form': form, 'error': error, 'field_label': field_label,
    })


def evaluate(request, session_uuid):
    session = _get_session(session_uuid)
    student = student_from_request(request, session)
    if student is None:
        return redirect('student_join', session_uuid=session.uuid)

    if session.status == AssessmentSession.Status.LIVE:
        # Self-healing: if the active turn's timer already ran out (e.g. no
        # Celery worker is running to pick up the scheduled task), catch it
        # up right now instead of leaving the page frozen on an expired
        # countdown until someone manually intervenes.
        ensure_turn_progressed(session)

    turn = session.active_turn()
    error = None
    submitted = False

    if session.status == AssessmentSession.Status.CLOSED:
        return render(request, 'assessments/student/closed.html', {'session': session, 'student': student})

    eligible = eligible_to_evaluate(student, turn)
    already_done = turn is not None and already_submitted(student, turn)

    if request.method == 'POST':
        # Resolve the turn this POST was actually scored against by its own
        # `turn_id`, not whatever `session.active_turn()` says right now -
        # `ensure_turn_progressed` above may have already closed that turn
        # and opened the next one before this request was handled, and
        # scoring against "whatever's active now" would silently attribute
        # a late submission to the wrong group. No `turn_id` (e.g. a direct
        # POST from before this field existed) falls back to `turn` as
        # before.
        posted_turn_id = request.POST.get('turn_id')
        post_turn = turn
        if posted_turn_id:
            post_turn = PresentationTurn.objects.filter(
                pk=posted_turn_id, assessment_session=session).first()

        post_eligible = post_turn is not None and eligible_to_evaluate(student, post_turn)
        post_already_done = post_turn is not None and already_submitted(student, post_turn)
        turn_expired = (
            post_turn is None or post_turn.status != PresentationTurn.Status.ACTIVE
            or (post_turn.voting_ends_at is not None and post_turn.voting_ends_at <= timezone.now()))

        if post_turn and post_eligible and not post_already_done and not turn_expired:
            group_categories = list(session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP))
            individual_categories = list(session.rubric_categories.filter(scope=RubricCategory.Scope.INDIVIDUAL))
            teammates = [m.student for m in post_turn.group.memberships.all()]

            try:
                group_scores = {
                    cat.pk: _parse_decimal(request.POST.get(f'group_{cat.pk}'))
                    for cat in group_categories
                }
                individual_scores = {}
                if individual_categories:
                    for teammate in teammates:
                        individual_scores[teammate.pk] = {
                            cat.pk: _parse_decimal(request.POST.get(f'ind_{teammate.pk}_{cat.pk}'))
                            for cat in individual_categories
                        }
                submit_evaluation(post_turn, student, group_scores, individual_scores)
                submitted = True
                # Wakes the lecturer's live panel the instant a vote lands, not
                # just when a turn opens/closes - rankings, the "N of M voted"
                # count on the hero, and participation are all computed fresh
                # from submitted scores, so without this they were correct on
                # the next unrelated event but silently stale in between.
                broadcast_session_event(session, 'evaluation.submitted', {
                    'group_id': post_turn.group_id, 'student_id': student.pk,
                })
            except (ValidationError, InvalidOperation, TypeError) as exc:
                error = exc.messages[0] if hasattr(exc, 'messages') and exc.messages else str(exc)
        elif turn_expired and not post_already_done:
            # Deliberately not scored against whatever's active now, and
            # not counted as a vote for the group this was meant for - the
            # student simply didn't grade it in time.
            error = "Time ran out before your evaluation was submitted, so it wasn't counted."

    if submitted:
        return redirect('student_submitted', session_uuid=session.uuid)

    group_categories = session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP)
    individual_categories = session.rubric_categories.filter(scope=RubricCategory.Scope.INDIVIDUAL)
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
    return render(request, 'assessments/student/submitted.html', {'session': session, 'student': student})


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


def join_screen(request, session_uuid):
    """A no-login page meant to be projected in the room: the join QR code
    plus the session's name and live status, nothing else. Reachable only
    by the session's UUID - the same "can't be guessed or enumerated"
    identifier the join flow already uses (`AssessmentSession.uuid`) -
    rather than a separate share token, since this is just another public
    view of the same join link the QR on the admin panel already encodes.
    """
    session = _get_session(session_uuid)
    return render(request, 'assessments/student/join_screen.html', {'session': session, **_public_live_state(session)})


def _public_live_state(session):
    """Aggregate-only state for the projected room display; never names a student."""
    if session.status == AssessmentSession.Status.LIVE:
        ensure_turn_progressed(session)
    turn = session.active_turn()
    joined_count = session.participation_records.count()
    student_count = session.students.count()
    submitted_count = 0
    if turn:
        submitted_count = Evaluation.objects.filter(
            presentation_turn=turn, target_student__isnull=True
        ).values('evaluator_id').distinct().count()
    grading_now_count = sum(
        1 for student in session.students.only('pk')
        if cache.get(f'assessment-grading:{session.uuid}:{student.pk}')
    )
    if session.status == AssessmentSession.Status.CLOSED:
        stage = 'Session complete'
    elif session.status != AssessmentSession.Status.LIVE:
        stage = 'Waiting for session to start'
    elif session.pending_next_group:
        stage = 'Transitioning to next group'
    elif turn and not turn.is_voting_open:
        stage = 'Presentation in progress'
    elif turn:
        stage = 'Voting open'
    else:
        stage = 'Waiting for presentation'

    next_group = session.pending_next_group or session.get_effective_next_group()
    total_groups = session.groups.count()
    completed_groups = session.presentation_turns.filter(status=PresentationTurn.Status.CLOSED).values('group_id').distinct().count()
    current_index = None
    if turn:
        ordered_group_ids = list(session.groups.order_by('order', 'id').values_list('pk', flat=True))
        if turn.group_id in ordered_group_ids:
            current_index = ordered_group_ids.index(turn.group_id) + 1

    return {
        'public_stage': stage,
        'public_group_name': turn.group.name if turn else '',
        'public_started_at': turn.opened_at.isoformat() if turn and turn.opened_at else '',
        'public_presentation_ends_at': turn.presentation_ends_at.isoformat() if turn and turn.presentation_ends_at else '',
        'public_ends_at': turn.voting_ends_at.isoformat() if turn and turn.voting_ends_at else '',
        'public_transition_ends_at': session.pending_transition_at.isoformat() if session.pending_transition_at else '',
        'public_next_group_name': next_group.name if next_group else '',
        'public_total_groups': total_groups,
        'public_completed_groups': completed_groups,
        'public_current_index': current_index,
        'public_joined_count': joined_count,
        'public_student_count': student_count,
        'public_submitted_count': submitted_count,
        'public_grading_now_count': grading_now_count,
        'public_joining_locked': session.joining_locked,
    }


def join_screen_state(request, session_uuid):
    session = _get_session(session_uuid)
    return JsonResponse(_public_live_state(session))


def join_screen_qr(request, session_uuid):
    """PNG for the join screen above - a separate, unauthenticated
    counterpart to the admin-only `session_qr` (which requires login and
    organization scoping); both just wrap the same `qr_png(join_url(...))`.
    """
    session = _get_session(session_uuid)
    return HttpResponse(qr_png(join_url(request, session)), content_type='image/png')


def _parse_decimal(raw):
    if raw is None or str(raw).strip() == '':
        raise ValidationError('Please score every category before submitting.')
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise ValidationError('Scores must be numbers.')
