"""Privacy-safe, provisional progress for Panel live controls and displays."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import (PanelAbstention, PanelCandidate, PanelConflict, PanelCriterion,
                     PanelHearing, PanelScoringPanel, PanelScore, PanelVote,
                     PanelWrittenReview)
from .operations import panel_aggregate


SCORED_WORKFLOWS = {PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID}
VOTED_WORKFLOWS = {PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
                   PanelHearing.Workflow.REVIEW_VOTE}
REVIEW_WORKFLOWS = {PanelHearing.Workflow.REVIEW, PanelHearing.Workflow.REVIEW_VOTE}


def panel_live_activity(hearing):
    """Return aggregate progress and provisional, equally weighted scores.

    Panel criteria have no configured weights, so provisional overall scores use
    an arithmetic mean. Conflicted examiners are excluded, as they are at compile.
    """
    organization = hearing.organization_id
    candidates = list(PanelCandidate.objects.filter(organization_id=organization)
        .filter(hearing=hearing).order_by('order', 'pk'))
    panels = list(PanelScoringPanel.objects.filter(organization_id=organization)
        .filter(hearing=hearing).prefetch_related('criteria').order_by('pk'))
    criteria = list(PanelCriterion.objects.filter(organization_id=organization)
        .filter(hearing=hearing).prefetch_related('panels').order_by('order', 'pk'))
    conflicts_by_candidate = defaultdict(set)
    for invitation_id, candidate_id in (PanelConflict.objects.filter(organization_id=organization)
            .filter(candidate__hearing=hearing).values_list('invitation_id', 'candidate_id')):
        conflicts_by_candidate[candidate_id].add(invitation_id)

    score_groups = defaultdict(list)
    responded = defaultdict(set)
    for score in (PanelScore.objects.filter(organization_id=organization)
            .filter(candidate__hearing=hearing, criterion__hearing=hearing)
            .select_related('invitation')):
        if score.invitation_id in conflicts_by_candidate[score.candidate_id]:
            continue
        score_groups[(score.candidate_id, score.criterion_id,
                      score.invitation.panel_id)].append(score.score)
        responded[score.candidate_id].add(score.invitation_id)
    for model, filters in (
            (PanelAbstention, {'candidate__hearing': hearing}),
            (PanelVote, {'hearing': hearing}),
            (PanelWrittenReview, {'hearing': hearing})):
        for candidate_id, invitation_id in (model.objects.filter(organization_id=organization)
                .filter(**filters).values_list('candidate_id', 'invitation_id')):
            if invitation_id not in conflicts_by_candidate[candidate_id]:
                responded[candidate_id].add(invitation_id)

    # Read each response family once. Sets protect counts from duplicate rows and
    # make the minimum coverage checks match distinct examiner participation.
    vote_ids = defaultdict(set)
    for candidate_id, panel_id, invitation_id in (PanelVote.objects.filter(organization_id=organization)
            .filter(hearing=hearing).exclude(choice=PanelVote.Choice.ABSTAIN)
            .values_list('candidate_id', 'invitation__panel_id', 'invitation_id')):
        if invitation_id not in conflicts_by_candidate[candidate_id]:
            vote_ids[(candidate_id, panel_id)].add(invitation_id)
    review_ids = defaultdict(set)
    for candidate_id, panel_id, invitation_id in (PanelWrittenReview.objects.filter(organization_id=organization)
            .filter(hearing=hearing).values_list('candidate_id', 'invitation__panel_id', 'invitation_id')):
        if invitation_id not in conflicts_by_candidate[candidate_id]:
            review_ids[(candidate_id, panel_id)].add(invitation_id)

    scoring = hearing.workflow in SCORED_WORKFLOWS
    voting = hearing.workflow in VOTED_WORKFLOWS
    reviewing = hearing.workflow in REVIEW_WORKFLOWS
    entries = []
    required_units = completed_units = 0
    for candidate in candidates:
        criterion_scores = []
        complete = bool(criteria) and scoring
        candidate_required = candidate_completed = 0
        for criterion in criteria if scoring else []:
            assigned = [panel for panel in panels
                if panel.share > 0 and criterion in panel.criteria.all()]
            if not assigned:
                complete = False
                continue
            share_total = sum((panel.share for panel in assigned), Decimal('0'))
            weighted_score = Decimal('0')
            observed_share = Decimal('0')
            criterion_complete = True
            for panel in assigned:
                values = score_groups.get((candidate.pk, criterion.pk, panel.pk), [])
                count = len(values)
                minimum = panel.minimum_examiners
                candidate_required += minimum
                candidate_completed += min(count, minimum)
                if count < minimum:
                    criterion_complete = False
                if values:
                    effective_share = panel.share / share_total
                    weighted_score += panel_aggregate(values, panel.aggregation,
                        panel.trim_fraction) * effective_share
                    observed_share += effective_share
                else:
                    criterion_complete = False
            if observed_share:
                if observed_share != 1:
                    weighted_score /= observed_share
                criterion_scores.append((weighted_score, criterion.weight))
            if not criterion_complete:
                complete = False
        if voting or reviewing:
            for panel in panels:
                if voting:
                    candidate_required += panel.minimum_examiners
                    candidate_completed += min(len(vote_ids[(candidate.pk, panel.pk)]),
                                                panel.minimum_examiners)
                if reviewing:
                    candidate_required += panel.minimum_examiners
                    candidate_completed += min(len(review_ids[(candidate.pk, panel.pk)]),
                                                panel.minimum_examiners)

        required_units += candidate_required
        completed_units += candidate_completed
        score = (sum((value * weight for value, weight in criterion_scores), Decimal('0')) /
                 sum((weight for value, weight in criterion_scores), Decimal('0'))
                 if criterion_scores else None)
        entries.append({
            'id': candidate.pk,
            'name': candidate.display_name,
            'active': candidate.pk == hearing.active_candidate_id,
            'responses': len(responded[candidate.pk]),
            'score': (format(score.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
                      if score is not None else None),
            'complete': bool(complete and score is not None),
            'candidate_progress': candidate_completed,
            'candidate_required': candidate_required,
            'progress_unit': 'score items' if scoring and not voting and not reviewing
                else 'response items',
        })

    if scoring:
        entries.sort(key=lambda item: (not item['complete'],
            -(Decimal(item['score']) if item['complete'] else Decimal('-1')), item['id']))
        previous_score = None
        rank = 0
        for position, item in enumerate(entries, 1):
            if not item['complete']:
                item['rank'] = None
            else:
                if item['score'] != previous_score:
                    rank = position
                item['rank'] = rank
                previous_score = item['score']
    else:
        entries.sort(key=lambda item: (not item['active'], item['name'].casefold(), item['id']))
        for item in entries:
            item['rank'] = None

    percentage = round((completed_units / required_units) * 100) if required_units else 0
    return {
        'hearing': hearing.name,
        'active_candidate': hearing.active_candidate.display_name if hearing.active_candidate_id else '',
        'open': hearing.status == PanelHearing.Status.OPEN,
        'status': hearing.status,
        'scoring': scoring,
        'progress_unit': 'score items' if scoring and not voting and not reviewing
            else 'response items',
        'progress': {'completed': completed_units, 'required': required_units,
                     'percent': min(percentage, 100)},
        'responses': sum(item['responses'] for item in entries),
        'candidates': entries,
        'updated': timezone.now().isoformat(),
    }
