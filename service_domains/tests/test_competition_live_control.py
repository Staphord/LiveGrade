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

    def test_draft_redirects_to_manage(self):
        url = reverse('competition_live_control', kwargs={'competition_id': self.competition.pk})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 302)
        self.assertIn(reverse('competition_manage', args=(self.competition.pk,)), res['Location'])

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
