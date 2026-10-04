"""The live-update WebSocket, driven through the real ASGI application.

Students never log in; the session's uuid is the whole credential. These prove
the wiring around that: the route reaches the consumer, a connection is accepted
with no login, and what a consumer joins is what the broadcaster sends to.

A TransactionTestCase because the consumer touches the channel layer from
another thread.
"""
import uuid

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase

from accounts.testing import make_org
from assessments.consumers import AssessmentSessionConsumer
from assessments.models import AssessmentSession
from assessments.realtime import broadcast_session_event
from config.asgi import application

QUIET = 0.2


class AssessmentSocketTests(TransactionTestCase):

    def setUp(self):
        org = make_org('Acme')
        self.session = AssessmentSession.objects.create(organization_id=org.pk, name='Demo day')
        self.other = AssessmentSession.objects.create(organization_id=org.pk, name='Other day')

    def path(self, session):
        return f'/ws/assessments/{session.uuid}/'

    async def open(self, path):
        communicator = WebsocketCommunicator(application, path)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def assert_silent(self, communicator):
        self.assertTrue(await communicator.receive_nothing(timeout=QUIET))

    async def test_it_accepts_a_connection_with_no_login(self):
        communicator, connected = await self.open(self.path(self.session))
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_events_reach_only_the_session_they_belong_to(self):
        mine, _ = await self.open(self.path(self.session))
        theirs, _ = await self.open(self.path(self.other))

        await sync_to_async(broadcast_session_event)(self.session, 'turn.started', {'n': 1})

        self.assertEqual(await mine.receive_json_from(),
                         {'type': 'turn.started', 'data': {'n': 1}})
        await self.assert_silent(theirs)
        await mine.disconnect()
        await theirs.disconnect()

    async def test_a_guessed_uuid_hears_nothing_from_a_real_session(self):
        guesser, connected = await self.open(f'/ws/assessments/{uuid.uuid4()}/')
        self.assertTrue(connected)
        await sync_to_async(broadcast_session_event)(self.session, 'turn.started')
        await self.assert_silent(guesser)
        await guesser.disconnect()

    async def test_a_disconnected_socket_stops_receiving(self):
        communicator, _ = await self.open(self.path(self.session))
        await communicator.disconnect()
        # Sending to a group nobody is in must not raise.
        await sync_to_async(broadcast_session_event)(self.session, 'turn.started')

    async def test_disconnecting_a_consumer_that_never_joined_is_harmless(self):
        self.assertIsNone(await AssessmentSessionConsumer().disconnect(1000))

    async def test_any_other_path_is_not_a_socket(self):
        communicator = WebsocketCommunicator(application, '/ws/anything-else/')
        with self.assertRaises(ValueError):
            await communicator.connect()
