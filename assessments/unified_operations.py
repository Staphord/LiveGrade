"""Unified assessment operations on top of assessments.AssessmentSession.

Handles readiness/feasibility checks, panel score aggregation, peer + panel blending,
chair moderation, asynchronous submissions, bipartite allocation, independent review
autosave/submission/recusal, and immutable publication snapshots.
"""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import random

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    AssessmentSession,
    EntrySubmission,
    EntrySubmissionVersion,
    PanelScoreValue,
    PanelScorecard,
    ResultPublication,
    ResultSnapshot,
    ReviewAssignment,
    RubricCategory,
    SessionAuditEvent,
    TurnModeration,
)
from .scoring import group_score_percent


class ConflictError(Exception):
    """Raised on optimistic concurrency version mismatch (HTTP 409)."""


def _q2(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _q4(value):
    return value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def record_session_audit(session, actor, action, summary, object_ref=''):
    """Append an immutable audit entry for the session."""
    return SessionAuditEvent.objects.create(
        session=session,
        actor=actor,
        action=action,
        object_ref=object_ref[:120],
        summary=summary[:500],
    )


def aggregate_panel_scores(scores, method, trim_fraction=None):
    """Aggregate numeric panel scores using mean, median, or trimmed mean."""
    if not scores:
        return None
    ordered = sorted(Decimal(str(s)) for s in scores)
    n = len(ordered)
    if method == AssessmentSession.PanelAggregation.MEDIAN:
        mid = n // 2
        if n % 2 == 1:
            return _q4(ordered[mid])
        return _q4((ordered[mid - 1] + ordered[mid]) / Decimal('2'))
    if method == AssessmentSession.PanelAggregation.TRIMMED_MEAN and trim_fraction is not None:
        k = int(Decimal(n) * Decimal(str(trim_fraction)))
        if 2 * k < n:
            ordered = ordered[k:n - k]
    return _q4(sum(ordered, Decimal('0')) / Decimal(len(ordered)))


def panel_group_score_percent(group, session):
    """Compute a group's panel score (0-100) from live PanelScorecards or async ReviewAssignments."""
    if group.is_practice:
        return None
    if session.delivery_mode == AssessmentSession.DeliveryMode.ASYNC_SUBMISSION:
        scores = list(
            ReviewAssignment.objects.filter(
                session=session,
                track=ReviewAssignment.Track.PANEL,
                submission_version__submission__group=group,
                status=ReviewAssignment.Status.SUBMITTED,
                total_score__isnull=False,
            ).values_list('total_score', flat=True)
        )
        return aggregate_panel_scores(scores, session.panel_aggregation, session.panel_trim_fraction)

    group_cats = {
        c.pk: c.weight
        for c in session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP)
    }
    if not group_cats:
        return None
    group_weight_total = sum(group_cats.values(), Decimal('0'))
    if group_weight_total <= 0:
        return None

    cards = list(
        PanelScorecard.objects.filter(
            turn__assessment_session=session,
            turn__group=group,
        ).prefetch_related('values')
    )
    if not cards:
        return None
    card_totals = []
    for card in cards:
        weighted = Decimal('0')
        seen = False
        for val in card.values.all():
            if val.target_student_id is None and val.category_id in group_cats:
                weighted += val.score * group_cats[val.category_id] / Decimal('100')
                seen = True
        if seen:
            card_totals.append(weighted * Decimal('100') / group_weight_total)
    return aggregate_panel_scores(card_totals, session.panel_aggregation, session.panel_trim_fraction)


