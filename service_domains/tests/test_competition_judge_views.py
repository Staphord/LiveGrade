from datetime import timedelta
import re
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (Competition, CompetitionEntry,
    CompetitionJudgeInvitation, CompetitionJudgeScore, CompetitionRound,
    CompetitionRoundEntry, CompetitionTrack)
from service_domains.operations import issue_competition_judge_invitation
from service_domains.operations import (create_initial_competition_round,
    start_competition)


class CompetitionJudgeScorecardViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Competition judge view org')
        self.other_org = make_org('Other competition judge view org')
        self.owner = make_user('competition-judge-view-owner')
        self.other = make_user('competition-judge-view-other')
        self.competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Innovation Cup', status=Competition.Status.ACTIVE)
        self.track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        self.round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Final', order=1)
        self.rows = []
        for index in range(3):
            entry = CompetitionEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=self.track, name=f'Entry {index + 1}', order=index)
            self.rows.append(CompetitionRoundEntry.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                round=self.round, entry=entry, track=self.track))

    def request(self, client, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            return getattr(client, method)(url, *args, **kwargs)

    def invite(self, email='judge@example.org'):
        return issue_competition_judge_invitation(self.competition, self.round, self.track,
            email, timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)

    def score_url(self, token):
        return reverse('competition_judge_scorecard', kwargs={'token': token})

    def test_invited_judge_submits_complete_private_scorecard_once(self):
        invitation, token = self.invite()
        client = Client()
        page = self.request(client, 'get', self.score_url(token))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Entry 1')
        self.assertContains(page, 'Score from 0 to 100')
        self.assertNotContains(page, invitation.email)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        self.assertIn('private, no-store', page['Cache-Control'])
        data = {f'entry_{row.pk}': str(75 + index) for index, row in enumerate(self.rows)}
        submitted = self.request(client, 'post', self.score_url(token), data)
        self.assertContains(submitted, 'Scorecard submitted')
        self.assertEqual(CompetitionJudgeScore.objects.count(), 3)
        self.assertEqual(list(CompetitionJudgeScore.objects.order_by('round_entry_id')
            .values_list('score', flat=True)), [75, 76, 77])
        self.assertIsNotNone(invitation.__class__.objects.get(pk=invitation.pk).submitted_at)
        receipt = self.request(client, 'get', self.score_url(token))
        self.assertContains(receipt, 'Scorecard submitted')
        self.assertNotContains(receipt, 'Score from 0 to 100')
        retry = self.request(client, 'post', self.score_url(token), data)
        self.assertContains(retry, 'Scorecard submitted')
        changed = {**data, f'entry_{self.rows[0].pk}': '99'}
        rejected = self.request(client, 'post', self.score_url(token), changed)
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, 'already submitted and cannot be changed', status_code=400)
        incomplete_retry = self.request(client, 'post', self.score_url(token), {
            f'entry_{self.rows[0].pk}': '75'})
        self.assertEqual(incomplete_retry.status_code, 400)
        self.assertContains(incomplete_retry, 'no further changes are accepted', status_code=400)
        self.assertEqual(CompetitionJudgeScore.objects.count(), 3)
        self.assertEqual(list(CompetitionJudgeScore.objects.order_by('round_entry_id')
            .values_list('score', flat=True)), [75, 76, 77])

    def test_json_transport_returns_csrf_and_accepts_idempotent_offline_retry(self):
        invitation, token = self.invite()
        client = Client()
        url = self.score_url(token)
        headers = {'HTTP_ACCEPT': 'application/json'}
        state = self.request(client, 'get', url, **headers)
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()['status'], 'open')
        self.assertTrue(state.json()['csrf_token'])
        self.assertIn('private', state['Cache-Control'])
        self.assertIn('no-store', state['Cache-Control'])

        data = {f'entry_{row.pk}': str(60 + index) for index, row in enumerate(self.rows)}
        accepted = self.request(client, 'post', url, data, **headers)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()['status'], 'accepted')
        self.assertEqual(accepted.json()['next_url'], url)
        self.assertEqual(CompetitionJudgeScore.objects.filter(invitation=invitation).count(), 3)

        state = self.request(client, 'get', url, **headers)
        self.assertEqual(state.json()['status'], 'submitted')
        replay = self.request(client, 'post', url, data, **headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['status'], 'accepted')
        self.assertEqual(CompetitionJudgeScore.objects.filter(invitation=invitation).count(), 3)

        changed = {**data, f'entry_{self.rows[0].pk}': '99'}
        rejected = self.request(client, 'post', url, changed, **headers)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()['status'], 'rejected')
        self.assertEqual(list(CompetitionJudgeScore.objects.filter(invitation=invitation)
            .order_by('round_entry_id').values_list('score', flat=True)), [60, 61, 62])

    def test_invalid_and_unknown_judge_links_fail_closed(self):
        client = Client()
        self.assertEqual(self.request(client, 'get', self.score_url('unknown-token')).status_code, 404)
        with patch('service_domains.competition_judge_views.allow_unknown_bearer_request',
                   return_value=False):
            limited = self.request(client, 'get', self.score_url('unknown-token'))
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited)
        with patch('service_domains.competition_judge_views.allow_unknown_bearer_request',
                   return_value=None):
            unavailable = self.request(client, 'get', self.score_url('unknown-token'))
        self.assertEqual(unavailable.status_code, 503)
        invitation, token = self.invite()
        invalid = self.request(client, 'post', self.score_url(token), {
            f'entry_{self.rows[0].pk}': '120',
            f'entry_{self.rows[1].pk}': '80',
            f'entry_{self.rows[2].pk}': '70',
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(CompetitionJudgeScore.objects.count(), 0)
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=('revoked_at', 'updated_at'))
        self.assertEqual(self.request(client, 'get', self.score_url(token)).status_code, 404)

    def test_owner_invites_judge_and_tallies_private_scores(self):
        sign_in(self.client, self.owner, self.org)
        create = self.request(self.client, 'post', reverse('competition_create'), {
            'name': 'Needs round', 'audience_weight': '0'})
        self.assertEqual(create.status_code, 302)

        draft = Competition.objects.get(name='Needs round')
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=draft, name='Open')
        entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name='One')
        round_obj = create_initial_competition_round(draft, 'Final',
            organization=self.org.as_claim(), user=self.owner)
        start_competition(draft, organization=self.org.as_claim(), user=self.owner)
        response = self.request(self.client, 'post', reverse('competition_manage', args=(draft.pk,)), {
            'action': 'invite_judge', 'track': str(track.pk), 'email': 'invited@example.org',
            'expires_at': (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Judge invitation emailed')
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotContains(response, '/competition/judge/')
        self.assertIn('/competition/judge/', mail.outbox[-1].body)
        self.assertEqual(round_obj.round_entries.count(), 1)
        self.assertEqual(entry.name, 'One')

        token = re.search(r'/competition/judge/([A-Za-z0-9_-]+)/', mail.outbox[-1].body).group(1)
        score_url = self.score_url(token)
        score_page = self.request(self.client, 'get', score_url)
        self.assertEqual(score_page.status_code, 200)
        scored = self.request(self.client, 'post', score_url, {
            f'entry_{round_obj.round_entries.get().pk}': '88.50'})
        self.assertContains(scored, 'Scorecard submitted')
        tally = self.request(self.client, 'post', reverse('competition_manage', args=(draft.pk,)), {
            'action': 'tally_judges', 'track_id': str(track.pk)})
        self.assertContains(tally, 'Judge means recorded for 1 entries.')
        self.assertContains(tally, '88.50')
        self.assertContains(tally, 'Judge count')

    def test_owner_gets_one_time_invitation_link_only_when_email_delivery_fails(self):
        sign_in(self.client, self.owner, self.org)
        with patch('service_domains.competition_views.send_mail', side_effect=RuntimeError('offline')):
            fallback = self.request(self.client, 'post', reverse('competition_manage', args=(self.competition.pk,)), {
                'action': 'invite_judge', 'track': str(self.track.pk), 'email': 'backup@example.org',
                'expires_at': (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
            })
        self.assertContains(fallback, 'Copy this private link now')
        self.assertContains(fallback, '/competition/judge/')
        with patch('service_domains.competition_views.send_mail', return_value=1):
            emailed = self.request(self.client, 'post', reverse('competition_manage', args=(self.competition.pk,)), {
                'action': 'invite_judge', 'track': str(self.track.pk), 'email': 'again@example.org',
                'expires_at': (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
            })
        self.assertNotContains(emailed, '/competition/judge/')

    def test_owner_can_revoke_an_unsubmitted_invitation(self):
        sign_in(self.client, self.owner, self.org)
        invitation, _ = self.invite()
        response = self.request(self.client, 'post', reverse('competition_manage', args=(
            self.competition.pk,)), {'action': 'revoke_judge', 'invitation_id': invitation.pk})
        self.assertContains(response, 'Judge invitation revoked.')
        self.assertIsNotNone(CompetitionJudgeInvitation.objects.get(pk=invitation.pk).revoked_at)

    def test_competition_manager_renders_judge_action_errors_and_fails_closed(self):
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))

        invalid_revoke = self.request(self.client, 'post', manage, {
            'action': 'revoke_judge', 'invitation_id': 'missing'})
        self.assertEqual(invalid_revoke.status_code, 400)
        self.assertContains(invalid_revoke, 'This invitation cannot be revoked', status_code=400)

        invalid_tally_track = self.request(self.client, 'post', manage, {
            'action': 'tally_judges', 'track_id': '99999999'})
        self.assertEqual(invalid_tally_track.status_code, 404)

        with patch('service_domains.competition_views.compile_competition_judge_scores',
                   side_effect=PermissionDenied):
            denied_tally = self.request(self.client, 'post', manage, {
                'action': 'tally_judges', 'track_id': self.track.pk})
        self.assertEqual(denied_tally.status_code, 400)
        self.assertContains(denied_tally, 'Judge scores could not be tallied', status_code=400)

        invalid_invite = self.request(self.client, 'post', manage, {
            'action': 'invite_judge', 'track': self.track.pk,
            'email': 'not-an-email', 'expires_at': (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
        })
        self.assertEqual(invalid_invite.status_code, 400)
        self.assertContains(invalid_invite, 'Enter a valid email address', status_code=400)

        with patch('service_domains.competition_views.issue_competition_judge_invitation',
                   side_effect=PermissionDenied):
            denied_invite = self.request(self.client, 'post', manage, {
                'action': 'invite_judge', 'track': self.track.pk,
                'email': 'judge@example.org',
                'expires_at': (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            })
        self.assertEqual(denied_invite.status_code, 400)
        self.assertContains(denied_invite, 'This competition cannot accept judge invitations', status_code=400)

        no_round = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='No round event', status=Competition.Status.ACTIVE)
        no_round_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=no_round, name='No round track')
        no_round_response = self.request(self.client, 'post', reverse(
            'competition_manage', args=(no_round.pk,)), {
                'action': 'invite_judge', 'track': no_round_track.pk,
                'email': 'judge@example.org',
                'expires_at': (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            })
        self.assertEqual(no_round_response.status_code, 400)
        self.assertContains(no_round_response, 'Create a round before inviting judges', status_code=400)

    def test_judge_limiter_fails_closed_and_returns_retry_after(self):
        invitation, token = self.invite()
        url = self.score_url(token)
        with patch('service_domains.competition_judge_views.allow_competition_judge_request',
                   return_value=None):
            unavailable = self.request(Client(), 'get', url)
        self.assertEqual(unavailable.status_code, 503)
        with patch('service_domains.competition_judge_views.allow_competition_judge_request',
                   return_value=False):
            limited = self.request(Client(), 'get', url)
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited)

    def test_judge_view_rejects_other_service_and_renders_submit_errors(self):
        from service_domains.competition_judge_views import competition_judge_scorecard

        invitation, token = self.invite()
        request = RequestFactory().get(self.score_url(token))
        request.service = 'panel'
        with self.assertRaises(Http404):
            competition_judge_scorecard(request, token)

        valid_data = {f'entry_{row.pk}': '75' for row in self.rows}
        with patch('service_domains.competition_judge_views.submit_competition_judge_scores',
                   side_effect=PermissionDenied):
            denied = self.request(Client(), 'post', self.score_url(token), valid_data)
        self.assertEqual(denied.status_code, 404)
        json_client = Client()
        with patch('service_domains.competition_judge_views.submit_competition_judge_scores',
                   side_effect=PermissionDenied):
            json_denied = self.request(json_client, 'post', self.score_url(token), valid_data,
                HTTP_ACCEPT='application/json')
        self.assertEqual(json_denied.status_code, 403)
        self.assertEqual(json_denied.json()['status'], 'rejected')
        self.assertEqual(json_denied.json()['code'], 'invitation_unavailable')
        with patch('service_domains.competition_judge_views.submit_competition_judge_scores',
                   side_effect=ValidationError('Scorecard changed; reload the page.')):
            rejected = self.request(Client(), 'post', self.score_url(token), valid_data)
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, 'Scorecard changed; reload the page.', status_code=400)

    def test_limiter_handles_cache_window_reset_and_fail_closed_errors(self):
        from service_domains.competition_judge_views import allow_competition_judge_request

        invitation, _ = self.invite()
        with patch('service_domains.competition_judge_views.cache') as cache:
            cache.add.return_value = True
            self.assertTrue(allow_competition_judge_request(invitation))
            cache.add.return_value = False
            cache.incr.return_value = 1
            self.assertTrue(allow_competition_judge_request(invitation))
            cache.incr.return_value = 121
            self.assertFalse(allow_competition_judge_request(invitation))
            cache.incr.side_effect = ValueError
            cache.add.side_effect = [False, True]
            self.assertTrue(allow_competition_judge_request(invitation))
            cache.add.side_effect = RuntimeError('cache unavailable')
            self.assertIsNone(allow_competition_judge_request(invitation))
