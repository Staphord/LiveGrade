"""Competition Arena Live Control Console tests."""
from decimal import Decimal
from unittest.mock import patch
import uuid
import re
from django.core import mail
from django.test import TestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    Competition,
    CompetitionAudienceBallot,
    CompetitionEntry,
    CompetitionJudgeInvitation,
    CompetitionJudgeScore,
    CompetitionRound,
    CompetitionRoundEntry,
    CompetitionTrack,
)
from service_domains.operations import (
    start_competition,
)


@override_settings(LIVEGRADE_LIVE_SERVICES=['assessment', 'panel', 'polls', 'team', 'showcase', 'competition'])
class CompetitionLiveControlTests(TestCase):

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.org = make_org(f'Arena Org {suffix}')
        self.other_org = make_org(f'Other Arena Org {suffix}')
        self.owner = make_user(f'arena-owner-{suffix}')
        self.other = make_user(f'arena-other-{suffix}')
        sign_in(self.client, self.owner, self.org)

        self.competition = Competition.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='National AI Hackathon',
            status=Competition.Status.DRAFT,
            audience_weight=Decimal('30.00'),
            minimum_judges_per_entry=1,
            minimum_audience_ballots_per_track=1,
        )
        self.track = CompetitionTrack.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            competition=self.competition,
            name='HealthTech',
        )
        self.entry1 = CompetitionEntry.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            track=self.track,
            name='NeuralScan AI',
            reference='H-101',
            order=1,
        )
        self.entry2 = CompetitionEntry.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            track=self.track,
            name='PulseGuard',
            reference='H-102',
            order=2,
        )
        self.round1 = CompetitionRound.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            competition=self.competition,
            name='Preliminary Round',
            order=1,
        )
        self.round_entry1 = CompetitionRoundEntry.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            round=self.round1,
            entry=self.entry1,
            track=self.track,
            judge_score=Decimal('85.50'),
            score=Decimal('85.50'),
        )
        self.round_entry2 = CompetitionRoundEntry.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            round=self.round1,
            entry=self.entry2,
            track=self.track,
            judge_score=Decimal('92.00'),
            score=Decimal('92.00'),
        )
        self.judge = CompetitionJudgeInvitation.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            competition=self.competition,
            round=self.round1,
            track=self.track,
            email='judge@arena.org',
            token_digest=CompetitionJudgeInvitation.digest_token('sampletoken' * 4),
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )

    def test_draft_arena_shows_readiness_and_starts_the_competition(self):
        CompetitionRound.objects.filter(competition=self.competition).delete()
        url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['competition_status'], 'draft')
        self.assertTrue(res.context['can_start'])
        self.assertContains(res, 'Start National AI Hackathon')
        self.assertContains(res, 'id="confirm-competition-start-btn"')
        self.assertNotContains(res, 'id="competition-live-judges"')
        self.assertNotContains(res, 'Arena screen')
        action = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        started = self.client.post(action, {'action': 'start_competition'},
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()['redirect'], url)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.ACTIVE)
        self.assertEqual(CompetitionRound.objects.get(competition=self.competition).name, 'Round 1')

    def test_draft_arena_blocks_starting_until_setup_is_ready(self):
        CompetitionRound.objects.filter(competition=self.competition).delete()
        self.entry1.delete()
        self.entry2.delete()
        url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(url)
        self.assertFalse(res.context['can_start'])
        self.assertContains(res, 'Every track needs at least one entry')
        self.assertContains(res, 'disabled title="Finish the required setup steps first"')

        action = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        refused = self.client.post(action, {'action': 'start_competition'},
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(refused.status_code, 400)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.DRAFT)

    def test_draft_arena_requires_at_least_one_track(self):
        competition = Competition.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Competition without tracks', status=Competition.Status.DRAFT,
            audience_weight=Decimal('30.00'), minimum_judges_per_entry=1,
            minimum_audience_ballots_per_track=1)

        response = self.client.get(reverse(
            'competition_live_control', kwargs={'competition_id': competition.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_start'])
        self.assertContains(response, 'Add at least one track')

    def test_draft_arena_requires_criteria_that_total_100(self):
        from service_domains.models import CompetitionCriterion
        CompetitionCriterion.objects.create(track=self.track, name='Idea', weight=Decimal('60'),
            organization_id=self.org.id, created_by=self.owner)
        res = self.client.get(reverse('competition_live_control',
                                      kwargs={'competition_id': self.competition.pk}))
        self.assertFalse(res.context['can_start'])
        self.assertContains(res, 'must total exactly 100%')

    def test_draft_readiness_aggregates_entries_and_criteria_once_for_many_tracks(self):
        from service_domains.models import CompetitionCriterion
        for number in (2, 3):
            track = CompetitionTrack.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                competition=self.competition, name=f'Track {number}')
            CompetitionEntry.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                track=track, name=f'Entry {number}')
            CompetitionCriterion.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                track=track, name=f'Criterion {number}', weight=Decimal('100'))
        CompetitionCriterion.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            track=self.track, name='Criterion 1', weight=Decimal('100'))

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse(
                'competition_live_control', kwargs={'competition_id': self.competition.pk}))

        self.assertEqual(response.status_code, 200)
        entry_queries = [query for query in queries if CompetitionEntry._meta.db_table.lower() in query['sql'].lower()]
        criterion_queries = [query for query in queries if CompetitionCriterion._meta.db_table.lower() in query['sql'].lower()]
        self.assertEqual(len(entry_queries), 1)
        self.assertEqual(len(criterion_queries), 1)
        self.assertTrue(response.context['can_start'])

    def test_other_actions_still_need_an_active_competition(self):
        action = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        refused = self.client.post(action, {'action': 'toggle_audience_voting'},
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()['error'], 'Competition is not active.')

    def test_finished_competition_shows_only_the_closed_status(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        Competition.objects.filter(pk=self.competition.pk).update(status=Competition.Status.COMPLETE)
        res = self.client.get(reverse('competition_live_control',
                                      kwargs={'competition_id': self.competition.pk}))
        html = res.content.decode()

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'This competition is finished')
        self.assertContains(res, 'View results and standings')
        for gone in ('id="competition-live-judges"', 'id="competition-live-standings"',
                     'id="competition-conn-badge"', 'Arena screen', 'Edit setup', 'conclude_competition'):
            self.assertNotIn(gone, html)
        self.assertIn('Results &amp; Standings', html)

    def test_active_arena_has_one_finish_one_edit_setup_and_one_screen_link(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        html = self.client.get(reverse('competition_live_control',
                                       kwargs={'competition_id': self.competition.pk})).content.decode()
        projector = reverse('competition_projector', kwargs={'competition_id': self.competition.pk})
        self.assertEqual(html.count(f'href="{projector}"'), 1)
        self.assertEqual(html.count('Edit setup'), 1)
        self.assertEqual(html.count('name="action" value="conclude_competition"'), 1)
        self.assertEqual(html.count('competition-end-modal'), 0)
        self.assertEqual(html.count('aria-label="Competition workspace"'), 1)

    def test_active_arena_tabs_open_dedicated_setup_and_results_pages_in_order(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        live_url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        manage_url = reverse('competition_manage', kwargs={'competition_id': self.competition.pk})
        results_url = reverse('competition_results', kwargs={'competition_id': self.competition.pk})
        html = self.client.get(live_url).content.decode()
        tabs = html.split('<nav class="page-tabs mb-3" aria-label="Competition workspace">', 1)[1].split(
            '</nav>', 1)[0]

        self.assertLess(tabs.index('Live arena'), tabs.index('Edit setup'))
        self.assertLess(tabs.index('Edit setup'), tabs.index('Results &amp; Standings'))
        self.assertIn(f'href="{manage_url}?from=live"', tabs)
        self.assertIn(f'href="{results_url}"', tabs)
        self.assertNotIn('#competition-live-standings', tabs)
        self.assertIn('id="competition-live-standings"', html)

    def test_results_tab_has_dedicated_live_standings_and_round_history(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))

        response = self.client.get(reverse('competition_results', args=(self.competition.pk,)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Round progress')
        self.assertContains(response, 'Preliminary Round')
        self.assertContains(response, 'data-live-view="results"')
        self.assertContains(response, 'Advance to the next round')

    def test_setup_tab_is_marked_active_and_results_tab_is_dedicated(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

        response = self.client.get(reverse('competition_manage', args=(self.competition.pk,)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="{}"'.format(reverse(
            'competition_results', args=(self.competition.pk,))))
        self.assertContains(response, 'aria-current="page"')

    def test_active_competition_live_control_renders_arena_console(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.ACTIVE)

        url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Arena Live Stage')
        self.assertContains(res, 'NeuralScan AI')
        self.assertContains(res, 'PulseGuard')
        self.assertContains(res, 'HealthTech')

    def test_live_state_endpoint_returns_json_and_fragments(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

        state_url = reverse('competition_live_state', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(state_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('competition_status'), 'active')
        self.assertIn('hero_html', data)
        self.assertIn('judges_html', data)
        self.assertIn('audience_html', data)
        self.assertIn('standings_html', data)
        self.assertIn('projector_html', data)
        self.assertIn('rounds_html', data)
        self.assertIn('pitch_timer_duration_seconds', data)
        self.assertIn('pitch_timer_state', data)

    def test_countdown_uses_server_deadline_and_pause_resume_preserves_time(self):
        from service_domains.operations import (adjust_competition_pitch_timer,
            get_competition_live_state_context, toggle_competition_pitch_pause)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        now = timezone.now()
        claim = {'organization': self.org.as_claim(), 'user': self.owner}

        with patch('service_domains.operations.timezone.now', return_value=now):
            initial = get_competition_live_state_context(self.competition)
        self.assertEqual(initial['pitch_seconds_remaining'], 180)
        self.assertEqual(initial['pitch_timer_state'], 'running')

        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=40)):
            running = get_competition_live_state_context(self.competition)
        self.assertEqual(running['pitch_seconds_remaining'], 140)

        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=40)):
            toggle_competition_pitch_pause(self.competition, **claim)
        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=100)):
            paused = get_competition_live_state_context(self.competition)
        self.assertEqual(paused['pitch_seconds_remaining'], 140)
        self.assertEqual(paused['pitch_timer_state'], 'paused')

        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=100)):
            toggle_competition_pitch_pause(self.competition, **claim)
        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=110)):
            resumed = get_competition_live_state_context(self.competition)
        self.assertEqual(resumed['pitch_seconds_remaining'], 130)

        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=110)):
            adjust_competition_pitch_timer(self.competition, -30, **claim)
        with patch('service_domains.operations.timezone.now', return_value=now + timezone.timedelta(seconds=211)):
            expired = get_competition_live_state_context(self.competition)
        self.assertEqual(expired['pitch_seconds_remaining'], 0)
        self.assertEqual(expired['pitch_timer_state'], 'expired')

    def test_advancement_waits_for_all_scores_and_locked_weighted_results(self):
        from service_domains.operations import get_competition_live_state_context
        self.track.advancement_value = Decimal('2')
        self.track.save(update_fields=('advancement_value',))
        self.competition = start_competition(
            self.competition, organization=self.org.as_claim(), user=self.owner)

        waiting_for_audience = get_competition_live_state_context(self.competition)
        self.assertFalse(waiting_for_audience['can_advance'])
        self.assertIn('Close audience voting', waiting_for_audience['advancement_block_reason'])

        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))
        self.round_entry1.score = None
        self.round_entry1.save(update_fields=('score',))
        waiting_for_score = get_competition_live_state_context(self.competition)
        self.assertFalse(waiting_for_score['can_advance'])
        self.assertIn('Finalize scores for all entrants', waiting_for_score['advancement_block_reason'])

        self.round_entry1.score = Decimal('85.50')
        self.round_entry1.save(update_fields=('score',))
        ready = get_competition_live_state_context(self.competition)
        self.assertTrue(ready['can_advance'], ready['advancement_block_reason'])
        self.assertEqual(ready['next_round_qualifier_count'], 2)

    def test_audience_vote_gate_can_pause_and_reopen_public_join_code(self):
        from service_domains.operations import get_competition_live_state_context
        self.competition = start_competition(
            self.competition, organization=self.org.as_claim(), user=self.owner)
        context = get_competition_live_state_context(self.competition)
        code = context['join_code']
        action_url = reverse('competition_live_action', args=(self.competition.pk,))
        join_url = reverse('competition_audience_join')

        paused = self.client.post(action_url, {'action': 'toggle_audience_voting'},
                                  HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(paused.status_code, 200)
        self.assertIn('Ballots Locked', paused.json()['audience_html'])
        self.assertIn('Open Audience Ballots', paused.json()['audience_html'])
        rejected_join = self.client.post(join_url, {'code': code})
        self.assertEqual(rejected_join.status_code, 400)
        self.assertContains(rejected_join, 'That code is unavailable or voting has closed.', status_code=400)

        reopened = self.client.post(action_url, {'action': 'toggle_audience_voting'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(reopened.status_code, 200)
        accepted_join = self.client.post(join_url, {'code': code})
        self.assertEqual(accepted_join.status_code, 302)
        self.assertIn('/vote/', accepted_join['Location'])

    def test_weighted_results_finalize_from_live_results_and_next_round_reopens_voting(self):
        from service_domains.operations import (
            get_competition_live_state_context,
            get_or_create_competition_live_state,
            submit_competition_audience_ballot,
        )
        self.competition = start_competition(
            self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.refresh_from_db()
        self.judge.submitted_at = timezone.now()
        self.judge.save(update_fields=('submitted_at', 'updated_at'))
        token = CompetitionAudienceBallot.issue_device_token(
            self.competition, self.round1, self.track)
        submit_competition_audience_ballot(
            self.competition.public_id, self.round1.pk, self.track.pk,
            token, [self.entry1.pk, self.entry2.pk])
        live_state = get_or_create_competition_live_state(self.competition)
        live_state.audience_voting_open = False
        live_state.save(update_fields=('audience_voting_open', 'updated_at'))

        context = get_competition_live_state_context(self.competition)
        self.assertTrue(context['can_finalize_weighted'])
        self.assertContains(self.client.get(reverse(
            'competition_results', args=(self.competition.pk,))),
            'Close voting and finalize results')

        action_url = reverse('competition_live_action', args=(self.competition.pk,))
        finalized = self.client.post(action_url, {'action': 'finalize_weighted_scores'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(finalized.status_code, 200)
        self.assertTrue(finalized.json()['can_advance'])
        self.assertIn('Next round preview', finalized.json()['standings_html'])
        live_state.refresh_from_db()
        self.assertFalse(live_state.audience_voting_open)

        advanced = self.client.post(action_url, {
            'action': 'advance_round', 'next_round_name': 'Championship Round',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(advanced.status_code, 200)
        latest_round = CompetitionRound.objects.filter(
            competition=self.competition).order_by('-order').first()
        live_state.refresh_from_db()
        self.assertEqual(live_state.active_round_id, latest_round.pk)
        self.assertTrue(live_state.audience_voting_open)
        self.assertEqual(latest_round.name, 'Championship Round')

    def test_no_qualifiers_keeps_round_advancement_locked(self):
        from service_domains.operations import get_competition_live_state_context
        self.track.advancement_rule = 'threshold'
        self.track.advancement_value = Decimal('99')
        self.track.save(update_fields=('advancement_rule', 'advancement_value', 'updated_at'))
        self.competition.audience_weight = Decimal('0.00')
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        self.competition = start_competition(
            self.competition, organization=self.org.as_claim(), user=self.owner)

        context = get_competition_live_state_context(self.competition)

        self.assertEqual(context['next_round_qualifier_count'], 0)
        self.assertFalse(context['can_advance'])
        self.assertIn('select no entrants', context['advancement_block_reason'])

    def test_live_actions_dispatch_and_mutate_arena_state(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})

        # Action: adjust pitch timer
        res = self.client.post(action_url, {'action': 'adjust_pitch_timer', 'delta': 60})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('pitch_seconds_remaining'), 240)

        # Action: toggle pitch pause
        res = self.client.post(action_url, {'action': 'toggle_pitch_pause'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('timer_paused'))

        # Action: toggle audience voting
        res = self.client.post(action_url, {'action': 'toggle_audience_voting'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data.get('audience_voting_open'))

        # Action: set active entry
        res = self.client.post(action_url, {'action': 'set_active_entry', 'entry_id': str(self.entry2.pk)})
        self.assertEqual(res.status_code, 200)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.live_state.active_entry_id, self.entry2.pk)

        # Action: set projector mode
        res = self.client.post(action_url, {'action': 'set_projector_mode', 'mode': 'voting'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('projector_mode'), 'voting')

        # Action: nudge judge
        res = self.client.post(action_url, {'action': 'nudge_judge', 'judge_id': str(self.judge.pk)})
        self.assertEqual(res.status_code, 200)

    def test_nudge_emails_fresh_private_link_and_rotates_old_token(self):
        from service_domains.operations import resolve_competition_judge_invitation
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        old_token = 'sampletoken' * 4
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})

        response = self.client.post(action_url, {'action': 'nudge_judge', 'judge_id': self.judge.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        token = re.search(r'/competition/judge/([A-Za-z0-9_-]+)/', mail.outbox[0].body).group(1)
        self.assertIsNone(resolve_competition_judge_invitation(old_token))
        self.assertEqual(resolve_competition_judge_invitation(token).pk, self.judge.pk)

    def test_failed_nudge_keeps_existing_judge_link_valid(self):
        from service_domains.operations import resolve_competition_judge_invitation
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        old_token = 'sampletoken' * 4
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        with patch('service_domains.competition_views.send_mail', return_value=0):
            response = self.client.post(action_url, {'action': 'nudge_judge', 'judge_id': self.judge.pk},
                                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(resolve_competition_judge_invitation(old_token).pk, self.judge.pk)
        with patch('service_domains.competition_views.send_mail', return_value=1):
            retried = self.client.post(action_url, {
                'action': 'nudge_judge', 'judge_id': self.judge.pk},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(retried.status_code, 200)
        self.assertIsNone(resolve_competition_judge_invitation(old_token))

    def test_advancing_round_moves_live_arena_to_new_round(self):
        from service_domains.operations import (get_competition_live_state_context,
            get_or_create_competition_live_state)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))
        live_state = get_or_create_competition_live_state(self.competition)

        from service_domains.operations import create_next_competition_round
        next_round, _ = create_next_competition_round(self.competition, self.round1, 'Final',
            organization=self.org.as_claim(), user=self.owner)

        live_state.refresh_from_db()
        self.assertEqual(live_state.active_round_id, next_round.pk)
        self.assertEqual(get_competition_live_state_context(self.competition)['active_round'].pk,
                         next_round.pk)

    def test_live_standings_use_track_top_n_and_threshold_rules(self):
        from service_domains.models import CompetitionTrack
        from service_domains.operations import get_competition_live_state_context
        self.track.advancement_rule = 'top_n'
        self.track.advancement_value = Decimal('1')
        self.track.save()
        for order, score in ((3, '80.00'), (4, '70.00')):
            entry = CompetitionEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=self.track, name=f'Entry {order}', order=order)
            CompetitionRoundEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, round=self.round1, entry=entry, track=self.track,
                score=Decimal(score))
        threshold_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='ClimateTech',
            advancement_rule='threshold', advancement_value=Decimal('75'))
        for order, score in ((1, '85.00'), (2, '70.00')):
            entry = CompetitionEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=threshold_track, name=f'Climate Entry {order}', order=order)
            CompetitionRoundEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, round=self.round1, entry=entry, track=threshold_track,
                score=Decimal(score))
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

        context = get_competition_live_state_context(self.competition)

        qualified = {row['entry'].name for row in context['standings'] if row['is_qualified']}
        self.assertEqual(qualified, {'PulseGuard', 'Climate Entry 1'})

    def test_boundary_tie_remains_unresolved_until_advancement_decision(self):
        from service_domains.operations import get_competition_live_state_context
        self.track.advancement_rule = 'top_n'
        self.track.advancement_value = Decimal('1')
        self.track.save(update_fields=('advancement_rule', 'advancement_value'))
        self.round_entry1.score = self.round_entry2.score
        self.round_entry1.save(update_fields=('score',))
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

        context = get_competition_live_state_context(self.competition)

        self.assertTrue(context['has_boundary_tie'])
        self.assertTrue(context['unresolved_ties'])
        self.assertFalse(context['can_advance'])

    def test_boundary_tie_decision_is_consumed_by_round_advancement(self):
        from service_domains.models import CompetitionAdvancementDecision
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.round_entry1.score = Decimal('92.00')
        self.round_entry1.save(update_fields=('score',))
        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})

        response = self.client.post(action_url, {
            'action': 'advance_round', 'next_round_name': 'Final',
            'track_id': self.track.pk, 'reason': 'Head judge decision',
            'ordered_entry_ids': [self.entry1.pk]}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200, response.content)
        decision = CompetitionAdvancementDecision.objects.get(source_round=self.round1,
            track=self.track)
        self.assertEqual(decision.head_judge_decision, [self.entry1.pk])
        self.assertEqual(list(CompetitionRoundEntry.objects.filter(
            round=decision.next_round).values_list('entry_id', flat=True)), [self.entry1.pk])

    def test_projector_toggle_view(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        toggle_url = reverse('competition_live_projector_toggle', kwargs={'competition_id': self.competition.pk})
        res = self.client.post(toggle_url, {'mode': 'standings'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get('projector_mode'), 'standings')

    def test_tenant_isolation_denies_cross_organization_access(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        other_client = self.client_class()
        sign_in(other_client, self.other, self.other_org)

        url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        res = other_client.get(url)
        self.assertEqual(res.status_code, 404)

    def test_competition_projector_screen_renders_venue_modes(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        proj_url = reverse('competition_projector', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(proj_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Live Stage')
        self.assertContains(res, self.competition.name)
        self.assertContains(res, 'Presenting on Stage')

        # Test with voting mode
        self.competition.live_state.projector_mode = 'voting'
        self.competition.live_state.save()
        res_voting = self.client.get(proj_url)
        self.assertEqual(res_voting.status_code, 200)
        self.assertContains(res_voting, 'Cast Your Ballot')

        # Test with standings mode
        self.competition.live_state.projector_mode = 'standings'
        self.competition.live_state.save()
        res_standings = self.client.get(proj_url)
        self.assertEqual(res_standings.status_code, 200)
        self.assertContains(res_standings, 'Round Leaderboard')

        # Test with winner mode
        self.competition.live_state.projector_mode = 'winner'
        self.competition.live_state.save()
        res_winner = self.client.get(proj_url)
        self.assertEqual(res_winner.status_code, 200)
        self.assertContains(res_winner, 'Tournament Champions')

        state_response = self.client.get(reverse(
            'competition_projector_state', args=(self.competition.pk,)))
        self.assertEqual(state_response.status_code, 200)
        self.assertIn('id="projector-screen-content"', state_response.json()['projector_screen_html'])

    def test_competition_projector_qr_generates_png(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        qr_url = reverse('competition_projector_qr', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(qr_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')

        live_url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        live = self.client.get(live_url)
        self.assertTrue(live.context['audience_ballot_available'])
        code = live.context['join_code']
        with patch('service_domains.competition_views.allow_public_event_request', return_value=True):
            joined = self.client.post(reverse('competition_audience_join'), {'code': code})
        self.assertEqual(joined.status_code, 302)
        self.assertEqual(joined['Location'], reverse('competition_audience_vote', args=(
            self.competition.public_id, self.round1.pk, self.track.pk)))

    def test_ineligible_single_entry_track_does_not_show_an_audience_code_or_qr(self):
        self.round_entry2.delete()
        self.competition.audience_weight = Decimal('0.00')
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

        live_url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        live = self.client.get(live_url)
        self.assertFalse(live.context['audience_ballot_available'])
        self.assertIsNone(live.context['join_code'])
        self.assertContains(live, 'Ballot Unavailable')
        self.assertContains(live, 'Audience ranked voting requires at least two entries in this track.')
        self.assertNotContains(live, 'Public Join Code')
        self.assertContains(live, 'Voting unavailable')
        self.assertContains(live, 'disabled aria-describedby="competition-voting-unavailable-reason"')

        self.competition.live_state.projector_mode = 'voting'
        self.competition.live_state.save(update_fields=('projector_mode', 'updated_at'))
        projector_url = reverse('competition_projector', kwargs={'competition_id': self.competition.pk})
        projector = self.client.get(projector_url)
        self.assertContains(projector, 'Audience Ballot Unavailable')
        self.assertContains(projector, 'Audience ranked voting requires at least two entries in this track.')
        self.assertNotContains(projector, reverse('competition_projector_qr', args=(self.competition.pk,)))

        qr_url = reverse('competition_projector_qr', kwargs={'competition_id': self.competition.pk})
        self.assertEqual(self.client.get(qr_url).status_code, 404)

    def test_live_state_hides_codes_for_stale_locked_and_non_live_rounds(self):
        from service_domains.operations import (get_competition_live_state_context,
            get_or_create_competition_live_state)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.refresh_from_db()
        live_state = get_or_create_competition_live_state(self.competition)
        next_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Final', order=2)

        context = get_competition_live_state_context(self.competition)
        self.assertFalse(context['audience_ballot_available'])
        self.assertFalse(context['active_round_is_latest'])
        self.assertFalse(context['can_advance'])
        self.assertIn('earlier round', context['advancement_block_reason'])
        self.assertEqual(context['audience_ballot_unavailable_reason'],
            'Audience voting is only available for the current round.')

        live_state.active_round = next_round
        live_state.active_entry = None
        live_state.save(update_fields=('active_round', 'active_entry', 'updated_at'))
        next_round.audience_locked_at = timezone.now()
        next_round.save(update_fields=('audience_locked_at', 'updated_at'))
        context = get_competition_live_state_context(self.competition)
        self.assertFalse(context['audience_ballot_available'])
        self.assertEqual(context['audience_ballot_unavailable_reason'], 'Voting is closed for this round.')

        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        context = get_competition_live_state_context(self.competition)
        self.assertFalse(context['audience_ballot_available'])
        self.assertEqual(context['audience_ballot_unavailable_reason'],
            'Voting is unavailable because this competition is not live.')

    def test_weighted_ballot_needs_at_least_one_entry_and_a_code(self):
        from service_domains.operations import get_competition_live_state_context
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.refresh_from_db()
        CompetitionRoundEntry.objects.filter(round=self.round1).delete()

        context = get_competition_live_state_context(self.competition)

        self.assertFalse(context['audience_ballot_available'])
        self.assertIsNone(context['join_code'])
        self.assertEqual(context['audience_ballot_unavailable_reason'],
            'This track needs at least one entrant before audience voting is available.')

    def test_code_issuance_failure_does_not_create_a_fake_join_code(self):
        from django.core.exceptions import ValidationError
        from service_domains.operations import get_competition_live_state_context
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.refresh_from_db()

        with patch('service_domains.operations.competition_audience_join_code',
                   side_effect=ValidationError('Code unavailable')):
            context = get_competition_live_state_context(self.competition)

        self.assertFalse(context['audience_ballot_available'])
        self.assertIsNone(context['join_code'])
        self.assertEqual(context['audience_ballot_unavailable_reason'],
            'An audience ballot code could not be issued. Please try again.')

    def test_live_action_conclude_standard_post_redirects(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.judge.submitted_at = timezone.now()
        self.judge.save(update_fields=('submitted_at',))
        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        res = self.client.post(action_url, {'action': 'conclude_competition'})
        self.assertEqual(res.status_code, 302)
        self.assertIn(reverse('competition_results', args=(self.competition.pk,)), res['Location'])
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.COMPLETE)

    def test_live_action_advance_round_standard_post_redirects(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        res = self.client.post(action_url, {
            'action': 'advance_round',
            'next_round_name': 'Championship Round',
        })
        self.assertEqual(res.status_code, 302)
        self.assertIn(reverse('competition_live_control', args=(self.competition.pk,)), res['Location'])
        self.assertEqual(self.competition.rounds.count(), 2)

    def test_audience_vote_when_voting_locked_shows_waiting_message(self):
        from unittest.mock import patch
        from service_domains.operations import get_or_create_competition_live_state
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        live_state = get_or_create_competition_live_state(self.competition)
        live_state.audience_voting_open = False
        live_state.save()

        vote_url = reverse('competition_audience_vote', kwargs={
            'competition_id': self.competition.public_id,
            'round_id': self.round1.pk,
            'track_id': self.track.pk,
        })
        anon_client = self.client_class()
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            res = anon_client.get(vote_url)
            self.assertEqual(res.status_code, 200)
            self.assertContains(res, 'Audience Voting Paused')

            post_res = anon_client.post(vote_url, {'rank_1': '1'})
            self.assertEqual(post_res.status_code, 400)
            self.assertContains(post_res, 'Audience voting is currently paused or closed', status_code=400)

    def test_entry_form_omitted_order_auto_increments(self):
        from service_domains.competition_forms import CompetitionEntryForm
        form = CompetitionEntryForm(
            data={'track': self.track.pk, 'name': 'BioSynth AI', 'reference': 'B-101'},
            competition=self.competition,
            organization=self.org.as_claim(),
            user=self.owner,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['order'], 3)

    def test_judge_invitation_omitted_expiry_defaults_to_future(self):
        from service_domains.competition_forms import CompetitionJudgeInvitationForm
        form = CompetitionJudgeInvitationForm(
            data={'track': self.track.pk, 'email': 'guest-judge@example.com'},
            organization=self.org.as_claim(),
            user=self.owner,
            competition=self.competition,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data['expires_at'])
        self.assertGreater(form.cleaned_data['expires_at'], timezone.now())