def unified_group_outcome(group, session):
    """Return peer score, panel score, discrepancy, moderation state, and effective score."""
    if session.delivery_mode == AssessmentSession.DeliveryMode.ASYNC_SUBMISSION:
        peer_scores = list(
            ReviewAssignment.objects.filter(
                session=session,
                track=ReviewAssignment.Track.PEER,
                submission_version__submission__group=group,
                status=ReviewAssignment.Status.SUBMITTED,
                total_score__isnull=False,
            ).values_list('total_score', flat=True)
        )
        peer_score = (
            _q2(sum((Decimal(str(s)) for s in peer_scores), Decimal('0')) / Decimal(len(peer_scores)))
            if peer_scores else None
        )
    else:
        raw_peer = group_score_percent(group, session)
        group_weight_total = sum(
            session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP).values_list('weight', flat=True),
            Decimal('0'),
        )
        if raw_peer is not None and group_weight_total > 0:
            peer_score = _q2(raw_peer * Decimal('100') / group_weight_total)
        else:
            peer_score = None

    raw_panel = panel_group_score_percent(group, session)
    panel_score = _q2(raw_panel) if raw_panel is not None else None

    discrepancy = None
    if session.enable_peer and session.enable_panel and peer_score is not None and panel_score is not None:
        discrepancy = abs(panel_score - peer_score)

    moderation_required = (
        discrepancy is not None and discrepancy > session.moderation_threshold
    )

    combined_score = None
    if session.enable_peer and not session.enable_panel:
        combined_score = peer_score
    elif session.enable_panel and not session.enable_peer:
        combined_score = panel_score
    elif peer_score is not None and panel_score is not None:
        combined_score = _q2(
            (peer_score * session.peer_weight / Decimal('100'))
            + (panel_score * session.panel_weight / Decimal('100'))
        )

    latest_turn = group.turns.order_by('-pk').first()
    latest_mod = latest_turn.moderations.order_by('-created_at').first() if latest_turn else None
    effective_score = latest_mod.final_score if latest_mod else combined_score

    return {
        'group': group,
        'latest_turn_id': latest_turn.pk if latest_turn else None,
        'peer_score': peer_score,
        'panel_score': panel_score,
        'discrepancy': discrepancy,
        'moderation_required': moderation_required,
        'latest_moderation': latest_mod,
        'combined_score': combined_score,
        'effective_score': effective_score,
    }


def compute_session_readiness(session):
    """Evaluate blockers, warnings, and peer/panel feasibility for a draft session."""
    blockers = []
    warnings = []

    if not session.enable_peer and not session.enable_panel:
        blockers.append('Enable at least one assessment mode (Peer or Panel).')

    if session.enable_peer and session.enable_panel:
        if (session.peer_weight + session.panel_weight) != Decimal('100.00'):
            blockers.append('Peer and Panel weights must total 100%.')

    categories = list(session.rubric_categories.all())
    if not categories:
        blockers.append('Add at least one rubric criterion.')
    else:
        total_weight = sum((c.weight for c in categories), Decimal('0'))
        if total_weight != Decimal('100.00'):
            blockers.append(f'Rubric weights currently total {total_weight}%; they must equal 100%.')

    groups = list(session.groups.filter(is_practice=False).prefetch_related('memberships'))
    if not groups:
        blockers.append('Add at least one entry to the roster.')

    total_participants = sum(len(g.memberships.all()) for g in groups)
    peer_feasibility = {'feasible': True, 'eligible_min': 0, 'message': 'Peer mode disabled.'}
    if session.enable_peer:
        if session.entry_mode == AssessmentSession.EntryMode.GROUP and len(groups) < 2:
            warnings.append('Peer assessment works best with at least 2 groups so participants can score other entries.')
        if total_participants == 0:
            warnings.append('No participants are assigned to entries yet; participants can still join via code if live.')
        else:
            min_eligible = min(
                (total_participants - len(g.memberships.all()) for g in groups),
                default=0,
            )
            feasible = min_eligible >= session.minimum_peer_evaluators
            peer_feasibility = {
                'feasible': feasible,
                'eligible_min': min_eligible,
                'message': (
                    f'{total_participants} participants across {len(groups)} entries; '
                    f'minimum {min_eligible} eligible peers per entry '
                    f'({"feasible" if feasible else f"below target of {session.minimum_peer_evaluators}"}).'
                ),
            }
            if not feasible and session.delivery_mode == AssessmentSession.DeliveryMode.ASYNC_SUBMISSION:
                blockers.append(
                    f'Async peer allocation requires at least {session.minimum_peer_evaluators} '
                    f'eligible reviewers per entry (currently {min_eligible}).'
                )
            elif not feasible:
                warnings.append(peer_feasibility['message'])

    examiner_count = session.panel_examiners.count()
    panel_feasibility = {'feasible': True, 'examiner_count': examiner_count, 'message': 'Panel mode disabled.'}
    if session.enable_panel:
        feasible = examiner_count >= session.minimum_panel_evaluators
        panel_feasibility = {
            'feasible': feasible,
            'examiner_count': examiner_count,
            'message': (
                f'{examiner_count} panelist(s) invited; target is {session.minimum_panel_evaluators} per entry.'
            ),
        }
        if examiner_count == 0:
            blockers.append('Invite at least one panelist when Panel mode is enabled.')
        elif not feasible:
            warnings.append(panel_feasibility['message'])

    return {
        'ready': len(blockers) == 0,
        'blockers': blockers,
        'warnings': warnings,
        'peer_feasibility': peer_feasibility,
        'panel_feasibility': panel_feasibility,
    }


