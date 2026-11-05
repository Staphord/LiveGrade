"""One ranking policy for owner and public Classroom standings."""
from .models import PresentationTurn
from .scoring import group_score_percents, group_vote_counts


def group_standings(session, *, completed_only=False, active_group_id=None):
    groups = list(session.groups.filter(is_practice=False).order_by('order', 'pk'))
    scores = group_score_percents(session, groups)
    votes = group_vote_counts(session)
    completed = set()
    if completed_only:
        completed = set(session.presentation_turns.filter(
            status=PresentationTurn.Status.CLOSED).values_list('group_id', flat=True))
        active = session.active_turn()
        active_group_id = active.group_id if active else None
    rows = []
    for group in groups:
        eligible = not completed_only or (group.pk in completed and (active_group_id != group.pk))
        score = scores[group.pk] if eligible else None
        rows.append({'group': group, 'percent': score, 'votes': votes.get(group.pk, 0),
                     'is_active': active_group_id == group.pk})
    rows.sort(key=lambda row: (row['percent'] is None, -(row['percent'] or 0), row['group'].order, row['group'].pk))
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
