"""Turn transitions — activating a group, closing one, and the timed
auto-advance chain. Pulled into its own module because both the lecturer's
manual "activate group" / "close voting" buttons and the scheduled Celery
task need the exact same close-then-open-next logic; duplicating it between
a view and a task is how the two quietly drift apart.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import AssessmentSession, PresentationTurn
from .realtime import broadcast_session_event


def activate_turn(session, group, duration_seconds=None, gap_seconds=None,
                  presentation_seconds=None):
    """Close whatever turn is active, open `group`, and — if a duration was
    given — schedule this turn to auto-close (and auto-advance) then.

    `duration_seconds=None` means no timer: the lecturer closes voting
    manually, same as before this feature existed. `gap_seconds`, when
    given, updates the session's remembered pause-between-groups setting
    (see `transition_gap_seconds` on the model); omit it (as the scheduled
    transition below and the Celery task both do) to leave whatever the
    lecturer last chose untouched.

    A manual activation — the lecturer picking a group directly, whether or
    not one was already counting down to start on its own — always
    supersedes any pending timed transition, so that gets cleared here too.

    If voting is currently paused, the new turn's timer is born already
    paused — frozen at the full duration via `paused_remaining_seconds`
    instead of a live `voting_ends_at` — rather than ignoring the pause and
    starting a fresh countdown regardless. Without this, pausing while no
    group was presenting (nothing yet for `pause_timer` to freeze) and then
    picking a group would start a normal running timer that closes and
    auto-advances right on schedule, even though the control panel still
    shows "Voting paused" and students still can't vote.

    The close-then-create sequence below runs under a row lock on `session`
    (`select_for_update`), so two calls for the same session can never
    interleave. Without it, a lecturer's manual click landing at the same
    instant as a scheduled auto-transition (the winner of the `close_turn`/
    `activate_pending_transition` races above, itself calling this
    function) could both read "nothing active yet", both create a turn,
    and have the second commit close the first one's turn a few
    milliseconds after opening it — the same "skips a group" symptom, one
    layer up. The lock serializes them instead: whichever call's
    transaction commits second simply sees the first one's turn as active
    and closes it cleanly, same as any other sequential re-activation.
    """
    with transaction.atomic():
        session = AssessmentSession.objects.select_for_update().get(pk=session.pk)
        now = timezone.now()
        session.presentation_turns.filter(status=PresentationTurn.Status.ACTIVE).update(
            status=PresentationTurn.Status.CLOSED, closed_at=now)

        starts_paused = bool(duration_seconds) and session.voting_paused
        voting_ends_at = None if starts_paused else (
            now + timedelta(seconds=duration_seconds) if duration_seconds else None)
        presenting_only = bool(presentation_seconds)
        presentation_ends_at = now + timedelta(seconds=presentation_seconds) if presentation_seconds else None
        voting_open_at = now + timedelta(seconds=int(presentation_seconds * .4)) if presentation_seconds else None
        voting_close_at = now + timedelta(seconds=int(presentation_seconds * .8)) if presentation_seconds else voting_ends_at
        turn = PresentationTurn.objects.create(
            assessment_session=session, group=group,
            status=PresentationTurn.Status.ACTIVE, opened_at=now,
            voting_opened_at=None if presenting_only else now,
            voting_ends_at=voting_close_at,
            presentation_ends_at=presentation_ends_at,
            paused_remaining_seconds=duration_seconds if starts_paused else None)

        session.default_turn_seconds = duration_seconds
        update_fields = ['default_turn_seconds']
        if gap_seconds is not None:
            session.transition_gap_seconds = gap_seconds
            update_fields.append('transition_gap_seconds')
        if session.pending_next_group_id is not None or session.pending_transition_at is not None:
            session.pending_next_group = None
            session.pending_transition_at = None
            update_fields += ['pending_next_group', 'pending_transition_at']
        if session.designated_next_group_id == group.pk:
            session.designated_next_group = None
            update_fields.append('designated_next_group')
        session.save(update_fields=update_fields)

    broadcast_session_event(session, 'turn.activated', {
        'group_id': group.pk, 'group_name': group.name,
        'member_ids': list(group.member_ids()),
        'voting_opened_at': now.isoformat(),
        'voting_ends_at': voting_ends_at.isoformat() if voting_ends_at else None,
    })

    if presentation_seconds:
        from .tasks import open_scheduled_voting, close_voting_window, finish_presentation
        open_scheduled_voting.apply_async((turn.pk,), eta=voting_open_at)
        close_voting_window.apply_async((turn.pk,), eta=voting_close_at)
        finish_presentation.apply_async((turn.pk,), eta=presentation_ends_at)
    elif voting_ends_at:
        # Imported here, not at module level: this module must stay
        # importable (and its functions callable directly in tests) even in
        # a process with no Celery app configured.
        from .tasks import close_voting_window
        close_voting_window.apply_async((turn.pk,), eta=voting_ends_at)

    return turn


def open_voting(turn, duration_seconds=None):
    """Move a presenting turn into its voting window."""
    now = timezone.now()
    if turn.status != PresentationTurn.Status.ACTIVE:
        return turn
    duration = duration_seconds if duration_seconds is not None else turn.assessment_session.default_turn_seconds
    turn.voting_opened_at = now
    if not turn.presentation_ends_at:
        turn.voting_ends_at = now + timedelta(seconds=duration) if duration else None
    turn.save(update_fields=['voting_opened_at', 'voting_ends_at'])
    broadcast_session_event(turn.assessment_session, 'voting.opened', {
        'turn_id': turn.pk, 'group_id': turn.group_id,
        'voting_ends_at': turn.voting_ends_at.isoformat() if turn.voting_ends_at else None,
    })
    if turn.voting_ends_at and not turn.presentation_ends_at:
        from .tasks import auto_advance_turn
        auto_advance_turn.apply_async((turn.pk,), eta=turn.voting_ends_at)
    return turn


def _advance_to(session, next_group):
    """Open `next_group` next — immediately if the session has no
    transition gap configured, or after a countdown otherwise, so a fully
    hands-off session doesn't snap from one group straight into the next
    with zero breathing room. Does nothing if there's no next group."""
    if next_group is None:
        return

    gap = session.transition_gap_seconds or 0
    if gap <= 0:
        activate_turn(session, next_group, duration_seconds=session.default_turn_seconds,
                      presentation_seconds=session.default_presentation_seconds)
        return

    resumes_at = timezone.now() + timedelta(seconds=gap)
    session.pending_next_group = next_group
    session.pending_transition_at = resumes_at
    session.save(update_fields=['pending_next_group', 'pending_transition_at'])

    broadcast_session_event(session, 'transition.scheduled', {
        'next_group_id': next_group.pk, 'next_group_name': next_group.name,
        'resumes_at': resumes_at.isoformat(),
    })

    from .tasks import activate_pending_group
    activate_pending_group.apply_async((session.pk,), eta=resumes_at)


