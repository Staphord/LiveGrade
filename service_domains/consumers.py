"""WebSocket consumers for real-time service domains updates."""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from accounts.access import ACTIVE_KEY, ORGS_KEY
from portal.services import is_released

from .models import Competition, PanelHearing, ShowcaseEvent, TrainingSeriesSession
from .operations import (
    resolve_panel_invitation, resolve_panel_live_display_link, resolve_showcase_judge_invitation,
)


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


class PanelChamberConsumer(AsyncJsonWebsocketConsumer):
    """Authenticated staff event stream for hearing live chamber console."""

    async def connect(self):
        if not is_released('panel'):
            await self.close(code=4404)
            return
        self.hearing_id = self.scope['url_route']['kwargs']['hearing_id']
        if not await self._may_view_hearing():
            await self.close(code=4403)
            return
        self.group_name = f'panel_hearing_staff_{self.hearing_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def panel_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': event.get('data', {})})

    @database_sync_to_async
    def _may_view_hearing(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            return False
        hearing = PanelHearing.objects.filter(pk=self.hearing_id).only('organization_id', 'created_by_id').first()
        state = self.scope.get('session')
        if hearing is None or state is None:
            return False
        if hearing.created_by_id == user.pk:
            return True
        organizations = state.get(ORGS_KEY) or []
        if not isinstance(organizations, list) or not organizations:
            return False
        active_id = state.get(ACTIVE_KEY)
        organization = next((org for org in organizations if isinstance(org, dict)
                             and org.get('id') == active_id), organizations[0])
        if not isinstance(organization, dict) or organization.get('id') != hearing.organization_id:
            return False
        return organization.get('can_oversee') is True and organization.get('public') is not True


class PanelExaminerConsumer(AsyncJsonWebsocketConsumer):
    """Examiner socket; broadcasts candidate and phase changes to invited examiners."""

    async def connect(self):
        if not is_released('panel'):
            await self.close(code=4404)
            return
        self.token = self.scope['url_route']['kwargs']['token']
        invitation_data = await self._resolve_invitation()
        if invitation_data is None:
            await self.close(code=4404)
            return

        self.hearing_id = invitation_data['hearing_id']
        self.invitation_id = invitation_data['invitation_id']
        self.group_name = f'panel_hearing_examiners_{self.hearing_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({
            'type': 'hearing.state',
            'data': {
                'hearing_id': self.hearing_id,
                'active_candidate_id': invitation_data['active_candidate_id'],
                'active_candidate_name': invitation_data['active_candidate_name'],
                'status': invitation_data['status'],
            }
        })

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def panel_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': event.get('data', {})})

    @database_sync_to_async
    def _resolve_invitation(self):
        invitation = resolve_panel_invitation(self.token)
        if invitation is None:
            return None
        hearing = invitation.hearing
        if hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
            return None
        return {
            'hearing_id': hearing.pk,
            'invitation_id': invitation.pk,
            'status': hearing.status,
            'active_candidate_id': hearing.active_candidate_id,
            'active_candidate_name': (hearing.active_candidate.display_name if hearing.active_candidate else ''),
        }


class PanelDisplayConsumer(AsyncJsonWebsocketConsumer):
    """Waiting room and projector display socket."""

    async def connect(self):
        if not is_released('panel'):
            await self.close(code=4404)
            return
        self.token = self.scope['url_route']['kwargs']['token']
        display_data = await self._resolve_display()
        if display_data is None:
            await self.close(code=4404)
            return

        self.hearing_id = display_data['hearing_id']
        self.group_name = f'panel_hearing_display_{self.hearing_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def panel_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': event.get('data', {})})

    @database_sync_to_async
    def _resolve_display(self):
        link = resolve_panel_live_display_link(self.token)
        if link is None or not link.usable_at():
            return None
        return {'hearing_id': link.hearing_id}


