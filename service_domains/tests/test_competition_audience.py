from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user
from service_domains.forms import CompetitionAudienceBallotForm
from service_domains.models import (Competition, CompetitionAudienceBallot,
    CompetitionEntry, CompetitionRound, CompetitionRoundEntry, CompetitionTrack)
from service_domains.operations import (competition_audience_ranked_choice,
    competition_audience_join_code, submit_competition_audience_ballot)


class CompetitionAudienceBallotTests(TestCase):
    def setUp(self):
        self.org = make_org('Audience competition org')
        self.other_org = make_org('Other audience org')
        self.owner = make_user('audience-competition-owner')
        self.other = make_user('audience-competition-other')
        self.competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Innovation Cup', status=Competition.Status.ACTIVE)
        self.track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        self.round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Final', order=1)
        self.entries = []
        for order, name in enumerate(('Aster', 'Birch', 'Cedar'), start=1):
            entry = CompetitionEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=self.track, name=name, order=order)
            self.entries.append(entry)
            CompetitionRoundEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, round=self.round, entry=entry,
                track=self.track, score=80 - order)
        self.url = reverse('competition_audience_vote', kwargs={
            'competition_id': self.competition.public_id,
            'round_id': self.round.pk, 'track_id': self.track.pk,
        })

    def request(self, client, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            return getattr(client, method)(url, *args, **kwargs)

    @property
    def cookie_name(self):
        return f'livegrade_competition_audience_{self.competition.public_id.hex}_{self.round.pk}_{self.track.pk}'

    def post_ranking(self, client, ranking):
        return self.request(client, 'post', self.url, {
            f'rank_{entry.pk}': str(rank)
            for rank, entry in enumerate(ranking, start=1)
        })

    def test_public_ranked_vote_is_signed_scoped_and_unique_per_round_track_browser(self):
        client = Client()
        page = self.request(client, 'get', self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Rank: Aster')
        self.assertContains(page, 'Rank: Birch')
        self.assertTrue(page.cookies[self.cookie_name]['httponly'])
        self.assertEqual(page.cookies[self.cookie_name]['samesite'], 'Lax')
        self.assertIn('private, no-store', page['Cache-Control'])
        self.assertEqual(CompetitionAudienceBallot.objects.count(), 0)

        first = self.post_ranking(client, self.entries)
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, 'ranked ballot was recorded')
        ballot = CompetitionAudienceBallot.objects.get()
        self.assertIsNone(ballot.created_by_id)
        self.assertEqual(ballot.ranking, [entry.pk for entry in self.entries])
        self.assertEqual(len(ballot.device_digest), 64)
        self.assertNotIn(client.cookies[self.cookie_name].value, ballot.device_digest)
        duplicate = self.post_ranking(client, list(reversed(self.entries)))
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(CompetitionAudienceBallot.objects.count(), 1)

    def test_ballot_validation_partial_rankings_and_stored_ranked_choice_count(self):
        form = CompetitionAudienceBallotForm({
            f'rank_{self.entries[0].pk}': '2',
            f'rank_{self.entries[1].pk}': '1',
            f'rank_{self.entries[2].pk}': '',
        }, round_obj=self.round, track=self.track)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['ranking'], [self.entries[1].pk, self.entries[0].pk])
        duplicate_rank = CompetitionAudienceBallotForm({
            f'rank_{self.entries[0].pk}': '1',
            f'rank_{self.entries[1].pk}': '1',
            f'rank_{self.entries[2].pk}': '',
        }, round_obj=self.round, track=self.track)
        self.assertFalse(duplicate_rank.is_valid())
        empty = CompetitionAudienceBallotForm({}, round_obj=self.round, track=self.track)
        self.assertFalse(empty.is_valid())

        rankings = (
            self.entries,
            [self.entries[1], self.entries[2], self.entries[0]],
            [self.entries[1], self.entries[0], self.entries[2]],
        )
        for ranking in rankings:
            client = Client()
            self.request(client, 'get', self.url)
            self.assertEqual(self.post_ranking(client, ranking).status_code, 200)
        result = competition_audience_ranked_choice(self.competition, self.round, self.track,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(result['winner'], self.entries[1].pk)
        self.assertEqual(result['rounds'][0], {
            self.entries[0].pk: 1, self.entries[1].pk: 2, self.entries[2].pk: 0,
        })

    def test_vote_operation_rejects_foreign_scope_round_track_token_and_inactive_competition(self):
        token = CompetitionAudienceBallot.issue_device_token(
            self.competition, self.round, self.track)
        with self.assertRaises(PermissionDenied):
            competition_audience_ranked_choice(self.competition, self.round, self.track,
                organization=self.other_org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            competition_audience_ranked_choice(self.competition, self.round, self.track,
                organization=self.org.as_claim(), user=self.other)
        with self.assertRaises(ValidationError):
            submit_competition_audience_ballot('00000000-0000-0000-0000-000000000000',
                self.round.pk, self.track.pk, token, [self.entries[0].pk])

        other_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event', status=Competition.Status.ACTIVE)
        other_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=other_competition, name='Other track')
        with self.assertRaises(PermissionDenied):
            competition_audience_ranked_choice(self.competition,
                CompetitionRound.objects.create(organization_id=self.org.id,
                    created_by=self.owner, competition=other_competition, name='Other round', order=1),
                self.track, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'ballot is unavailable'):
            submit_competition_audience_ballot(other_competition.public_id,
                self.round.pk, self.track.pk, token, [self.entries[0].pk])
        with self.assertRaisesMessage(ValidationError, 'unavailable'):
            submit_competition_audience_ballot(self.competition.public_id,
                self.round.pk, 999999, token, [self.entries[0].pk])

        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'only open during an active'):
            submit_competition_audience_ballot(self.competition.public_id,
                self.round.pk, self.track.pk, token, [self.entries[0].pk])

    def test_stale_round_and_unknown_entry_or_invalid_token_are_refused(self):
        token = CompetitionAudienceBallot.issue_device_token(
            self.competition, self.round, self.track)
        later = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Next round', order=2)
        with self.assertRaisesMessage(ValidationError, 'latest competition round'):
            submit_competition_audience_ballot(self.competition.public_id,
                self.round.pk, self.track.pk, token, [self.entries[0].pk])

        self.competition.rounds.filter(pk=later.pk).delete()
        with self.assertRaisesMessage(ValidationError, 'token is invalid or expired'):
            submit_competition_audience_ballot(self.competition.public_id,
                self.round.pk, self.track.pk, 'tampered', [self.entries[0].pk])
        with self.assertRaises(ValidationError):
            submit_competition_audience_ballot(self.competition.public_id,
                self.round.pk, self.track.pk, token, [999999])

    def test_ballot_model_is_append_only_and_cross_track_entries_are_rejected(self):
        token = CompetitionAudienceBallot.issue_device_token(
            self.competition, self.round, self.track)
        ballot = submit_competition_audience_ballot(self.competition.public_id,
            self.round.pk, self.track.pk, token, [self.entries[0].pk])
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            ballot.save()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            ballot.delete()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            CompetitionAudienceBallot.objects.filter(pk=ballot.pk).update(ranking=[])
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            CompetitionAudienceBallot.objects.filter(pk=ballot.pk).delete()

        other_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Another Cup')
        other_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=other_competition, name='Other')
        invalid = CompetitionAudienceBallot(competition=self.competition, round=self.round,
            track=other_track, device_digest='a' * 64, ranking=[self.entries[0].pk],
            organization_id=self.org.id)
        with self.assertRaisesMessage(ValidationError, 'track must belong'):
            invalid.clean()

    def test_ballot_model_rejects_invalid_anonymity_scope_round_ranking_and_digest(self):
        cases = [
            (dict(created_by=self.owner), 'cannot be linked to an account'),
            (dict(organization_id=self.other_org.id), 'belong to this organization'),
            (dict(competition=Competition.objects.create(organization_id=self.org.id,
                created_by=self.owner, name='Closed', status=Competition.Status.COMPLETE)),
                'only during an active'),
            (dict(round=CompetitionRound.objects.create(organization_id=self.org.id,
                created_by=self.owner, competition=Competition.objects.create(
                    organization_id=self.org.id, created_by=self.owner, name='Elsewhere'),
                name='Round', order=1)), 'round must belong'),
            (dict(ranking='invalid'), 'Rank at least one'),
            (dict(ranking=[str(self.entries[0].pk)]), 'unique integer identifiers'),
            (dict(ranking=[self.entries[0].pk, self.entries[0].pk]), 'unique integer identifiers'),
            (dict(device_digest='short'), 'digest is invalid'),
        ]
        for kwargs, message in cases:
            with self.subTest(message=message):
                values = dict(competition=self.competition, round=self.round, track=self.track,
                    device_digest='a' * 64, ranking=[self.entries[0].pk], organization_id=self.org.id)
                values.update(kwargs)
                ballot = CompetitionAudienceBallot(**values)
                with self.assertRaisesMessage(ValidationError, message):
                    ballot.clean()

        token = CompetitionAudienceBallot.issue_device_token(self.competition, self.round, self.track)
        other_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Wrong event')
        with self.assertRaisesMessage(ValidationError, 'token is invalid or expired'):
            CompetitionAudienceBallot.digest_device_token(
                token, other_competition, self.round, self.track)
        unscoped = CompetitionAudienceBallot(organization_id=self.org.id,
            device_digest='a' * 64, ranking=[self.entries[0].pk])
        with self.assertRaises(ValidationError):
            unscoped.clean()
        old_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Old', order=0)
        stale = CompetitionAudienceBallot(competition=self.competition, round=old_round,
            track=self.track, organization_id=self.org.id, device_digest='a' * 64,
            ranking=[self.entries[0].pk])
        with self.assertRaisesMessage(ValidationError, 'latest round'):
            stale.clean()

    def test_public_route_hides_unknown_closed_stale_and_empty_ballots(self):
        client = Client()
        missing = self.request(client, 'get', reverse('competition_audience_vote', kwargs={
            'competition_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            'round_id': self.round.pk, 'track_id': self.track.pk,
        }))
        self.assertEqual(missing.status_code, 404)
        latest = self.round
        CompetitionRound.objects.create(organization_id=self.org.id, created_by=self.owner,
            competition=self.competition, name='Later', order=2)
        stale = self.request(client, 'get', self.url)
        self.assertEqual(stale.status_code, 404)
        self.competition.rounds.filter(order=2).delete()
        for row in CompetitionRoundEntry.objects.filter(round=latest):
            row.delete()
        empty = self.request(client, 'get', self.url)
        self.assertEqual(empty.status_code, 404)
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        closed = self.request(client, 'get', self.url)
        self.assertEqual(closed.status_code, 404)

    def test_audience_only_ranked_choice_requires_two_entries_before_a_ballot_is_available(self):
        CompetitionRoundEntry.objects.filter(round=self.round, track=self.track).exclude(
            entry=self.entries[0]).delete()
        client = Client()

        page = self.request(client, 'get', self.url)
        self.assertEqual(page.status_code, 404)

        token = CompetitionAudienceBallot.issue_device_token(
            self.competition, self.round, self.track)
        with self.assertRaisesMessage(ValidationError, 'at least two entries'):
            submit_competition_audience_ballot(self.competition.public_id,
                self.round.pk, self.track.pk, token, [self.entries[0].pk])
        self.assertEqual(CompetitionAudienceBallot.objects.count(), 0)

        join_url = reverse('competition_audience_join')
        code = competition_audience_join_code(self.competition, self.round, self.track)
        with patch('service_domains.competition_views.allow_unknown_bearer_request',
                   return_value=True):
            joined = self.request(client, 'post', join_url, {'code': code})
        self.assertEqual(joined.status_code, 400)
        self.assertContains(joined, 'unavailable or voting has closed', status_code=400)

    def test_public_route_rejects_invalid_cookie_and_invalid_or_missing_ballot(self):
        client = Client()
        client.cookies[self.cookie_name] = 'tampered-token'
        rejected_token = self.request(client, 'post', self.url, {
            f'rank_{self.entries[0].pk}': '1',
        })
        self.assertEqual(rejected_token.status_code, 400)
        self.assertContains(rejected_token, 'token expired', status_code=400)

        no_cookie = Client()
        self.request(no_cookie, 'get', self.url)
        invalid_form = self.request(no_cookie, 'post', self.url, {})
        self.assertEqual(invalid_form.status_code, 400)
        self.assertEqual(CompetitionAudienceBallot.objects.count(), 0)

    def test_a_concurrent_duplicate_insert_is_reported_as_already_voted(self):
        from django.db import IntegrityError
        token = CompetitionAudienceBallot.issue_device_token(self.competition, self.round, self.track)
        with patch.object(CompetitionAudienceBallot, 'save', side_effect=IntegrityError):
            with self.assertRaisesMessage(ValidationError, 'already voted'):
                submit_competition_audience_ballot(self.competition.public_id,
                    self.round.pk, self.track.pk, token, [self.entries[0].pk])

    def test_valid_public_ballot_race_failure_is_rendered_as_validation_error(self):
        from django.core.exceptions import ValidationError
        client = Client()
        self.request(client, 'get', self.url)
        with patch('service_domains.views.submit_competition_audience_ballot',
                   side_effect=ValidationError('The competition stopped accepting ballots.')):
            response = self.post_ranking(client, self.entries[:1])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'stopped accepting ballots', status_code=400)