def activate_pending_transition(session):
    """The gap counted down (or someone is checking in on it early via
    `ensure_turn_progressed`): if it's genuinely elapsed, open the group it
    was holding. A no-op if nothing is pending, or if the gap hasn't
    actually finished yet, or if a manual action already superseded it
    (`activate_turn` clears these fields the moment it runs)."""
    session.refresh_from_db()
    if session.pending_next_group_id is None or session.pending_transition_at is None:
        return
    if timezone.now() < session.pending_transition_at:
        return
    group = session.pending_next_group
    # Same race as `close_turn`: `activate_pending_group` (Celery, at the
    # gap's eta) and a client's own `initTransitionCountdown` ping can both
    # land here for the same pending transition. A conditional update lets
    # only one of them actually clear the pending fields and proceed —
    # otherwise both call `activate_turn(group)` and the second one's call
    # closes the first one's brand new turn a few milliseconds after it
    # opened, the same "skips a group" symptom as the auto-advance race.
    won = AssessmentSession.objects.filter(
        pk=session.pk, pending_next_group_id=group.pk
    ).exclude(pending_transition_at=None).update(
        pending_next_group=None, pending_transition_at=None)
    if not won:
        return
    session.pending_next_group = None
    session.pending_transition_at = None
    activate_turn(session, group, duration_seconds=session.default_turn_seconds,
                  presentation_seconds=session.default_presentation_seconds)


def close_turn(turn):
    """Close one turn (manual "Close voting" button). Does not advance —
    the lecturer picks the next group themselves, or nothing happens if
    they were relying on a timer that this supersedes.

    An expired presentation-timed turn can get raced to this point from
    more than one direction at once — the `finish_presentation` Celery
    task firing for its `eta`, a student's or the lecturer's own
    self-heal ping landing at the same deadline, another browser tab's
    countdown doing the same — all reading the turn as still ACTIVE before
    any of them has written. Closing via a conditional `UPDATE ... WHERE
    status=ACTIVE` (rather than an unconditional save) makes only one of
    them actually perform the transition; the rest see 0 rows affected and
    return False. Callers that advance to the next group afterwards must
    check this return value and only advance if they won — otherwise two
    racing callers each advance independently, and the second one's
    `activate_turn` closes the first one's brand new turn out from under
    it (marking that group "presented" after a few milliseconds) and
    opens yet another group in its place, skipping one in the process.
    """
    closed_at = timezone.now()
    won = PresentationTurn.objects.filter(
        pk=turn.pk, status=PresentationTurn.Status.ACTIVE
    ).update(status=PresentationTurn.Status.CLOSED, closed_at=closed_at)
    if not won:
        return False
    turn.status = PresentationTurn.Status.CLOSED
    turn.closed_at = closed_at
    broadcast_session_event(turn.assessment_session, 'turn.closed', {'group_id': turn.group_id})
    return True


