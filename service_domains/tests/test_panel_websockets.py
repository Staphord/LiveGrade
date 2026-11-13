"""Real-time WebSocket and throttling isolation tests for Panel and live services."""
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.access import ACTIVE_KEY, ORGS_KEY
from accounts.testing import make_org, make_user, sign_in
from config.asgi import application
from service_domains.models import PanelCandidate, PanelCriterion, PanelHearing
from service_domains.operations import (
    bind_panel_invitation_device,
    issue_panel_device_cookie,
    issue_panel_invitation,
    issue_panel_live_display_link,
    open_panel_hearing,
)
from service_domains.realtime import broadcast_panel_hearing_event

QUIET = 0.2
OWN_ORIGIN = [(b'origin', b'http://localhost')]


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'showcase'])
class PanelWebSocketTests(TransactionTestCase):
    """Verifies WebSocket connectivity, permissions, and broadcast routing."""

    def setUp(self):
        self.org = make_org('Panel Socket Org')
        self.owner = make_user('panel-socket-owner')
        self.other_user = make_user('panel-socket-other')
        self.hearing = PanelHearing.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Oral Examination Socket Test',
            status=PanelHearing.Status.OPEN,
        )
        self.candidate = PanelCandidate.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=self.hearing,
            display_name='Candidate Alice',
            order=1,
        )
        self.criterion = PanelCriterion.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=self.hearing,
            name='Subject Knowledge',
            order=1,
        )
        self.hearing.active_candidate = self.candidate
        self.hearing.save(update_fields=('active_candidate',))

        self.invitation, self.token = issue_panel_invitation(
            self.hearing,
            'examiner@example.test',
            timezone.now() + timedelta(hours=2),
            organization=self.org.as_claim(),
            user=self.owner,
        )
        self.cookie_val = issue_panel_device_cookie(self.invitation)
        self.cookie_name = f'livegrade_panel_examiner_{self.invitation.pk}'
        bind_panel_invitation_device(self.invitation, self.cookie_val)

    def staff_headers(self, user=None):
        client = Client()
        client.force_login(user or self.owner)
        state = client.session
        state[ORGS_KEY] = [{'id': self.org.pk, 'public': False, 'can_oversee': True}]
        state[ACTIVE_KEY] = self.org.pk
        state.save()
        cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        return OWN_ORIGIN + [(b'cookie', f'{settings.SESSION_COOKIE_NAME}={cookie}'.encode())]

    async def open_socket(self, path, headers=OWN_ORIGIN):
        communicator = WebsocketCommunicator(application, path, headers=headers)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def test_examiner_socket_connects_and_receives_current_state(self):
        comm, connected = await self.open_socket(f'/ws/panel/examiner/{self.token}/')
        self.assertTrue(connected)
        initial = await comm.receive_json_from()
        self.assertEqual(initial['type'], 'hearing.state')
        self.assertEqual(initial['data']['active_candidate_id'], self.candidate.pk)
        await comm.disconnect()

    async def test_examiner_socket_rejects_unknown_token(self):
        comm, connected = await self.open_socket('/ws/panel/examiner/nonexistent-token/')
        self.assertFalse(connected)
        await comm.disconnect()

    async def test_staff_socket_authorizes_owner_and_rejects_unauthenticated(self):
        unauth_comm, unauth_connected = await self.open_socket(
            f'/ws/panel/hearing/{self.hearing.pk}/live/',
            headers=OWN_ORIGIN,
        )
        self.assertFalse(unauth_connected)
        await unauth_comm.disconnect()

        headers = await sync_to_async(self.staff_headers)(self.owner)
        auth_comm, auth_connected = await self.open_socket(
            f'/ws/panel/hearing/{self.hearing.pk}/live/',
            headers=headers,
        )
        self.assertTrue(auth_connected)
        await auth_comm.disconnect()

    async def test_display_socket_connects_with_active_display_link(self):
        display_link, display_token = await sync_to_async(issue_panel_live_display_link)(
            self.hearing,
            organization=self.org.as_claim(),
            user=self.owner,
        )
        comm, connected = await self.open_socket(f'/ws/panel/display/{display_token}/')
        self.assertTrue(connected)
        await comm.disconnect()

    async def test_broadcast_event_delivers_to_staff_and_examiners(self):
        headers = await sync_to_async(self.staff_headers)(self.owner)
        staff_comm, staff_ok = await self.open_socket(
            f'/ws/panel/hearing/{self.hearing.pk}/live/',
            headers=headers,
        )
        self.assertTrue(staff_ok)

        examiner_comm, examiner_ok = await self.open_socket(
            f'/ws/panel/examiner/{self.token}/',
        )
        self.assertTrue(examiner_ok)
        await examiner_comm.receive_json_from()  # consume initial hearing.state

        await sync_to_async(broadcast_panel_hearing_event)(
            self.hearing,
            'candidate.changed',
            {'active_candidate_id': self.candidate.pk, 'active_candidate_name': self.candidate.display_name},
        )

        staff_msg = await staff_comm.receive_json_from()
        self.assertEqual(staff_msg['type'], 'candidate.changed')
        self.assertEqual(staff_msg['data']['active_candidate_id'], self.candidate.pk)

        examiner_msg = await examiner_comm.receive_json_from()
        self.assertEqual(examiner_msg['type'], 'candidate.changed')
        self.assertEqual(examiner_msg['data']['active_candidate_id'], self.candidate.pk)

        await staff_comm.disconnect()
        await examiner_comm.disconnect()

    def test_state_queries_use_isolated_throttle_and_do_not_block_score_submission(self):
        sign_in(self.client, self.owner, self.org)
        self.client.cookies[self.cookie_name] = self.cookie_val

        with override_settings(PANEL_EXAMINER_RATE_LIMIT=1, PANEL_EXAMINER_RATE_WINDOW=60):
            examiner_url = reverse('panel_examiner', kwargs={'token': self.token})

            # State polling uses dedicated quota and does not exhaust submission limit
            state_res1 = self.client.get(f'{examiner_url}?state=1')
            self.assertEqual(state_res1.status_code, 200)

            state_res2 = self.client.get(f'{examiner_url}?state=1')
            self.assertEqual(state_res2.status_code, 200)

            # Score submission succeeds because state checks did not deplete primary budget
            post_res = self.client.post(examiner_url, {
                'action': 'score',
                'candidate_id': self.candidate.pk,
                'criterion_id': self.criterion.pk,
                'score': '88',
            })
            self.assertEqual(post_res.status_code, 302)
