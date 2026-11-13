"""Best-effort public updates for Competition's revealed leaderboard."""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction


logger = logging.getLogger(__name__)


def broadcast_competition_leaderboard_state(public_id, available):
    """Send a minimal availability signal; clients fetch the public projection."""
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f'competition_leaderboard_{public_id}',
            {'type': 'competition.leaderboard_state', 'available': bool(available)},
        )
    except Exception:
        logger.warning('Competition leaderboard update could not be broadcast.')


def schedule_competition_leaderboard_update(public_id, available):
    """Broadcast after the publishing transaction commits."""
    public_id = str(public_id)
    transaction.on_commit(
        lambda: broadcast_competition_leaderboard_state(public_id, available))


def broadcast_panel_hearing_event(hearing, event_type, data=None):
    """Broadcast an event across panel staff, examiner, and display channel groups."""
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    payload = {
        'type': 'panel_update',
        'event_type': event_type,
        'data': data or {},
    }
    hearing_id = hearing.pk if hasattr(hearing, 'pk') else hearing
    try:
        async_to_sync(channel_layer.group_send)(
            f'panel_hearing_staff_{hearing_id}', payload
        )
        async_to_sync(channel_layer.group_send)(
            f'panel_hearing_examiners_{hearing_id}', payload
        )
        async_to_sync(channel_layer.group_send)(
            f'panel_hearing_display_{hearing_id}', payload
        )
    except Exception:
        logger.warning('Panel hearing event could not be broadcast: %s', event_type)


SHOWCASE_GROUPS = ('showcase_event_staff_{}', 'showcase_event_judges_{}', 'showcase_event_display_{}')


def broadcast_showcase_event(showcase_event, event_type, data=None):
    """Tell the console, judge workspaces and public screen that something changed.

    Payloads carry no scores or identities; each client refetches what it may see.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    payload = {
        'type': 'showcase_update',
        'event_type': event_type,
        'data': data or {},
    }
    event_id = showcase_event.pk if hasattr(showcase_event, 'pk') else showcase_event
    for group in SHOWCASE_GROUPS:
        try:
            async_to_sync(channel_layer.group_send)(group.format(event_id), payload)
        except Exception:
            logger.warning('Showcase event could not be broadcast: %s', event_type)



def broadcast_training_session_event(series_session_id, event_type):
    """Tell the workshop console and projector to refetch; the payload carries no data."""
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f'training_session_live_{series_session_id}',
            {'type': 'training_update', 'event_type': event_type},
        )
    except Exception:
        logger.warning('Training session event could not be broadcast: %s', event_type)
