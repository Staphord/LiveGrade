"""Read-only composition of Classroom and Panel results for linked events."""

from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count

from assessments.models import Evaluation, RubricCategory
from assessments.scoring import group_score_percent
from .models import (CombinedPresentationAssessment, CombinedPresentationMapping,
                     CombinedModerationDecision, PanelDecision, PanelHearing,
                     PanelResultSnapshot, PanelVote, PanelWrittenReview)


def mapping_is_complete(assessment):
    """Require exact entry coverage and unchanged group membership snapshots."""
    expected_groups = set(assessment.classroom_session.groups.filter(
        is_practice=False).values_list('pk', flat=True))
    expected_candidates = set(assessment.panel_hearing.candidates.values_list('pk', flat=True))
    mapped_groups = set(assessment.mappings.values_list('group_id', flat=True))
    mapped_candidates = set(assessment.mappings.values_list('candidate_id', flat=True))
    if not (expected_groups and expected_candidates and
            mapped_groups == expected_groups and mapped_candidates == expected_candidates):
        return False
    for mapping in assessment.mappings.select_related('group', 'candidate').prefetch_related(
            'group__memberships__student').all():
        current_members = sorted(mapping.group.memberships.values_list('student_id', flat=True))
        if current_members != mapping.group_member_ids_snapshot:
            return False
        if mapping.candidate.source_group_id not in (None, mapping.group_id):
            return False
    return True


def normalized_peer_group_score(group, session):
    """Normalize Classroom's group component to 0–100 for cross-source use.

    Classroom category weights can be a share of a larger group-plus-individual
    grade. The combined presentation result compares the group-scoped peer
    component with the Panel presentation score, so divide by the configured
    group-rubric weight total before applying event-level weights.
    """
    raw_score = group_score_percent(group, session)
    maximum = sum(session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP)
        .values_list('weight', flat=True), Decimal('0'))
    if raw_score is None or maximum <= 0:
        return None
    return (raw_score * Decimal('100') / maximum).quantize(
        Decimal('0.000001'), rounding=ROUND_HALF_UP)


