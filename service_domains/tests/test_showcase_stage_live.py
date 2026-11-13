"""Stage state, judge gating, live sockets, visitor access and the results page."""
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from config.asgi import application
from service_domains.models import (
    ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit, ShowcaseJudgeCriterion,
    ShowcaseRating,
)
from service_domains.operations import (
    SHOWCASE_NOT_ON_STAGE, get_showcase_live_context, issue_showcase_judge_invitation,
    replace_showcase_invited_judge_assignments, showcase_exhibit_on_stage,
    stage_clear_showcase_exhibit, stage_set_showcase_exhibit, submit_showcase_judge_evaluation,
)
from service_domains.realtime import broadcast_showcase_event
from service_domains.tests.stage_helpers import put_on_stage

OWN_ORIGIN = [(b'origin', b'http://localhost')]
RELEASED = lambda slug: slug == 'showcase'  # noqa: E731


class ShowcaseFixture:
    def build(self, status=ShowcaseEvent.Status.OPEN):
        self.org = make_org('Stage live org')
        self.owner = make_user('stage-live-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Stage expo', status=status, minimum_judges_per_exhibit=0)
        self.category = ShowcaseCategory.objects.create(event=self.event,
            organization_id=self.org.id, created_by=self.owner, name='Engineering')
        self.pump = ShowcaseExhibit.objects.create(event=self.event, category=self.category,
            organization_id=self.org.id, created_by=self.owner, name='Solar pump', order=1)
        self.filter = ShowcaseExhibit.objects.create(event=self.event, category=self.category,
            organization_id=self.org.id, created_by=self.owner, name='Water filter', order=2)
        self.criterion = ShowcaseJudgeCriterion.objects.create(event=self.event,
            organization_id=self.org.id, created_by=self.owner, name='Quality', weight=100,
            scale='points', max_points=10)
        self.invitation, self.token = issue_showcase_judge_invitation(self.event,
            'judge@example.org', timezone.now() + timedelta(hours=4),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {self.invitation.pk: [self.pump.pk, self.filter.pk]},
            organization=self.org.as_claim(), user=self.owner)
        self.client.defaults['REMOTE_ADDR'] = '127.0.0.1'

    def call(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=RELEASED):
            return getattr(self.client, method)(url, *args, **kwargs)

    def claim(self):
        return {'organization': self.org.as_claim(), 'user': self.owner}


class StageStateTests(ShowcaseFixture, TestCase):
    def setUp(self):
        self.build()

    def test_nothing_is_on_stage_until_the_organizer_stages_an_exhibit(self):
        context = get_showcase_live_context(self.event)

        self.assertIsNone(context['active_exhibit'])
        self.assertEqual({row['stage_status'] for row in context['rows']}, {'waiting'})
        self.assertFalse(showcase_exhibit_on_stage(self.event, self.pump.pk))

    def test_staging_marks_one_exhibit_and_remembers_who_presented(self):
        stage_set_showcase_exhibit(self.event, self.pump.pk, **self.claim())
        stage_set_showcase_exhibit(self.event, self.filter.pk, **self.claim())
        self.event.refresh_from_db()

        statuses = {row['exhibit'].name: row['stage_status']
                    for row in get_showcase_live_context(self.event)['rows']}
        self.assertEqual(statuses, {'Solar pump': 'presented', 'Water filter': 'on_stage'})
        self.assertTrue(showcase_exhibit_on_stage(self.event, self.filter.pk))
        self.assertFalse(showcase_exhibit_on_stage(self.event, self.pump.pk))

        stage_set_showcase_exhibit(self.event, self.filter.pk, **self.claim())
        self.event.refresh_from_db()
        self.assertEqual(self.event.workflow_snapshot['stage_state']['presented'], [self.pump.pk])

    def test_ending_a_pitch_clears_the_stage_and_the_clock(self):
        stage_set_showcase_exhibit(self.event, self.pump.pk, **self.claim())
        stage_clear_showcase_exhibit(self.event, **self.claim())
        self.event.refresh_from_db()

        context = get_showcase_live_context(self.event)
        self.assertIsNone(context['active_exhibit'])
        self.assertEqual(context['pitch_state'], 'idle')
        statuses = {row['exhibit'].name: row['stage_status'] for row in context['rows']}
        self.assertEqual(statuses['Solar pump'], 'presented')
        with self.assertRaisesMessage(ValidationError, 'No exhibit is on stage.'):
            stage_clear_showcase_exhibit(self.event, **self.claim())

    def test_a_deleted_staged_exhibit_leaves_the_stage_empty(self):
        stage_set_showcase_exhibit(self.event, self.pump.pk, **self.claim())
        self.pump.delete()
        context = get_showcase_live_context(self.event)
        self.assertIsNone(context['active_exhibit'])
        self.assertEqual(context['pitch_state'], 'idle')

    def test_console_shows_stage_states_and_end_pitch(self):
        sign_in(self.client, self.owner, self.org)
        action = reverse('showcase_live_action', args=(self.event.pk,))
        self.call('post', action, {'action': 'stage_set_exhibit', 'exhibit_id': str(self.pump.pk)})
        page = self.call('get', reverse('showcase_live_control', args=(self.event.pk,)))
        self.assertContains(page, 'On stage')
        self.assertContains(page, 'data-showcase-live-action="stage_clear"')
        self.assertContains(page, 'Nobody is on stage', count=0)

        self.call('post', action, {'action': 'stage_set_exhibit', 'exhibit_id': str(self.filter.pk)})
        page = self.call('get', reverse('showcase_live_control', args=(self.event.pk,)))
        self.assertContains(page, 'Presented')
        self.assertContains(page, 'Stage again')

        cleared = self.call('post', action, {'action': 'stage_clear'})
        self.assertEqual(cleared.status_code, 200)
        self.assertContains(self.call('get', reverse('showcase_live_control', args=(self.event.pk,))),
                            'Nobody is on stage')
        again = self.call('post', action, {'action': 'stage_clear'})
        self.assertEqual(again.status_code, 400)