class ShowcaseLiveConsumer(AsyncJsonWebsocketConsumer):
    """Staff socket for showcase live control."""

    async def connect(self):
        if not is_released('showcase'):
            await self.close(code=4404)
            return
        self.event_id = self.scope['url_route']['kwargs']['event_id']
        if not await self._may_view_event():
            await self.close(code=4403)
            return
        self.group_name = f'showcase_event_staff_{self.event_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def showcase_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': event.get('data', {})})

    @database_sync_to_async
    def _may_view_event(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            return False
        event = ShowcaseEvent.objects.filter(pk=self.event_id).only('organization_id', 'created_by_id').first()
        state = self.scope.get('session')
        if event is None or state is None:
            return False
        if event.created_by_id == user.pk:
            return True
        organizations = state.get(ORGS_KEY) or []
        if not isinstance(organizations, list) or not organizations:
            return False
        active_id = state.get(ACTIVE_KEY)
        organization = next((org for org in organizations if isinstance(org, dict)
                             and org.get('id') == active_id), organizations[0])
        if not isinstance(organization, dict) or organization.get('id') != event.organization_id:
            return False
        return organization.get('can_oversee') is True and organization.get('public') is not True


class ShowcaseJudgeConsumer(AsyncJsonWebsocketConsumer):
    """Judge socket: a nudge to refetch which exhibits are on stage."""

    async def connect(self):
        if not is_released('showcase'):
            await self.close(code=4404)
            return
        event_id = await self._resolve_event_id(self.scope['url_route']['kwargs']['token'])
        if event_id is None:
            await self.close(code=4404)
            return
        self.group_name = f'showcase_event_judges_{event_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def showcase_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': event.get('data', {})})

    @database_sync_to_async
    def _resolve_event_id(self, token):
        invitation = resolve_showcase_judge_invitation(token)
        return invitation.event_id if invitation is not None else None


class ShowcaseDisplayConsumer(AsyncJsonWebsocketConsumer):
    """Public venue screen socket, keyed by the event's public id."""

    async def connect(self):
        if not is_released('showcase'):
            await self.close(code=4404)
            return
        event_id = await self._resolve_event_id(self.scope['url_route']['kwargs']['public_id'])
        if event_id is None:
            await self.close(code=4404)
            return
        self.group_name = f'showcase_event_display_{event_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def showcase_update(self, event):
        await self.send_json({'type': event['event_type'], 'data': {}})

    @database_sync_to_async
    def _resolve_event_id(self, public_id):
        return (ShowcaseEvent.objects.filter(public_id=public_id).values_list('pk', flat=True).first())


class CoAssessmentLiveConsumer(AsyncJsonWebsocketConsumer):
    """Real-time event stream for Co-Assessment stage console, projector, and mobile raters."""

    async def connect(self):
        if not is_released('coassessment'):
            await self.close(code=4404)
            return
        self.session_id = self.scope['url_route']['kwargs'].get('session_id')
        if not self.session_id:
            await self.close(code=4400)
            return
        self.group_name = f'coassessment_{self.session_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        state = await self._get_initial_state()
        if state:
            await self.send_json({'type': 'init', 'state': state})

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def coassessment_update(self, event):
        """Forward state broadcast to connected websocket client."""
        await self.send_json({
            'type': 'update',
            'state': event.get('state', {}),
        })

    @database_sync_to_async
    def _get_initial_state(self):
        from .models import CoAssessmentSession
        from .coassessment_operations import get_coassessment_live_state
        session = CoAssessmentSession.objects.filter(pk=self.session_id).first()
        if session:
            return get_coassessment_live_state(session)
        return None




class TrainingSessionLiveConsumer(AsyncJsonWebsocketConsumer):
    """Owner-only socket for the workshop console and projector of one demo."""

    async def connect(self):
        if not is_released('team'):
            await self.close(code=4404)
            return
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        if not await self._may_view():
            await self.close(code=4403)
            return
        self.group_name = f'training_session_live_{self.session_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def training_update(self, event):
        await self.send_json({'type': event['event_type']})

    @database_sync_to_async
    def _may_view(self):
        user = self.scope.get('user')
        state = self.scope.get('session')
        if not user or not user.is_authenticated or state is None:
            return False
        link = TrainingSeriesSession.objects.filter(pk=self.session_id).only(
            'organization_id', 'created_by_id').first()
        if link is None or link.created_by_id != user.pk:
            return False
        organizations = state.get(ORGS_KEY) or []
        if not isinstance(organizations, list):
            return False
        return any(isinstance(org, dict) and org.get('id') == link.organization_id
                   for org in organizations)
