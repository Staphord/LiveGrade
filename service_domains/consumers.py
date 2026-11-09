"""Public, data-minimized Competition leaderboard WebSocket consumer."""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from portal.services import is_released

from .models import Competition


class CompetitionLeaderboardConsumer(AsyncJsonWebsocketConsumer):
    """Allow public listeners only after an owner reveals the final board."""

    async def connect(self):
        if not is_released('competition'):
            await self.close(code=4404)
            return

        self.public_id = self.scope['url_route']['kwargs']['competition_id']
        self.group_name = f'competition_leaderboard_{self.public_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        available = await self._leaderboard_availability()
        if available is None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            await self.close(code=4404)
            return

        await self.accept()
        await self.send_json({'type': 'leaderboard.state', 'available': available})

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def competition_leaderboard_state(self, event):
        await self.send_json({
            'type': 'leaderboard.state',
            'available': bool(event.get('available')),
        })

    @database_sync_to_async
    def _leaderboard_availability(self):
        competition = Competition.objects.filter(
            public_id=self.public_id,
            status=Competition.Status.REVEALED,
        ).only('public_leaderboard_enabled').first()
        if competition is None:
            return None
        return competition.public_leaderboard_enabled