class JudgeStageGateTests(ShowcaseFixture, TestCase):
    def setUp(self):
        self.build()
        self.url = reverse('showcase_judge_workspace', args=(self.token,))
        self.state_url = reverse('showcase_judge_state', args=(self.token,))

    def scores(self):
        return {str(self.criterion.pk): '8'}

    def test_scorecard_is_refused_until_the_exhibit_is_on_stage(self):
        with self.assertRaisesMessage(ValidationError, SHOWCASE_NOT_ON_STAGE):
            submit_showcase_judge_evaluation(self.token, self.pump.public_id, self.scores())

        put_on_stage(self.event, self.filter)
        with self.assertRaisesMessage(ValidationError, SHOWCASE_NOT_ON_STAGE):
            submit_showcase_judge_evaluation(self.token, self.pump.public_id, self.scores())

        put_on_stage(self.event, self.pump)
        evaluation = submit_showcase_judge_evaluation(self.token, self.pump.public_id, self.scores())
        self.assertEqual(str(evaluation.score), '80.00')

    def test_closed_events_still_accept_outstanding_scorecards(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.CLOSED)
        self.event.refresh_from_db()
        self.assertTrue(submit_showcase_judge_evaluation(self.token, self.pump.public_id, self.scores()))

    def test_workspace_locks_every_exhibit_but_the_staged_one(self):
        page = self.call('get', self.url)
        self.assertEqual([card['stage_locked'] for card in page.context['cards']], [True, True])
        self.assertContains(page, 'Waiting for the stage.')
        self.assertContains(page, 'data-score-fieldset disabled')

        put_on_stage(self.event, self.filter)
        page = self.call('get', self.url)
        by_name = {card['assignment'].exhibit.name: card for card in page.context['cards']}
        self.assertTrue(by_name['Water filter']['on_stage'])
        self.assertFalse(by_name['Water filter']['stage_locked'])
        self.assertTrue(by_name['Solar pump']['stage_locked'])
        self.assertContains(page, 'On stage now')
        self.assertContains(page, 'data-stage-gated="true"')

    def test_posting_a_locked_exhibit_explains_why_and_saves_nothing(self):
        from service_domains.models import ShowcaseJudgeEvaluation, ShowcaseJudgeExhibitAssignment
        assignment = ShowcaseJudgeExhibitAssignment.objects.get(exhibit=self.pump)
        payload = {'exhibit_id': str(self.pump.public_id),
                   f'exhibit_{assignment.pk}-criterion_{self.criterion.pk}': '8'}
        refused = self.call('post', self.url, payload)
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, 'not on stage yet', status_code=400)
        self.assertEqual(ShowcaseJudgeEvaluation.objects.count(), 0)

        put_on_stage(self.event, self.pump)
        accepted = self.call('post', self.url, payload)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(ShowcaseJudgeEvaluation.objects.count(), 1)

    def test_closed_events_do_not_gate_the_workspace(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.CLOSED)
        page = self.call('get', self.url)
        self.assertEqual([card['stage_locked'] for card in page.context['cards']], [False, False])
        self.assertContains(page, 'data-stage-gated="false"')

    def test_state_endpoint_reports_only_this_judges_staged_exhibit(self):
        empty = self.call('get', self.state_url).json()
        self.assertEqual((empty['status'], empty['on_stage'], empty['scoring_paused']), ('open', [], False))

        put_on_stage(self.event, self.pump)
        staged = self.call('get', self.state_url)
        self.assertEqual(staged.json()['on_stage'], [str(self.pump.public_id)])
        self.assertIn('no-store', staged['Cache-Control'])

        other, _ = issue_showcase_judge_invitation(self.event, 'other@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        other_token = issue_showcase_judge_invitation(self.event, 'third@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)[1]
        unassigned = self.call('get', reverse('showcase_judge_state', args=(other_token,))).json()
        self.assertEqual(unassigned['on_stage'], [])

    def test_state_endpoint_guards_tokens_limits_and_service(self):
        self.assertEqual(self.call('get', reverse('showcase_judge_state', args=('bogus',))).status_code, 404)
        with patch('service_domains.views.allow_showcase_judge_request', return_value=None):
            self.assertEqual(self.call('get', self.state_url).status_code, 503)
        with patch('service_domains.views.allow_showcase_judge_request', return_value=False):
            limited = self.call('get', self.state_url)
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited)
        self.assertEqual(self.client.get(self.state_url).status_code, 404)

    def test_workspace_script_listens_for_stage_changes(self):
        page = self.call('get', self.url)
        self.assertContains(page, '/ws/showcase/judge/')
        self.assertContains(page, 'syncStage')