@transaction.atomic
def submit_entry_work(session, group, participant, title, content, external_url=''):
    """Save a new revision of an entry's asynchronous submission."""
    if session.delivery_mode != AssessmentSession.DeliveryMode.ASYNC_SUBMISSION:
        raise ValidationError('This session is not configured for asynchronous submissions.')
    if session.status != AssessmentSession.Status.SUBMISSIONS_OPEN:
        raise ValidationError('Submissions are not currently open for this session.')
    if not title or not title.strip():
        raise ValidationError('Submission title is required.')
    if not (content and content.strip()) and not (external_url and external_url.strip()):
        raise ValidationError('Provide submission text or an external link.')

    submission, _ = EntrySubmission.objects.get_or_create(
        session=session,
        group=group,
    )
    next_rev = submission.current_revision + 1
    digest = hashlib.sha256(
        f'{title.strip()}\n{content.strip()}\n{external_url.strip()}'.encode('utf-8')
    ).hexdigest()
    version = EntrySubmissionVersion.objects.create(
        submission=submission,
        revision=next_rev,
        title=title.strip()[:200],
        content=content.strip(),
        external_url=external_url.strip()[:500],
        submitted_by=participant,
        submitted_at=timezone.now(),
        content_hash=digest,
    )
    submission.current_revision = next_rev
    submission.status = EntrySubmission.Status.SUBMITTED
    submission.save(update_fields=['current_revision', 'status'])
    return version


@transaction.atomic
def freeze_session_submissions(session, actor):
    """Freeze the latest submitted version for every entry and advance to Live reviewing."""
    if session.status != AssessmentSession.Status.SUBMISSIONS_OPEN:
        raise ValidationError('Session must be in Submissions Open stage to freeze.')
    now = timezone.now()
    frozen_count = 0
    for submission in session.entry_submissions.select_related('group').all():
        latest = submission.versions.order_by('-revision').first()
        if latest is None:
            continue
        latest.frozen_at = now
        latest.save(update_fields=['frozen_at'])
        submission.frozen_version = latest
        submission.status = EntrySubmission.Status.FROZEN
        submission.save(update_fields=['frozen_version', 'status'])
        frozen_count += 1
    if frozen_count == 0:
        raise ValidationError('At least one submitted entry is required before freezing.')
    session.submissions_frozen_at = now
    session.status = AssessmentSession.Status.LIVE
    session.save(update_fields=['submissions_frozen_at', 'status'])
    record_session_audit(
        session, actor, 'submissions.frozen',
        f'Froze {frozen_count} submission(s) and opened reviewing.',
    )
    return frozen_count


def _balance_allocate(items, candidates_by_item, required_per_item, seed):
    rng = random.Random(seed)
    load = defaultdict(int)
    allocations = {}
    unmet = {}
    for item in items:
        pool = list(candidates_by_item[item.pk])
        rng.shuffle(pool)
        pool.sort(key=lambda c: load[c.pk])
        chosen = pool[:required_per_item]
        for c in chosen:
            load[c.pk] += 1
        allocations[item.pk] = chosen
        if len(chosen) < required_per_item:
            unmet[item.pk] = required_per_item - len(chosen)
    return allocations, unmet, dict(load)


def preview_allocations(session, seed=42):
    """Compute a dry-run allocation preview for peer and panel tracks."""
    frozen_versions = [
        sub.frozen_version
        for sub in session.entry_submissions.select_related('frozen_version', 'group').all()
        if sub.frozen_version_id is not None
    ]
    participants = list(session.students.prefetch_related('group_memberships').all())
    examiners = list(session.panel_examiners.all())

    group_by_student = {}
    for p in participants:
        m = p.group_memberships.first()
        group_by_student[p.pk] = m.group_id if m else None

    peer_alloc, peer_unmet, peer_load = {}, {}, {}
    if session.enable_peer:
        candidates = {
            v.pk: [p for p in participants if group_by_student.get(p.pk) != v.submission.group_id]
            for v in frozen_versions
        }
        peer_alloc, peer_unmet, peer_load = _balance_allocate(
            frozen_versions, candidates, session.minimum_peer_evaluators, seed
        )

    panel_alloc, panel_unmet, panel_load = {}, {}, {}
    if session.enable_panel:
        candidates = {v.pk: list(examiners) for v in frozen_versions}
        panel_alloc, panel_unmet, panel_load = _balance_allocate(
            frozen_versions, candidates, session.minimum_panel_evaluators, seed + 1
        )

    return {
        'frozen_versions': frozen_versions,
        'peer_allocations': peer_alloc,
        'peer_unmet': peer_unmet,
        'peer_load': peer_load,
        'panel_allocations': panel_alloc,
        'panel_unmet': panel_unmet,
        'panel_load': panel_load,
        'feasible': not peer_unmet and not panel_unmet and len(frozen_versions) > 0,
    }


