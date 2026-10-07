"""An uploaded spreadsheet waiting for the lecturer's decision.

Uploading a groups or rubric file does not change the session: the file is read,
kept in the lecturer's own session, and shown in a review window over the setup
page. Pressing Import applies it; Cancel drops it. These helpers build what that
window shows, from the stored file, against the session as it is right now.
"""
from . import group_import, rubric_import, rubric_rules
from .models import TOTAL_WEIGHT
from .setup_rules import rubric_structure_lock

GROUP_KEY = 'group_import'
RUBRIC_KEY = 'rubric_import'


def pending_group_import(request, session):
    """The plan for a waiting groups file, or ``None``."""
    stash = request.session.get(GROUP_KEY)
    if not stash or stash['session'] != session.pk:
        return None
    return group_import.plan_import(session, stash['parsed'])


def pending_rubric_import(request, session):
    """What the review window shows for a waiting rubric file, or ``None``.

    A rubric that can no longer change (votes exist) drops the file instead of
    offering it.
    """
    stash = request.session.get(RUBRIC_KEY)
    if not stash or stash['session'] != session.pk:
        return None
    if rubric_structure_lock(session):
        request.session.pop(RUBRIC_KEY, None)
        return None
    verdict = rubric_import.check_import(stash['rows'], session, stash['mode'])
    totals = [{'label': rubric_rules.SCOPE_LABEL[scope], 'total': total}
              for scope, total in verdict['totals'].items() if total]
    total = verdict.get('total')
    problems = {}
    for number, message in verdict['errors']:
        problems.setdefault(number, []).append(message)
    return {
        'verdict': verdict, 'replacing': stash['mode'] == 'replace', 'totals': totals,
        'total': total, 'total_ok': total == TOTAL_WEIGHT,
        'existing_count': session.rubric_categories.count(),
        'general_errors': problems.get(None, []),
        'rows': [{**raw, 'problems': problems.get(raw['row'], [])} for raw in stash['rows']],
    }
