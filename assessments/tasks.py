import logging
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)

@shared_task
def open_scheduled_voting(turn_pk):
    from .models import PresentationTurn
    from .turns import open_voting
    turn = PresentationTurn.objects.get(pk=turn_pk)
    if turn.status == PresentationTurn.Status.ACTIVE and turn.voting_opened_at is None:
        open_voting(turn, duration_seconds=None)

@shared_task
def close_voting_window(turn_pk):
    from .models import PresentationTurn
    from .realtime import broadcast_session_event
    turn = PresentationTurn.objects.get(pk=turn_pk)
    if turn.status == PresentationTurn.Status.ACTIVE and turn.voting_ends_at:
        # Voting expiry is deliberately non-terminal in manual/voting-only
        # mode: the lecturer still owns the presentation and next-group move.
        if timezone.now() < turn.voting_ends_at:
            return
        broadcast_session_event(turn.assessment_session, 'voting.closed', {'group_id': turn.group_id})

@shared_task
def finish_presentation(turn_pk):
    from .models import PresentationTurn
    from .turns import auto_advance
    turn = PresentationTurn.objects.get(pk=turn_pk)
    if turn.status == PresentationTurn.Status.ACTIVE:
        turn.voting_ends_at = timezone.now()
        turn.save(update_fields=['voting_ends_at'])
        auto_advance(turn)


@shared_task(bind=True, max_retries=2, default_retry_delay=5)
def auto_advance_turn(self, turn_pk):
    """Scheduled (via `apply_async(eta=...)`) by `assessments.turns.activate_turn`
    for the exact moment a timed turn's voting window ends. Callable directly
    (no broker needed) — `assessments.turns.auto_advance` does the real work
    and is what tests exercise; this is just the scheduling wrapper.
    """
    from .models import PresentationTurn
    from .turns import auto_advance

    try:
        turn = (PresentationTurn.objects
                .select_related('assessment_session', 'group')
                .get(pk=turn_pk))
    except PresentationTurn.DoesNotExist:
        return

    try:
        auto_advance(turn)
    except Exception as exc:
        logger.exception('auto_advance_turn failed for turn %s', turn_pk)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=5)
def activate_pending_group(self, session_pk):
    """Scheduled (via `apply_async(eta=...)`) by `assessments.turns._advance_to`
    for the moment a session's configured transition gap finishes. The real
    logic lives in `assessments.turns.activate_pending_transition`, which
    tests call directly; this is just the scheduling wrapper, same pattern
    as `auto_advance_turn` above."""
    from .models import AssessmentSession
    from .turns import activate_pending_transition

    try:
        session = AssessmentSession.objects.get(pk=session_pk)
    except AssessmentSession.DoesNotExist:
        return

    try:
        activate_pending_transition(session)
    except Exception as exc:
        logger.exception('activate_pending_group failed for session %s', session_pk)
        raise self.retry(exc=exc)
