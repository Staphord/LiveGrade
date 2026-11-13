"""Websocket doors of every live service: who may connect, and what they are sent."""
from datetime import timedelta
from uuid import uuid4

from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.access import ACTIVE_KEY, ORGS_KEY
from accounts.testing import make_org, make_user
from config.asgi import application
from service_domains.models import (
    CoAssessmentSession, Competition, PanelHearing, ShowcaseEvent)
from service_domains.operations import (
    issue_panel_invitation, issue_panel_live_display_link)

ORIGIN = [(b'origin', b'http://localhost')]
ALL = ['classroom', 'panel', 'showcase', 'competition', 'coassessment', 'team']


@override_settings(LIVEGRADE_LIVE_SERVICES=ALL)
class SocketDoorTests(TransactionTestCase):
    def setUp(self):
        self.org = make_org('Socket org')
        self.owner = make_user('socket-owner')
        self.colleague = make_user('socket-colleague')

    def headers(self, user, organizations):
        client = Client()
        client.force_login(user)
        state = client.session
        state[ORGS_KEY] = organizations
        state[ACTIVE_KEY] = self.org.pk
        state.save()
        cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        return ORIGIN + [(b'cookie', f'{settings.SESSION_COOKIE_NAME}={cookie}'.encode())]

    async def connect(self, path, headers=ORIGIN):
        communicator = WebsocketCommunicator(application, path, headers=headers)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def refused(self, path, headers=ORIGIN):
        communicator, connected = await self.connect(path, headers)
        self.assertFalse(connected)
        await communicator.disconnect()

    async def staff_headers(self, user, **org):
        entry = {'id': self.org.pk, 'public': False, 'can_oversee': False, **org}
        return await sync_to_async(self.headers)(user, [entry])

    # --- competition leaderboard -------------------------------------------------

    def competition(self, **fields):
        return Competition.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Cup', **fields)

    async def test_leaderboard_is_closed_until_revealed(self):
        draft = await sync_to_async(self.competition)()
        await self.refused(f'/ws/competition/{draft.public_id}/leaderboard/')
        await self.refused(f'/ws/competition/{uuid4()}/leaderboard/')

    async def test_revealed_leaderboard_reports_availability_and_updates(self):
        revealed = await sync_to_async(self.competition)(
            status=Competition.Status.REVEALED, revealed_at=timezone.now(), public_leaderboard_enabled=True)
        communicator, connected = await self.connect(f'/ws/competition/{revealed.public_id}/leaderboard/')
        self.assertTrue(connected)
        self.assertEqual(await communicator.receive_json_from(), {'type': 'leaderboard.state', 'available': True})
        await get_channel_layer().group_send(
            f'competition_leaderboard_{revealed.public_id}',
            {'type': 'competition.leaderboard_state', 'available': False})
        self.assertEqual(await communicator.receive_json_from(), {'type': 'leaderboard.state', 'available': False})
        await communicator.disconnect()

    @override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
    async def test_leaderboard_is_closed_while_the_service_is_not_released(self):
        await self.refused(f'/ws/competition/{uuid4()}/leaderboard/')

    # --- panel ---------------------------------------------------------------------

    def hearing(self, **fields):
        return PanelHearing.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Viva', **fields)

    async def test_panel_staff_door_checks_the_organization_and_oversight(self):
        hearing = await sync_to_async(self.hearing)(status=PanelHearing.Status.OPEN)
        path = f'/ws/panel/hearing/{hearing.pk}/live/'
        await self.refused('/ws/panel/hearing/999999/live/', await self.staff_headers(self.owner))
        await self.refused(path, await sync_to_async(self.headers)(self.colleague, []))
        await self.refused(path, await sync_to_async(self.headers)(self.colleague, ['not a dict']))
        await self.refused(path, await sync_to_async(self.headers)(
            self.colleague, [{'id': self.org.pk + 99, 'can_oversee': True}]))
        await self.refused(path, await self.staff_headers(self.colleague))
        await self.refused(path, await self.staff_headers(self.colleague, can_oversee=True, public=True))
        communicator, connected = await self.connect(path, await self.staff_headers(self.colleague, can_oversee=True))
        self.assertTrue(connected)
        await communicator.disconnect()

    @override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
    async def test_panel_doors_are_closed_while_the_service_is_not_released(self):
        await self.refused('/ws/panel/hearing/1/live/')
        await self.refused('/ws/panel/examiner/token/')
        await self.refused('/ws/panel/display/token/')

    async def test_panel_examiner_door_refuses_a_closed_hearing(self):
        hearing = await sync_to_async(self.hearing)(status=PanelHearing.Status.OPEN)
        _, token = await sync_to_async(issue_panel_invitation)(
            hearing, 'examiner@example.test', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        await sync_to_async(PanelHearing.objects.filter(pk=hearing.pk).update)(status=PanelHearing.Status.SIGNED_OFF)
        await self.refused(f'/ws/panel/examiner/{token}/')

    async def test_panel_display_door_refuses_revoked_links_and_forwards_updates(self):
        hearing = await sync_to_async(self.hearing)(status=PanelHearing.Status.OPEN)
        link, token = await sync_to_async(issue_panel_live_display_link)(
            hearing, organization=self.org.as_claim(), user=self.owner)
        communicator, connected = await self.connect(f'/ws/panel/display/{token}/')
        self.assertTrue(connected)
        await get_channel_layer().group_send(
            f'panel_hearing_display_{hearing.pk}',
            {'type': 'panel_update', 'event_type': 'phase.changed', 'data': {'phase': 'q'}})
        self.assertEqual(await communicator.receive_json_from(), {'type': 'phase.changed', 'data': {'phase': 'q'}})
        await communicator.disconnect()
        link.revoked_at = timezone.now()
        await sync_to_async(link.save)()
        await self.refused(f'/ws/panel/display/{token}/')
        await self.refused('/ws/panel/display/unknown-token/')

    # --- showcase --------------------------------------------------------------------

    def event(self):
        return ShowcaseEvent.objects.create(organization_id=self.org.pk, created_by=self.owner, name='Expo')

    async def test_showcase_staff_door_checks_the_owner_and_oversight(self):
        event = await sync_to_async(self.event)()
        path = f'/ws/showcase/event/{event.pk}/live/'
        await self.refused(path)
        await self.refused('/ws/showcase/event/999999/live/', await self.staff_headers(self.owner))
        await self.refused(path, await sync_to_async(self.headers)(self.colleague, []))
        await self.refused(path, await sync_to_async(self.headers)(self.colleague, ['not a dict']))
        await self.refused(path, await sync_to_async(self.headers)(
            self.colleague, [{'id': self.org.pk + 99, 'can_oversee': True}]))
        await self.refused(path, await self.staff_headers(self.colleague))
        await self.refused(path, await self.staff_headers(self.colleague, can_oversee=True, public=True))
        for user, org in ((self.owner, {}), (self.colleague, {'can_oversee': True})):
            communicator, connected = await self.connect(path, await self.staff_headers(user, **org))
            self.assertTrue(connected)
            await get_channel_layer().group_send(
                f'showcase_event_staff_{event.pk}',
                {'type': 'showcase_update', 'event_type': 'stage.changed', 'data': {'a': 1}})
            self.assertEqual(await communicator.receive_json_from(), {'type': 'stage.changed', 'data': {'a': 1}})
            await communicator.disconnect()

    @override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
    async def test_showcase_doors_are_closed_while_the_service_is_not_released(self):
        await self.refused('/ws/showcase/event/1/live/')
        await self.refused('/ws/showcase/judge/token/')
        await self.refused(f'/ws/showcase/display/{uuid4()}/')

    async def test_showcase_judge_and_display_doors_refuse_unknown_links(self):
        await self.refused('/ws/showcase/judge/unknown-token/')
        await self.refused(f'/ws/showcase/display/{uuid4()}/')

    # --- co-assessment -----------------------------------------------------------------

    async def test_coassessment_socket_sends_state_then_updates(self):
        session = await sync_to_async(CoAssessmentSession.objects.create)(
            organization_id=self.org.pk, created_by=self.owner, name='Defense')
        communicator, connected = await self.connect(f'/ws/coassessment/{session.pk}/live/')
        self.assertTrue(connected)
        self.assertEqual((await communicator.receive_json_from())['type'], 'init')
        await get_channel_layer().group_send(
            f'coassessment_{session.pk}', {'type': 'coassessment_update', 'state': {'n': 1}})
        self.assertEqual(await communicator.receive_json_from(), {'type': 'update', 'state': {'n': 1}})
        await communicator.disconnect()

    async def test_coassessment_socket_for_an_unknown_session_sends_no_state(self):
        communicator, connected = await self.connect('/ws/coassessment/999999/live/')
        self.assertTrue(connected)
        self.assertTrue(await communicator.receive_nothing(timeout=0.2))
        await communicator.disconnect()

    async def test_coassessment_socket_needs_a_session_number(self):
        await self.refused('/ws/coassessment/0/live/')

    @override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
    async def test_coassessment_socket_is_closed_while_the_service_is_not_released(self):
        await self.refused('/ws/coassessment/1/live/')