def auto_advance(turn):
    """The timer for `turn` was scheduled to run out now: close it and, if
    another group hasn't presented yet, open it automatically with the same
    duration — so a lecturer who sets a 90-second limit gets a fully
    hands-off session instead of having to re-click for every single group.

    Silently does nothing if:
    - `turn` isn't the active turn any more (the lecturer closed it, or
      activated something else, before the timer fired) — the manual
      action already superseded it;
    - the turn has no `voting_ends_at` any more (voting was paused — see
      `pause_timer` — which clears it precisely so a scheduled call that
      lands during the pause has nothing to act on);
    - `voting_ends_at` is still in the future (the lecturer extended the
      timer with `adjust_timer` after this call was already scheduled for
      the old deadline — the *new* deadline has its own freshly-scheduled
      call, so this stale one just needs to back off, not re-fire early).
    """
    turn.refresh_from_db()
    if turn.status != PresentationTurn.Status.ACTIVE:
        return
    if turn.voting_ends_at is None or timezone.now() < turn.voting_ends_at:
        return

    session = turn.assessment_session
    group = turn.group
    if not close_turn(turn):
        # Lost the race to close this turn — whoever won is already
        # advancing (or already has), so doing it again here would create
        # a duplicate turn and skip a group. See `close_turn`.
        return

    next_group = session.next_group_after(group)
    _advance_to(session, next_group)


def ensure_turn_progressed(session):
    """Defensive, idempotent safety net: if the session's active turn's
    deadline has already passed, advance it right now.

    Auto-advance is normally driven by a Celery task scheduled for the exact
    deadline (`activate_turn`/`adjust_timer`/`resume_timer` above). If no
    worker is running to pick that task up — or a broker message is lost —
    the schedule alone is not enough, and the session would otherwise sit
    frozen on an expired timer until someone manually intervenes. Calling
    this from any request that touches the session (a student's page load,
    the admin control panel, or a client's own countdown reaching zero)
    means the transition happens the moment *anyone* looks, regardless of
    whether Celery is up. It's a plain read-then-maybe-write, safe to call
    as often as needed — `auto_advance` itself already no-ops unless the
    deadline has genuinely passed.
    """
    turn = session.active_turn()
    if turn is not None:
        if (turn.presentation_ends_at and turn.voting_opened_at is None
                and turn.opened_at and timezone.now() >= turn.opened_at +
                (turn.presentation_ends_at - turn.opened_at) * .4):
            open_voting(turn)
        if turn.presentation_ends_at and timezone.now() >= turn.presentation_ends_at:
            turn.voting_ends_at = timezone.now()
            turn.save(update_fields=['voting_ends_at'])
            auto_advance(turn)
    elif session.pending_next_group_id is not None:
        activate_pending_transition(session)


def adjust_timer(turn, delta_seconds):
    """Extend or shrink the running countdown on `turn` by `delta_seconds`
    (negative to shrink) — the lecturer's "+30s" / "-30s" controls.

    Reducing it to zero or below closes the turn immediately (and advances,
    same as the timer legitimately running out) rather than leaving a
    negative or already-past deadline sitting there. Raises ValueError if
    this turn has no timer running at all (nothing to adjust) — the view
    turns that into a user-facing message rather than a silent no-op.
    """
    if turn.status != PresentationTurn.Status.ACTIVE or turn.voting_ends_at is None:
        raise ValueError('This turn has no running timer to adjust.')

    new_end = turn.voting_ends_at + timedelta(seconds=delta_seconds)
    if new_end <= timezone.now():
        session = turn.assessment_session
        group = turn.group
        if close_turn(turn):
            next_group = session.next_group_after(group)
            _advance_to(session, next_group)
        return None

    turn.voting_ends_at = new_end
    turn.save(update_fields=['voting_ends_at'])

    from .tasks import auto_advance_turn
    auto_advance_turn.apply_async((turn.pk,), eta=new_end)

    broadcast_session_event(turn.assessment_session, 'turn.timer_adjusted', {
        'group_id': turn.group_id, 'voting_ends_at': new_end.isoformat(),
    })
    return turn


def pause_timer(session):
    """Freeze the active turn's countdown for as long as voting stays
    paused — without this, a lecturer pausing voting mid-vote would find
    the timer had kept running underneath and auto-advanced anyway."""
    turn = session.active_turn()
    if turn is None or turn.voting_ends_at is None:
        return
    remaining = max(int((turn.voting_ends_at - timezone.now()).total_seconds()), 0)
    turn.paused_remaining_seconds = remaining
    turn.voting_ends_at = None
    turn.save(update_fields=['paused_remaining_seconds', 'voting_ends_at'])


def resume_timer(session):
    """Restore the active turn's countdown to exactly where it was when
    voting was paused, and reschedule the auto-advance for that new time."""
    turn = session.active_turn()
    if turn is None or turn.paused_remaining_seconds is None:
        return
    new_end = timezone.now() + timedelta(seconds=turn.paused_remaining_seconds)
    turn.voting_ends_at = new_end
    turn.paused_remaining_seconds = None
    turn.save(update_fields=['voting_ends_at', 'paused_remaining_seconds'])

    from .tasks import auto_advance_turn
    auto_advance_turn.apply_async((turn.pk,), eta=new_end)
