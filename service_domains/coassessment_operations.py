"""Atomic domain operations and business logic for Co-Assessment.

Manages presentation stage turns, synchronized peer and panel scoring submissions,
discrepancy detection, chair moderation decisions, and result sign-offs.
"""

from decimal import Decimal, ROUND_HALF_UP
import secrets
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Avg, Max
from django.utils import timezone

from assessments.sheets import UNREADABLE, cell_text, clean, map_headers, read_rows, workbook_bytes
from .models import (
    CoAssessmentSession,
    CoAssessmentCriterion,
    CoAssessmentGroup,
    CoAssessmentParticipant,
    CoAssessmentExaminer,
    CoAssessmentTurn,
    CoAssessmentScorecard,
    CoAssessmentScoreValue,
    CoAssessmentModeration,
)


def get_coassessment_live_state(session):
    """Computes comprehensive live presentation state for WebSockets and polling."""
    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).select_related('group').first()
    now = timezone.now()

    elapsed = 0
    presentation_remaining = session.presentation_duration_seconds
    qa_remaining = session.qa_duration_seconds
    phase = 'presentation'
    timer_paused = False

    if active_turn and active_turn.opened_at:
        timer_paused = active_turn.is_paused
        if active_turn.is_paused and active_turn.paused_at:
            active_duration = int((active_turn.paused_at - active_turn.opened_at).total_seconds()) - active_turn.total_paused_seconds
        else:
            active_duration = int((now - active_turn.opened_at).total_seconds()) - active_turn.total_paused_seconds
        elapsed = max(0, active_duration)

        presentation_remaining = max(0, session.presentation_duration_seconds - elapsed)
        if elapsed >= session.presentation_duration_seconds:
            phase = 'qa'
            qa_elapsed = elapsed - session.presentation_duration_seconds
            qa_remaining = max(0, session.qa_duration_seconds - qa_elapsed)

    # Quorums
    panel_count = 0
    peer_count = 0
    panel_total = session.examiners.count()
    if active_turn:
        peer_total = session.participants.exclude(group_id=active_turn.group_id).count()
        panel_count = active_turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PANEL).count()
        peer_count = active_turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PEER).count()
    else:
        peer_total = session.participants.count()

    panel_percent = int((panel_count / panel_total * 100)) if panel_total > 0 else 0
    peer_percent = int((peer_count / peer_total * 100)) if peer_total > 0 else 0

    return {
        'session_id': session.pk,
        'session_status': session.status,
        'has_active_turn': bool(active_turn),
        'active_turn_id': active_turn.pk if active_turn else None,
        'active_group_id': active_turn.group_id if active_turn else None,
        'active_group_name': active_turn.group.name if active_turn else None,
        'active_group_topic': active_turn.group.topic if active_turn else None,
        'active_group_order': active_turn.group.order if active_turn else None,
        'timer_paused': timer_paused,
        'phase': phase,
        'elapsed_seconds': elapsed,
        'presentation_remaining_seconds': presentation_remaining,
        'qa_remaining_seconds': qa_remaining,
        'presentation_clock_formatted': f'{presentation_remaining // 60:02d}:{presentation_remaining % 60:02d}',
        'qa_clock_formatted': f'{qa_remaining // 60:02d}:{qa_remaining % 60:02d}',
        'panel_count': panel_count,
        'panel_total': panel_total,
        'panel_percent': panel_percent,
        'panel_quorum_met': panel_count >= session.minimum_panel_evaluators,
        'peer_count': peer_count,
        'peer_total': peer_total,
        'peer_percent': peer_percent,
        'peer_quorum_met': peer_count >= session.minimum_peer_evaluators,
    }


def broadcast_coassessment_update(session_id):
    """Pushes the current live state to all connected WebSockets in the session group."""
    try:
        session = CoAssessmentSession.objects.filter(pk=session_id).first()
        if not session:
            return
        state = get_coassessment_live_state(session)
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f'coassessment_{session_id}',
                {
                    'type': 'coassessment.update',
                    'state': state,
                }
            )
    except Exception:
        pass


