"""Public and staff WebSocket confidentiality and session scoping tests."""
import uuid

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client, TransactionTestCase

from accounts.access import ACTIVE_KEY, ORGS_KEY
from accounts.testing import make_org, make_user
from assessments.consumers import AssessmentSessionConsumer
from assessments.models import AssessmentSession
from assessments.realtime import broadcast_session_event
from config.asgi import application

QUIET = 0.2
OWN_ORIGIN = [(b'origin', b'http://localhost')]


class AssessmentSocketTests(TransactionTestCase):

    def setUp(self):
        org = make_org('Acme')
        self.owner = make_user('socket-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=org.pk, name='Demo day', created_by=self.owner)
        self.other = AssessmentSession.objects.create(organization_id=org.pk, name='Other day')
        self.org_id = org.pk

    def path(self, session):
        return f'/ws/assessments/{session.uuid}/'

    def staff_path(self, session):
        return f'/ws/assessments/{session.uuid}/staff/'

    def staff_headers(self, org_id=None, user=None, oversee=False, organizations=None):
        client = Client()
        client.force_login(user or self.owner)
        state = client.session
        state[ORGS_KEY] = organizations if organizations is not None else [
            {'id': self.org_id if org_id is None else org_id,
             'public': False, 'can_oversee': oversee}]
        state[ACTIVE_KEY] = self.org_id if org_id is None else org_id
        state.save()
        cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        return OWN_ORIGIN + [(b'cookie', f'{settings.SESSION_COOKIE_NAME}={cookie}'.encode())]

    async def open(self, path, headers=OWN_ORIGIN):
        communicator = WebsocketCommunicator(application, path, headers=headers)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def assert_silent(self, communicator):
        self.assertTrue(await communicator.receive_nothing(timeout=QUIET))

    async def test_participant_socket_accepts_no_login(self):
        communicator, connected = await self.open(self.path(self.session))
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_non_sensitive_events_reach_only_their_public_session(self):
        mine, _ = await self.open(self.path(self.session))
        theirs, _ = await self.open(self.path(self.other))

        await sync_to_async(broadcast_session_event)(self.session, 'turn.started', {'n': 1})

        self.assertEqual(await mine.receive_json_from(), {'type': 'turn.started', 'data': {'n': 1}})
        await self.assert_silent(theirs)
        await mine.disconnect()
        await theirs.disconnect()

    async def test_public_socket_receives_no_evaluator_or_group_ids(self):
        public, connected = await self.open(self.path(self.session))
        self.assertTrue(connected)

        private_payload = {'student_id': 42, 'group_id': 7, 'answered': 3, 'total': 4}
        for event_type in ('grading.progress', 'evaluation.submitted'):
            await sync_to_async(broadcast_session_event)(self.session, event_type, private_payload)
            self.assertEqual(await public.receive_json_from(), {'type': 'activity.updated', 'data': {}})
        await public.disconnect()

    async def test_staff_socket_receives_detail_while_public_socket_gets_generic_signal(self):
        public, public_connected = await self.open(self.path(self.session))
        staff, staff_connected = await self.open(
            self.staff_path(self.session), headers=await sync_to_async(self.staff_headers)())
        self.assertTrue(public_connected)
        self.assertTrue(staff_connected)

        payload = {'student_id': 42, 'group_id': 7, 'answered': 3, 'total': 4}
        for event_type in ('grading.progress', 'evaluation.submitted'):
            await sync_to_async(broadcast_session_event)(self.session, event_type, payload)
            self.assertEqual(await public.receive_json_from(), {'type': 'activity.updated', 'data': {}})
            self.assertEqual(await staff.receive_json_from(), {'type': event_type, 'data': payload})
        await public.disconnect()
        await staff.disconnect()

    async def test_staff_socket_denies_anonymous_users_and_wrong_active_organization(self):
        anonymous, anonymous_connected = await self.open(self.staff_path(self.session))
        self.assertFalse(anonymous_connected)
        wrong_org, wrong_org_connected = await self.open(
            self.staff_path(self.session),
            headers=await sync_to_async(self.staff_headers)(org_id=self.org_id + 100))
        self.assertFalse(wrong_org_connected)
        await anonymous.disconnect()
        await wrong_org.disconnect()

    async def test_staff_socket_denies_users_with_no_organization_claim(self):
        headers = await sync_to_async(self.staff_headers)(organizations=[])
        communicator, connected = await self.open(self.staff_path(self.session), headers=headers)
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_staff_socket_denies_non_owner_without_oversight(self):
        another_user = await sync_to_async(make_user)('other-socket-user')
        headers = await sync_to_async(self.staff_headers)(user=another_user)
        communicator, connected = await self.open(self.staff_path(self.session), headers=headers)
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_staff_socket_denies_sessions_from_another_service(self):
        panel = await sync_to_async(AssessmentSession.objects.create)(
            organization_id=self.org_id, name='Panel session', created_by=self.owner, service='panel')
        headers = await sync_to_async(self.staff_headers)()
        communicator, connected = await self.open(self.staff_path(panel), headers=headers)
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_organization_overseer_can_join_the_staff_socket(self):
        other_owner = await sync_to_async(make_user)('session-owner')
        await sync_to_async(AssessmentSession.objects.filter(pk=self.session.pk).update)(
            created_by=other_owner)
        overseer = await sync_to_async(make_user)('socket-overseer')
        headers = await sync_to_async(self.staff_headers)(user=overseer, oversee=True)
        communicator, connected = await self.open(self.staff_path(self.session), headers=headers)
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_a_guessed_uuid_hears_nothing_from_a_real_session(self):
        guesser, connected = await self.open(f'/ws/assessments/{uuid.uuid4()}/')
        self.assertTrue(connected)
        await sync_to_async(broadcast_session_event)(self.session, 'turn.started')
        await self.assert_silent(guesser)
        await guesser.disconnect()

    async def test_a_disconnected_socket_stops_receiving(self):
        communicator, _ = await self.open(self.path(self.session))
        await communicator.disconnect()
        await sync_to_async(broadcast_session_event)(self.session, 'turn.started')

    async def test_disconnecting_a_consumer_that_never_joined_is_harmless(self):
        self.assertIsNone(await AssessmentSessionConsumer().disconnect(1000))

    async def test_any_other_path_is_not_a_socket(self):
        communicator = WebsocketCommunicator(application, '/ws/anything-else/', headers=OWN_ORIGIN)
        with self.assertRaises(ValueError):
            await communicator.connect()

    async def test_a_page_from_another_site_cannot_open_the_socket(self):
        communicator = WebsocketCommunicator(
            application, self.path(self.session), headers=[(b'origin', b'https://evil.example')])
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_a_connection_that_names_no_origin_is_refused(self):
        communicator = WebsocketCommunicator(application, self.path(self.session))
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