class VisitorAccessTests(ShowcaseFixture, TestCase):
    def setUp(self):
        self.build()
        self.rate_url = reverse('showcase_rate_exhibit', args=(self.pump.public_id,))
        self.visit_url = reverse('showcase_visit', args=(self.event.public_id,))

    def test_visit_page_offers_rating_for_the_exhibit_on_stage_only(self):
        put_on_stage(self.event, self.pump)
        page = self.call('get', self.visit_url)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Rate the exhibits')
        self.assertContains(page, 'On stage now')
        self.assertContains(page, 'Rate Solar pump')
        self.assertContains(page, 'Engineering')
        self.assertContains(page, 'Water filter')
        self.assertContains(page, self.rate_url, count=2)
        other = reverse('showcase_rate_exhibit', args=(self.filter.public_id,))
        self.assertNotContains(page, other)
        self.assertContains(page, f'/ws/showcase/display/{self.event.public_id}/')

    def test_visit_page_explains_when_nobody_is_on_stage(self):
        page = self.call('get', self.visit_url)
        self.assertContains(page, 'No exhibit is on stage right now')
        self.assertNotContains(page, self.rate_url)
        self.assertContains(page, 'Solar pump')

    def test_visit_page_explains_a_paused_rating_stream(self):
        put_on_stage(self.event, self.pump)
        self.event.refresh_from_db()
        snapshot = dict(self.event.workflow_snapshot)
        snapshot['floor_state'] = {'visitor_voting_paused': True}
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(workflow_snapshot=snapshot)
        page = self.call('get', self.visit_url)
        self.assertContains(page, 'temporarily paused by the organizer')
        self.assertNotContains(page, self.rate_url)

    def test_exhibits_without_a_category_are_grouped_together(self):
        ShowcaseExhibit.objects.create(event=self.event, organization_id=self.org.id,
            created_by=self.owner, name='Loose exhibit')
        page = self.call('get', self.visit_url)
        self.assertContains(page, 'Exhibits')
        self.assertContains(page, 'Loose exhibit')

    def test_draft_and_closed_events_explain_instead_of_hiding(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.DRAFT)
        draft = self.call('get', self.visit_url)
        self.assertContains(draft, 'has not opened for ratings yet')
        self.assertNotContains(draft, 'Solar pump')

        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.CLOSED)
        closed = self.call('get', self.visit_url)
        self.assertContains(closed, 'Ratings for this showcase are closed')
        self.assertContains(closed, 'Solar pump')
        self.assertNotContains(closed, self.rate_url)

    def test_unknown_event_and_qr_images(self):
        import uuid
        self.assertEqual(self.call('get', reverse('showcase_visit', args=(uuid.uuid4(),))).status_code, 404)
        qr = self.call('get', reverse('showcase_visit_qr', args=(self.event.public_id,)))
        self.assertEqual(qr['Content-Type'], 'image/png')
        self.assertTrue(qr.content.startswith(b'\x89PNG'))
        self.assertEqual(self.call('get', reverse('showcase_visit_qr', args=(uuid.uuid4(),))).status_code, 404)

    def test_a_booth_label_link_explains_a_draft_event_instead_of_page_not_found(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.DRAFT)
        page = self.call('get', self.rate_url)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Ratings are not open right now')
        self.assertContains(page, 'has not opened for ratings yet')
        self.assertContains(page, self.visit_url)
        self.assertNotContains(page, 'class="showcase-stars"')
        self.assertEqual(self.call('post', self.rate_url, {'stars': '5'}).status_code, 403)
        self.assertEqual(ShowcaseRating.objects.count(), 0)

    def test_unknown_exhibit_is_still_a_404(self):
        import uuid
        self.assertEqual(self.call('get', reverse('showcase_rate_exhibit', args=(uuid.uuid4(),))).status_code, 404)

    def test_console_leads_with_the_projector_controls_before_the_stage(self):
        sign_in(self.client, self.owner, self.org)
        page = self.call('get', reverse('showcase_live_control', args=(self.event.pk,))).content.decode()

        self.assertNotIn('How visitors rate', page)
        projector = page.index('id="showcase-live-projector-control"')
        stage = page.index('id="showcase-live-stage"')
        radar = page.index('id="showcase-exhibit-radar-table"')
        self.assertLess(projector, stage)
        self.assertLess(stage, radar)
        self.assertEqual(page.count('id="showcase-live-projector-control"'), 1)
        self.assertIn('data-mode="qr_welcome"', page)

    def test_public_screen_shows_the_venue_code_and_a_stage_code(self):
        sign_in(self.client, self.owner, self.org)
        action = reverse('showcase_live_action', args=(self.event.pk,))
        projector = reverse('showcase_projector_screen', args=(self.event.public_id,))
        welcome = self.call('get', projector)
        self.assertContains(welcome, reverse('showcase_visit_qr', args=(self.event.public_id,)))
        self.assertContains(welcome, 'Scan to find an exhibit and rate it')
        self.assertContains(welcome, f'/ws/showcase/display/{self.event.public_id}/')

        self.call('post', action, {'action': 'set_projector_mode', 'mode': 'stage_spotlight'})
        self.call('post', action, {'action': 'stage_set_exhibit', 'exhibit_id': str(self.pump.pk)})
        stage = self.call('get', projector)
        self.assertContains(stage, reverse('showcase_exhibit_qr', args=(self.pump.public_id,)))
        self.assertContains(stage, 'Scan to rate this exhibit')


