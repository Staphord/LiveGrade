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
