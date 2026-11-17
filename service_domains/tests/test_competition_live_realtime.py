"""Authentication and event delivery tests for Competition live WebSockets."""

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client, TransactionTestCase, override_settings

from accounts.testing import make_org, make_user, sign_in
from config.asgi import application
from service_domains.models import Competition
from service_domains.realtime import broadcast_competition_live_update


@override_settings(LIVEGRADE_LIVE_SERVICES=['competition'])
class CompetitionLiveSocketTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.org = make_org('Competition socket org')
        self.other_org = make_org('Other competition socket org')
        self.owner = make_user('competition-socket-owner')
        self.other_user = make_user('competition-socket-other')
        self.competition = Competition.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Live Socket Showcase',
            status=Competition.Status.DRAFT,
        )
        self.owner_headers = self._headers(self.owner, self.org)
        self.non_owner_headers = self._headers(self.other_user, self.org)
        self.wrong_org_headers = self._headers(self.owner, self.other_org)

    def _headers(self, user, organization):
        client = Client()
        sign_in(client, user, organization)
        cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        return [
            (b'origin', b'http://localhost'),
            (b'cookie', f'{settings.SESSION_COOKIE_NAME}={cookie}'.encode()),
        ]

    async def _connect(self, user, organization):
        path = f'/ws/competition/{self.competition.pk}/live/'
        headers = (self.owner_headers if user == self.owner and organization == self.org
                   else self.wrong_org_headers if user == self.owner
                   else self.non_owner_headers)
        communicator = WebsocketCommunicator(
            application, path, headers=headers)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def test_owner_receives_minimal_live_update_event(self):
        communicator, connected = await self._connect(self.owner, self.org)
        self.assertTrue(connected)
        self.assertEqual(await communicator.receive_json_from(), {
            'type': 'competition.live.ready',
        })

        await sync_to_async(broadcast_competition_live_update)(self.competition.pk)

        self.assertEqual(await communicator.receive_json_from(), {
            'type': 'competition.live.update',
        })
        await communicator.disconnect()

    async def test_socket_rejects_non_owner_and_wrong_active_organization(self):
        non_owner, allowed = await self._connect(self.other_user, self.org)
        self.assertFalse(allowed)
        await non_owner.disconnect()

        wrong_org, allowed = await self._connect(self.owner, self.other_org)
        self.assertFalse(allowed)
        await wrong_org.disconnect()