class VisitorRatedStateTests(ShowcaseFixture, TestCase):
    """After rating, a visitor returns to the list and waits for the next exhibit."""

    def setUp(self):
        self.build()
        self.visit_url = reverse('showcase_visit', args=(self.event.public_id,))
        self.pump_url = reverse('showcase_rate_exhibit', args=(self.pump.public_id,))
        self.filter_url = reverse('showcase_rate_exhibit', args=(self.filter.public_id,))
        put_on_stage(self.event, self.pump)

    def rate(self, url, stars='5'):
        self.call('get', url)
        return self.call('post', url, {'stars': stars})

    def test_thank_you_page_leads_back_to_the_exhibit_list(self):
        done = self.rate(self.pump_url)

        self.assertContains(done, 'Your rating has been recorded')
        self.assertContains(done, 'Back to all exhibits')
        self.assertContains(done, f'href="{self.visit_url}"')
        self.assertContains(done, 'wait there')

    def test_list_shows_the_rated_exhibit_and_waits_for_the_next(self):
        self.rate(self.pump_url)
        page = self.call('get', self.visit_url)

        self.assertTrue(page.context['staged_rated'])
        self.assertContains(page, 'You have rated this exhibit')
        self.assertContains(page, 'it will appear here')
        self.assertNotContains(page, 'Rate Solar pump')
        self.assertNotContains(page, self.pump_url)
        self.assertContains(page, 'Rated')

    def test_next_exhibit_on_stage_becomes_ratable_and_the_first_stays_locked(self):
        self.rate(self.pump_url)
        put_on_stage(self.event, self.filter)
        page = self.call('get', self.visit_url)

        self.assertFalse(page.context['staged_rated'])
        self.assertContains(page, 'Rate Water filter')
        self.assertContains(page, self.filter_url, count=2)
        self.assertNotContains(page, self.pump_url)
        self.assertContains(self.call('get', self.pump_url), 'You have already rated this exhibit')

        done = self.rate(self.filter_url, '4')
        self.assertContains(done, 'Your rating has been recorded')
        final = self.call('get', self.visit_url)
        self.assertEqual(final.context['rated_ids'], {self.pump.pk, self.filter.pk})

    def test_rerating_is_refused_even_by_post(self):
        self.rate(self.pump_url)
        again = self.call('post', self.pump_url, {'stars': '1'})
        self.assertContains(again, 'already rated', status_code=400)
        self.assertEqual(ShowcaseRating.objects.count(), 1)

    def test_other_devices_and_forged_cookies_see_nothing_as_rated(self):
        self.rate(self.pump_url)
        stranger = Client()
        stranger.defaults['REMOTE_ADDR'] = '127.0.0.2'
        with patch('portal.middleware.is_released', side_effect=RELEASED):
            page = stranger.get(self.visit_url)
            self.assertEqual(page.context['rated_ids'], set())
            self.assertContains(page, 'Rate Solar pump')
            stranger.cookies[f'livegrade_showcase_device_{self.event.public_id.hex}'] = 'forged-token'
            self.assertEqual(stranger.get(self.visit_url).context['rated_ids'], set())

    def test_a_first_time_visitor_without_a_cookie_has_rated_nothing(self):
        page = self.call('get', self.visit_url)
        self.assertEqual(page.context['rated_ids'], set())
        self.assertFalse(page.context['staged_rated'])
        self.assertNotContains(page, 'You have rated this exhibit')


