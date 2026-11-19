"""One ranking policy for owner and public Assessment standings across Peer, Panel, and Both modes."""

from django.db.models import Count

from .models import PanelScorecard, PresentationTurn
from .scoring import group_score_percents, group_vote_counts


def group_standings(session, *, completed_only=False, active_group_id=None):
    groups = list(session.groups.filter(is_practice=False).order_by('order', 'pk'))
    completed = set()
    if completed_only:
        completed = set(
            session.presentation_turns.filter(status=PresentationTurn.Status.CLOSED)
            .values_list('group_id', flat=True)
        )
        active = session.active_turn()
        active_group_id = active.group_id if active else None

    enable_peer = getattr(session, 'enable_peer', True)
    enable_panel = getattr(session, 'enable_panel', False)

    votes = group_vote_counts(session) if enable_peer else {}
    panel_counts = {}
    if enable_panel:
        panel_counts = dict(
            PanelScorecard.objects.filter(turn__assessment_session=session)
            .values('turn__group_id')
            .annotate(c=Count('id'))
            .values_list('turn__group_id', 'c')
        )

    if enable_peer and not enable_panel:
        scores = group_score_percents(session, groups)
    else:
        scores = {}

    if enable_panel:
        from .unified_operations import panel_group_score_percent, unified_group_outcome
    else:
        panel_group_score_percent = None
        unified_group_outcome = None

    rows = []
    for group in groups:
        eligible = not completed_only or (group.pk in completed and active_group_id != group.pk)
        peer_score = None
        panel_score = None

        if not eligible:
            score = None
        elif enable_peer and enable_panel:
            outcome = unified_group_outcome(group, session)
            peer_score = outcome['peer_score']
            panel_score = outcome['panel_score']
            score = outcome['effective_score']
            if score is None:
                score = panel_score if panel_score is not None else peer_score
        elif enable_panel:
            raw_panel = panel_group_score_percent(group, session)
            panel_score = round(raw_panel, 2) if raw_panel is not None else None
            score = panel_score
        else:
            score = scores.get(group.pk)

        rows.append({
            'group': group,
            'percent': score,
            'peer_score': peer_score,
            'panel_score': panel_score,
            'votes': votes.get(group.pk, 0),
            'panel_count': panel_counts.get(group.pk, 0),
            'is_active': active_group_id == group.pk,
        })

    rows.sort(
        key=lambda row: (
            row['percent'] is None,
            -(row['percent'] or 0),
            row['group'].order,
            row['group'].pk,
        )
    )
    last_score, rank = None, None
    for index, row in enumerate(rows, 1):
        score = row['percent']
        if score is None:
            row['rank'] = None
        else:
            if score != last_score:
                rank, last_score = index, score
            row['rank'] = rank
    return rows
