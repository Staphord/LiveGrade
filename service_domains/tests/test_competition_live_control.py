"""Competition Arena Live Control Console tests."""
from decimal import Decimal
import uuid
from django.test import TestCase, override_settings
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


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'polls', 'team', 'showcase', 'competition'])
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

    def test_draft_arena_requires_criteria_that_total_100(self):
        from service_domains.models import CompetitionCriterion
        CompetitionCriterion.objects.create(track=self.track, name='Idea', weight=Decimal('60'),
            organization_id=self.org.id, created_by=self.owner)
        res = self.client.get(reverse('competition_live_control',
                                      kwargs={'competition_id': self.competition.pk}))
        self.assertFalse(res.context['can_start'])
        self.assertContains(res, 'must total exactly 100%')

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

    def test_competition_projector_qr_generates_png(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        qr_url = reverse('competition_projector_qr', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(qr_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')

    def test_live_action_conclude_standard_post_redirects(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.judge.submitted_at = timezone.now()
        self.judge.save(update_fields=('submitted_at',))
        self.round1.audience_locked_at = timezone.now()
        self.round1.save(update_fields=('audience_locked_at',))
        action_url = reverse('competition_live_action', kwargs={'competition_id': self.competition.pk})
        res = self.client.post(action_url, {'action': 'conclude_competition'})
        self.assertEqual(res.status_code, 302)
        self.assertIn(reverse('competition_manage', args=(self.competition.pk,)), res['Location'])
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