class SharedTabsAndResultsTests(ShowcaseFixture, TestCase):
    def setUp(self):
        self.build()
        sign_in(self.client, self.owner, self.org)
        self.results_url = reverse('showcase_event_results', args=(self.event.pk,))

    def test_console_and_results_share_one_tab_list(self):
        for url, active in ((reverse('showcase_live_control', args=(self.event.pk,)), 'Live console'),
                            (self.results_url, 'Results')):
            page = self.call('get', url).content.decode()
            self.assertEqual(page.count('aria-label="Event workspace"'), 1)
            self.assertEqual(page.count('class="page-tabs'), 1)
            for label in ('Live console', 'Edit setup', 'Results &amp; Awards'):
                self.assertIn(label, page)
            self.assertNotIn('Print labels</a>\n  <a', page)
            self.assertEqual(page.count('aria-current="page"'), 1)

    def test_closed_events_show_only_the_console_and_results_tabs(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.CLOSED)
        for url in (reverse('showcase_live_control', args=(self.event.pk,)), self.results_url):
            page = self.call('get', url).content.decode()
            tabs = page[page.index('aria-label="Event workspace"'):]
            tabs = tabs[:tabs.index('</nav>')]
            self.assertEqual(tabs.count('class="page-tab'), 2)
            self.assertIn('Live console', tabs)
            self.assertIn('Results &amp; Awards', tabs)
            self.assertNotIn('Edit setup', tabs)

    def test_results_tab_is_hidden_for_a_draft_event(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.DRAFT)
        page = self.call('get', reverse('showcase_live_control', args=(self.event.pk,))).content.decode()
        self.assertNotIn(self.results_url, page)

    def test_results_page_has_kpis_standings_and_awards(self):
        put_on_stage(self.event, self.pump)
        submit_showcase_judge_evaluation(self.token, self.pump.public_id, {str(self.criterion.pk): '9'})
        ShowcaseRating.objects.create(event=self.event, exhibit=self.pump,
            organization_id=self.org.id, created_by=self.owner, device_digest='a' * 64, stars=5)
        ShowcaseRating.objects.create(event=self.event, exhibit=self.filter,
            organization_id=self.org.id, created_by=self.owner, device_digest='b' * 64, stars=3)

        page = self.call('get', self.results_url)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Standings')
        self.assertContains(page, 'id="showcase-results-live"')
        self.assertEqual(page.context['exhibit_count'], 2)
        self.assertEqual(page.context['total_ratings'], 2)
        self.assertEqual(page.context['submitted_scorecards'], 1)
        self.assertEqual(page.context['assigned_scorecards'], 2)
        self.assertEqual(page.context['scorecard_percent'], 50)
        standings = page.context['standings']
        self.assertEqual([item['row']['exhibit'].name for item in standings], ['Solar pump', 'Water filter'])
        self.assertEqual([item['rank'] for item in standings], [1, 2])
        self.assertTrue(standings[0]['is_winner'])
        self.assertEqual((page.context['awards_decided'], page.context['awards_total']), (2, 2))
        self.assertContains(page, 'Winner')
        self.assertContains(page, 'Audience Favorite')
        self.assertContains(page, 'Pending')

    def test_empty_event_results_do_not_divide_by_zero(self):
        ShowcaseExhibit.objects.all().delete()
        from service_domains.models import ShowcaseJudgeExhibitAssignment
        ShowcaseJudgeExhibitAssignment.objects.all().delete()
        page = self.call('get', self.results_url)
        self.assertEqual(page.context['scorecard_percent'], 0)
        self.assertContains(page, 'No exhibits have been added yet.')

    def test_socket_refresh_receives_only_the_fragment(self):
        page = self.call('get', self.results_url)
        self.assertContains(page, '/ws/showcase/event/')
        fragment = self.call('get', self.results_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertTemplateNotUsed(fragment, 'service_domains/showcase_event_results.html')
        self.assertTemplateUsed(fragment, 'service_domains/_partials/showcase_results_body.html')
        self.assertNotContains(fragment, '<html')
        self.assertContains(fragment, 'Standings')
        self.assertIn('no-store', fragment['Cache-Control'])

    def test_publishing_without_the_required_scorecards_shows_the_reason(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(
            status=ShowcaseEvent.Status.CLOSED, minimum_judges_per_exhibit=1)
        response = self.call('post', self.results_url, {'action': 'publish_results'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'submitted judge scorecards before results can be published')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.CLOSED)

    def test_editing_a_criterion_link_without_a_step_opens_the_rubric_step(self):
        criterion = self.criterion
        page = self.call('get', f"{reverse('showcase_event_manage', args=(self.event.pk,))}?edit_criterion={criterion.pk}")
        self.assertEqual(page.context['active_step'], 'criteria')

    def test_live_badge_only_shows_while_the_event_is_open(self):
        open_page = self.call('get', self.results_url)
        self.assertContains(open_page, 'id="results-live-badge"')

        for status in (ShowcaseEvent.Status.CLOSED, ShowcaseEvent.Status.PUBLISHED):
            ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=status, public_results=True)
            page = self.call('get', self.results_url)
            self.assertNotContains(page, 'id="results-live-badge"')
            self.assertNotContains(page, 'results-live-badge"><span class="dot">')
        fragment = self.call('get', self.results_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertNotContains(fragment, 'id="results-live-badge"')

    def test_draft_events_have_no_results_page(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.DRAFT)
        self.assertEqual(self.call('get', self.results_url).status_code, 404)

    def test_published_results_page_states(self):
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(
            status=ShowcaseEvent.Status.PUBLISHED, public_results=True)
        page = self.call('get', self.results_url)
        self.assertContains(page, 'Public results page')
        self.assertContains(page, 'Return results to review')
        self.assertContains(page, 'data-live-enabled="false"')


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'showcase'])
class ShowcaseSocketTests(ShowcaseFixture, TransactionTestCase):
    def setUp(self):
        self.build()

    async def connect(self, path):
        communicator = WebsocketCommunicator(application, path, headers=OWN_ORIGIN)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def test_judge_and_display_sockets_receive_broadcasts_but_staff_does_not_need_them(self):
        judge, judge_ok = await self.connect(f'/ws/showcase/judge/{self.token}/')
        display, display_ok = await self.connect(f'/ws/showcase/display/{self.event.public_id}/')
        self.assertTrue(judge_ok and display_ok)

        await sync_to_async(broadcast_showcase_event)(self.event, 'showcase.stage', {'secret': 'hidden'})

        message = await judge.receive_json_from()
        self.assertEqual(message['type'], 'showcase.stage')
        shown = await display.receive_json_from()
        self.assertEqual(shown, {'type': 'showcase.stage', 'data': {}})
        await judge.disconnect()
        await display.disconnect()

    async def test_judge_socket_rejects_unknown_tokens_and_unreleased_service(self):
        rejected, ok = await self.connect('/ws/showcase/judge/not-a-real-token/')
        self.assertFalse(ok)
        await rejected.disconnect()
        with patch('service_domains.consumers.is_released', return_value=False):
            unreleased, ok = await self.connect(f'/ws/showcase/judge/{self.token}/')
            self.assertFalse(ok)
            await unreleased.disconnect()
            screen, ok = await self.connect(f'/ws/showcase/display/{self.event.public_id}/')
            self.assertFalse(ok)
            await screen.disconnect()

    async def test_display_socket_rejects_unknown_events(self):
        import uuid
        unknown, ok = await self.connect(f'/ws/showcase/display/{uuid.uuid4()}/')
        self.assertFalse(ok)
        await unknown.disconnect()

    async def test_broadcasts_survive_a_missing_channel_layer(self):
        with patch('service_domains.realtime.get_channel_layer', return_value=None):
            await sync_to_async(broadcast_showcase_event)(self.event, 'showcase.stage')
        with patch('service_domains.realtime.async_to_sync', side_effect=RuntimeError('layer down')):
            await sync_to_async(broadcast_showcase_event)(self.event, 'showcase.stage')

    async def test_state_changes_fan_out_to_every_group(self):
        judge, _ = await self.connect(f'/ws/showcase/judge/{self.token}/')
        display, _ = await self.connect(f'/ws/showcase/display/{self.event.public_id}/')

        def stage():
            client = Client()
            sign_in(client, self.owner, self.org)
            with patch('portal.middleware.is_released', side_effect=RELEASED):
                return client.post(reverse('showcase_live_action', args=(self.event.pk,)),
                                   {'action': 'stage_set_exhibit', 'exhibit_id': str(self.pump.pk)})
        response = await sync_to_async(stage)()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await judge.receive_json_from())['type'], 'showcase.stage_set_exhibit')
        self.assertEqual((await display.receive_json_from())['type'], 'showcase.stage_set_exhibit')
        await judge.disconnect()
        await display.disconnect()


