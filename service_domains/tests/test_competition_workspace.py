from unittest.mock import patch
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (Competition, CompetitionEntry, CompetitionRound,
                                    CompetitionAudienceBallot,
                                    CompetitionJudgeInvitation,
                                    CompetitionAdvancementDecision, CompetitionRoundEntry,
                                    CompetitionTrack,
                                    CompetitionLeaderboardTieDecision,
                                    CompetitionTransitionAudit,
                                    CompetitionLeaderboardAudit,
                                    CompetitionCriterion,
                                    CompetitionAudienceJoinCode)
from service_domains.models import CompetitionWeightedResultAudit
from service_domains.operations import (create_initial_competition_round,
                                        compile_competition_judge_scores,
                                        issue_competition_judge_invitation,
                                        start_competition,
                                        complete_competition,
                                        reveal_competition_leaderboard,
                                        competition_final_tie_requirements,
                                        resolve_competition_final_ties,
                                        submit_competition_judge_scores,
                                        submit_competition_audience_ballot,
                                        finalize_competition_weighted_scores)
from service_domains.operations import set_competition_public_leaderboard
from service_domains.operations import competition_audience_join_code
from service_domains.competition_forms import (CompetitionFinalTieForm,
    competition_head_judge_requirements)


class CompetitionWorkspaceTests(TestCase):
    def setUp(self):
        self.org = make_org('Competition workspace org')
        self.other_org = make_org('Other competition workspace org')
        self.owner = make_user('competition-workspace-owner')
        self.other_owner = make_user('competition-workspace-other')
        self.competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Innovation challenge')

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def active_scored_round(self, scores, *, audience_weight=0):
        self.competition.audience_weight = audience_weight
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open',
            advancement_rule='top_n', advancement_value=2)
        entries = [CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name=f'Entry {index + 1}', order=index)
            for index in range(len(scores))]
        source = create_initial_competition_round(self.competition, 'Round 1',
            organization=self.org.as_claim(), user=self.owner)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        _, token = issue_competition_judge_invitation(
            self.competition, source, track, 'judge@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner)
        rows = list(source.round_entries.order_by('entry_id'))
        submit_competition_judge_scores(token, {
            str(row.pk): str(score) for row, score in zip(rows, scores)})
        compile_competition_judge_scores(self.competition, source, track,
            organization=self.org.as_claim(), user=self.owner)
        return track, entries, source

    def test_weighted_audience_scores_are_normalized_frozen_and_published_with_components(self):
        track, entries, source = self.active_scored_round([80, 60], audience_weight=20)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.workflow_snapshot['audience_method'],
            'complete_ranking_borda_0_100_v1')
        self.assertEqual(self.competition.effective_audience_weight, Decimal('20'))
        self.competition.audience_weight = 0
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.effective_audience_weight, Decimal('20'))
        token = CompetitionAudienceBallot.issue_device_token(self.competition, source, track)
        with self.assertRaisesMessage(ValidationError, 'rank every entry'):
            submit_competition_audience_ballot(self.competition.public_id, source.pk,
                track.pk, token, [entries[0].pk])
        submit_competition_audience_ballot(self.competition.public_id, source.pk,
            track.pk, token, [entries[0].pk, entries[1].pk])
        finalized = finalize_competition_weighted_scores(self.competition, source,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual([row.judge_score for row in finalized], [Decimal('80.00'), Decimal('60.00')])
        self.assertEqual([row.audience_score for row in finalized], [Decimal('100.00'), Decimal('0.00')])
        self.assertEqual([row.score for row in finalized], [Decimal('84.00'), Decimal('48.00')])
        audit = CompetitionWeightedResultAudit.objects.get(round=source)
        self.assertEqual(audit.audience_ballot_count, 1)
        self.assertEqual(audit.audience_weight, Decimal('20'))
        source.refresh_from_db()
        self.assertIsNotNone(source.audience_locked_at)
        with self.assertRaisesMessage(ValidationError, 'already been finalized'):
            finalize_competition_weighted_scores(self.competition, source,
                organization=self.org.as_claim(), user=self.owner)
        another_token = CompetitionAudienceBallot.issue_device_token(self.competition, source, track)
        with self.assertRaisesMessage(ValidationError, 'voting is closed'):
            submit_competition_audience_ballot(self.competition.public_id, source.pk,
                track.pk, another_token, [entries[0].pk, entries[1].pk])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition, organization=self.org.as_claim(), user=self.owner)
        sign_in(self.client, self.owner, self.org)
        page = self.request('get', reverse('competition_public_leaderboard',
            args=(self.competition.public_id,)))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '80.00% judge scores and 20.00% audience rankings')
        self.assertContains(page, '84.00')
        self.assertContains(page, '100.00')

    def test_weighted_audience_ballot_supports_a_single_entry_track(self):
        track, entries, round_obj = self.active_scored_round([80], audience_weight=20)
        sign_in(self.client, self.owner, self.org)
        ballot_url = reverse('competition_audience_vote', kwargs={
            'competition_id': self.competition.public_id,
            'round_id': round_obj.pk,
            'track_id': track.pk,
        })

        page = self.request('get', ballot_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'Rank: {entries[0].name}')
        self.assertContains(page, 'rank every entry')
        submitted = self.request('post', ballot_url, {
            f'rank_{entries[0].pk}': '1',
        })
        self.assertEqual(submitted.status_code, 200)
        self.assertContains(submitted, 'Your ranked ballot has been recorded.')

        finalized = finalize_competition_weighted_scores(self.competition, round_obj,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(finalized[0].audience_score, Decimal('100.00'))
        self.assertEqual(finalized[0].score, Decimal('84.00'))

    def test_declared_judge_and_audience_coverage_floors_are_enforced(self):
        self.competition.audience_weight = 25
        self.competition.minimum_judges_per_entry = 2
        self.competition.minimum_audience_ballots_per_track = 2
        self.competition.save(update_fields=('audience_weight', 'minimum_judges_per_entry',
            'minimum_audience_ballots_per_track', 'updated_at'))
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        entries = [CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name=f'Entry {index}', order=index)
            for index in range(2)]
        round_obj = create_initial_competition_round(self.competition, 'Round 1',
            organization=self.org.as_claim(), user=self.owner)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        rows = list(round_obj.round_entries.order_by('entry_id'))
        expiry = timezone.now() + timedelta(hours=1)
        for index in range(2):
            _, judge_token = issue_competition_judge_invitation(self.competition,
                round_obj, track, f'judge{index}@example.org', expiry,
                organization=self.org.as_claim(), user=self.owner)
            submit_competition_judge_scores(judge_token,
                {str(row.pk): str(80 - index * 10) for row in rows})
            if index == 0:
                with self.assertRaisesMessage(ValidationError, 'at least 2 judge scores'):
                    compile_competition_judge_scores(self.competition, round_obj, track,
                        organization=self.org.as_claim(), user=self.owner)
        compile_competition_judge_scores(self.competition, round_obj, track,
            organization=self.org.as_claim(), user=self.owner)
        for index in range(2):
            device = CompetitionAudienceBallot.issue_device_token(self.competition, round_obj, track)
            submit_competition_audience_ballot(self.competition.public_id, round_obj.pk,
                track.pk, device, [entry.pk for entry in entries])
            if index == 0:
                with self.assertRaisesMessage(ValidationError, 'at least 2 audience ballots'):
                    finalize_competition_weighted_scores(self.competition, round_obj,
                        organization=self.org.as_claim(), user=self.owner)
        finalize_competition_weighted_scores(self.competition, round_obj,
            organization=self.org.as_claim(), user=self.owner)

    def test_audience_only_competition_needs_no_judges_and_can_reach_public_results(self):
        self.competition.audience_weight = 100
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        entries = [CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name=f'Entry {index}', order=index)
            for index in range(2)]
        round_obj = create_initial_competition_round(self.competition, 'Final',
            organization=self.org.as_claim(), user=self.owner)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(self.competition.effective_audience_weight, Decimal('100'))
        self.assertEqual(self.competition.judge_weight, Decimal('0'))
        with self.assertRaisesMessage(ValidationError, 'not used for a 100% audience-scored event'):
            issue_competition_judge_invitation(self.competition, round_obj, track,
                'judge@example.org', timezone.now() + timedelta(hours=1),
                organization=self.org.as_claim(), user=self.owner)
        device = CompetitionAudienceBallot.issue_device_token(self.competition, round_obj, track)
        submit_competition_audience_ballot(self.competition.public_id, round_obj.pk,
            track.pk, device, [entries[1].pk, entries[0].pk])
        rows = finalize_competition_weighted_scores(self.competition, round_obj,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual([row.score for row in rows], [Decimal('0.00'), Decimal('100.00')])
        self.assertTrue(all(row.judge_score is None for row in rows))
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition, organization=self.org.as_claim(), user=self.owner)
        sign_in(self.client, self.owner, self.org)
        manage = self.request('get', reverse('competition_manage', args=(self.competition.pk,)))
        self.assertIsNone(manage.context['judge_form'])
        page = self.request('get', reverse('competition_public_leaderboard',
            args=(self.competition.public_id,)))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '0.00% judge scores and 100.00% audience rankings')
        self.assertContains(page, 'Audience score:')

    def test_organizer_weighted_rubric_scores_are_applied_and_retained(self):
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        criteria = [
            CompetitionCriterion.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=track, name='Evidence', weight=70, order=0),
            CompetitionCriterion.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=track, name='Delivery', weight=30, order=1),
        ]
        entries = [CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name=f'Entry {index}', order=index)
            for index in range(2)]
        round_obj = create_initial_competition_round(self.competition, 'Final',
            organization=self.org.as_claim(), user=self.owner)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        _, token = issue_competition_judge_invitation(self.competition, round_obj, track,
            'judge@example.org', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        rows = list(round_obj.round_entries.order_by('entry__order'))
        submit_competition_judge_scores(token, {
            str(rows[0].pk): {str(criteria[0].pk): '80', str(criteria[1].pk): '60'},
            str(rows[1].pk): {str(criteria[0].pk): '70', str(criteria[1].pk): '100'},
        })
        compile_competition_judge_scores(self.competition, round_obj, track,
            organization=self.org.as_claim(), user=self.owner)
        rows = list(round_obj.round_entries.order_by('entry__order'))
        self.assertEqual([row.judge_score for row in rows],
            [Decimal('74.00'), Decimal('79.00')])
        self.assertEqual(rows[0].score_breakdown, [
            {'criterion': 'Evidence', 'weight': '70.00', 'mean': '80.00', 'judge_count': 1},
            {'criterion': 'Delivery', 'weight': '30.00', 'mean': '60.00', 'judge_count': 1},
        ])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        public = self.request('get', reverse('competition_public_leaderboard',
            args=(self.competition.public_id,)), HTTP_ACCEPT='application/json')
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()['track_results'][0]['entries'][0]['judge_count'], 1)

    def test_workspace_fails_closed_while_competition_service_is_unreleased(self):
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', return_value=False):
            self.assertEqual(self.client.get(reverse('competition_list')).status_code, 404)
            self.assertEqual(self.client.get(reverse('competition_manage', args=(self.competition.pk,))).status_code, 404)

    def test_list_and_invalid_competition_model_error_are_rendered(self):
        sign_in(self.client, self.owner, self.org)
        empty = self.request('get', reverse('competition_list'))
        self.assertEqual(empty.status_code, 200)
        self.assertContains(empty, 'Getting started')
        self.assertContains(empty, 'Your Competition workspace')
        self.assertNotContains(empty, 'Set up your first competition')
        self.assertContains(empty, 'Add tracks and entries')
        self.assertContains(empty, 'The public leaderboard remains hidden')
        self.assertContains(empty, 'href="#create-competition"')
        invalid = self.request('post', reverse('competition_list'), {'name': ''})
        self.assertContains(invalid, 'This field is required')

        original = Competition.full_clean
        calls = 0

        def fail_after_form_validation(instance, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValidationError('Competition model refused the draft.')
            return original(instance, *args, **kwargs)

        with patch.object(Competition, 'full_clean', new=fail_after_form_validation):
            refused = self.request('post', reverse('competition_list'), {'name': 'Refused'})
        self.assertContains(refused, 'Competition model refused the draft')

    def test_first_time_owner_sees_getting_started_guidance(self):
        self.competition.delete()
        sign_in(self.client, self.owner, self.org)

        response = self.request('get', reverse('competition_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Getting started')
        self.assertContains(response, 'Set up your first competition')

    def test_audience_weight_is_owner_configured_and_validated(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('competition_list')
        invalid = self.request('post', url, {'name': 'Out of range', 'audience_weight': '100.01'})
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(Competition.objects.filter(name='Out of range').exists())
        self.assertFormError(invalid.context['form'], 'audience_weight',
            'Ensure this value is less than or equal to 100.')

        created = self.request('post', url, {'name': 'Weighted event', 'audience_weight': '35.00'})
        competition = Competition.objects.get(name='Weighted event')
        self.assertEqual(created.status_code, 302)
        self.assertEqual(competition.audience_weight, 35)
        self.assertEqual(competition.judge_weight, 65)
        page = self.request('get', reverse('competition_manage', args=(competition.pk,)))
        self.assertContains(page, 'Audience 35.00% · Judges 65.00%')
        self.assertContains(page, 'Judges 65.00%')
        self.assertContains(page, 'Setup checklist')
        self.assertContains(page, 'check the judging rules')
        self.assertContains(page, 'Public results stay hidden until you reveal')

    def test_short_audience_join_code_resolves_only_to_current_active_ballot(self):
        track, entries, source = self.active_scored_round([90, 80])
        code = competition_audience_join_code(self.competition, source, track)
        self.assertEqual(len(code), 8)
        self.assertRegex(code, r'^[A-HJ-NP-Z2-9]{8}$')

        sign_in(self.client, self.owner, self.org)
        manage = self.request('get', reverse('competition_manage', args=(self.competition.pk,)))
        self.assertContains(manage, f'<code>{code}</code>', html=True)
        self.assertContains(manage, reverse('competition_audience_join'))

        join_url = reverse('competition_audience_join')
        self.assertEqual(self.request('get', join_url).status_code, 200)
        joined = self.request('post', join_url, {'code': code[:4] + '-' + code[4:]})
        self.assertRedirects(joined, reverse('competition_audience_vote', args=(
            self.competition.public_id, source.pk, track.pk)), fetch_redirect_response=False)
        ballot = self.request('get', joined.url)
        self.assertEqual(ballot.status_code, 200)
        self.assertTrue(any(key.startswith('livegrade_competition_audience_')
            for key in ballot.cookies))

        later = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Round 2', order=2)
        unavailable = self.request('post', join_url, {'code': code})
        self.assertEqual(unavailable.status_code, 400)
        self.assertContains(unavailable, 'unavailable or voting has closed', status_code=400)
        self.assertNotEqual(code, competition_audience_join_code(self.competition, later, track))

    def test_single_entry_audience_only_track_hides_ranked_choice_controls(self):
        track, entries, source = self.active_scored_round([90])
        sign_in(self.client, self.owner, self.org)

        page = self.request('get', reverse('competition_manage', args=(self.competition.pk,)))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page,
            'Audience voting needs at least two entries in this track.')
        self.assertNotContains(page, 'Open ranked-choice ballot')
        self.assertNotContains(page, 'Review current audience-only tally')
        self.assertNotContains(page, competition_audience_join_code(
            self.competition, source, track))

    def test_latest_round_shows_only_its_own_judge_invitations(self):
        track, entries, first_round = self.active_scored_round([90, 80])
        second_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition,
            name='Round 2', order=2)
        CompetitionRoundEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, round=second_round, entry=entries[0], track=track)

        sign_in(self.client, self.owner, self.org)
        response = self.request('get', reverse('competition_manage',
            args=(self.competition.pk,)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Advance Round 2')
        self.assertNotContains(response, 'judge@example.org')
        self.assertEqual(list(response.context['tracks'][0].judge_invitation_rows), [])
        self.assertEqual(CompetitionJudgeInvitation.objects.filter(
            competition=self.competition, round=first_round).count(), 1)

    def test_judge_only_public_leaderboard_shows_judge_summary_once(self):
        self.active_scored_round([88])
        complete_competition(self.competition, organization=self.org.as_claim(),
            user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)

        response = self.request('get', reverse('competition_public_leaderboard',
            args=(self.competition.public_id,)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Judge score:', count=1)
        self.assertContains(response, '1 judge')
        self.assertContains(response, 'Score details', count=1)

    def test_audience_join_code_rejects_invalid_format_and_fails_closed_without_throttle(self):
        join_url = reverse('competition_audience_join')
        malformed = self.request('post', join_url, {'code': 'AAAA-0000'})
        self.assertEqual(malformed.status_code, 400)
        with patch('service_domains.competition_views.allow_public_event_request', return_value=None):
            unavailable_malformed = self.request('post', join_url, {'code': 'bad'})
        self.assertEqual(unavailable_malformed.status_code, 503)
        with patch('service_domains.competition_views.allow_public_event_request', return_value=None):
            unavailable = self.request('post', join_url, {'code': 'ABCDEFGH'})
        self.assertEqual(unavailable.status_code, 503)

    def test_audience_join_code_throttles_invalid_attempts_and_ignores_inactive_rounds(self):
        join_url = reverse('competition_audience_join')
        with patch('service_domains.competition_views.allow_public_event_request', return_value=False):
            limited_invalid = self.request('post', join_url, {'code': 'bad'})
        self.assertEqual(limited_invalid.status_code, 429)
        self.assertIn('Retry-After', limited_invalid)

        no_round = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Active without round', status=Competition.Status.ACTIVE)
        no_track_round = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Active without track', status=Competition.Status.ACTIVE)
        CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=no_track_round, name='Round 1', order=1)
        empty_track_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Active empty track', status=Competition.Status.ACTIVE)
        empty_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=empty_track_competition, name='Empty')
        empty_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=empty_track_competition, name='Round 1', order=1)
        empty_code = competition_audience_join_code(
            empty_track_competition, empty_round, empty_track)
        with patch('service_domains.competition_views.allow_public_event_request', return_value=True):
            unavailable_code = self.request('post', join_url, {'code': empty_code})
        self.assertEqual(unavailable_code.status_code, 400)
        self.assertContains(unavailable_code, 'unavailable or voting has closed', status_code=400)

        with patch('service_domains.competition_views.allow_public_event_request', return_value=False):
            limited_unknown = self.request('post', join_url, {'code': 'ABCDEFGH'})
        self.assertEqual(limited_unknown.status_code, 429)

    def test_owner_reviews_private_audience_tally_and_unresolved_ranked_choice_tie(self):
        track, entries, source = self.active_scored_round([90, 80])
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))

        tied = self.request('post', manage, {
            'action': 'tally_audience', 'track_id': str(track.pk),
        })
        self.assertEqual(tied.status_code, 200)
        self.assertContains(tied, 'No audience ballots have been submitted yet')
        self.assertContains(tied, 'Audience ranked-choice ballots remain a separate audience-choice result')

        for ranking in ([entries[0].pk], [entries[1].pk]):
            token = CompetitionAudienceBallot.issue_device_token(
                self.competition, source, track)
            submit_competition_audience_ballot(self.competition.public_id,
                source.pk, track.pk, token, ranking)
        actual_tie = self.request('post', manage, {
            'action': 'tally_audience', 'track_id': str(track.pk),
        })
        self.assertContains(actual_tie, 'A head-judge decision is required')
        self.assertContains(actual_tie, entries[0].name)
        self.assertContains(actual_tie, entries[1].name)

        for _ in range(3):
            token = CompetitionAudienceBallot.issue_device_token(
                self.competition, source, track)
            submit_competition_audience_ballot(self.competition.public_id,
                source.pk, track.pk, token, [entries[0].pk])
        winner = self.request('post', manage, {
            'action': 'tally_audience', 'track_id': str(track.pk),
        })
        self.assertEqual(winner.status_code, 200)
        self.assertEqual(winner.context['audience_tally']['winner'], entries[0].name)
        self.assertContains(winner, 'active votes')

    def test_non_owner_cannot_request_private_audience_tally(self):
        track, _, _ = self.active_scored_round([90, 80])
        sign_in(self.client, self.other_owner, self.org)
        response = self.request('post', reverse('competition_manage', args=(self.competition.pk,)), {
            'action': 'tally_audience', 'track_id': str(track.pk),
        })
        self.assertEqual(response.status_code, 404)

    def test_audience_tally_rejects_missing_track_or_round(self):
        self.active_scored_round([90, 80])
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        missing_track = self.request('post', manage, {
            'action': 'tally_audience', 'track_id': '999999',
        })
        self.assertEqual(missing_track.status_code, 404)

        empty_round_competition = Competition.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Active without round', status=Competition.Status.ACTIVE)
        empty_round_track = CompetitionTrack.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            competition=empty_round_competition, name='Track')
        missing_round = self.request('post', reverse('competition_manage', args=(
            empty_round_competition.pk,)), {
                'action': 'tally_audience', 'track_id': str(empty_round_track.pk),
            })
        self.assertEqual(missing_round.status_code, 404)

    def test_audience_tally_converts_operation_scope_and_data_errors_safely(self):
        track, _, _ = self.active_scored_round([90, 80])
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        payload = {'action': 'tally_audience', 'track_id': str(track.pk)}
        with patch('service_domains.competition_views.competition_audience_ranked_choice',
                   side_effect=PermissionDenied):
            denied = self.request('post', manage, payload)
        self.assertEqual(denied.status_code, 404)
        with patch('service_domains.competition_views.competition_audience_ranked_choice',
                   side_effect=ValidationError('Audience ballot data is invalid.')):
            invalid = self.request('post', manage, payload)
        self.assertContains(invalid, 'Audience ballot data is invalid.', status_code=400)

    def test_invalid_track_and_unknown_actions_show_errors_without_writes(self):
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        invalid_track = self.request('post', manage, {
            'action': 'track', 'name': 'Invalid', 'advancement_rule': 'top_n',
            'advancement_value': '0',
        })
        self.assertEqual(invalid_track.status_code, 400)
        self.assertFalse(CompetitionTrack.objects.filter(competition=self.competition).exists())
        unknown = self.request('post', manage, {'action': 'unknown'})
        self.assertEqual(unknown.status_code, 400)
        self.assertContains(unknown, 'Choose a valid Competition setup action', status_code=400)

    def test_explicit_track_and_entry_model_validation_errors_render(self):
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        original_track_clean = CompetitionTrack.full_clean
        track_calls = 0

        def fail_track_after_form_validation(track, *args, **kwargs):
            nonlocal track_calls
            track_calls += 1
            if track_calls == 2:
                raise ValidationError('Track model refused the draft.')
            return original_track_clean(track, *args, **kwargs)

        with patch.object(CompetitionTrack, 'full_clean', new=fail_track_after_form_validation):
            refused_track = self.request('post', manage, {
                'action': 'track', 'name': 'Refused', 'advancement_rule': 'top_n',
                'advancement_value': '1',
            })
        self.assertContains(refused_track, 'Track model refused the draft', status_code=400)

        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        original_entry_clean = CompetitionEntry.full_clean
        entry_calls = 0

        def fail_entry_after_form_validation(entry, *args, **kwargs):
            nonlocal entry_calls
            entry_calls += 1
            if entry_calls == 2:
                raise ValidationError('Entry model refused the draft.')
            return original_entry_clean(entry, *args, **kwargs)

        with patch.object(CompetitionEntry, 'full_clean', new=fail_entry_after_form_validation):
            refused_entry = self.request('post', manage, {
                'action': 'entry', 'track': track.pk, 'name': 'Refused',
                'reference': 'R-1', 'order': '1',
            })
        self.assertContains(refused_entry, 'Entry model refused the draft', status_code=400)

    def test_owner_creates_competition_track_and_entry_in_scoped_draft(self):
        sign_in(self.client, self.owner, self.org)
        created = self.request('post', reverse('competition_list'), {'name': 'Build week'})
        competition = Competition.objects.get(name='Build week')
        self.assertEqual(created.status_code, 302)
        self.assertEqual(created['Location'], reverse('competition_manage', args=(competition.pk,)))
        self.assertEqual(competition.organization_id, self.org.id)
        self.assertEqual(competition.created_by, self.owner)

        manage = reverse('competition_manage', args=(competition.pk,))
        track_response = self.request('post', manage, {
            'action': 'track', 'name': 'Open', 'advancement_rule': 'top_n',
            'advancement_value': '3',
        })
        self.assertEqual(track_response.status_code, 302)
        track = CompetitionTrack.objects.get(competition=competition)
        criterion_response = self.request('post', manage, {
            'action': 'criterion', 'track': track.pk, 'name': 'Presentation quality',
            'description': 'Clear, complete and well-supported presentation.',
            'weight': '100', 'order': '0',
        })
        self.assertEqual(criterion_response.status_code, 302)
        criterion = CompetitionCriterion.objects.get(track=track)
        self.assertEqual(criterion.organization_id, self.org.id)
        self.assertEqual(criterion.created_by, self.owner)

        entry_response = self.request('post', manage, {
            'action': 'entry', 'track': track.pk, 'name': 'Solar pump',
            'reference': 'SP-1', 'order': '1',
        })
        self.assertEqual(entry_response.status_code, 302)
        entry = CompetitionEntry.objects.get(track=track)
        self.assertEqual(entry.name, 'Solar pump')
        page = self.request('get', manage)
        self.assertContains(page, 'Solar pump')
        self.assertContains(page, 'Top N')
        self.assertIn('private, no-store', page['Cache-Control'])

    def test_manage_scope_and_entry_track_choices_are_owner_and_competition_bound(self):
        sign_in(self.client, self.owner, self.org)
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        other_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other competition')
        other_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=other_competition, name='Other track')
        manage = reverse('competition_manage', args=(self.competition.pk,))
        denied_choice = self.request('post', manage, {
            'action': 'entry', 'track': other_track.pk, 'name': 'Foreign entry',
            'reference': 'X-1', 'order': '1',
        })
        self.assertEqual(denied_choice.status_code, 400)
        self.assertFalse(CompetitionEntry.objects.filter(name='Foreign entry').exists())

        sign_in(self.client, self.other_owner, self.org)
        self.assertEqual(self.request('get', manage).status_code, 404)
        sign_in(self.client, self.owner, self.other_org)
        self.assertEqual(self.request('get', manage).status_code, 404)
        self.assertEqual(track.competition_id, self.competition.pk)

    def test_draft_setup_is_locked_after_competition_leaves_draft(self):
        sign_in(self.client, self.owner, self.org)
        self.competition.status = Competition.Status.ACTIVE
        self.competition.save(update_fields=('status', 'updated_at'))
        manage = reverse('competition_manage', args=(self.competition.pk,))
        refused = self.request('post', manage, {
            'action': 'track', 'name': 'Late track', 'advancement_rule': 'top_n',
            'advancement_value': '1',
        })
        self.assertEqual(refused.status_code, 404)
        self.assertFalse(CompetitionTrack.objects.filter(name='Late track').exists())

    def test_database_uniqueness_races_return_form_errors(self):
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        with patch('service_domains.models.CompetitionTrack.save', side_effect=IntegrityError):
            duplicate_track = self.request('post', manage, {
                'action': 'track', 'name': 'Open', 'advancement_rule': 'top_n',
                'advancement_value': '1',
            })
        self.assertEqual(duplicate_track.status_code, 400)
        self.assertContains(duplicate_track, 'A track with this name already exists', status_code=400)

        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        with patch('service_domains.models.CompetitionEntry.save', side_effect=IntegrityError):
            duplicate_entry = self.request('post', manage, {
                'action': 'entry', 'track': track.pk, 'name': 'Solar pump',
                'reference': 'SP-1', 'order': '0',
            })
        self.assertEqual(duplicate_entry.status_code, 400)
        self.assertIn('An entry with this reference already exists',
            str(duplicate_entry.context['entry_form'].errors))

    def test_owner_creates_private_initial_round_with_every_entry_atomically(self):
        sign_in(self.client, self.owner, self.org)
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        for number in range(20):
            CompetitionEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=track, name=f'Entry {number + 1}', order=number)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        created = self.request('post', manage, {'action': 'first_round', 'name': 'Qualifiers'})
        self.assertEqual(created.status_code, 302)
        round_obj = CompetitionRound.objects.get(competition=self.competition)
        rows = CompetitionRoundEntry.objects.filter(round=round_obj)
        self.assertEqual((round_obj.name, round_obj.order), ('Qualifiers', 1))
        self.assertEqual(rows.count(), 20)
        self.assertSetEqual(set(rows.values_list('entry__name', flat=True)),
            {f'Entry {number + 1}' for number in range(20)})
        self.assertEqual(self.competition.status, Competition.Status.DRAFT)
        self.assertEqual(self.request('get', manage).status_code, 200)
        self.assertContains(self.request('get', manage), 'This round has not been opened yet.')

    def test_initial_round_requires_entries_for_each_track_and_locks_setup_afterward(self):
        sign_in(self.client, self.owner, self.org)
        first = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        empty = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Junior')
        CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=first, name='Entry 1')
        manage = reverse('competition_manage', args=(self.competition.pk,))
        refused = self.request('post', manage, {'action': 'first_round', 'name': 'Qualifiers'})
        self.assertEqual(refused.status_code, 400)
        self.assertFalse(CompetitionRound.objects.filter(competition=self.competition).exists())
        CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=empty, name='Entry 2')
        created = self.request('post', manage, {'action': 'first_round', 'name': 'Qualifiers'})
        self.assertEqual(created.status_code, 302)
        locked = self.request('post', manage, {'action': 'track', 'name': 'Late track',
            'advancement_rule': 'top_n', 'advancement_value': '1'})
        self.assertEqual(locked.status_code, 400)
        self.assertFalse(CompetitionTrack.objects.filter(name='Late track').exists())

    def test_initial_round_operation_rejects_invalid_scope_state_name_and_empty_setup(self):
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            create_initial_competition_round(self.competition, 'Round 1',
                organization=self.other_org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            create_initial_competition_round(self.competition, 'Round 1',
                organization=self.org.as_claim(), user=self.other_owner)
        with self.assertRaises(ValidationError):
            create_initial_competition_round(self.competition, '   ',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            create_initial_competition_round(self.competition, 'x' * 101,
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            create_initial_competition_round(self.competition, None,
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            create_initial_competition_round(self.competition, 'Round 1',
                organization=self.org.as_claim(), user=self.owner)

        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name='Entry')
        first = create_initial_competition_round(self.competition, 'Round 1',
            organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            create_initial_competition_round(self.competition, 'Round 2',
                organization=self.org.as_claim(), user=self.owner)
        self.competition.refresh_from_db()
        self.competition.status = Competition.Status.ACTIVE
        self.competition.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(ValidationError):
            create_initial_competition_round(self.competition, 'Round 2',
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(CompetitionRoundEntry.objects.filter(round=first, entry=entry).count(), 1)

    def test_first_round_form_validation_errors_do_not_write(self):
        sign_in(self.client, self.owner, self.org)
        CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        manage = reverse('competition_manage', args=(self.competition.pk,))
        response = self.request('post', manage, {'action': 'first_round', 'name': ''})
        self.assertEqual(response.status_code, 400)
        self.assertFormError(response.context['first_round_form'], 'name', 'This field is required.')
        self.assertFalse(CompetitionRound.objects.filter(competition=self.competition).exists())

    def test_owner_starts_prepared_competition_and_gets_audience_ballot_link(self):
        sign_in(self.client, self.owner, self.org)
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name='Solar pump')
        CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name='Water monitor', order=1)
        round_obj = create_initial_competition_round(self.competition, 'Qualifiers',
            organization=self.org.as_claim(), user=self.owner)
        response = self.request('post', reverse('competition_manage', args=(self.competition.pk,)),
            {'action': 'start'})
        self.assertEqual(response.status_code, 302)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.ACTIVE)
        audit = CompetitionTransitionAudit.objects.get(competition=self.competition)
        self.assertEqual((audit.from_status, audit.to_status), ('draft', 'active'))
        self.assertEqual(audit.actor_user, self.owner)
        page = self.request('get', reverse('competition_manage', args=(self.competition.pk,)))
        self.assertContains(page, reverse('competition_audience_vote', args=(
            self.competition.public_id, round_obj.pk, track.pk)))
        self.assertContains(page, entry.name)

    def test_competition_cannot_start_before_round_or_with_a_track_gap(self):
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(ValidationError):
            start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        empty = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Junior')
        round_obj = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Qualifiers', order=1)
        entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name='Entry')
        CompetitionRoundEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, round=round_obj, entry=entry, track=track)
        with self.assertRaises(ValidationError):
            start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            start_competition(self.competition, organization=self.org.as_claim(), user=self.other_owner)

        CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=empty, name='Other entry')
        other_entry = CompetitionEntry.objects.get(track=empty)
        CompetitionRoundEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, round=round_obj, entry=other_entry, track=empty)
        started = start_competition(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(started.status, Competition.Status.ACTIVE)
        audit = CompetitionTransitionAudit.objects.get(competition=self.competition)
        with self.assertRaises(ValidationError):
            CompetitionTransitionAudit.objects.filter(pk=audit.pk).update(reason='tampered')
        with self.assertRaises(ValidationError):
            audit.delete()
        with self.assertRaises(ValidationError):
            CompetitionTransitionAudit.objects.filter(pk=audit.pk).delete()
        with self.assertRaises(ValidationError):
            audit.save()
        with self.assertRaises(ValidationError):
            start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

    def test_competition_start_rejects_empty_track_set_and_audit_requires_change_and_reason(self):
        empty_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='No tracks')
        CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=empty_competition, name='Round 1', order=1)
        with self.assertRaises(ValidationError):
            start_competition(empty_competition, organization=self.org.as_claim(), user=self.owner)

        audit = CompetitionTransitionAudit(competition=self.competition,
            from_status='draft', to_status='draft', reason='',
            actor_user=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            audit.full_clean()
        self.assertIn('to_status', caught.exception.message_dict)
        self.assertIn('reason', caught.exception.message_dict)
        audit.from_status = 'draft'
        audit.to_status = 'active'
        audit.reason = '   '
        with self.assertRaises(ValidationError) as caught:
            audit.full_clean()
        self.assertIn('reason', caught.exception.message_dict)

    def test_start_action_displays_validation_error_when_round_is_missing(self):
        sign_in(self.client, self.owner, self.org)
        response = self.request('post', reverse('competition_manage', args=(self.competition.pk,)),
            {'action': 'start'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Create the initial round before starting the competition.',
            status_code=400)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.DRAFT)
        self.assertFalse(CompetitionTransitionAudit.objects.exists())

    def test_owner_advances_scored_round_by_configured_top_n(self):
        sign_in(self.client, self.owner, self.org)
        track, entries, source = self.active_scored_round(list(range(100, 80, -1)))
        manage = reverse('competition_manage', args=(self.competition.pk,))
        page = self.request('get', manage)
        self.assertContains(page, 'Advance Round 1')
        self.assertContains(page, 'Create next round')
        advanced = self.request('post', manage, {
            'action': 'advance_round', 'name': 'Final',
        })
        self.assertEqual(advanced.status_code, 302)
        final = CompetitionRound.objects.get(competition=self.competition, order=2)
        self.assertEqual(list(final.round_entries.order_by('entry_id')
            .values_list('entry_id', flat=True)), [entries[0].pk, entries[1].pk])
        decision = CompetitionAdvancementDecision.objects.get(source_round=source, track=track)
        self.assertEqual(decision.selected_entry_ids, [entries[0].pk, entries[1].pk])
        source.refresh_from_db()
        self.assertIsNotNone(source.advancement_locked_at)

    def test_twenty_entry_event_advances_tallies_and_reveals_without_manual_tally(self):
        """Acceptance fixture for the complete judge-only 20-entry path."""
        sign_in(self.client, self.owner, self.org)
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open',
            advancement_rule='top_n', advancement_value=5)
        entries = [CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name=f'Entry {index:02d}', order=index)
            for index in range(1, 21)]
        first_round = create_initial_competition_round(self.competition, 'Qualifying',
            organization=self.org.as_claim(), user=self.owner)
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        _, first_token = issue_competition_judge_invitation(
            self.competition, first_round, track, 'qualifying-judge@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner)
        first_rows = list(first_round.round_entries.order_by('entry_id'))
        submit_competition_judge_scores(first_token, {
            str(row.pk): str(100 - index) for index, row in enumerate(first_rows)})
        first_tally = compile_competition_judge_scores(self.competition, first_round, track,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(first_tally), 20)
        # Independent manual answer: Entry 01..20 receive 100..81 points.
        self.assertEqual(first_tally, [
            {'entry_id': entry.pk, 'score': Decimal(100 - index), 'judge_count': 1}
            for index, entry in enumerate(entries)
        ])

        manage = reverse('competition_manage', args=(self.competition.pk,))
        advanced = self.request('post', manage, {
            'action': 'advance_round', 'name': 'Final',
        })
        self.assertEqual(advanced.status_code, 302)
        selected = list(CompetitionAdvancementDecision.objects.get(
            source_round=first_round, track=track).selected_entry_ids)
        self.assertEqual(selected, [entry.pk for entry in entries[:5]])

        final_round = CompetitionRound.objects.get(competition=self.competition, order=2)
        final_rows = list(final_round.round_entries.order_by('entry_id'))
        _, final_token = issue_competition_judge_invitation(
            self.competition, final_round, track, 'final-judge@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner)
        final_scores = [75, 100, 80, 90, 70]
        submit_competition_judge_scores(final_token, {
            str(row.pk): str(score) for row, score in zip(final_rows, final_scores)})
        final_tally = compile_competition_judge_scores(self.competition, final_round, track,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(final_tally), 5)
        # The final rows are Entry 01..05; their manually submitted scores
        # are 75, 100, 80, 90, 70, so the exact expected rank is 02, 04, 03,
        # 01, 05 with corresponding scores 100, 90, 80, 75, 70.
        self.assertEqual(final_tally, [
            {'entry_id': entries[index].pk, 'score': Decimal(score), 'judge_count': 1}
            for index, score in enumerate(final_scores)
        ])

        public_url = reverse('competition_public_leaderboard',
            args=(self.competition.public_id,))
        self.assertEqual(self.request('get', public_url).status_code, 404)
        self.assertEqual(self.request('post', manage, {'action': 'complete'}).status_code, 302)
        self.assertEqual(self.request('post', manage, {'action': 'reveal'}).status_code, 302)
        leaderboard = self.request('get', public_url)
        self.assertEqual(leaderboard.status_code, 200)
        ranked_entries = (2, 4, 3, 1, 5)
        order = [leaderboard.content.index(f'Entry {number:02d}'.encode())
                 for number in ranked_entries]
        self.assertEqual(order, sorted(order))

    def test_live_leaderboard_json_projection_is_public_only_after_reveal(self):
        track, entries, _ = self.active_scored_round([91, 73, 84])
        public_url = reverse('competition_public_leaderboard', args=(self.competition.public_id,))
        hidden = self.request('get', public_url, HTTP_ACCEPT='application/json')
        self.assertEqual(hidden.status_code, 404)
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        response = self.request('get', public_url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        payload = response.json()
        self.assertEqual(payload['competition'], self.competition.name)
        self.assertEqual([row['entry']['name'] for row in payload['track_results'][0]['entries']],
            ['Entry 1', 'Entry 3', 'Entry 2'])
        self.assertEqual([row['display_rank'] for row in payload['track_results'][0]['entries']],
            [1, 2, 3])
        self.assertEqual(payload['round'], 'Round 1')

    def test_cutoff_tie_requires_and_records_head_judge_decision_from_workspace(self):
        sign_in(self.client, self.owner, self.org)
        track, entries, source = self.active_scored_round([90, 80, 80])
        manage = reverse('competition_manage', args=(self.competition.pk,))
        page = self.request('get', manage)
        self.assertContains(page, 'head-judge decision')
        invalid = self.request('post', manage, {'action': 'advance_round', 'name': 'Final'})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(CompetitionRound.objects.filter(competition=self.competition).count(), 1)
        over_selected = self.request('post', manage, {
            'action': 'advance_round', 'name': 'Final',
            f'head_judge_decision_{track.pk}': [str(entries[1].pk), str(entries[2].pk)],
            f'head_judge_reason_{track.pk}': 'Head judge ruling after review',
        })
        self.assertEqual(over_selected.status_code, 400)
        decided = self.request('post', manage, {
            'action': 'advance_round', 'name': 'Final',
            f'head_judge_decision_{track.pk}': str(entries[1].pk),
            f'head_judge_reason_{track.pk}': 'Head judge ruling after review',
        })
        self.assertEqual(decided.status_code, 302)
        decision = CompetitionAdvancementDecision.objects.get(source_round=source, track=track)
        self.assertEqual(decision.head_judge_decision, [entries[1].pk])
        self.assertEqual(decision.head_judge_reason, 'Head judge ruling after review')

    def test_advance_form_helper_skips_threshold_and_invalid_top_n_tracks(self):
        tracks = [
            SimpleNamespace(pk=1, advancement_rule='threshold', advancement_value='75'),
            SimpleNamespace(pk=2, advancement_rule='top_n', advancement_value='bad'),
        ]
        self.assertEqual(competition_head_judge_requirements(tracks, []), {})

    def test_advancement_view_handles_missing_round_and_operation_denials(self):
        sign_in(self.client, self.owner, self.org)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        self.competition.status = Competition.Status.ACTIVE
        self.competition.save(update_fields=('status', 'updated_at'))
        missing_round = self.request('post', manage, {
            'action': 'advance_round', 'name': 'Final',
        })
        self.assertEqual(missing_round.status_code, 404)

        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status', 'updated_at'))
        self.active_scored_round([90, 80, 40])
        with patch('service_domains.competition_views.create_next_competition_round',
                   side_effect=PermissionDenied):
            denied = self.request('post', manage, {
                'action': 'advance_round', 'name': 'Final',
            })
        self.assertEqual(denied.status_code, 404)
        with patch('service_domains.competition_views.create_next_competition_round',
                   side_effect=ValidationError('Round is no longer advanceable.')):
            rejected = self.request('post', manage, {
                'action': 'advance_round', 'name': 'Final',
            })
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, 'Round is no longer advanceable', status_code=400)

    def test_audience_weighted_round_waits_for_complete_ballots_and_finalized_scores(self):
        sign_in(self.client, self.owner, self.org)
        self.active_scored_round([90, 80, 40], audience_weight=35)
        manage = reverse('competition_manage', args=(self.competition.pk,))
        page = self.request('get', manage)
        self.assertEqual(page.context['competition'].audience_weight, 35)
        self.assertEqual(page.context['competition'].status, Competition.Status.ACTIVE)
        self.assertIsNone(page.context['latest_round'].advancement_locked_at)
        self.assertTrue(page.context['audience_weighted_round'])
        self.assertContains(page, 'Advance Round 1')
        self.assertContains(page, 'collect at least one complete audience ranking')
        self.assertFalse(page.context['can_finalize_weighted'])
        denied = self.request('post', manage, {
            'action': 'advance_round', 'name': 'Final',
        })
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(CompetitionRound.objects.filter(competition=self.competition).count(), 1)

    def test_leaderboard_is_hidden_until_owner_completes_and_reveals_results(self):
        sign_in(self.client, self.owner, self.org)
        track, entries, source = self.active_scored_round([81, 95, 95])
        public_url = reverse('competition_public_leaderboard', args=(self.competition.public_id,))
        self.assertEqual(self.request('get', public_url).status_code, 404)
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        completed = self.request('post', manage_url, {'action': 'complete'})
        self.assertEqual(completed.status_code, 302)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.COMPLETE)
        self.assertEqual(self.request('get', public_url).status_code, 404)
        blocked_reveal = self.request('post', manage_url, {'action': 'reveal'})
        self.assertEqual(blocked_reveal.status_code, 400)
        self.assertContains(blocked_reveal, 'head-judge order', status_code=400)
        self.assertContains(blocked_reveal, 'Final leaderboard decisions', status_code=400)
        prefix = f'tie_{track.pk}_9500'
        invalid_order = self.request('post', manage_url, {
            'action': 'resolve_final_ties', f'{prefix}_rank_1': str(entries[2].pk),
            f'{prefix}_rank_2': str(entries[2].pk), f'{prefix}_reason': '   ',
        })
        self.assertEqual(invalid_order.status_code, 400)
        self.assertEqual(CompetitionLeaderboardTieDecision.objects.count(), 0)
        with patch('service_domains.competition_views.competition_final_tie_requirements',
                   side_effect=PermissionDenied):
            self.assertEqual(self.request('get', manage_url).status_code, 404)
        valid_order_payload = {
            'action': 'resolve_final_ties', f'{prefix}_rank_1': str(entries[2].pk),
            f'{prefix}_rank_2': str(entries[1].pk),
            f'{prefix}_reason': 'The head judge reviewed the final presentation quality.',
        }
        with patch('service_domains.competition_views.resolve_competition_final_ties',
                   side_effect=PermissionDenied):
            self.assertEqual(self.request('post', manage_url, valid_order_payload).status_code, 404)
        with patch('service_domains.competition_views.resolve_competition_final_ties',
                   side_effect=ValidationError('tie state changed')):
            failed_record = self.request('post', manage_url, valid_order_payload)
        self.assertEqual(failed_record.status_code, 400)
        self.assertContains(failed_record, 'tie state changed', status_code=400)
        resolved = self.request('post', manage_url, {
            'action': 'resolve_final_ties', f'{prefix}_rank_1': str(entries[2].pk),
            f'{prefix}_rank_2': str(entries[1].pk),
            f'{prefix}_reason': 'The head judge reviewed the final presentation quality.',
        })
        self.assertEqual(resolved.status_code, 302)
        decision = CompetitionLeaderboardTieDecision.objects.get(round=source, track=track)
        self.assertEqual(decision.ordered_entry_ids, [entries[2].pk, entries[1].pk])
        self.assertEqual(decision.decided_by, self.owner)
        self.assertContains(self.request('get', manage_url), 'The head judge reviewed the final presentation quality.')
        revealed = self.request('post', manage_url, {'action': 'reveal'})
        self.assertEqual(revealed.status_code, 302)
        response = self.request('get', public_url)
        self.assertEqual(response.status_code, 200)
        self.assertLess(response.content.index(b'Entry 3'), response.content.index(b'Entry 2'))
        self.assertLess(response.content.index(b'Entry 2'), response.content.index(b'Entry 1'))
        self.assertContains(response, 'aria-label="Place 1"')
        self.assertContains(response, 'aria-label="Place 2"')
        self.assertContains(response, 'aria-label="Place 3"')
        self.assertContains(response, 'recorded head-judge decision')
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.REVEALED)
        self.assertEqual(CompetitionTransitionAudit.objects.filter(
            competition=self.competition).count(), 3)

    def test_final_leaderboard_without_ties_can_be_revealed_without_a_decision(self):
        track, entries, _ = self.active_scored_round([92, 83, 71])
        sign_in(self.client, self.owner, self.org)
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        self.assertEqual(self.request('post', manage_url, {'action': 'complete'}).status_code, 302)
        manage = self.request('get', manage_url)
        self.assertContains(manage, 'No unresolved final-score ties remain')
        self.assertNotContains(manage, 'Record head-judge order')
        self.assertEqual(self.request('post', manage_url, {'action': 'reveal'}).status_code, 302)
        response = self.request('get', reverse('competition_public_leaderboard',
            args=(self.competition.public_id,)))
        self.assertEqual(response.status_code, 200)
        self.assertLess(response.content.index(b'Entry 1'), response.content.index(b'Entry 2'))
        self.assertContains(response, 'aria-label="Place 1"')
        self.assertContains(response, 'aria-label="Place 2"')

    def test_revealed_leaderboard_uses_shared_cache_for_its_frozen_projection(self):
        _, _, _ = self.active_scored_round([92, 83, 71])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        public_url = reverse('competition_public_leaderboard',
            args=(self.competition.public_id,))
        cache.clear()

        first = self.request('get', public_url)

        self.assertEqual(first.status_code, 200)
        self.assertIn('no-store', first['Cache-Control'])
        with patch('service_domains.competition_views.CompetitionRoundEntry.objects.filter',
                   side_effect=AssertionError('The cached projection should avoid entry queries.')):
            second = self.request('get', public_url)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, 'Entry 1')
        self.assertLess(second.content.index(b'Entry 1'), second.content.index(b'Entry 2'))

    def test_owner_can_pause_and_resume_revealed_public_leaderboard_with_audit(self):
        sign_in(self.client, self.owner, self.org)
        self.active_scored_round([92, 83, 71])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        public_url = reverse('competition_public_leaderboard',
            args=(self.competition.public_id,))

        self.assertContains(self.request('get', manage_url), 'Pause public display')
        paused = self.request('post', manage_url, {'action': 'hide_leaderboard'})
        self.assertEqual(paused.status_code, 302)
        self.assertEqual(self.request('get', public_url).status_code, 404)
        self.assertEqual(self.request('get', public_url,
            HTTP_ACCEPT='application/json').status_code, 404)
        audit = list(CompetitionLeaderboardAudit.objects.filter(competition=self.competition))
        self.assertEqual(len(audit), 1)
        self.assertFalse(audit[0].enabled)
        self.assertEqual(audit[0].actor_user, self.owner)
        self.assertIn('paused', audit[0].reason)

        # Repeating a state transition is idempotent and does not create
        # duplicate audit rows.
        self.assertEqual(self.request('post', manage_url,
            {'action': 'hide_leaderboard'}).status_code, 302)
        self.assertEqual(CompetitionLeaderboardAudit.objects.filter(
            competition=self.competition).count(), 1)
        resumed = self.request('post', manage_url, {'action': 'show_leaderboard'})
        self.assertEqual(resumed.status_code, 302)
        public = self.request('get', public_url)
        self.assertEqual(public.status_code, 200)
        self.assertIn('no-store', public['Cache-Control'])
        self.assertEqual(list(CompetitionLeaderboardAudit.objects.filter(
            competition=self.competition).values_list('enabled', flat=True)), [False, True])

    def test_public_leaderboard_control_requires_revealed_owner_scope(self):
        with self.assertRaises(ValidationError):
            set_competition_public_leaderboard(self.competition, True,
                organization=self.org.as_claim(), user=self.owner)
        self.active_scored_round([92, 83])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            set_competition_public_leaderboard(self.competition, False,
                organization=self.org.as_claim(), user=self.other_owner)
        with self.assertRaises(PermissionDenied):
            set_competition_public_leaderboard(self.competition, False,
                organization=self.other_org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            set_competition_public_leaderboard(self.competition, 1,
                organization=self.org.as_claim(), user=self.owner)

    def test_competition_display_control_is_owner_only_and_rejects_unknown_action(self):
        self.active_scored_round([92, 83])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        sign_in(self.client, self.other_owner, self.org)
        denied = self.request('post', manage_url, {'action': 'hide_leaderboard'})
        self.assertEqual(denied.status_code, 404)
        sign_in(self.client, self.owner, self.org)
        invalid = self.request('post', manage_url, {'action': 'change_leaderboard'})
        self.assertEqual(invalid.status_code, 404)

    def test_display_control_operation_errors_are_rendered(self):
        self.active_scored_round([92, 83])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        sign_in(self.client, self.owner, self.org)
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        with patch('service_domains.competition_views.set_competition_public_leaderboard',
                   side_effect=ValidationError('display policy changed')):
            invalid = self.request('post', manage_url, {'action': 'hide_leaderboard'})
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'display policy changed', status_code=400)
        with patch('service_domains.competition_views.set_competition_public_leaderboard',
                   side_effect=PermissionDenied()):
            denied = self.request('post', manage_url, {'action': 'hide_leaderboard'})
        self.assertEqual(denied.status_code, 400)
        self.assertContains(denied, 'public leaderboard could not be updated', status_code=400)

    def test_leaderboard_audit_validation_and_immutability(self):
        self.competition.status = Competition.Status.REVEALED
        self.competition.revealed_at = timezone.now()
        self.competition.save(update_fields=('status', 'revealed_at', 'updated_at'))
        missing_reason = CompetitionLeaderboardAudit(competition=self.competition,
            actor_user=self.owner, enabled=False, reason='',
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError):
            missing_reason.full_clean()
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        wrong_status = CompetitionLeaderboardAudit(competition=self.competition,
            actor_user=self.owner, enabled=False, reason='Owner paused display',
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError):
            wrong_status.full_clean()
        self.competition.status = Competition.Status.REVEALED
        self.competition.save(update_fields=('status', 'updated_at'))
        event = CompetitionLeaderboardAudit(competition=self.competition,
            actor_user=self.owner, enabled=False, reason='Owner paused display',
            organization_id=self.org.id, created_by=self.owner)
        event.full_clean()
        event.save(force_insert=True)
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_public_leaderboard_falls_back_to_database_when_shared_cache_fails(self):
        self.active_scored_round([92, 83, 71])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        public_url = reverse('competition_public_leaderboard',
            args=(self.competition.public_id,))
        with patch('service_domains.competition_views.cache.get', side_effect=RuntimeError('cache down')), \
                patch('service_domains.competition_views.cache.set', side_effect=RuntimeError('cache down')):
            response = self.request('get', public_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Entry 1')

    def test_final_tie_operations_require_complete_exact_decisions_and_owner_scope(self):
        track, entries, source = self.active_scored_round([90, 90, 70])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        requirements = competition_final_tie_requirements(self.competition,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual([row.entry_id for row in requirements[0]['entries']],
            [entries[0].pk, entries[1].pk])
        payload = [{'track_id': track.pk, 'score': '90.00',
            'ordered_entry_ids': [entries[1].pk, entries[0].pk],
            'reason': 'The head judge reviewed the final demonstrations.'}]
        incomplete_form = CompetitionFinalTieForm(data={}, tie_groups=requirements)
        self.assertFalse(incomplete_form.is_valid())
        with self.assertRaises(ValueError):
            incomplete_form.decision_payloads()
        with self.assertRaises(PermissionDenied):
            resolve_competition_final_ties(self.competition, payload,
                organization=self.org.as_claim(), user=self.other_owner)
        with self.assertRaises(ValidationError):
            resolve_competition_final_ties(self.competition, [],
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            resolve_competition_final_ties(self.competition, [dict(payload[0],
                ordered_entry_ids=[entries[0].pk, entries[0].pk])],
                organization=self.org.as_claim(), user=self.owner)
        malformed = (
            [None],
            [dict(payload[0], track_id=True)],
            [dict(payload[0], track_id='invalid')],
            [dict(payload[0], score='NaN')],
            [dict(payload[0], score='70.00')],
            [dict(payload[0], ordered_entry_ids=())],
            [dict(payload[0], ordered_entry_ids=[entries[0].pk])],
            [dict(payload[0], reason='')],
            [dict(payload[0], reason='r' * 501)],
        )
        for bad_payload in malformed:
            with self.subTest(bad_payload=bad_payload):
                with self.assertRaises(ValidationError):
                    resolve_competition_final_ties(self.competition, bad_payload,
                        organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(CompetitionLeaderboardTieDecision.objects.count(), 0)
        sign_in(self.client, self.other_owner, self.other_org)
        denied = self.request('post', reverse('competition_manage', args=(self.competition.pk,)), {
            'action': 'resolve_final_ties',
        })
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(CompetitionLeaderboardTieDecision.objects.count(), 0)
        sign_in(self.client, self.owner, self.org)
        recorded = resolve_competition_final_ties(self.competition, payload,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(competition_final_tie_requirements(self.competition,
            organization=self.org.as_claim(), user=self.owner), [])
        with self.assertRaises(ValidationError):
            resolve_competition_final_ties(self.competition, payload,
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(reveal_competition_leaderboard(self.competition,
            organization=self.org.as_claim(), user=self.owner).status,
            Competition.Status.REVEALED)
        decision = recorded[0]
        other_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other decision scope')
        other_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=other_competition, name='Other')
        wrong_track_decision = CompetitionLeaderboardTieDecision(round=source,
            track=other_track, tied_score=90,
            ordered_entry_ids=[entries[0].pk, entries[1].pk], reason='Wrong track.',
            decided_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            wrong_track_decision.full_clean()
        self.assertIn('tie track must belong', str(caught.exception))
        wrong_order_decision = CompetitionLeaderboardTieDecision(round=source,
            track=track, tied_score=90,
            ordered_entry_ids=[entries[0].pk, entries[2].pk], reason='Wrong entries.',
            decided_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            wrong_order_decision.full_clean()
        self.assertIn('every entry in this exact final-score tie', str(caught.exception))
        malformed_ids = CompetitionLeaderboardTieDecision(round=source, track=track,
            tied_score=90, ordered_entry_ids=(entries[0].pk, entries[1].pk),
            reason='Malformed list.', decided_by=self.owner,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            malformed_ids.full_clean()
        self.assertIn('must be a list of entry identifiers', str(caught.exception))
        duplicate_ids = CompetitionLeaderboardTieDecision(round=source, track=track,
            tied_score=90, ordered_entry_ids=[entries[0].pk, entries[0].pk],
            reason='Duplicate order.', decided_by=self.owner,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            duplicate_ids.full_clean()
        self.assertIn('at least two unique entries', str(caught.exception))
        valid_order = CompetitionLeaderboardTieDecision(round=source, track=track,
            tied_score=90, ordered_entry_ids=[entries[0].pk, entries[1].pk],
            reason='A valid decision.', decided_by=self.owner,
            organization_id=self.org.id, created_by=self.owner)
        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(ValidationError) as caught:
            valid_order.full_clean()
        self.assertIn('recorded after scoring is completed', str(caught.exception))
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        blank_reason = CompetitionLeaderboardTieDecision(round=source, track=track,
            tied_score=90, ordered_entry_ids=[entries[0].pk, entries[1].pk],
            reason='   ', decided_by=self.owner,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            blank_reason.full_clean()
        self.assertIn('reason is required', str(caught.exception))
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            decision.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            decision.delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            CompetitionLeaderboardTieDecision.objects.filter(pk=decision.pk).update(reason='changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            CompetitionLeaderboardTieDecision.objects.filter(pk=decision.pk).delete()

    def test_final_tie_helpers_fail_closed_for_scope_missing_round_and_weight(self):
        with self.assertRaises(PermissionDenied):
            competition_final_tie_requirements(self.competition,
                organization=self.org.as_claim(), user=self.other_owner)
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        self.assertEqual(competition_final_tie_requirements(self.competition,
            organization=self.org.as_claim(), user=self.owner), [])
        with self.assertRaises(ValidationError):
            resolve_competition_final_ties(self.competition, [],
                organization=self.org.as_claim(), user=self.owner)

        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status', 'updated_at'))
        track, _, source = self.active_scored_round([90, 90])
        complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.audience_weight = 20
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        with self.assertRaises(ValidationError):
            resolve_competition_final_ties(self.competition, [],
                organization=self.org.as_claim(), user=self.owner)
        self.competition.audience_weight = 0
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        source.round_entries.update(score=None)
        with self.assertRaises(ValidationError):
            resolve_competition_final_ties(self.competition, [],
                organization=self.org.as_claim(), user=self.owner)

    def test_finalization_requires_complete_scores_no_pending_judge_and_supported_weight(self):
        _, _, source = self.active_scored_round([80, 70], audience_weight=20)
        with self.assertRaises(ValidationError):
            complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.audience_weight = 0
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        source.round_entries.update(score=None)
        with self.assertRaises(ValidationError):
            complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        source.round_entries.update(score=80)
        with self.assertRaises(PermissionDenied):
            complete_competition(self.competition, organization=self.org.as_claim(), user=self.other_owner)
        with self.assertRaises(ValidationError):
            reveal_competition_leaderboard(self.competition,
                organization=self.org.as_claim(), user=self.owner)

    def test_finalization_requires_a_round_and_resolves_live_judge_links(self):
        self.competition.status = Competition.Status.ACTIVE
        self.competition.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(ValidationError):
            complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)
        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status', 'updated_at'))
        track, _, source = self.active_scored_round([80, 70])
        pending = CompetitionJudgeInvitation(competition=self.competition, round=source,
            track=track, email='another-judge@example.org',
            token_digest=CompetitionJudgeInvitation.digest_token('pending-judge-fixture'),
            expires_at=timezone.now() + timedelta(hours=1),
            organization_id=self.org.id, created_by=self.owner)
        pending.full_clean()
        pending.save(force_insert=True)
        with self.assertRaises(ValidationError):
            complete_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

    def test_owner_transition_errors_render_and_revealed_incomplete_data_stays_hidden(self):
        sign_in(self.client, self.owner, self.org)
        self.active_scored_round([90, 80])
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        with patch('service_domains.competition_views.complete_competition',
                   side_effect=PermissionDenied):
            failed_complete = self.request('post', manage_url, {'action': 'complete'})
        self.assertEqual(failed_complete.status_code, 400)
        self.assertContains(failed_complete, 'Competition results could not be finalized', status_code=400)
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        with patch('service_domains.competition_views.reveal_competition_leaderboard',
                   side_effect=PermissionDenied):
            failed_reveal = self.request('post', manage_url, {'action': 'reveal'})
        self.assertEqual(failed_reveal.status_code, 400)
        self.assertContains(failed_reveal, 'leaderboard could not be revealed', status_code=400)
        with self.assertRaises(PermissionDenied):
            reveal_competition_leaderboard(self.competition,
                organization=self.org.as_claim(), user=self.other_owner)

        empty = Competition.objects.create(organization_id=self.org.id, created_by=self.owner,
            name='Corrupt empty reveal', status=Competition.Status.REVEALED,
            revealed_at=timezone.now(), public_leaderboard_enabled=True)
        empty_url = reverse('competition_public_leaderboard', args=(empty.public_id,))
        self.assertEqual(self.request('get', empty_url).status_code, 404)
        incomplete = Competition.objects.create(organization_id=self.org.id, created_by=self.owner,
            name='Corrupt incomplete reveal', status=Competition.Status.REVEALED,
            revealed_at=timezone.now(), public_leaderboard_enabled=True)
        incomplete_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=incomplete, name='Open')
        entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=incomplete_track, name='Unscored entry')
        incomplete_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=incomplete, name='Final', order=1)
        CompetitionRoundEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, round=incomplete_round, entry=entry, track=incomplete_track)
        incomplete_url = reverse('competition_public_leaderboard', args=(incomplete.public_id,))
        self.assertEqual(self.request('get', incomplete_url).status_code, 404)

        unresolved = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Corrupt unresolved tie',
            status=Competition.Status.REVEALED, revealed_at=timezone.now(),
            public_leaderboard_enabled=True)
        unresolved_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=unresolved, name='Final')
        unresolved_entries = [CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=unresolved_track, name=f'Tied {index}')
            for index in range(2)]
        unresolved_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=unresolved, name='Final', order=1)
        for entry in unresolved_entries:
            CompetitionRoundEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, round=unresolved_round, entry=entry,
                track=unresolved_track, score=85)
        unresolved_url = reverse('competition_public_leaderboard',
            args=(unresolved.public_id,))
        self.assertEqual(self.request('get', unresolved_url).status_code, 404)

    def test_competition_results_export_csv(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('competition_results_export', args=(self.competition.pk,))
        # Export fails while draft/not complete
        self.assertEqual(self.request('get', url).status_code, 404)

        # Mark complete with track, round, and entries
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save()
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Innovation Track')
        entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=track, name='=HYPERLINK("formula")', reference='REF-1')
        round_obj = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Round 1', order=1)
        CompetitionRoundEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, round=round_obj, entry=entry, track=track,
            judge_score=Decimal('85.00'), audience_score=Decimal('90.00'), score=Decimal('87.50'))

        response = self.request('get', url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        content = response.content.decode('utf-8')
        self.assertIn("'=HYPERLINK", content)  # Formula injection escaped
        self.assertIn('87.5', content)

        # Non-owner cannot export
        sign_in(self.client, self.other_owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 404)

    def test_competition_manage_tally_and_finalize_actions(self):
        sign_in(self.client, self.owner, self.org)
        manage_url = reverse('competition_manage', args=(self.competition.pk,))
        
        # Test finalize_weighted_scores when no rounds exist returns 404
        res = self.request('post', manage_url, {'action': 'finalize_weighted_scores'})
        self.assertEqual(res.status_code, 404)

        # Create active scored round with audience weight
        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status',))
        track, entries, source = self.active_scored_round([85, 75], audience_weight=20)

        # Tally audience on weighted event produces notification error
        res = self.request('post', manage_url, {'action': 'tally_audience', 'track_id': track.pk})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Weighted events use the disclosed Borda ranking score', res.context['action_error'])

        # Finalize weighted scores redirects
        token = CompetitionAudienceBallot.issue_device_token(self.competition, source, track)
        submit_competition_audience_ballot(self.competition.public_id, source.pk, track.pk, token, [entries[0].pk, entries[1].pk])
        res = self.request('post', manage_url, {'action': 'finalize_weighted_scores'})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], manage_url)

        # Attempting to finalize already finalized scores records error
        res = self.request('post', manage_url, {'action': 'finalize_weighted_scores'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('already been finalized', res.context['action_error'])

    def test_competition_manage_criterion_lifecycle(self):
        sign_in(self.client, self.owner, self.org)
        track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Lifecycle Track')
        manage_url = reverse('competition_manage', args=(self.competition.pk,))

        # Invalid criterion form sets errors and returns 400
        res = self.request('post', manage_url, {
            'action': 'criterion',
            'track': track.pk,
            'name': '',
            'weight': '50.00',
        })
        self.assertEqual(res.status_code, 400)

        # Add criterion successfully
        res = self.request('post', manage_url, {
            'action': 'criterion',
            'track': track.pk,
            'name': 'Innovation',
            'description': 'Novel ideas',
            'weight': '50.00',
            'order': '1',
        })
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], manage_url)
        criterion = CompetitionCriterion.objects.get(track=track, name='Innovation')

        # Duplicate criterion name on same track triggers form error
        res = self.request('post', manage_url, {
            'action': 'criterion',
            'track': track.pk,
            'name': 'Innovation',
            'description': 'Duplicate name',
            'weight': '50.00',
            'order': '2',
        })
        self.assertEqual(res.status_code, 400)
        self.assertTrue(res.context['criterion_form'].errors)

        # Criterion model validation error handling
        with patch.object(CompetitionCriterion, 'full_clean', side_effect=[None, ValidationError(['Simulated validation failure'])]):
            res = self.request('post', manage_url, {
                'action': 'criterion',
                'track': track.pk,
                'name': 'CleanFail',
                'description': 'Desc',
                'weight': '50.00',
                'order': '3',
            })
            self.assertEqual(res.status_code, 400)
            self.assertIn('Simulated validation failure', res.context['criterion_form'].errors['__all__'][0])

        # Criterion database integrity error handling
        with patch.object(CompetitionCriterion, 'save', side_effect=IntegrityError('Unique constraint')):
            res = self.request('post', manage_url, {
                'action': 'criterion',
                'track': track.pk,
                'name': 'SaveFail',
                'description': 'Desc',
                'weight': '50.00',
                'order': '4',
            })
            self.assertEqual(res.status_code, 400)
            self.assertIn('already has a criterion with that name', res.context['criterion_form'].errors['name'][0])

        # Criterion model validation error handling
        with patch.object(CompetitionCriterion, 'full_clean', side_effect=ValidationError('Simulated validation failure')):
            res = self.request('post', manage_url, {
                'action': 'criterion',
                'track': track.pk,
                'name': 'CleanFail',
                'description': 'Desc',
                'weight': '50.00',
                'order': '3',
            })
            self.assertEqual(res.status_code, 400)
            self.assertIn('Simulated validation failure', res.context['criterion_form'].errors['__all__'][0])

        # Criterion database integrity error handling
        with patch.object(CompetitionCriterion, 'save', side_effect=IntegrityError('Unique constraint')):
            res = self.request('post', manage_url, {
                'action': 'criterion',
                'track': track.pk,
                'name': 'SaveFail',
                'description': 'Desc',
                'weight': '50.00',
                'order': '4',
            })
            self.assertEqual(res.status_code, 400)
            self.assertIn('already has a criterion with that name', res.context['criterion_form'].errors['name'][0])

        # Delete criterion with invalid id returns 404
        res = self.request('post', manage_url, {'action': 'delete_criterion', 'criterion_id': '999999'})
        self.assertEqual(res.status_code, 404)

        # Delete criterion successfully redirects
        res = self.request('post', manage_url, {'action': 'delete_criterion', 'criterion_id': str(criterion.pk)})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], manage_url)
        self.assertFalse(CompetitionCriterion.objects.filter(pk=criterion.pk).exists())

    def test_competition_service_scoping_and_join_edge_cases(self):
        # Service scoping rejection (404) when service is not competition
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'classroom'):
            res = self.client.get(reverse('competition_audience_join'))
            self.assertEqual(res.status_code, 404)
            res = self.client.get(reverse('competition_public_leaderboard', args=(self.competition.public_id,)))
            self.assertEqual(res.status_code, 404)

        # Audience join with unknown code returns 400
        res = self.request('post', reverse('competition_audience_join'), {'code': 'ZZZZ9999'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('That code is unavailable or voting has closed.', res.context['error'])

        # Audience join with inactive / closed code returns 400
        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status',))
        track, entries, source = self.active_scored_round([90, 80], audience_weight=0)
        code = CompetitionAudienceJoinCode.objects.create(
            round=source, track=track, code='ABCD2345')
        source.audience_locked_at = timezone.now()
        source.save()

        res = self.request('post', reverse('competition_audience_join'), {'code': 'ABCD2345'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('That code is unavailable or voting has closed.', res.context['error'])