@transaction.atomic
def commit_allocations(session, actor, seed=42):
    """Persist feasible peer and panel review assignments atomically."""
    preview = preview_allocations(session, seed=seed)
    if not preview['frozen_versions']:
        raise ValidationError('No frozen submissions exist to allocate.')
    if not preview['feasible']:
        raise ValidationError('Allocation targets cannot be satisfied with current eligible reviewers.')

    created_count = 0
    for version in preview['frozen_versions']:
        for peer in preview['peer_allocations'].get(version.pk, []):
            _, created = ReviewAssignment.objects.get_or_create(
                session=session,
                track=ReviewAssignment.Track.PEER,
                submission_version=version,
                peer_evaluator=peer,
            )
            if created:
                created_count += 1
        for examiner in preview['panel_allocations'].get(version.pk, []):
            _, created = ReviewAssignment.objects.get_or_create(
                session=session,
                track=ReviewAssignment.Track.PANEL,
                submission_version=version,
                examiner_evaluator=examiner,
            )
            if created:
                created_count += 1

    record_session_audit(
        session, actor, 'allocations.committed',
        f'Committed {created_count} review assignment(s) (seed={seed}).',
    )
    return created_count


@transaction.atomic
def refresh_assignment_link(assignment):
    """Rotate an active review capability and its expiry under a row lock."""
    locked = ReviewAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status not in (ReviewAssignment.Status.ACTIVE, ReviewAssignment.Status.DRAFT):
        raise ValidationError('Only an active review assignment can receive a new link.')
    locked.rotate_invite_token()
    locked.save(update_fields=['invite_token', 'token_digest', 'expires_at'])
    return locked


@transaction.atomic
def save_assignment_draft(assignment, expected_row_version, payload):
    """Save an in-progress review draft with optimistic row_version locking."""
    locked = ReviewAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status == ReviewAssignment.Status.SUBMITTED:
        raise ValidationError('Submitted reviews cannot be overwritten as drafts.')
    if locked.status in (ReviewAssignment.Status.RECUSED, ReviewAssignment.Status.SUPERSEDED):
        raise ValidationError('This assignment is no longer active.')
    if locked.row_version != expected_row_version:
        raise ConflictError('Your draft is out of date. Reload the latest version before saving.')
    if not isinstance(payload, dict):
        raise ValidationError('Draft payload must be an object.')

    allowed_fields = {'overall_feedback'}
    allowed_fields.update(
        f'category_{category_id}'
        for category_id in locked.session.rubric_categories.values_list('pk', flat=True)
    )
    if len(payload) > len(allowed_fields) or set(payload) - allowed_fields:
        raise ValidationError('Draft contains an unrecognized rubric field.')
    for field, value in payload.items():
        max_length = 2000 if field == 'overall_feedback' else 32
        if not isinstance(value, str) or len(value) > max_length:
            raise ValidationError('Draft fields must be text within the allowed length.')

    locked.draft_payload = payload
    locked.status = ReviewAssignment.Status.DRAFT
    locked.row_version += 1
    locked.save(update_fields=['draft_payload', 'status', 'row_version'])
    return locked


@transaction.atomic
def submit_assignment_review(assignment, scores_by_category_id, overall_feedback=''):
    """Validate and submit an async review assignment, computing normalized 0-100 score."""
    locked = ReviewAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status in (ReviewAssignment.Status.RECUSED, ReviewAssignment.Status.SUPERSEDED):
        raise ValidationError('This assignment is no longer active.')
    if locked.status == ReviewAssignment.Status.SUBMITTED:
        return locked

    categories = list(locked.session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP))
    if not categories:
        categories = list(locked.session.rubric_categories.all())
    if not categories:
        raise ValidationError('Session has no rubric criteria configured.')

    weighted_sum = Decimal('0')
    total_weight = Decimal('0')
    for cat in categories:
        if cat.pk not in scores_by_category_id:
            raise ValidationError(f'Missing score for criterion "{cat.name}".')
        raw = Decimal(str(scores_by_category_id[cat.pk]))
        if raw < Decimal('0') or raw > Decimal('100'):
            raise ValidationError(f'Score for "{cat.name}" must be between 0 and 100.')
        weighted_sum += raw * cat.weight
        total_weight += cat.weight

    normalized = _q4(weighted_sum / total_weight) if total_weight > 0 else Decimal('0')
    locked.total_score = normalized
    locked.overall_feedback = (overall_feedback or '').strip()[:2000]
    locked.status = ReviewAssignment.Status.SUBMITTED
    locked.submitted_at = timezone.now()
    locked.row_version += 1
    locked.save(update_fields=[
        'total_score', 'overall_feedback', 'status', 'submitted_at', 'row_version',
    ])
    return locked


