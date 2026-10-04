"""Live updates for a presentation session — open to unauthenticated
connections, since students never log in. Scoped by the session's uuid
(unguessable) rather than by user, which is the anonymity/access model this
whole feature already relies on for the student side.
"""

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class AssessmentSessionConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.session_uuid = self.scope['url_route']['kwargs']['session_uuid']
        self.group_name = f'assessment_session_{self.session_uuid}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def assessment_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': event['data']})
