"""Unified Panel, Moderation, Async Submission, Allocation, and Independent Review views."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import secrets

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.access import lecturer_required
from ..forms import AsyncSubmissionForm, PanelExaminerForm, PanelVoteForm
from ..models import (
    AssessmentSession,
    PanelExaminer,
    PanelScoreValue,
    PanelScorecard,
    PanelVote,
    PresentationGroup,
    PresentationTurn,
    ReviewAssignment,
    RubricCategory,
    Student,
)
from ..tasks import send_assessment_panel_invitation_email
from ..unified_operations import (
    ConflictError,
    commit_allocations,
    compute_session_readiness,
    freeze_session_submissions,
    preview_allocations,
    publish_session_snapshot,
    record_turn_moderation,
    recuse_assignment,
    save_assignment_draft,
    submit_assignment_review,
    submit_entry_work,
    unified_group_outcome,
)
from .lecturer_views import _session


def _dispatch_examiner_invitation(request, examiner_id):
    """Queue invitation email via Celery, marking failure safely if queueing fails."""
    origin = request.build_absolute_uri('/').rstrip('/')

    def _enqueue():
        try:
            send_assessment_panel_invitation_email.delay(examiner_id, origin)
        except Exception:
            try:
                send_assessment_panel_invitation_email(examiner_id, origin)
            except Exception:
                PanelExaminer.objects.filter(pk=examiner_id).update(
                    delivery_status=PanelExaminer.DeliveryStatus.FAILED,
                    delivery_error='enqueue_failed',
                )

    transaction.on_commit(_enqueue)


@lecturer_required
@require_http_methods(['GET', 'POST'])
def panel_examiners(request, pk):
    """Manage invited panel examiners for a session (skipped if Panel mode is off)."""
    session = _session(request, pk)
    if not session.enable_panel:
        return redirect('assessment_session_detail', pk=session.pk)

    form = PanelExaminerForm(request.POST or None)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'remove_examiner':
            ex_id = request.POST.get('examiner_id')
            session.panel_examiners.filter(pk=ex_id).delete()
            messages.info(request, 'Examiner removed.')
            if request.POST.get('next') == 'setup':
                return redirect(f"{redirect('assessment_setup', pk=session.pk).url}?step=panel")
            return redirect('assessment_panel_examiners', pk=session.pk)
        if action == 'resend_examiner':
            ex_id = request.POST.get('examiner_id')
            examiner = session.panel_examiners.filter(pk=ex_id).first()
            if examiner is not None:
                examiner.delivery_status = PanelExaminer.DeliveryStatus.PENDING
                examiner.delivery_error = ''
                examiner.expires_at = timezone.now() + timedelta(days=14)
                examiner.save(update_fields=['delivery_status', 'delivery_error', 'expires_at'])
                _dispatch_examiner_invitation(request, examiner.pk)
                messages.success(request, f'Invitation resent to {examiner.email}.')
            if request.POST.get('next') == 'setup':
                return redirect(f"{redirect('assessment_setup', pk=session.pk).url}?step=panel")
            return redirect('assessment_panel_examiners', pk=session.pk)
        if form.is_valid():
            examiner = form.save(commit=False)
            examiner.session = session
            raw_token = secrets.token_urlsafe(32)
            examiner.invite_token = raw_token
            examiner.token_digest = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
            examiner.delivery_status = PanelExaminer.DeliveryStatus.PENDING
            examiner.expires_at = timezone.now() + timedelta(days=14)
            examiner.save()
            _dispatch_examiner_invitation(request, examiner.pk)
            messages.success(request, f'Invited panelist "{examiner.display_name}" and queued email.')
            if request.POST.get('next') == 'setup':
                return redirect(f"{redirect('assessment_setup', pk=session.pk).url}?step=panel")
            return redirect('assessment_panel_examiners', pk=session.pk)

    examiners = list(session.panel_examiners.all())
    readiness = compute_session_readiness(session)
    return render(request, 'assessments/panel_examiners.html', {
        'session': session,
        'examiners': examiners,
        'form': form,
        'readiness': readiness,
    })


def _parse_panel_score(raw, label):
    """Validate and parse a 0-100 panel score input."""
    if raw is None or not str(raw).strip():
        raise ValidationError(f'Please enter a score for {label}.')
    try:
        val = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f'Score for {label} must be a valid number.') from exc
    if val < Decimal('0') or val > Decimal('100'):
        raise ValidationError(f'Score for {label} must be between 0 and 100.')
    return val


@require_http_methods(['GET', 'POST'])
def examiner_portal(request, token):
    """Private capability-link portal for an invited panelist to score live turns."""
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    examiner = get_object_or_404(
        PanelExaminer.objects.select_related('session'),
        token_digest=digest,
    )
    session = examiner.session
    active_turn = session.active_turn()
    group_categories = list(
        session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP)
        .prefetch_related('level_descriptors')
        .order_by('order', 'id')
    )
    individual_categories = list(
        session.rubric_categories.filter(scope=RubricCategory.Scope.INDIVIDUAL)
        .prefetch_related('level_descriptors')
        .order_by('order', 'id')
    )
    memberships = (
        list(
            active_turn.group.memberships.select_related('student')
            .order_by('student__full_name', 'student__student_id')
        )
        if active_turn else []
    )
    already_done = bool(
        active_turn and PanelScorecard.objects.filter(turn=active_turn, examiner=examiner).exists()
    )

    if request.method == 'POST' and active_turn:
        if already_done:
            messages.error(request, f'You have already evaluated {active_turn.group.name}.')
            return redirect('assessment_examiner_portal', token=token)

        qualitative_feedback = request.POST.get('qualitative_feedback', '').strip()[:2000]
        choice = (request.POST.get('choice') or '').strip()
        group_scores = []
        individual_scores = []

        try:
            if session.panel_enable_rubric and (group_categories or (individual_categories and memberships)):
                for cat in group_categories:
                    val = _parse_panel_score(request.POST.get(f'category_{cat.pk}'), cat.name)
                    group_scores.append((cat, val))
                for m in memberships:
                    member_label = m.student.full_name or m.student.student_id
                    for cat in individual_categories:
                        val = _parse_panel_score(
                            request.POST.get(f'ind_{m.student_id}_{cat.pk}'),
                            f'{member_label} - {cat.name}',
                        )
                        individual_scores.append((m.student, cat, val))
            elif not qualitative_feedback and not (session.panel_enable_vote and choice):
                raise ValidationError('Please complete the evaluation before submitting.')

            if session.panel_enable_vote and not choice and not group_scores and not individual_scores and not qualitative_feedback:
                raise ValidationError('Please select a recommendation or enter feedback before submitting.')
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect('assessment_examiner_portal', token=token)

        try:
            with transaction.atomic():
                card = PanelScorecard.objects.create(
                    turn=active_turn,
                    examiner=examiner,
                    qualitative_feedback=qualitative_feedback,
                )
                for cat, val in group_scores:
                    PanelScoreValue.objects.create(
                        scorecard=card,
                        category=cat,
                        target_student=None,
                        score=val,
                    )
                for student_obj, cat, val in individual_scores:
                    PanelScoreValue.objects.create(
                        scorecard=card,
                        category=cat,
                        target_student=student_obj,
                        score=val,
                    )
                if session.panel_enable_vote and choice:
                    PanelVote.objects.update_or_create(
                        session=session,
                        group=active_turn.group,
                        examiner=examiner,
                        defaults={
                            'choice': choice,
                            'written_review': request.POST.get('written_review', '')[:4000],
                            'private_note': request.POST.get('private_note', '')[:2000],
                        },
                    )
        except IntegrityError:
            messages.error(request, f'You have already evaluated {active_turn.group.name}.')
            return redirect('assessment_examiner_portal', token=token)

        from ..realtime import broadcast_session_event
        broadcast_session_event(session, 'activity.updated', {'group_id': active_turn.group_id})

        messages.success(request, f'Saved panel evaluation for {active_turn.group.name}.')
        return redirect('assessment_examiner_portal', token=token)

    return render(request, 'assessments/examiner_portal.html', {
        'session': session,
        'examiner': examiner,
        'active_turn': active_turn,
        'already_done': already_done,
        'group_categories': group_categories,
        'individual_categories': individual_categories,
        'memberships': memberships,
        'vote_form': PanelVoteForm() if session.panel_enable_vote else None,
        'token': token,
    })


@require_safe
@never_cache
def examiner_portal_state(request, token):
    """Lightweight live-state endpoint for fallback polling on the examiner portal."""
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    examiner = get_object_or_404(
        PanelExaminer.objects.select_related('session'),
        token_digest=digest,
    )
    session = examiner.session
    active_turn = session.active_turn()
    return JsonResponse({
        'status': session.status,
        'status_display': session.get_status_display(),
        'active_turn_id': active_turn.pk if active_turn else None,
        'active_group_name': active_turn.group.name if active_turn else None,
    })


@lecturer_required
@require_http_methods(['GET', 'POST'])
def session_moderation(request, pk):
    """Unified moderation, peer-vs-panel discrepancy review, and immutable publication."""
    session = _session(request, pk)
    groups = list(session.groups.filter(is_practice=False).order_by('order', 'pk'))
    outcomes = [unified_group_outcome(g, session) for g in groups]

    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'moderate_turn':
                turn_id = int(request.POST.get('turn_id'))
                turn = get_object_or_404(PresentationTurn, pk=turn_id, assessment_session=session)
                record_turn_moderation(
                    turn,
                    request.POST.get('mod_action', 'adjusted'),
                    request.POST.get('final_score'),
                    request.POST.get('justification', ''),
                    request.user,
                )
                messages.success(request, f'Recorded moderation for {turn.group.name}.')
            elif action == 'publish_snapshot':
                reason = request.POST.get('correction_reason', '')
                pub = publish_session_snapshot(session, request.user, outcomes, correction_reason=reason)
                messages.success(request, f'Published immutable result snapshot (Revision {pub.revision}).')
        except (ValidationError, ValueError, TypeError) as exc:
            messages.error(request, getattr(exc, 'message', str(exc)))
        return redirect('assessment_moderation', pk=session.pk)

    publications = list(session.publications.all()[:10])
    return render(request, 'assessments/session_moderation.html', {
        'session': session,
        'outcomes': outcomes,
        'publications': publications,
    })


@lecturer_required
@require_http_methods(['GET', 'POST'])
def async_console(request, pk):
    """Organizer console for freezing async submissions and allocating peer/panel reviews."""
    session = _session(request, pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'open_submissions':
                session.status = AssessmentSession.Status.SUBMISSIONS_OPEN
                session.save(update_fields=['status'])
                messages.success(request, 'Submissions are now open.')
            elif action == 'freeze':
                count = freeze_session_submissions(session, request.user)
                messages.success(request, f'Froze {count} submission(s) and opened reviewing.')
            elif action == 'commit_allocations':
                seed = int(request.POST.get('seed') or 42)
                created = commit_allocations(session, request.user, seed=seed)
                messages.success(request, f'Created {created} review assignment(s).')
        except (ValidationError, ValueError) as exc:
            messages.error(request, getattr(exc, 'message', str(exc)))
        return redirect('assessment_async_console', pk=session.pk)

    preview = preview_allocations(session)
    submissions = list(session.entry_submissions.select_related('group', 'frozen_version').all())
    assignments = list(
        session.review_assignments.select_related(
            'submission_version__submission__group',
            'peer_evaluator',
            'examiner_evaluator',
        ).all()
    )
    return render(request, 'assessments/async_console.html', {
        'session': session,
        'submissions': submissions,
        'assignments': assignments,
        'preview': preview,
    })


@require_http_methods(['GET', 'POST'])
def async_submit_entry(request, session_uuid, group_pk):
    """Participant entry submission page for asynchronous assessments."""
    session = get_object_or_404(AssessmentSession, uuid=session_uuid)
    group = get_object_or_404(PresentationGroup, pk=group_pk, assessment_session=session)
    form = AsyncSubmissionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        participant = None
        pid = request.POST.get('participant_id')
        if pid:
            participant = Student.objects.filter(pk=pid, assessment_session=session).first()
        try:
            version = submit_entry_work(
                session,
                group,
                participant,
                form.cleaned_data['title'],
                form.cleaned_data.get('content', ''),
                form.cleaned_data.get('external_url', ''),
            )
            messages.success(request, f'Submission revision {version.revision} saved.')
            return redirect('assessment_async_submit_entry', session_uuid=session_uuid, group_pk=group.pk)
        except ValidationError as exc:
            form.add_error(None, getattr(exc, 'message', str(exc)))

    existing = getattr(group, 'entry_submission', None)
    return render(request, 'assessments/async_submit.html', {
        'session': session,
        'group': group,
        'form': form,
        'existing': existing,
    })


@require_http_methods(['GET'])
def assignment_review(request, assignment_id):
    """Independent review workspace for an assigned peer or panelist."""
    assignment = get_object_or_404(
        ReviewAssignment.objects.select_related(
            'session', 'submission_version__submission__group',
            'peer_evaluator', 'examiner_evaluator',
        ),
        pk=assignment_id,
    )
    categories = list(assignment.session.rubric_categories.prefetch_related('descriptors').all())
    return render(request, 'assessments/assignment_review.html', {
        'assignment': assignment,
        'session': assignment.session,
        'submission_version': assignment.submission_version,
        'categories': categories,
        'vote_form': PanelVoteForm() if assignment.track == ReviewAssignment.Track.PANEL else None,
    })


@require_POST
def assignment_draft(request, assignment_id):
    """Optimistic-locked draft autosave endpoint for review assignments."""
    assignment = get_object_or_404(ReviewAssignment, pk=assignment_id)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'code': 'invalid_json', 'message': 'Invalid JSON payload.'}, status=400)

    expected_version = body.get('row_version')
    if not isinstance(expected_version, int):
        return JsonResponse({'code': 'missing_version', 'message': 'row_version is required.'}, status=422)

    try:
        updated = save_assignment_draft(assignment, expected_version, body.get('payload') or {})
    except ConflictError as exc:
        assignment.refresh_from_db()
        return JsonResponse({
            'code': 'version_conflict',
            'message': str(exc),
            'current_version': assignment.row_version,
        }, status=409)
    except ValidationError as exc:
        return JsonResponse({
            'code': 'validation_error',
            'message': getattr(exc, 'message', str(exc)),
        }, status=422)

    return JsonResponse({'status': 'saved', 'row_version': updated.row_version})


@require_POST
def assignment_submit(request, assignment_id):
    """Final submission of an independent review assignment."""
    assignment = get_object_or_404(
        ReviewAssignment.objects.select_related('session'),
        pk=assignment_id,
    )
    categories = list(assignment.session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP))
    if not categories:
        categories = list(assignment.session.rubric_categories.all())

    scores = {}
    for cat in categories:
        val = request.POST.get(f'category_{cat.pk}')
        if val not in (None, ''):
            scores[cat.pk] = val

    try:
        submitted = submit_assignment_review(
            assignment, scores, request.POST.get('overall_feedback', '')
        )
        if assignment.track == ReviewAssignment.Track.PANEL and assignment.session.panel_enable_vote:
            choice = request.POST.get('choice')
            if choice:
                PanelVote.objects.update_or_create(
                    group=assignment.submission_version.submission.group,
                    examiner=assignment.examiner_evaluator,
                    defaults={
                        'session': assignment.session,
                        'choice': choice,
                        'written_review': request.POST.get('written_review', '')[:4000],
                        'private_note': request.POST.get('private_note', '')[:2000],
                    },
                )
        messages.success(request, f'Review submitted (Total: {submitted.total_score}).')
    except ValidationError as exc:
        messages.error(request, getattr(exc, 'message', str(exc)))
    return redirect('assessment_assignment_review', assignment_id=assignment.pk)


@require_POST
def assignment_recuse(request, assignment_id):
    """Recuse an active review assignment with a documented reason."""
    assignment = get_object_or_404(ReviewAssignment, pk=assignment_id)
    reason = request.POST.get('reason', 'Conflict of interest')
    recuse_assignment(assignment, reason, actor=request.user if request.user.is_authenticated else None)
    messages.info(request, 'Assignment recused and excluded from calculations.')
    return redirect('assessment_assignment_review', assignment_id=assignment.pk)
