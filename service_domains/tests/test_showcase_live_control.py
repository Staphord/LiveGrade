from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    ShowcaseCategory, ShowcaseEvent, ShowcaseEventAudit, ShowcaseExhibit,
    ShowcaseJudgeCriterion, ShowcaseJudgeExhibitAssignment,
    ShowcaseJudgeEvaluation, ShowcaseJudgeInvitation, ShowcaseRating,
)
from service_domains.operations import (
    get_showcase_live_context, issue_showcase_judge_invitation,
    replace_showcase_invited_judge_assignments, replace_showcase_judge_criteria,
    showcase_awards, submit_showcase_judge_evaluation,
)


class ShowcaseLiveControlTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase Live Control Org')
        self.owner = make_user('showcase-live-owner')
        self.other = make_user('showcase-live-other')
        self.event = ShowcaseEvent.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Science Fair 2026',
            status=ShowcaseEvent.Status.OPEN,
            minimum_judges_per_exhibit=1,
        )
        self.category = ShowcaseCategory.objects.create(
            event=self.event,
            organization_id=self.org.id,
            created_by=self.owner,
            name='Engineering',
        )
        self.exhibit1 = ShowcaseExhibit.objects.create(
            event=self.event,
            category=self.category,
            organization_id=self.org.id,
            created_by=self.owner,
            name='Solar Rover',
            order=1,
        )
        self.exhibit2 = ShowcaseExhibit.objects.create(
            event=self.event,
            category=self.category,
            organization_id=self.org.id,
            created_by=self.owner,
            name='Wind Turbine',
            order=2,
        )
        self.client.defaults['REMOTE_ADDR'] = '127.0.0.1'
        sign_in(self.client, self.owner, self.org)

    def test_live_control_renders_with_tabs_and_arithmetic_pending_count(self):
        url = reverse('showcase_live_control', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Live console')
        self.assertContains(response, 'Edit setup')
        self.assertContains(response, 'Results &amp; Awards')
        self.assertContains(response, reverse('showcase_event_results', args=(self.event.pk,)))
        self.assertContains(response, 'Solar Rover')
        self.assertContains(response, 'Wind Turbine')

    def test_live_state_endpoint_returns_json_fragments(self):
        url = reverse('showcase_live_state', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.get(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'open')
        self.assertIn('hero_html', data)
        self.assertIn('stage_html', data)
        self.assertIn('table_html', data)
        self.assertIn('projector_html', data)

    def test_toggle_visitor_voting_pauses_and_resumes_ratings(self):
        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.post(action_url, {'action': 'toggle_visitor_voting'},
                                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertTrue(self.event.workflow_snapshot['floor_state']['visitor_voting_paused'])

        rate_url = reverse('showcase_rate_exhibit', args=(self.exhibit1.public_id,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            rate_response = self.client.get(rate_url)
        self.assertEqual(rate_response.status_code, 200)
        self.assertContains(rate_response, 'temporarily paused by the organizer')

        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.post(action_url, {'action': 'toggle_visitor_voting'},
                                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertFalse(self.event.workflow_snapshot['floor_state']['visitor_voting_paused'])

    def test_toggle_judge_scoring_locks_judge_workspace(self):
        invitation, token = issue_showcase_judge_invitation(
            self.event, 'judge1@example.org', timezone.now() + timedelta(days=2),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(
            self.event, {invitation.pk: [self.exhibit1.pk]},
            organization=self.org.as_claim(), user=self.owner)
        criterion = ShowcaseJudgeCriterion.objects.create(
            event=self.event,
            organization_id=self.org.id,
            created_by=self.owner,
            name='Innovation',
            description='',
            weight=Decimal('100'),
            scale='points',
            max_points=Decimal('100'),
            checklist_items=[],
        )

        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.post(action_url, {'action': 'toggle_judge_scoring'},
                                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)

        judge_url = reverse('showcase_judge_workspace', args=(token,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            post_response = self.client.post(judge_url, {
                'exhibit_id': str(self.exhibit1.public_id),
                f'exhibit_{ShowcaseJudgeExhibitAssignment.objects.get().pk}-criterion_{ShowcaseJudgeCriterion.objects.get().pk}': '90',
            })
        self.assertEqual(post_response.status_code, 400)
        self.assertContains(post_response, 'temporarily locked by the organizer', status_code=400)

    def test_close_ratings_action_transitions_event_cleanly(self):
        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.post(action_url, {'action': 'close_ratings'},
                                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.CLOSED)
        self.assertTrue(ShowcaseEventAudit.objects.filter(
            event=self.event, from_status=ShowcaseEvent.Status.OPEN,
            to_status=ShowcaseEvent.Status.CLOSED).exists())
        self.assertIn('redirect', response.json())

    def test_roving_judge_assignment_via_ajax(self):
        invitation, token = issue_showcase_judge_invitation(
            self.event, 'roving@example.org', timezone.now() + timedelta(days=2),
            organization=self.org.as_claim(), user=self.owner)
        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.post(action_url, {
                'action': 'assign_roving_judge',
                'exhibit_id': str(self.exhibit1.pk),
                'judge_invitation_id': str(invitation.pk),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ShowcaseJudgeExhibitAssignment.objects.filter(
            event=self.event, exhibit=self.exhibit1, invitation=invitation, active=True).exists())

    def test_stage_pitch_runner_and_timer_controls(self):
        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            res1 = self.client.post(action_url, {
                'action': 'stage_set_exhibit',
                'exhibit_id': str(self.exhibit2.pk),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(res1.status_code, 200)

            res2 = self.client.post(action_url, {
                'action': 'stage_adjust_timer',
                'delta': '30',
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(res2.status_code, 200)

            res3 = self.client.post(action_url, {
                'action': 'stage_toggle_pause',
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(res3.status_code, 200)

        self.event.refresh_from_db()
        stage_state = self.event.workflow_snapshot.get('stage_state', {})
        self.assertEqual(stage_state.get('active_exhibit_id'), self.exhibit2.pk)
        self.assertTrue(stage_state.get('timer_paused'))

    def test_organizer_results_screen_allows_preview_and_publishing(self):
        ShowcaseRating.objects.create(
            event=self.event, exhibit=self.exhibit1, organization_id=self.org.id,
            created_by=self.owner, device_digest='r' * 64, stars=5)

        results_url = reverse('showcase_event_results', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.get(results_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Solar Rover')
        self.assertContains(response, 'Audience Favorite')
        self.assertContains(response, 'Private organizer review')

        # When closed, owner can publish results
        self.event.status = ShowcaseEvent.Status.CLOSED
        self.event.minimum_judges_per_exhibit = 0
        self.event.save(update_fields=('status', 'minimum_judges_per_exhibit', 'updated_at'))

        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            publish_res = self.client.post(results_url, {'action': 'publish_results'})
        self.assertEqual(publish_res.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.PUBLISHED)
        self.assertTrue(self.event.public_results)

    def test_dedicated_projector_screen_renders_for_public(self):
        proj_url = reverse('showcase_projector_screen', args=(self.event.public_id,))
        # Does not require auth
        self.client.logout()
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            response = self.client.get(proj_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Science Fair 2026')
        self.assertContains(response, 'Scan to find an exhibit')

    def test_quick_add_exhibit_in_event_manage(self):
        draft_event = ShowcaseEvent.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Draft Showcase', status=ShowcaseEvent.Status.DRAFT)
        manage_url = reverse('showcase_event_manage', args=(draft_event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            post_res = self.client.post(manage_url, {
                'action': 'add_exhibit',
                'setup_step': 'exhibits',
                'name': 'AI Soil Sensor',
                'category': 'Agriculture',
                'description': 'Measures moisture in real time',
            })
        self.assertEqual(post_res.status_code, 302)
        self.assertTrue(ShowcaseExhibit.objects.filter(
            event=draft_event, name='AI Soil Sensor', category__name='Agriculture').exists())



class ShowcasePitchTimerFunctionTests(TestCase):
    def test_state_machine(self):
        from service_domains.operations import showcase_pitch_timer
        now = 1_000_000.0
        idle = showcase_pitch_timer({}, now)
        self.assertEqual((idle['state'], idle['remaining'], idle['duration'], idle['ends_at']),
                         ('idle', 180, 180, None))
        running = showcase_pitch_timer({'timer_ends_at': now + 59.2, 'timer_duration': 240}, now)
        self.assertEqual((running['state'], running['remaining'], running['duration']),
                         ('running', 60, 240))
        self.assertEqual(running['ends_at'], now + 59.2)
        finished = showcase_pitch_timer({'timer_ends_at': now - 5}, now)
        self.assertEqual((finished['state'], finished['remaining'], finished['ends_at']),
                         ('finished', 0, None))
        paused = showcase_pitch_timer({'timer_paused': True, 'paused_remaining_seconds': 42,
                                       'timer_ends_at': now + 999}, now)
        self.assertEqual((paused['state'], paused['remaining'], paused['ends_at']), ('paused', 42, None))
        self.assertEqual(showcase_pitch_timer({'timer_paused': True,
                                               'paused_remaining_seconds': 10 ** 9}, now)['remaining'], 86400)
        self.assertEqual(showcase_pitch_timer({'timer_duration': 0}, now)['duration'], 180)


class ShowcasePitchTimerTests(TestCase):
    """The pitch clock is owned by the server, so reloads and other screens agree."""

    def setUp(self):
        self.org = make_org('Pitch timer org')
        self.owner = make_user('pitch-timer-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Pitch expo', status=ShowcaseEvent.Status.OPEN)
        self.exhibit = ShowcaseExhibit.objects.create(event=self.event,
            organization_id=self.org.id, created_by=self.owner, name='Solar pump')
        self.client.defaults['REMOTE_ADDR'] = '127.0.0.1'
        sign_in(self.client, self.owner, self.org)
        self.t0 = timezone.now()

    def at(self, seconds):
        return patch('django.utils.timezone.now', return_value=self.t0 + timedelta(seconds=seconds))

    def post(self, seconds=0, **data):
        with self.at(seconds), patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            return self.client.post(reverse('showcase_live_action', args=(self.event.pk,)), data,
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def console(self, seconds=0):
        with self.at(seconds), patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            return self.client.get(reverse('showcase_live_control', args=(self.event.pk,)))

    def test_with_nobody_on_stage_there_is_no_clock_to_restart_on_reload(self):
        first = self.console(0)
        later = self.console(400)

        for page in (first, later):
            self.assertEqual(page.context['pitch_state'], 'idle')
            self.assertIsNone(page.context['active_exhibit'])
            self.assertContains(page, 'Nobody is on stage.')
            self.assertNotContains(page, 'id="showcase-pitch-timer-display"')
            self.assertNotContains(page, 'stage_toggle_pause')
            self.assertNotContains(page, 'stage_adjust_timer')
            self.assertEqual({row['stage_status'] for row in page.context['rows']}, {'waiting'})

    def test_idle_clock_refuses_pause_and_adjustments(self):
        for action in ({'action': 'stage_toggle_pause'}, {'action': 'stage_adjust_timer', 'delta': '30'}):
            response = self.post(**action)
            self.assertEqual(response.status_code, 400)
            self.assertIn('start the pitch clock first', response.json()['error'])

    def test_running_clock_is_derived_from_the_stored_deadline(self):
        started = self.post(0, action='stage_set_exhibit', exhibit_id=str(self.exhibit.pk))
        self.assertEqual(started.status_code, 200)
        stage = self.event.workflow_snapshot or {}
        self.event.refresh_from_db()
        stage = self.event.workflow_snapshot['stage_state']
        self.assertEqual(stage['timer_ends_at'], self.t0.timestamp() + 180)
        self.assertNotIn('pitch_seconds_remaining', stage)

        for elapsed, remaining in ((0, 180), (50, 130), (179, 1)):
            page = self.console(elapsed)
            self.assertContains(page, 'data-state="running"')
            self.assertContains(page, f'data-seconds="{remaining}"')
            self.assertContains(page, f'data-ends-at="{self.t0.timestamp() + 180}"')
            self.assertContains(page, 'data-duration="180"')
        done = self.console(200)
        self.assertContains(done, 'data-state="finished"')
        self.assertContains(done, 'data-seconds="0"')
        self.assertContains(done, 'data-ends-at=""')

    def test_pause_freezes_the_remaining_time_until_resumed(self):
        self.post(0, action='stage_set_exhibit', exhibit_id=str(self.exhibit.pk))
        self.assertEqual(self.post(50, action='stage_toggle_pause').status_code, 200)

        paused = self.console(500)
        self.assertContains(paused, 'data-state="paused"')
        self.assertContains(paused, 'data-seconds="130"')
        self.assertContains(paused, 'data-ends-at=""')
        self.assertTrue(paused.context['timer_paused'])
        self.assertContains(paused, 'Resume')

        self.post(500, action='stage_toggle_pause')
        self.event.refresh_from_db()
        self.assertEqual(self.event.workflow_snapshot['stage_state']['timer_ends_at'],
                         self.t0.timestamp() + 630)
        self.assertContains(self.console(530), 'data-seconds="100"')

    def test_adjustments_move_the_deadline_and_stretch_the_ring_duration(self):
        self.post(0, action='stage_set_exhibit', exhibit_id=str(self.exhibit.pk))
        self.post(10, action='stage_adjust_timer', delta='60')
        page = self.console(10)
        self.assertContains(page, 'data-seconds="230"')
        self.assertContains(page, 'data-duration="230"')

        self.post(10, action='stage_adjust_timer', delta='-1000')
        self.assertContains(self.console(10), 'data-seconds="0"')

        self.post(10, action='stage_toggle_pause')
        self.post(10, action='stage_adjust_timer', delta='45')
        self.assertContains(self.console(900), 'data-seconds="45"')

    def test_live_state_json_carries_the_server_clock(self):
        self.post(0, action='stage_set_exhibit', exhibit_id=str(self.exhibit.pk))
        with self.at(20), patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            data = self.client.get(reverse('showcase_live_state', args=(self.event.pk,))).json()
        self.assertEqual((data['pitch_state'], data['pitch_seconds_remaining'], data['pitch_duration']),
                         ('running', 160, 180))
        self.assertEqual(data['pitch_ends_at'], self.t0.timestamp() + 180)
        self.assertEqual(data['server_now'], self.t0.timestamp() + 20)
        self.assertFalse(data['timer_paused'])
        self.assertIn('data-state="running"', data['stage_html'])

    def test_public_projector_receives_the_deadline_not_a_bare_counter(self):
        self.post(0, action='set_projector_mode', mode='stage_spotlight')
        self.post(0, action='stage_set_exhibit', exhibit_id=str(self.exhibit.pk))
        with self.at(30), patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            page = self.client.get(reverse('showcase_projector_screen', args=(self.event.public_id,)))
        self.assertContains(page, 'data-state="running"')
        self.assertContains(page, 'data-seconds="150"')
        self.assertContains(page, f'data-ends-at="{self.t0.timestamp() + 180}"')
        self.assertContains(page, 'data-server-now=')

    def test_stage_operations_validate_the_exhibit_and_the_owner(self):
        from django.core.exceptions import PermissionDenied
        from service_domains.operations import stage_toggle_showcase_pause

        missing = self.post(0, action='stage_set_exhibit', exhibit_id='999999')
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()['error'], 'Exhibit not found.')
        with self.assertRaises(PermissionDenied):
            stage_toggle_showcase_pause(self.event, organization=self.org.as_claim(),
                                        user=make_user('pitch-timer-stranger'))

    def test_unknown_actions_and_non_live_state_requests_are_refused(self):
        unknown = self.post(0, action='launch_rocket')
        self.assertEqual(unknown.status_code, 400)
        self.assertIn('Unknown action', unknown.json()['error'])

        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.DRAFT)
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'showcase'):
            state = self.client.get(reverse('showcase_live_state', args=(self.event.pk,))).json()
        self.assertEqual(state['status'], 'draft')
        self.assertIn('redirect', state)

    def test_console_shows_the_average_once_ratings_exist(self):
        ShowcaseRating.objects.create(event=self.event, exhibit=self.exhibit,
            organization_id=self.org.id, created_by=self.owner, device_digest='p' * 64, stars=4)
        context = get_showcase_live_context(self.event)
        self.assertEqual(context['avg_stars_all'], 4.0)
        self.assertEqual(context['pitch_state'], 'idle')