@transaction.atomic
def activate_stage_turn(session, group, *, user):
    """Atomically activates a presenter stage slot while concluding the prior turn."""
    if session.status != CoAssessmentSession.Status.ACTIVE:
        raise ValidationError('The session must be active to call teams to the stage.')
    if group.session_id != session.pk:
        raise ValidationError('The group does not belong to this session.')

    target_turn = session.turns.filter(group=group).first()
    if target_turn and target_turn.status == CoAssessmentTurn.Status.CLOSED:
        raise ValidationError('This team has already presented and its turn is closed.')
    if target_turn and target_turn.status == CoAssessmentTurn.Status.ACTIVE:
        return target_turn

    # Conclude any currently active turn
    current_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
    if current_turn and (not target_turn or current_turn.pk != target_turn.pk):
        current_turn.status = CoAssessmentTurn.Status.CLOSED
        current_turn.closed_at = timezone.now()
        current_turn.is_paused = False
        current_turn.paused_at = None
        current_turn.save(update_fields=['status', 'closed_at', 'is_paused', 'paused_at', 'updated_at'])

    if not target_turn:
        turn = CoAssessmentTurn.objects.create(
            session=session,
            group=group,
            organization_id=session.organization_id,
            created_by=user,
            status=CoAssessmentTurn.Status.ACTIVE,
            opened_at=timezone.now(),
            is_paused=False,
        )
    else:
        turn = target_turn
        turn.status = CoAssessmentTurn.Status.ACTIVE
        turn.opened_at = timezone.now()
        turn.is_paused = False
        turn.paused_at = None
        turn.save(update_fields=['status', 'opened_at', 'is_paused', 'paused_at', 'updated_at'])

    broadcast_coassessment_update(session.pk)
    return turn


MIN_STAGE_MINUTES = 1
MAX_STAGE_MINUTES = 240


@transaction.atomic
def update_stage_timers(session, presentation_minutes, qa_minutes):
    """Validates and saves the presentation and Q&A clock lengths in minutes."""
    try:
        presentation = int(presentation_minutes)
        qa = int(qa_minutes)
    except (TypeError, ValueError):
        raise ValidationError('Enter whole numbers of minutes for both clocks.')
    for value in (presentation, qa):
        if value < MIN_STAGE_MINUTES or value > MAX_STAGE_MINUTES:
            raise ValidationError(f'Clock lengths must be between {MIN_STAGE_MINUTES} and {MAX_STAGE_MINUTES} minutes.')
    session.presentation_duration_seconds = presentation * 60
    session.qa_duration_seconds = qa * 60
    session.save(update_fields=['presentation_duration_seconds', 'qa_duration_seconds', 'updated_at'])
    broadcast_coassessment_update(session.pk)
    return session


@transaction.atomic
def pause_stage_turn(session, *, user):
    """Toggles pause/resume state on the active stage turn."""
    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
    if not active_turn:
        return None
    if active_turn.is_paused:
        if active_turn.paused_at:
            active_turn.total_paused_seconds += int((timezone.now() - active_turn.paused_at).total_seconds())
        active_turn.is_paused = False
        active_turn.paused_at = None
    else:
        active_turn.is_paused = True
        active_turn.paused_at = timezone.now()
    active_turn.save(update_fields=['is_paused', 'paused_at', 'total_paused_seconds', 'updated_at'])
    broadcast_coassessment_update(session.pk)
    return active_turn


@transaction.atomic
def conclude_stage_turn(session, *, user):
    """Concludes the active presentation turn."""
    active_turn = session.turns.filter(status=CoAssessmentTurn.Status.ACTIVE).first()
    if not active_turn:
        return None
    active_turn.status = CoAssessmentTurn.Status.CLOSED
    active_turn.closed_at = timezone.now()
    active_turn.is_paused = False
    active_turn.paused_at = None
    active_turn.save(update_fields=['status', 'closed_at', 'is_paused', 'paused_at', 'updated_at'])
    broadcast_coassessment_update(session.pk)
    return active_turn