@transaction.atomic
def recuse_assignment(assignment, reason, actor=None):
    """Mark an assignment as recused with an explicit reason."""
    locked = ReviewAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status == ReviewAssignment.Status.SUBMITTED:
        raise ValidationError('A submitted review cannot be recused.')
    if locked.status in (ReviewAssignment.Status.RECUSED, ReviewAssignment.Status.SUPERSEDED):
        raise ValidationError('This assignment is no longer active.')
    locked.status = ReviewAssignment.Status.RECUSED
    locked.recusal_reason = (reason or 'Conflict of interest').strip()[:300]
    locked.row_version += 1
    locked.save(update_fields=['status', 'recusal_reason', 'row_version'])
    record_session_audit(
        locked.session, actor, 'assignment.recused',
        f'Assignment #{locked.pk} ({locked.track}) recused: {locked.recusal_reason}',
        object_ref=f'assignment:{locked.pk}',
    )
    return locked


@transaction.atomic
def record_turn_moderation(turn, action, final_score, justification, actor):
    """Record a chair moderation override/approval on a presentation turn."""
    if not justification or not justification.strip():
        raise ValidationError('Moderation justification is required.')
    outcome = unified_group_outcome(turn.group, turn.assessment_session)
    mod = TurnModeration.objects.create(
        turn=turn,
        action=action,
        peer_score_snapshot=outcome['peer_score'],
        panel_score_snapshot=outcome['panel_score'],
        final_score=_q2(Decimal(str(final_score))),
        justification=justification.strip()[:1500],
        reviewed_by=actor,
    )
    record_session_audit(
        turn.assessment_session, actor, 'turn.moderated',
        f'Moderated {turn.group.name}: {mod.final_score} ({action}).',
        object_ref=f'turn:{turn.pk}',
    )
    return mod


@transaction.atomic
def publish_session_snapshot(session, actor, results_rows, correction_reason=''):
    """Create an immutable ResultPublication and per-entry ResultSnapshot rows."""
    prior = session.publications.order_by('-revision').first()
    next_rev = (prior.revision + 1) if prior else 1
    now = timezone.now()

    publication = ResultPublication.objects.create(
        session=session,
        revision=next_rev,
        policy_snapshot={
            'enable_peer': session.enable_peer,
            'enable_panel': session.enable_panel,
            'peer_weight': str(session.peer_weight),
            'panel_weight': str(session.panel_weight),
            'delivery_mode': session.delivery_mode,
            'panel_aggregation': session.panel_aggregation,
        },
        published_by=actor,
        published_at=now,
        correction_reason=(correction_reason or '').strip()[:1000],
        prior_publication=prior,
    )

    for row in results_rows:
        group = row['group']
        ResultSnapshot.objects.create(
            publication=publication,
            group=group,
            peer_average=row.get('peer_score'),
            panel_average=row.get('panel_score'),
            calculated_final=row.get('combined_score'),
            effective_final=row.get('effective_score'),
            override_reason=row.get('override_reason', ''),
            outcome_status=(
                ResultSnapshot.OutcomeStatus.WITHHELD
                if row.get('withheld')
                else ResultSnapshot.OutcomeStatus.APPROVED
            ),
            withheld_reason=row.get('withheld_reason', ''),
            breakdown_snapshot={
                'mode': session.assessment_mode_label,
                'discrepancy': str(row['discrepancy']) if row.get('discrepancy') is not None else None,
            },
        )

    session.publication_revision = next_rev
    session.public_results_released_at = now
    session.public_results_released_by = actor
    session.save(update_fields=[
        'publication_revision', 'public_results_released_at', 'public_results_released_by',
    ])
    record_session_audit(
        session, actor, 'publication.released',
        f'Published immutable result snapshot revision {next_rev}.',
        object_ref=f'publication:{publication.pk}',
    )
    return publication