class ConsoleDeduplicationTests(ShowcaseFixture, TestCase):
    """Each fact on the live console appears once."""

    def setUp(self):
        self.build()
        sign_in(self.client, self.owner, self.org)
        self.console_url = reverse('showcase_live_control', args=(self.event.pk,))

    def console(self):
        return self.call('get', self.console_url)

    def test_event_name_is_in_the_top_bar_and_only_screen_readers_get_a_second_heading(self):
        html = self.console().content.decode()
        self.assertEqual(html.count('class="visually-hidden">Stage expo: live console'), 1)
        self.assertNotIn('class="h3 fw-bold mb-1">Stage expo', html)
        self.assertEqual(html.count('<h1'), 1)

    def test_live_status_is_the_top_bar_badge_only(self):
        html = self.console().content.decode()
        self.assertIn('id="showcase-conn-badge"', html)
        self.assertNotIn('id="showcase-live-badge"', html)
        self.assertNotIn('Live Event', html)
        self.assertEqual(html.count('Open for ratings'), 1)

    def test_attention_count_lives_only_in_the_radar_filter(self):
        html = self.console().content.decode()
        self.assertNotIn('Need Attention', html)
        self.assertEqual(html.count('Needs attention ('), 1)

    def test_console_has_no_visitor_access_section_or_label_shortcuts(self):
        html = self.console().content.decode()
        self.assertNotIn('How visitors rate', html)
        self.assertNotIn('Print booth labels', html)
        self.assertNotIn('>Print labels<', html)
        self.assertNotIn(reverse('showcase_event_labels', args=(self.event.pk,)), html)
        self.assertNotIn('id="showcase-live-access"', html)

    def test_public_screen_controls_live_only_in_the_projector_section(self):
        html = self.console().content.decode()
        projector = reverse('showcase_projector_screen', args=(self.event.public_id,))
        self.assertEqual(html.count(f'href="{projector}"'), 1)
        self.assertEqual(html.count('Launch screen'), 1)
        self.assertEqual(html.count('data-mode="qr_welcome"'), 1)
        self.assertEqual(html.count('data-mode="stage_spotlight"'), 1)
        self.assertNotIn('Show on public screen', html)
        self.assertNotIn('Use stage mode', html)
        self.assertNotIn('Open public screen', html)


