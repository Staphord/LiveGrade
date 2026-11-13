"""Public room updates and authenticated staff updates for a session."""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from accounts.access import ACTIVE_KEY, ORGS_KEY
from assessments.models import AssessmentSession
from portal.services import is_released


class AssessmentSessionConsumer(AsyncJsonWebsocketConsumer):
    """Unauthenticated participant socket; broadcasts contain no rater identity."""

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


class AssessmentStaffSessionConsumer(AssessmentSessionConsumer):
    """Staff event stream; only the owner or permitted organization overseers may join."""

    async def connect(self):
        self.session_uuid = self.scope['url_route']['kwargs']['session_uuid']
        if not await self._may_view_staff_events():
            await self.close(code=4403)
            return
        self.group_name = f'assessment_owner_session_{self.session_uuid}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    @database_sync_to_async
    def _may_view_staff_events(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            return False
        session = AssessmentSession.objects.filter(
            uuid=self.session_uuid, service__in=('classroom', 'team')).only(
                'organization_id', 'created_by_id', 'service').first()
        state = self.scope.get('session')
        if session is None or state is None or not is_released(session.service):
            return False
        organizations = state.get(ORGS_KEY) or []
        if not isinstance(organizations, list) or not organizations:
            return False
        active_id = state.get(ACTIVE_KEY)
        organization = next((org for org in organizations if isinstance(org, dict)
                             and org.get('id') == active_id), organizations[0])
        if not isinstance(organization, dict) or organization.get('id') != session.organization_id:
            return False
        if session.created_by_id == user.pk:
            return True
        return organization.get('can_oversee') is True and organization.get('public') is not True

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
