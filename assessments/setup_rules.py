"""What may still be changed in a session's setup, and why not.

Results are worked out from the setup as it stands (see ``scoring``): a category's
scale and weight, which students sit in which group. So once people have graded,
some edits would silently rewrite the record, and a few would delete grades. Every
setup endpoint asks this module, and the pages use the same answers to explain a
locked control instead of just greying it out - the server enforces, the page
explains.

Each ``*_lock`` returns ``None`` when the change is allowed, otherwise the reason.
"""
from .models import AssessmentSession, Evaluation, GroupMembership, PresentationTurn, SessionChange
from .realtime import broadcast_session_event

CLOSED = 'This session is closed. It is now a read-only record.'


def closed_reason(session):
    return CLOSED if session.status == AssessmentSession.Status.CLOSED else None


def is_live(session):
    return session.status == AssessmentSession.Status.LIVE


def has_votes(session):
    return Evaluation.objects.filter(presentation_turn__assessment_session=session).exists()


def _presented_message(group):
    return f'"{group.name}" has presented or is presenting, so it stays in the record.'


def _graded_message(group):
    return (f'Students in "{group.name}" have already graded other groups, and removing '
            f'them would erase those grades.')


def group_members_lock(group):
    if group.turns.exists():
        return f'"{group.name}" has presented or is presenting, so its members are fixed.'
    return None


def group_delete_lock(group):
    if group.turns.exists():
        return _presented_message(group)
    if Evaluation.objects.filter(evaluator__group_memberships__group=group).exists():
        return _graded_message(group)
    return None


def annotate_groups(session, groups):
    """Put ``members_lock`` and ``delete_lock`` (a reason, or '') on each group.

    The same rules as ``group_members_lock`` / ``group_delete_lock``, worked out for
    a whole page of groups in three queries instead of two per group.
    """
    presented = set(PresentationTurn.objects.filter(
        assessment_session=session).values_list('group_id', flat=True))
    graders = set(Evaluation.objects.filter(
        presentation_turn__assessment_session=session).values_list('evaluator_id', flat=True))
    grading = {group_id for group_id, student_id in GroupMembership.objects.filter(
        group__assessment_session=session).values_list('group_id', 'student_id') if student_id in graders}
    for group in groups:
        group.members_lock = (f'"{group.name}" has presented or is presenting, so its members are fixed.'
                              if group.pk in presented else '')
        if group.pk in presented:
            group.delete_lock = _presented_message(group)
        elif group.pk in grading:
            group.delete_lock = _graded_message(group)
        else:
            group.delete_lock = ''


def student_delete_lock(student):
    if student.evaluations_given.exists() or student.evaluations_received.exists():
        return f'{student} has already graded or been graded, so they stay in the record.'
    return None


def category_core_lock(category):
    if category.scores.exists():
        return (f'"{category.name}" already has scores, so its scale and whether it is '
                f'group or individual cannot change.')
    return None


def rubric_structure_lock(session):
    if has_votes(session):
        return ('Votes have been submitted, so categories cannot be added or removed: '
                'later groups would be graded on a different rubric than earlier ones.')
    return None


def live_rubric_lock(session):
    """The one-row-at-a-time rubric forms cannot keep the weights adding up, so a
    live session's rubric is edited as a whole table instead."""
    if is_live(session):
        return 'This session is live: edit the rubric as a table so the weights keep adding up.'
    return None


def record_change(session, user, summary, notify='setup.changed'):
    """Log an edit made after go-live and tell open screens. Does nothing for a
    draft, which has no record to protect yet."""
    if not is_live(session):
        return
    SessionChange.objects.create(session=session, by_user=user, summary=summary[:255])
    broadcast_session_event(session, notify, {})
