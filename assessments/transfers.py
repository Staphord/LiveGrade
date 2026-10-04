"""Handing a session to another lecturer.

A session is private to the person who created it (see ``views.lecturer_views._session``),
so ownership is the whole access model: moving ``created_by`` moves every page
and button of the session at once, and the previous owner loses all of it
immediately. Students are unaffected - they use the join link, not an account.
"""
from django.db import transaction

from accounts.directory import colleagues

from .models import AssessmentSession, SessionTransfer


class TransferError(Exception):
    """The hand-over is not allowed; the message says why, in plain words."""


def eligible_recipients(session, excluding_owner=True):
    """Who the session may be handed to: the other people known to run
    LiveGrade in its organization."""
    return colleagues(session.organization_id, excluding=session.created_by if excluding_owner else None)


@transaction.atomic
def transfer_session(session, to_user, by_user=None, note='', require_known=True):
    """Make ``to_user`` the owner of ``session`` and record it.

    ``require_known=False`` is for an operator who has to hand a session to
    somebody who has not yet signed in on this system; everyone else must pick
    from ``eligible_recipients``.
    """
    # Locked, so two hand-overs at once cannot both succeed from the same owner.
    session = AssessmentSession.objects.select_for_update().get(pk=session.pk)
    if to_user.pk == session.created_by_id:
        raise TransferError('That lecturer already owns this session.')
    if require_known and not eligible_recipients(session).filter(pk=to_user.pk).exists():
        raise TransferError(
            'That lecturer is not known to run LiveGrade in this organization. '
            'They need to sign in to LiveGrade once first.')
    previous = session.created_by
    session.created_by = to_user
    session.save(update_fields=['created_by'])
    SessionTransfer.objects.create(
        session=session, from_user=previous, to_user=to_user, by_user=by_user, note=note)
    return session