def compute_turn_results(turn):
    """Calculates weighted panel average, weighted peer average, score discrepancy, and blended outcome."""
    session = turn.session
    criteria_weights = {c.pk: c.weight for c in session.criteria.all()}
    group_criteria = {c.pk: c for c in session.criteria.filter(scope=CoAssessmentCriterion.Scope.GROUP)}
    individual_criteria = {c.pk: c for c in session.criteria.filter(scope=CoAssessmentCriterion.Scope.INDIVIDUAL)}
    members = list(turn.group.members.all().order_by('full_name', 'pk'))

    def compute_card_breakdown(card):
        values = list(card.values.all())
        if not values:
            return None

        # Group criteria component
        group_total = Decimal('0.00')
        for v in values:
            if v.target_participant_id is None and v.criterion_id in group_criteria:
                w = criteria_weights.get(v.criterion_id, Decimal('0.00'))
                group_total += (Decimal(str(v.score)) * w / Decimal('100.00'))

        # Individual criteria components per member
        ind_totals = {m.pk: Decimal('0.00') for m in members}
        for v in values:
            if v.target_participant_id in ind_totals and v.criterion_id in individual_criteria:
                w = criteria_weights.get(v.criterion_id, Decimal('0.00'))
                ind_totals[v.target_participant_id] += (Decimal(str(v.score)) * w / Decimal('100.00'))

        # Card total score
        if ind_totals:
            mean_ind = sum(ind_totals.values()) / Decimal(len(ind_totals))
            card_total = group_total + mean_ind
        else:
            card_total = group_total

        return {
            'group_total': group_total,
            'ind_totals': ind_totals,
            'card_total': card_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
        }

    # Panel Examiner Scores
    panel_cards = list(turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PANEL).prefetch_related('values'))
    panel_breakdowns = [b for b in (compute_card_breakdown(c) for c in panel_cards) if b is not None]
    panel_count = len(panel_breakdowns)

    panel_score = None
    panel_group_mark = None
    panel_ind_marks = {m.pk: Decimal('0.00') for m in members}
    if panel_count > 0:
        panel_score = (sum(b['card_total'] for b in panel_breakdowns) / Decimal(panel_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        panel_group_mark = (sum(b['group_total'] for b in panel_breakdowns) / Decimal(panel_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        for m in members:
            panel_ind_marks[m.pk] = (sum(b['ind_totals'].get(m.pk, Decimal('0.00')) for b in panel_breakdowns) / Decimal(panel_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # Student Peer Scores
    peer_cards = list(turn.scorecards.filter(evaluator_tier=CoAssessmentScorecard.Tier.PEER).prefetch_related('values'))
    peer_breakdowns = [b for b in (compute_card_breakdown(c) for c in peer_cards) if b is not None]
    peer_count = len(peer_breakdowns)

    peer_score = None
    peer_group_mark = None
    peer_ind_marks = {m.pk: Decimal('0.00') for m in members}
    if peer_count > 0:
        peer_score = (sum(b['card_total'] for b in peer_breakdowns) / Decimal(peer_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        peer_group_mark = (sum(b['group_total'] for b in peer_breakdowns) / Decimal(peer_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        for m in members:
            peer_ind_marks[m.pk] = (sum(b['ind_totals'].get(m.pk, Decimal('0.00')) for b in peer_breakdowns) / Decimal(peer_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    discrepancy = None
    if panel_score is not None and peer_score is not None:
        discrepancy = abs(panel_score - peer_score)

    moderation_required = (
        discrepancy is not None and
        discrepancy > session.moderation_threshold
    )

    panel_quorum_met = panel_count >= session.minimum_panel_evaluators
    peer_quorum_met = peer_count >= session.minimum_peer_evaluators
    quorum_met = panel_quorum_met and peer_quorum_met

    combined_score = None
    blended_group_mark = None
    blended_ind_marks = {m.pk: Decimal('0.00') for m in members}
    if panel_score is not None and peer_score is not None and quorum_met:
        w_panel = session.panel_weight / Decimal('100.00')
        w_peer = session.peer_weight / Decimal('100.00')
        combined_score = (
            (panel_score * w_panel) +
            (peer_score * w_peer)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        blended_group_mark = (
            (panel_group_mark * w_panel) +
            (peer_group_mark * w_peer)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        for m in members:
            blended_ind_marks[m.pk] = (
                (panel_ind_marks[m.pk] * w_panel) +
                (peer_ind_marks[m.pk] * w_peer)
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif panel_score is not None and not quorum_met:
        combined_score = panel_score
        blended_group_mark = panel_group_mark
        blended_ind_marks = panel_ind_marks
    elif peer_score is not None and not quorum_met:
        combined_score = peer_score
        blended_group_mark = peer_group_mark
        blended_ind_marks = peer_ind_marks

    latest_moderation = turn.moderations.order_by('-created_at').first()
    final_mark = latest_moderation.final_score if latest_moderation else combined_score

    member_results = []
    for m in members:
        grp_score = blended_group_mark or Decimal('0.00')
        ind_score = blended_ind_marks.get(m.pk, Decimal('0.00'))
        raw_final = grp_score + ind_score
        if latest_moderation and combined_score and combined_score > Decimal('0'):
            adj_ratio = latest_moderation.final_score / combined_score
            member_final = (raw_final * adj_ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            member_final = raw_final.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        member_results.append({
            'participant_id': m.pk,
            'full_name': m.full_name,
            'student_id': m.student_id,
            'email': m.email,
            'group_score': grp_score,
            'individual_score': ind_score,
            'final_score': member_final,
        })

    return {
        'turn_id': turn.pk,
        'group_id': turn.group_id,
        'group_name': turn.group.name,
        'group_topic': turn.group.topic,
        'is_active': turn.status == CoAssessmentTurn.Status.ACTIVE,
        'is_closed': turn.status == CoAssessmentTurn.Status.CLOSED,
        'is_paused': turn.is_paused,
        'panel_score': panel_score,
        'panel_raters': panel_count,
        'panel_quorum_met': panel_quorum_met,
        'peer_score': peer_score,
        'peer_raters': peer_count,
        'peer_quorum_met': peer_quorum_met,
        'discrepancy': discrepancy,
        'moderation_required': moderation_required,
        'latest_moderation': latest_moderation,
        'combined_score': combined_score,
        'final_mark': final_mark,
        'group_mark': blended_group_mark or combined_score,
        'member_results': member_results,
        'has_individual_criteria': bool(individual_criteria),
    }


def compute_session_results(session):
    """Rolls up computed turn results across all scheduled groups."""
    turns = list(session.turns.select_related('group').prefetch_related(
        'scorecards__values', 'moderations'
    ).order_by('group__order', 'pk'))

    rows = []
    for turn in turns:
        rows.append(compute_turn_results(turn))
    return rows


def _save_scorecard_values(card, session, group, criterion_scores, individual_scores=None):
    """Persists group-scoped and individual-scoped score values on a scorecard."""
    expected_criteria = {c.pk: c for c in session.criteria.all()}
    members_by_id = {m.pk: m for m in group.members.all()}
    values_to_create = []

    # 1. Group criteria scores
    if criterion_scores:
        for crit_id, score_val in criterion_scores.items():
            if score_val is None or score_val == '':
                continue
            try:
                c_pk = int(crit_id)
            except (ValueError, TypeError):
                continue
            if c_pk not in expected_criteria:
                continue
            criterion = expected_criteria[c_pk]
            dec_score = Decimal(str(score_val))
            if dec_score < Decimal('0') or dec_score > Decimal('100'):
                raise ValidationError(f'Score for {criterion.name} must be between 0 and 100.')
            values_to_create.append(
                CoAssessmentScoreValue(
                    scorecard=card,
                    criterion=criterion,
                    target_participant=None,
                    score=dec_score,
                )
            )

    # 2. Individual criteria scores
    if individual_scores:
        for key, val in individual_scores.items():
            if val is None or val == '':
                continue
            if isinstance(key, tuple) and len(key) == 2:
                k1, k2 = key
                if k1 in expected_criteria and k2 in members_by_id:
                    c_pk, m_pk = k1, k2
                elif k2 in expected_criteria and k1 in members_by_id:
                    c_pk, m_pk = k2, k1
                else:
                    continue
                criterion = expected_criteria[c_pk]
                target_mem = members_by_id[m_pk]
                dec_score = Decimal(str(val))
                if dec_score < Decimal('0') or dec_score > Decimal('100'):
                    raise ValidationError(f'Score for {criterion.name} must be between 0 and 100.')
                values_to_create.append(
                    CoAssessmentScoreValue(
                        scorecard=card,
                        criterion=criterion,
                        target_participant=target_mem,
                        score=dec_score,
                    )
                )
            elif isinstance(val, dict):
                try:
                    m_pk = int(key)
                except (ValueError, TypeError):
                    continue
                if m_pk not in members_by_id:
                    continue
                target_mem = members_by_id[m_pk]
                for c_id, s_val in val.items():
                    if s_val is None or s_val == '':
                        continue
                    try:
                        c_pk = int(c_id)
                    except (ValueError, TypeError):
                        continue
                    if c_pk not in expected_criteria:
                        continue
                    criterion = expected_criteria[c_pk]
                    dec_score = Decimal(str(s_val))
                    if dec_score < Decimal('0') or dec_score > Decimal('100'):
                        raise ValidationError(f'Score for {criterion.name} must be between 0 and 100.')
                    values_to_create.append(
                        CoAssessmentScoreValue(
                            scorecard=card,
                            criterion=criterion,
                            target_participant=target_mem,
                            score=dec_score,
                        )
                    )

    CoAssessmentScoreValue.objects.bulk_create(values_to_create)


@transaction.atomic
def submit_peer_scorecard(turn, participant, criterion_scores, feedback='', individual_scores=None, *, user=None):
    """Submits a student peer scorecard with self-voting prevention."""
    session = turn.session
    if turn.status != CoAssessmentTurn.Status.ACTIVE:
        raise ValidationError('Scoring is closed for this presentation.')
    if participant.session_id != session.pk:
        raise ValidationError('Participant does not belong to this session.')
    if participant.group_id == turn.group_id:
        raise ValidationError('Students are prohibited from evaluating their own team.')

    card, created = CoAssessmentScorecard.objects.get_or_create(
        turn=turn,
        evaluator_tier=CoAssessmentScorecard.Tier.PEER,
        peer_evaluator=participant,
        defaults={
            'organization_id': session.organization_id,
            'created_by': user,
            'qualitative_feedback': feedback.strip(),
        },
    )
    if not created:
        raise ValidationError('You have already submitted a scorecard for this presentation.')

    _save_scorecard_values(card, session, turn.group, criterion_scores, individual_scores)
    broadcast_coassessment_update(session.pk)
    return card


@transaction.atomic
def submit_examiner_scorecard(turn, examiner, criterion_scores, feedback='', individual_scores=None, *, user=None):
    """Submits or updates a panel examiner evaluation for an active turn."""
    session = turn.session
    if turn.status != CoAssessmentTurn.Status.ACTIVE:
        raise ValidationError('Scoring is closed for this presentation.')
    if examiner.session_id != session.pk:
        raise ValidationError('Examiner does not belong to this session.')

    card, created = CoAssessmentScorecard.objects.get_or_create(
        turn=turn,
        evaluator_tier=CoAssessmentScorecard.Tier.PANEL,
        examiner_evaluator=examiner,
        defaults={
            'organization_id': session.organization_id,
            'created_by': user,
            'qualitative_feedback': feedback.strip(),
        },
    )
    if not created:
        card.qualitative_feedback = feedback.strip()
        card.save(update_fields=['qualitative_feedback', 'updated_at'])
        card.values.all().delete()

    _save_scorecard_values(card, session, turn.group, criterion_scores, individual_scores)
    broadcast_coassessment_update(session.pk)
    return card


@transaction.atomic
def record_chair_moderation(turn, action, final_score, justification, *, user):
    """Records an audited chair moderation decision for a turn."""
    session = turn.session
    if not justification or not justification.strip():
        raise ValidationError('A documented justification is required for chair moderation.')

    turn_data = compute_turn_results(turn)
    panel_snapshot = turn_data['panel_score'] or Decimal('0.00')
    peer_snapshot = turn_data['peer_score'] or Decimal('0.00')

    moderation = CoAssessmentModeration.objects.create(
        organization_id=session.organization_id,
        created_by=user,
        turn=turn,
        action=action,
        panel_score_snapshot=panel_snapshot,
        peer_score_snapshot=peer_snapshot,
        final_score=Decimal(str(final_score)),
        justification=justification.strip(),
        reviewed_by=user,
    )
    broadcast_coassessment_update(session.pk)
    return moderation


@transaction.atomic
def sign_off_session(session, *, user):
    """Signs off the co-assessment after checking docket completion, quorums, and moderations."""
    total_groups = session.groups.count()
    if total_groups == 0:
        raise ValidationError('Cannot sign off a session with no presenting teams.')

    closed_turns_count = session.turns.filter(status=CoAssessmentTurn.Status.CLOSED).count()
    if closed_turns_count < total_groups:
        raise ValidationError('All scheduled teams must complete their presentation turns before sign-off.')

    results = compute_session_results(session)
    for row in results:
        if row['moderation_required'] and not row['latest_moderation']:
            raise ValidationError(f'Team "{row["group_name"]}" requires a recorded chair moderation decision before sign-off.')
        if not (row['panel_quorum_met'] and row['peer_quorum_met']) and not row['latest_moderation']:
            raise ValidationError(f'Team "{row["group_name"]}" did not meet minimum evaluator quorum and requires chair moderation.')

    session.status = CoAssessmentSession.Status.SIGNED_OFF
    session.signed_off_at = timezone.now()
    session.save(update_fields=['status', 'signed_off_at', 'updated_at'])
    broadcast_coassessment_update(session.pk)
    return session


@transaction.atomic
def release_session_results(session, *, user):
    """Releases the final marks to the public leaderboard and gradebook."""
    if session.status != CoAssessmentSession.Status.SIGNED_OFF:
        raise ValidationError('The session must be signed off before releasing results.')

    session.status = CoAssessmentSession.Status.RELEASED
    session.released_at = timezone.now()
    session.save(update_fields=['status', 'released_at', 'updated_at'])
    broadcast_coassessment_update(session.pk)
    return session



def _split_cell(row, index):
    """Splits a comma separated spreadsheet cell into cleaned values."""
    if index is None or index >= len(row):
        return []
    return [part.strip() for part in clean(row[index]).split(',') if part.strip()]


ROSTER_SAMPLE_HEADERS = ['Group Name', 'Name', 'Student ID', 'Topic']
ROSTER_SAMPLE_ROWS = [
    ['Aurora', 'Ann Lee, Bo Ng, Cy Oh', 'A101, A102, A103', 'Smart campus map'],
    ['Meridian', 'Di Park, Ed Roy', 'B201, B202', 'Library kiosk app'],
    ['Horizon', 'Flo Kim, Gus Ito, Hal Yu', 'C301, C302, C303', ''],
]


def roster_sample_workbook():
    """Returns the downloadable sample roster workbook."""
    return workbook_bytes(ROSTER_SAMPLE_HEADERS, ROSTER_SAMPLE_ROWS, {'A': 18, 'B': 44, 'C': 26, 'D': 30})


def next_display_order(queryset):
    """Returns the next sequential display order for a queryset with an order field."""
    highest = queryset.aggregate(highest=Max('order'))['highest']
    return (highest or 0) + 1


def parse_roster_spreadsheet(uploaded_file):
    """Parses uploaded group and student roster spreadsheets."""
    rows = read_rows(uploaded_file)
    if rows is None:
        return {'fatal': UNREADABLE, 'groups': []}
    if not rows:
        return {'fatal': 'The uploaded file is empty.', 'groups': []}

    # Expected column headers: Group, Topic, Student Name, Student ID, Email
    header_variants = {
        'group': {'group', 'team', 'group name', 'team name'},
        'topic': {'topic', 'project', 'title', 'project title'},
        'name': {'name', 'student name', 'full name', 'member'},
        'student_id': {'student id', 'id', 'student number', 'matric'},
        'email': {'email', 'email address'},
    }
    header_indices = map_headers(rows[0], header_variants)
    if 'group' not in header_indices:
        return {'fatal': 'Could not find a "Group" or "Team" column in the spreadsheet header.', 'groups': []}

    parsed_groups = {}
    for idx, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        g_name = clean(row[header_indices['group']]) if header_indices.get('group') is not None and header_indices['group'] < len(row) else ''
        if not g_name:
            continue
        topic = clean(row[header_indices['topic']]) if header_indices.get('topic') is not None and header_indices['topic'] < len(row) else ''
        s_names = _split_cell(row, header_indices.get('name'))
        s_ids = _split_cell(row, header_indices.get('student_id'))
        email = clean(row[header_indices['email']]) if header_indices.get('email') is not None and header_indices['email'] < len(row) else ''

        if g_name not in parsed_groups:
            parsed_groups[g_name] = {'name': g_name, 'topic': topic, 'members': []}
        for position, s_name in enumerate(s_names):
            parsed_groups[g_name]['members'].append({
                'full_name': s_name,
                'student_id': s_ids[position] if position < len(s_ids) else '',
                'email': email if len(s_names) == 1 else '',
            })

    return {'fatal': None, 'groups': list(parsed_groups.values())}


@transaction.atomic
def apply_parsed_roster(session, parsed_groups, *, user):
    """Atomically commits parsed groups and participants to the database."""
    for item in parsed_groups:
        group, _ = CoAssessmentGroup.objects.get_or_create(
            session=session,
            name=item['name'],
            defaults={
                'organization_id': session.organization_id,
                'created_by': user,
                'topic': item.get('topic', ''),
                'order': next_display_order(session.groups.all()),
            },
        )
        for mem in item.get('members', []):
            s_id = (mem.get('student_id') or '').strip()
            f_name = (mem.get('full_name') or '').strip()
            em = (mem.get('email') or '').strip()
            if not f_name and not s_id:
                continue

            if s_id:
                participant, created = CoAssessmentParticipant.objects.get_or_create(
                    session=session,
                    student_id=s_id,
                    defaults={
                        'organization_id': session.organization_id,
                        'created_by': user,
                        'group': group,
                        'full_name': f_name or s_id,
                        'email': em,
                    },
                )
                if not created:
                    participant.group = group
                    if f_name:
                        participant.full_name = f_name
                    if em:
                        participant.email = em
                    participant.save(update_fields=['group', 'full_name', 'email', 'updated_at'])
            else:
                participant, created = CoAssessmentParticipant.objects.get_or_create(
                    session=session,
                    group=group,
                    full_name=f_name,
                    defaults={
                        'organization_id': session.organization_id,
                        'created_by': user,
                        'student_id': '',
                        'email': em,
                    },
                )
                if not created and em:
                    participant.email = em
                    participant.save(update_fields=['email', 'updated_at'])


@transaction.atomic
def delete_coassessment_sessions(sessions):
    """Atomically removes co-assessment sessions and dependent records."""
    session_list = list(sessions)
    if not session_list:
        return 0
    session_ids = [s.pk for s in session_list]
    CoAssessmentScoreValue.objects.filter(scorecard__turn__session_id__in=session_ids).delete()
    for session in session_list:
        session.delete()
    return len(session_list)