class ClosedConsoleTests(ShowcaseFixture, TestCase):
    def setUp(self):
        self.build(status=ShowcaseEvent.Status.CLOSED)
        sign_in(self.client, self.owner, self.org)
        self.console_url = reverse('showcase_live_control', args=(self.event.pk,))

    def test_closed_console_shows_status_summary_and_a_way_to_the_results(self):
        ShowcaseRating.objects.create(event=self.event, exhibit=self.pump,
            organization_id=self.org.id, created_by=self.owner, device_digest='c' * 64, stars=4)
        page = self.call('get', self.console_url)

        self.assertContains(page, 'This showcase is closed')
        self.assertContains(page, reverse('showcase_event_results', args=(self.event.pk,)))
        self.assertContains(page, 'View results and awards')
        self.assertContains(page, 'Visitor ratings')
        self.assertContains(page, '4.0 avg')
        self.assertContains(page, 'Judge scorecards')

    def test_closed_console_drops_every_live_control(self):
        html = self.call('get', self.console_url).content.decode()
        for gone in ('id="showcase-live-stage"', 'id="showcase-exhibit-radar-table"',
                     'id="showcase-live-access"', 'id="showcase-live-projector-control"',
                     'toggle_visitor_voting', 'confirm-showcase-close-ratings-btn',
                     'confirm-showcase-publish-btn', 'How visitors rate'):
            self.assertNotIn(gone, html)

    def test_publishing_is_not_offered_from_the_console(self):
        response = self.call('post', reverse('showcase_live_action', args=(self.event.pk,)),
                             {'action': 'publish_results'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Unknown action', response.json()['error'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.CLOSED)


class RatingStateEndpointTests(ShowcaseFixture, TestCase):
    """The rate page asks the server whether this device can still rate."""

    def setUp(self):
        self.build()
        put_on_stage(self.event, self.pump)
        self.rate_url = reverse('showcase_rate_exhibit', args=(self.pump.public_id,))
        self.state_url = reverse('showcase_rating_state', args=(self.pump.public_id,))
        self.filter_state = reverse('showcase_rating_state', args=(self.filter.public_id,))

    def state(self, url=None, client=None):
        with patch('portal.middleware.is_released', side_effect=RELEASED):
            return (client or self.client).get(url or self.state_url)

    def test_on_stage_and_unrated_can_rate(self):
        response = self.state()
        self.assertEqual(response.json(), {'can_rate': True, 'reason': '', 'message': ''})
        self.assertIn('no-store', response['Cache-Control'])

    def test_rating_it_closes_the_door_for_this_device_only(self):
        self.call('get', self.rate_url)
        self.call('post', self.rate_url, {'stars': '5'})
        mine = self.state().json()
        self.assertEqual((mine['can_rate'], mine['reason']), (False, 'rated'))
        self.assertIn('already rated', mine['message'])

        stranger = Client()
        stranger.defaults['REMOTE_ADDR'] = '127.0.0.9'
        self.assertTrue(self.state(client=stranger).json()['can_rate'])

    def test_off_stage_paused_and_closed_states(self):
        off = self.state(self.filter_state).json()
        self.assertEqual((off['can_rate'], off['reason']), (False, 'unavailable'))
        self.assertIn('not on stage right now', off['message'])

        self.event.refresh_from_db()
        snapshot = dict(self.event.workflow_snapshot)
        snapshot['floor_state'] = {'visitor_voting_paused': True}
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(workflow_snapshot=snapshot)
        paused = self.state().json()
        self.assertEqual((paused['can_rate'], paused['reason']), (False, 'paused'))

        snapshot['floor_state'] = {}
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(
            workflow_snapshot=snapshot, status=ShowcaseEvent.Status.CLOSED)
        closed = self.state().json()
        self.assertEqual(closed['reason'], 'unavailable')
        self.assertIn('closed', closed['message'])

    def test_unknown_exhibit_and_wrong_method(self):
        import uuid
        self.assertEqual(self.state(reverse('showcase_rating_state', args=(uuid.uuid4(),))).status_code, 404)
        with patch('portal.middleware.is_released', side_effect=RELEASED):
            self.assertEqual(self.client.post(self.state_url).status_code, 405)

    def test_rate_page_wires_the_live_guard(self):
        page = self.call('get', self.rate_url)
        self.assertContains(page, 'data-rate-form')
        self.assertContains(page, 'data-rate-blocked hidden')
        self.assertContains(page, self.state_url)
        self.assertContains(page, "document.addEventListener('visibilitychange'")
        self.assertContains(page, 'Back to all exhibits')

    def test_waiting_page_reloads_when_the_stage_changes(self):
        waiting = self.call('get', reverse('showcase_rate_exhibit', args=(self.filter.public_id,)))
        self.assertContains(waiting, 'data-rate-waiting')
        self.assertContains(waiting, 'reload as soon as the stage changes')
        self.assertNotContains(waiting, 'class="showcase-stars"')
        self.assertNotContains(waiting, '<form method="post"')

    def test_paused_stream_shows_the_message_on_the_rate_page(self):
        self.event.refresh_from_db()
        snapshot = dict(self.event.workflow_snapshot)
        snapshot['floor_state'] = {'visitor_voting_paused': True}
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(workflow_snapshot=snapshot)
        page = self.call('get', self.rate_url)
        self.assertContains(page, 'temporarily paused by the organizer')
        self.assertIn('no-store', page['Cache-Control'])