def combined_assessment_results(assessment):
    """Return safe, source-separated rows and policy-compliant combined marks.

    Scores are never inferred from missing data. Panel scores are comparable
    only for numeric rubric workflows; each Panel criterion is normalized to
    0–100 by the Panel scoring service before its immutable snapshot is stored.
    """
    if not isinstance(assessment, CombinedPresentationAssessment):
        raise TypeError('assessment must be a CombinedPresentationAssessment')
    entries_complete = mapping_is_complete(assessment)
    result_rows = []
    for mapping in assessment.mappings.select_related('group', 'candidate').all():
        peer_count = Evaluation.objects.filter(
            presentation_turn__assessment_session=assessment.classroom_session,
            presentation_turn__group=mapping.group,
            presentation_turn__group__is_practice=False,
            target_student__isnull=True,
        ).values('evaluator_id').distinct().count()
        peer_score = normalized_peer_group_score(mapping.group, assessment.classroom_session)
        snapshots = list(PanelResultSnapshot.objects.filter(
            hearing=assessment.panel_hearing, candidate=mapping.candidate,
        ).select_related('criterion').prefetch_related('adjustments').order_by('criterion_id'))
        expected_criteria = assessment.panel_hearing.criteria.count()
        panel_criteria_complete = expected_criteria > 0 and len(snapshots) == expected_criteria
        adjusted_scores = []
        for snapshot in snapshots:
            latest_adjustment = snapshot.adjustments.order_by('-revision').first()
            adjusted_scores.append((latest_adjustment.adjusted_score if latest_adjustment else snapshot.score)
                                   * snapshot.criterion.weight)
        panel_coverage = min((row.rater_count for row in snapshots), default=0)
        panel_score = None
        if panel_criteria_complete:
            panel_score = (sum(adjusted_scores, Decimal('0')) /
                           sum((row.criterion.weight for row in snapshots), Decimal('0'))).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        panel_decision = (PanelDecision.objects.filter(hearing=assessment.panel_hearing,
            candidate=mapping.candidate).order_by('-revision').first())
        panel_vote_counts = {row['choice']: row['count'] for row in PanelVote.objects.filter(
            hearing=assessment.panel_hearing, candidate=mapping.candidate
        ).values('choice').annotate(count=Count('pk'))}
        panel_review_count = PanelWrittenReview.objects.filter(
            hearing=assessment.panel_hearing, candidate=mapping.candidate).count()
        peer_covered = peer_score is not None and peer_count >= assessment.minimum_peer_ratings
        panel_covered = (panel_score is not None and
                         panel_coverage >= assessment.minimum_panel_raters)
        discrepancy = abs(peer_score - panel_score) if peer_score is not None and panel_score is not None else None
        moderation_required = (assessment.result_policy == CombinedPresentationAssessment.ResultPolicy.WEIGHTED
            and discrepancy is not None and assessment.moderation_threshold is not None
            and discrepancy > assessment.moderation_threshold)
        latest_moderation = mapping.moderation_decisions.order_by('-revision').first()
        review_matches_scores = bool(latest_moderation and peer_score is not None and panel_score is not None
            and latest_moderation.peer_score_at_review == peer_score.quantize(Decimal('0.000001'))
            and latest_moderation.panel_score_at_review == panel_score.quantize(Decimal('0.000001')))
        moderation_approved = (not moderation_required or (review_matches_scores and
            latest_moderation.decision == CombinedModerationDecision.Decision.APPROVE))
        moderation_state = ('not_required' if not moderation_required else
            latest_moderation.decision if review_matches_scores else 'required')
        combined_score = None
        sources_final = (assessment.classroom_session.status == 'closed' and
            assessment.panel_hearing.status in (PanelHearing.Status.SIGNED_OFF, PanelHearing.Status.RELEASED))
        if (assessment.result_policy == CombinedPresentationAssessment.ResultPolicy.WEIGHTED
                and peer_covered and panel_covered and sources_final and moderation_approved
                and entries_complete):
            combined_score = (
                peer_score * assessment.peer_weight / Decimal('100') +
                panel_score * assessment.panel_weight / Decimal('100')
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        moderation_history = list(mapping.moderation_decisions.select_related('reviewed_by').order_by('-revision'))
        result_rows.append({
            'mapping_id': mapping.pk,
            'group': mapping.group,
            'candidate': mapping.candidate,
            'already_presented': mapping.group.turns.filter(status='closed').exists(),
            'peer_score': peer_score,
            'peer_rater_count': peer_count,
            'peer_covered': peer_covered,
            'panel_score': panel_score,
            'panel_decision': panel_decision,
            'panel_vote_counts': panel_vote_counts,
            'panel_review_count': panel_review_count,
            'panel_rater_count': panel_coverage,
            'panel_covered': panel_covered,
            'sources_final': sources_final,
            'mapping_complete': entries_complete,
            'score_difference': discrepancy,
            'moderation_required': moderation_required,
            'moderation_state': moderation_state,
            'combined_score': combined_score,
            'moderation_history': moderation_history,
        })
    return result_rows


def record_combined_moderation(mapping, decision, reason, *, organization, user):
    """Append a reasoned decision against the exact currently final source scores."""
    from django.core.exceptions import PermissionDenied, ValidationError
    from django.db import transaction

    from .models import CombinedPresentationAssessment

    with transaction.atomic():
        locked = (CombinedPresentationMapping.objects.for_organization(organization)
            .owned_by(user).select_for_update().select_related(
                'assessment__classroom_session', 'assessment__panel_hearing', 'group', 'candidate')
            .filter(pk=mapping.pk).first())
        if locked is None:
            raise PermissionDenied
        assessment = locked.assessment
        if assessment.result_policy != CombinedPresentationAssessment.ResultPolicy.WEIGHTED:
            raise ValidationError('Moderation applies only to weighted combined marks.')
        row = next((item for item in combined_assessment_results(assessment)
                    if item['mapping_id'] == locked.pk), None)
        if not row or not row['sources_final'] or not row['peer_covered'] or not row['panel_covered']:
            raise ValidationError('Both source results must be final and meet minimum coverage before moderation.')
        if not row['moderation_required']:
            raise ValidationError('This source-score difference does not meet the configured moderation threshold.')
        previous = locked.moderation_decisions.order_by('-revision').first()
        review = CombinedModerationDecision(
            mapping=locked, decision=decision, reason=reason.strip(),
            peer_score_at_review=row['peer_score'].quantize(Decimal('0.000001')),
            panel_score_at_review=row['panel_score'].quantize(Decimal('0.000001')),
            revision=previous.revision + 1 if previous else 1, supersedes=previous,
            reviewed_by=user, created_by=user, organization_id=locked.organization_id)
        review.full_clean()
        review.save(force_insert=True)
        return review
