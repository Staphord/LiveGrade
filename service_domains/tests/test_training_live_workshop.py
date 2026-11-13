"""Team workshop console: server-owned clock, live socket, one tab list."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.access import ACTIVE_KEY, ORGS_KEY
from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession
from config.asgi import application
from service_domains.models import TrainingParticipant, TrainingSeries, TrainingSeriesSession
from service_domains.operations import training_drill_timer
from service_domains.realtime import broadcast_training_session_event

OWN_ORIGIN = [(b'origin', b'http://localhost')]
SERVICES = ['classroom', 'panel', 'polls', 'team', 'showcase', 'competition']


class Fixture:
    def build(self, status='live'):
        suffix = uuid.uuid4().hex[:8]
        self.org = make_org(f'Workshop {suffix}')
        self.owner = make_user(f'owner-{suffix}')
        self.series = TrainingSeries.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Crisis series',
            feedback_minimum_responses=1)
        TrainingParticipant.objects.create(
            organization_id=self.org.id, created_by=self.owner, series=self.series,
            display_name='Alice', group_name='Pod A', order=1)
        self.assessment = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='team',
            name='Drill one', status=status)
        self.link = TrainingSeriesSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, series=self.series,
            assessment_session=self.assessment, sequence=1)

    def url(self, name, *args):
        return reverse(name, args=args)

    def live_url(self):
        return self.url('training_session_live', self.series.pk, self.link.pk)

    def set_status(self, status):
        self.assessment.status = status
        self.assessment.save(update_fields=('status',))


class DrillTimerTests(TestCase):
    def test_running_timer_gives_the_server_deadline(self):
        now = timezone.now()
        snapshot = {'phase_duration_seconds': 900, 'phase_started_at': (now - timedelta(seconds=100)).isoformat()}
        timer = training_drill_timer(snapshot, now)
        self.assertEqual(timer['seconds'], 800)
        self.assertEqual(timer['display'], '13:20')
        self.assertEqual(timer['ends_at_ms'], timer['server_now_ms'] + 800_000)
        self.assertEqual(timer['percent'], 89)
        self.assertFalse(timer['paused'] or timer['expired'] or timer['urgent'])

    def test_paused_timer_has_no_deadline(self):
        timer = training_drill_timer({'timer_paused': True, 'drill_seconds_remaining': 45,
                                      'phase_duration_seconds': 300})
        self.assertEqual((timer['seconds'], timer['ends_at_ms'], timer['paused']), (45, None, True))
        self.assertTrue(timer['urgent'])
        self.assertFalse(timer['expired'])

    def test_finished_timer_is_expired_and_extended_duration_keeps_percent_valid(self):
        now = timezone.now()
        expired = training_drill_timer({'phase_duration_seconds': 60,
                                        'phase_started_at': (now - timedelta(seconds=90)).isoformat()}, now)
        self.assertTrue(expired['expired'])
        self.assertEqual((expired['seconds'], expired['display'], expired['percent']), (0, '0:00', 0))
        self.assertEqual(training_drill_timer({'drill_seconds_remaining': 500,
                                               'phase_duration_seconds': 100, 'timer_paused': True})['percent'], 100)


@override_settings(LIVEGRADE_LIVE_SERVICES=SERVICES)
class WorkspacePageTests(Fixture, TestCase):
    def setUp(self):
        self.build()
        sign_in(self.client, self.owner, self.org)

    def test_live_page_has_projector_button_not_tab_and_one_tab_list(self):
        html = self.client.get(self.live_url()).content.decode()
        projector = self.url('training_session_projector', self.series.pk, self.link.pk)
        self.assertEqual(html.count(f'href="{projector}"'), 1)
        self.assertIn('Open projector screen', html)
        self.assertEqual(html.count('aria-label="Workshop workspace"'), 1)
        self.assertIn('/ws/training/session/', html)
        self.assertIn('id="training-drill-ring"', html)
        self.assertNotIn('Live Attendee Comprehension Pulse', html)
        self.assertNotIn('—', html)

    def test_every_workspace_page_shares_the_same_tabs(self):
        outcomes = self.client.get(self.url('training_session_competencies', self.assessment.pk))
        trends = self.client.get(self.url('training_series_trends', self.series.pk) + f'?session={self.link.pk}')
        setup = self.client.get(self.url('training_series_manage', self.series.pk)
                                + f'?from=live&session_id={self.link.pk}')
        for page, active in ((outcomes, 'outcomes'), (trends, 'trends'), (setup, 'setup')):
            html = page.content.decode()
            self.assertEqual(html.count('aria-label="Workshop workspace"'), 1, active)
            self.assertEqual(html.count('aria-current="page"'), 1 + html.count('aria-current="page"') - 1)
            for label in ('Live workshop', 'Outcomes &amp; certificates', 'Competency trends'):
                self.assertIn(label, html)
            self.assertIn(self.live_url(), html)

    def test_trends_without_or_with_unusable_session_keep_the_plain_back_link(self):
        base = self.url('training_series_trends', self.series.pk)
        for query in ('', '?session=abc', '?session=999999'):
            page = self.client.get(base + query)
            self.assertIsNone(page.context['from_session'])
            self.assertNotContains(page, 'aria-label="Workshop workspace"')
        other = TrainingSeries.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Another')
        self.assertIsNone(self.client.get(
            self.url('training_series_trends', other.pk) + f'?session={self.link.pk}').context['from_session'])

    def test_setup_page_without_live_origin_keeps_back_link(self):
        page = self.client.get(self.url('training_series_manage', self.series.pk))
        self.assertIsNone(page.context['workspace_link'])
        page = self.client.get(self.url('training_series_manage', self.series.pk) + '?from=live&session_id=zzz')
        self.assertEqual(page.context['workspace_link'], self.link)

    def test_draft_hides_outcomes_and_closed_hides_setup(self):
        self.set_status('draft')
        html = self.client.get(self.live_url()).content.decode()
        self.assertNotIn('Outcomes &amp; certificates', html)
        self.assertIn('Edit setup', html)
        self.set_status('closed')
        html = self.client.get(self.live_url()).content.decode()
        self.assertIn('Outcomes &amp; certificates', html)
        self.assertNotIn('Edit setup', html)
        self.assertIn('QR code for the feedback link', html)
        self.assertIn('training-feedback-url', html)

    def test_closed_page_without_a_feedback_threshold_has_no_link(self):
        self.series.feedback_minimum_responses = None
        self.series.save()
        self.set_status('closed')
        self.assertNotIn('training-feedback-url', self.client.get(self.live_url()).content.decode())

    def test_projector_has_no_dead_feedback_qr_and_runs_on_the_server_clock(self):
        html = self.client.get(self.url('training_session_projector', self.series.pk, self.link.pk)).content.decode()
        self.assertNotIn('proj-qr-img', html)
        self.assertNotIn('Scan with your mobile', html)
        self.assertIn('"ends_at_ms"', html)
        self.assertIn('/ws/training/session/', html)
        self.assertIn('id="proj-phase-title"', html)

    def test_state_payload_carries_the_clock_and_fragments_but_no_feedback_panel(self):
        data = self.client.get(self.url('training_session_live_state', self.series.pk, self.link.pk)).json()
        self.assertEqual(data['drill_timer']['seconds'], data['drill_seconds_remaining'])
        self.assertIn('server_now_ms', data['drill_timer'])
        self.assertNotIn('feedback_html', data)
        self.assertIn('Time is up' if data['drill_timer']['expired'] else 'Running', data['hero_html'])

    def test_finishing_sends_the_organizer_to_outcomes(self):
        response = self.client.post(self.url('training_session_live_action', self.series.pk, self.link.pk),
                                    {'action': 'conclude_session'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.json()['redirect'],
                         self.url('training_session_competencies', self.assessment.pk))

    def test_actions_broadcast_to_open_screens(self):
        action = self.url('training_session_live_action', self.series.pk, self.link.pk)
        with patch('service_domains.views.broadcast_training_session_event') as send:
            self.client.post(action, {'action': 'set_drill_phase', 'phase': 'simulation'})
            self.client.post(action, {'action': 'conclude_session'})
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         ['training.set_drill_phase', 'training.conclude_session'])
        self.set_status('draft')
        with patch('service_domains.views.broadcast_training_session_event') as send, \
                patch.object(AssessmentSession, 'can_go_live', return_value=True):
            self.client.post(action, {'action': 'start_session'})
        send.assert_called_once()


@override_settings(LIVEGRADE_LIVE_SERVICES=SERVICES)
class WorkshopSocketTests(Fixture, TransactionTestCase):
    def setUp(self):
        self.build()

    def headers(self, user=None, orgs=None):
        client = Client()
        client.force_login(user or self.owner)
        state = client.session
        state[ORGS_KEY] = orgs if orgs is not None else [{'id': self.org.pk, 'public': False, 'can_oversee': False}]
        state[ACTIVE_KEY] = self.org.pk
        state.save()
        cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        return OWN_ORIGIN + [(b'cookie', f'{settings.SESSION_COOKIE_NAME}={cookie}'.encode())]

    async def connect(self, headers, session_id=None):
        communicator = WebsocketCommunicator(
            application, f'/ws/training/session/{session_id or self.link.pk}/live/', headers=headers)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def test_owner_socket_receives_every_broadcast(self):
        headers = await sync_to_async(self.headers)()
        socket, ok = await self.connect(headers)
        self.assertTrue(ok)
        await sync_to_async(broadcast_training_session_event)(self.link.pk, 'training.set_drill_phase')
        self.assertEqual(await socket.receive_json_from(), {'type': 'training.set_drill_phase'})
        await socket.disconnect()

    async def test_socket_refuses_strangers_other_owners_and_unknown_demos(self):
        anonymous, ok = await self.connect(OWN_ORIGIN)
        self.assertFalse(ok)
        await anonymous.disconnect()
        stranger = await sync_to_async(make_user)('someone-else')
        other, ok = await self.connect(await sync_to_async(self.headers)(stranger))
        self.assertFalse(ok)
        await other.disconnect()
        elsewhere, ok = await self.connect(await sync_to_async(self.headers)(orgs=[{'id': 999999}]))
        self.assertFalse(ok)
        await elsewhere.disconnect()
        bad_orgs, ok = await self.connect(await sync_to_async(self.headers)(orgs='nope'))
        self.assertFalse(ok)
        await bad_orgs.disconnect()
        missing, ok = await self.connect(await sync_to_async(self.headers)(), session_id=999999)
        self.assertFalse(ok)
        await missing.disconnect()

    async def test_unreleased_service_is_refused(self):
        headers = await sync_to_async(self.headers)()
        with patch('service_domains.consumers.is_released', return_value=False):
            socket, ok = await self.connect(headers)
            self.assertFalse(ok)
            await socket.disconnect()

    async def test_broadcast_survives_a_missing_or_broken_channel_layer(self):
        with patch('service_domains.realtime.get_channel_layer', return_value=None):
            await sync_to_async(broadcast_training_session_event)(self.link.pk, 'x')
        with patch('service_domains.realtime.async_to_sync', side_effect=RuntimeError('down')):
            await sync_to_async(broadcast_training_session_event)(self.link.pk, 'x')
