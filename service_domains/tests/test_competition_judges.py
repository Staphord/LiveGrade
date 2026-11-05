from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from accounts.testing import make_org, make_user
from service_domains.competition_forms import CompetitionJudgeScorecardForm
from service_domains.models import (Competition, CompetitionEntry,
    CompetitionJudgeAudit, CompetitionJudgeInvitation, CompetitionJudgeScore,
    CompetitionRound, CompetitionRoundEntry, CompetitionTrack, OwnedDomainRecord)
from service_domains.operations import (compile_competition_judge_scores,
    issue_competition_judge_invitation, resolve_competition_judge_invitation,
    revoke_competition_judge_invitation, submit_competition_judge_scores)


class CompetitionJudgeOperationsTests(TestCase):
    def setUp(self):
        self.org = make_org('Competition judges')
        self.other_org = make_org('Other competition judges')
        self.owner = make_user('competition-judge-owner')
        self.other = make_user('competition-judge-other')
        self.competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Solar challenge', status=Competition.Status.ACTIVE)
        self.track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Open')
        self.round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Final', order=1)
        self.entries = []
        self.round_entries = []
        for index in range(3):
            entry = CompetitionEntry.objects.create(organization_id=self.org.id,
                created_by=self.owner, track=self.track, name=f'Entry {index + 1}', order=index)
            self.entries.append(entry)
            self.round_entries.append(CompetitionRoundEntry.objects.create(
                organization_id=self.org.id, created_by=self.owner, round=self.round,
                entry=entry, track=self.track))

    def issue(self, email='judge@example.org', *, user=None, org=None, expires=None):
        return issue_competition_judge_invitation(self.competition, self.round, self.track,
            email, expires or timezone.now() + timedelta(hours=1),
            organization=org or self.org.as_claim(), user=user or self.owner)

    def test_invitation_persists_only_digest_and_scorecard_compiles_mean_and_counts(self):
        first, token1 = self.issue()
        second, token2 = self.issue('second@example.org')
        self.assertNotEqual(token1, first.token_digest)
        self.assertEqual(first.token_digest, CompetitionJudgeInvitation.digest_token(token1))
        self.assertEqual(resolve_competition_judge_invitation(token1), first)
        self.assertEqual(CompetitionJudgeAudit.objects.filter(
            invitation=first, action=CompetitionJudgeAudit.Action.INVITED).count(), 1)

        submit_competition_judge_scores(token1, {
            str(self.round_entries[0].pk): '80', str(self.round_entries[1].pk): '70',
            str(self.round_entries[2].pk): '60',
        })
        submit_competition_judge_scores(token2, {
            str(self.round_entries[0].pk): '90', str(self.round_entries[1].pk): '70',
            str(self.round_entries[2].pk): '50',
        })
        compiled = compile_competition_judge_scores(self.competition, self.round, self.track,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(compiled, [
            {'entry_id': self.entries[0].pk, 'score': Decimal('85.00'), 'judge_count': 2},
            {'entry_id': self.entries[1].pk, 'score': Decimal('70.00'), 'judge_count': 2},
            {'entry_id': self.entries[2].pk, 'score': Decimal('55.00'), 'judge_count': 2},
        ])
        self.assertEqual(list(CompetitionRoundEntry.objects.filter(round=self.round)
            .order_by('entry_id').values_list('score', flat=True)),
            [Decimal('85.00'), Decimal('70.00'), Decimal('55.00')])
        self.assertTrue(CompetitionJudgeInvitation.objects.filter(
            pk__in=(first.pk, second.pk), submitted_at__isnull=False).count() == 2)
        self.assertEqual(CompetitionJudgeAudit.objects.filter(
            action=CompetitionJudgeAudit.Action.SUBMITTED, actor_user__isnull=True).count(), 2)

    def test_invitation_and_score_submission_fail_closed_for_scope_and_state(self):
        with self.assertRaises(PermissionDenied):
            self.issue(user=self.other)
        with self.assertRaises(PermissionDenied):
            self.issue(org=self.other_org.as_claim())
        with self.assertRaises(ValidationError):
            self.issue('')
        with self.assertRaises(ValidationError):
            self.issue(expires=timezone.now() - timedelta(seconds=1))

        invitation, token = self.issue()
        with self.assertRaises(ValidationError):
            submit_competition_judge_scores(token, {'bad': '50'})
        with self.assertRaises(ValidationError):
            submit_competition_judge_scores(token, {str(self.round_entries[0].pk): '50'})
        with self.assertRaises(ValidationError):
            submit_competition_judge_scores(token, {
                str(row.pk): ('101' if index == 0 else '50')
                for index, row in enumerate(self.round_entries)})
        with self.assertRaises(ValidationError):
            submit_competition_judge_scores(token, {
                str(row.pk): ('not a score' if index == 0 else '50')
                for index, row in enumerate(self.round_entries)})
        self.assertFalse(CompetitionJudgeScore.objects.exists())

        submit_competition_judge_scores(token, {
            str(row.pk): '50' for row in self.round_entries})
        self.assertEqual(resolve_competition_judge_invitation(token), invitation)
        retried = submit_competition_judge_scores(token, {
            str(row.pk): '50.00' for row in self.round_entries})
        self.assertEqual(len(retried), 3)
        self.assertEqual(CompetitionJudgeScore.objects.count(), 3)
        self.competition.status = Competition.Status.COMPLETE
        self.competition.save(update_fields=('status', 'updated_at'))
        self.assertEqual(resolve_competition_judge_invitation(token), invitation)
        self.assertEqual(len(submit_competition_judge_scores(token, {
            str(row.pk): '50' for row in self.round_entries})), 3)
        with self.assertRaisesMessage(ValidationError, 'already submitted and cannot be changed'):
            submit_competition_judge_scores(token, {
                str(row.pk): '60' for row in self.round_entries})
        self.competition.status = Competition.Status.ACTIVE
        self.competition.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(ValidationError):
            revoke_competition_judge_invitation(self.competition, invitation.pk,
                organization=self.org.as_claim(), user=self.owner)

    def test_owner_can_revoke_pending_invitation_and_audit_is_immutable(self):
        invitation, token = self.issue()
        revoked = revoke_competition_judge_invitation(self.competition, invitation.pk,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIsNotNone(revoked.revoked_at)
        self.assertIsNone(resolve_competition_judge_invitation(token))
        event = CompetitionJudgeAudit.objects.get(invitation=invitation,
            action=CompetitionJudgeAudit.Action.REVOKED)
        self.assertEqual(event.actor_user, self.owner)
        with self.assertRaises(ValidationError):
            CompetitionJudgeAudit.objects.filter(pk=event.pk).update(action='invited')
        with self.assertRaises(ValidationError):
            CompetitionJudgeAudit.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            revoke_competition_judge_invitation(self.competition, invitation.pk,
                organization=self.org.as_claim(), user=self.owner)

    def test_invalid_scorecard_cannot_be_converted_into_submission_payload(self):
        form = CompetitionJudgeScorecardForm({}, entries=self.round_entries)
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValueError):
            form.score_payload()

    def test_judge_invitation_usable_at_requires_pending_unexpired_link(self):
        invitation, _ = self.issue()
        now = timezone.now()
        self.assertTrue(invitation.usable_at(now))
        invitation.submitted_at = now
        self.assertFalse(invitation.usable_at(now))
        invitation.submitted_at = None
        invitation.revoked_at = now
        self.assertFalse(invitation.usable_at(now))
        invitation.revoked_at = None
        self.assertFalse(invitation.usable_at(invitation.expires_at + timedelta(seconds=1)))

    def test_score_submission_denies_when_competition_disappears_after_digest_lookup(self):
        _, token = self.issue()
        with patch('service_domains.operations.Competition.objects.select_for_update') as locked:
            locked.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                submit_competition_judge_scores(token, {
                    str(row.pk): '60' for row in self.round_entries})

    def test_tally_waits_for_open_invitations_and_every_entry_score(self):
        invitation, token = self.issue()
        with self.assertRaises(ValidationError):
            compile_competition_judge_scores(self.competition, self.round, self.track,
                organization=self.org.as_claim(), user=self.owner)
        submit_competition_judge_scores(token, {
            str(row.pk): '65' for row in self.round_entries})
        result = compile_competition_judge_scores(self.competition, self.round, self.track,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual([row['judge_count'] for row in result], [1, 1, 1])
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.submitted_at)

    def test_judge_score_model_rejects_cross_track_rows_and_is_immutable(self):
        invitation, token = self.issue()
        other_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Junior')
        other_entry = CompetitionEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, track=other_track, name='Other')
        other_row = CompetitionRoundEntry.objects.create(organization_id=self.org.id,
            created_by=self.owner, round=self.round, entry=other_entry, track=other_track)
        score = CompetitionJudgeScore(invitation=invitation, round_entry=other_row,
            score=50, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError):
            score.full_clean()

        submit_competition_judge_scores(token, {
            str(row.pk): '50' for row in self.round_entries})
        score = CompetitionJudgeScore.objects.first()
        with self.assertRaises(ValidationError):
            CompetitionJudgeScore.objects.filter(pk=score.pk).update(score=100)
        with self.assertRaises(ValidationError):
            CompetitionJudgeScore.objects.filter(pk=score.pk).delete()
        with self.assertRaises(ValidationError):
            score.delete()

    def test_judge_models_validate_parent_boundaries_and_instance_immutability(self):
        invitation, token = self.issue()
        another_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event', status=Competition.Status.ACTIVE)
        other_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=another_competition, name='Other track')
        other_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=another_competition, name='Other round', order=1)

        with patch.object(OwnedDomainRecord, 'clean'):
            bad_round = CompetitionJudgeInvitation(
                competition=self.competition, round=other_round, track=self.track,
                email='round@example.org', token_digest='a' * 64,
                expires_at=timezone.now() + timedelta(hours=1),
                organization_id=self.org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                bad_round.clean()
            bad_track = CompetitionJudgeInvitation(
                competition=self.competition, round=self.round, track=other_track,
                email='track@example.org', token_digest='b' * 64,
                expires_at=timezone.now() + timedelta(hours=1),
                organization_id=self.org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                bad_track.clean()
            empty_track = CompetitionTrack.objects.create(organization_id=self.org.id,
                created_by=self.owner, competition=self.competition, name='Empty')
            no_entry = CompetitionJudgeInvitation(
                competition=self.competition, round=self.round, track=empty_track,
                email='empty@example.org', token_digest='c' * 64,
                expires_at=timezone.now() + timedelta(hours=1),
                organization_id=self.org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                no_entry.clean()
            expired = CompetitionJudgeInvitation(
                competition=self.competition, round=self.round, track=self.track,
                email='expired@example.org', token_digest='d' * 64,
                expires_at=timezone.now() - timedelta(seconds=1),
                organization_id=self.org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                expired.clean()

            wrong_org_score = CompetitionJudgeScore(invitation=invitation,
                round_entry=self.round_entries[0], score=50,
                organization_id=self.other_org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                wrong_org_score.clean()
            audit_wrong_org = CompetitionJudgeAudit(invitation=invitation,
                action=CompetitionJudgeAudit.Action.INVITED,
                organization_id=self.other_org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                audit_wrong_org.clean()
            audit_wrong_owner = CompetitionJudgeAudit(invitation=invitation,
                action=CompetitionJudgeAudit.Action.INVITED,
                organization_id=self.org.id, created_by=self.other)
            with self.assertRaises(ValidationError):
                audit_wrong_owner.clean()

        submit_competition_judge_scores(token, {
            str(row.pk): '50' for row in self.round_entries})
        score = CompetitionJudgeScore.objects.first()
        with self.assertRaises(ValidationError):
            score.save()
        audit = CompetitionJudgeAudit.objects.first()
        with self.assertRaises(ValidationError):
            audit.save()

    def test_invitation_and_tally_operations_reject_invalid_state_and_scope(self):
        draft = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Draft event')
        draft_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=draft, name='Draft track')
        draft_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=draft, name='Draft round', order=1)
        with self.assertRaises(ValidationError):
            issue_competition_judge_invitation(draft, draft_round, draft_track,
                'draft@example.org', timezone.now() + timedelta(hours=1),
                organization=self.org.as_claim(), user=self.owner)

        second_round = CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Earlier', order=0)
        with self.assertRaises(PermissionDenied):
            issue_competition_judge_invitation(self.competition, second_round, self.track,
                'old@example.org', timezone.now() + timedelta(hours=1),
                organization=self.org.as_claim(), user=self.owner)
        empty_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Empty')
        with self.assertRaises(ValidationError):
            issue_competition_judge_invitation(self.competition, self.round, empty_track,
                'empty@example.org', timezone.now() + timedelta(hours=1),
                organization=self.org.as_claim(), user=self.owner)

        with self.assertRaises(ValidationError):
            submit_competition_judge_scores('', {})
        with self.assertRaises(PermissionDenied):
            submit_competition_judge_scores('missing', {})
        with self.assertRaises(ValidationError):
            submit_competition_judge_scores('missing', [])
        invite, token = self.issue()
        CompetitionRound.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='New round', order=2)
        with self.assertRaises(PermissionDenied):
            submit_competition_judge_scores(token, {
                str(row.pk): '50' for row in self.round_entries})
        self.assertIsNone(resolve_competition_judge_invitation(token))

    def test_judge_tally_rejects_invalid_scope_and_empty_or_unscored_tracks(self):
        with self.assertRaises(PermissionDenied):
            compile_competition_judge_scores(self.competition, self.round, self.track,
                organization=self.other_org.as_claim(), user=self.owner)
        other_competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event', status=Competition.Status.ACTIVE)
        with self.assertRaises(PermissionDenied):
            compile_competition_judge_scores(other_competition, self.round, self.track,
                organization=self.org.as_claim(), user=self.owner)
        empty_track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Empty')
        with self.assertRaises(ValidationError):
            compile_competition_judge_scores(self.competition, self.round, empty_track,
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            compile_competition_judge_scores(self.competition, self.round, self.track,
                organization=self.org.as_claim(), user=self.owner)

    def test_expiry_inactive_competition_and_finalized_round_guards(self):
        self.round_entries[0].score = Decimal('50.00')
        self.round_entries[0].save(update_fields=('score', 'updated_at'))
        with self.assertRaises(ValidationError):
            self.issue('late@example.org')
        self.round_entries[0].score = None
        self.round_entries[0].save(update_fields=('score', 'updated_at'))

        invitation, token = self.issue()
        self.assertIsNone(resolve_competition_judge_invitation('x' * 257))
        self.competition.status = Competition.Status.DRAFT
        self.competition.save(update_fields=('status', 'updated_at'))
        self.assertIsNone(resolve_competition_judge_invitation(token))
        with self.assertRaises(PermissionDenied):
            submit_competition_judge_scores(token, {
                str(row.pk): '60' for row in self.round_entries})

    def test_revoke_requires_an_owned_active_competition_and_existing_invitation(self):
        with self.assertRaises(PermissionDenied):
            revoke_competition_judge_invitation(self.competition, 999999,
                organization=self.org.as_claim(), user=self.owner)
        invitation, _ = self.issue()
        with self.assertRaises(PermissionDenied):
            revoke_competition_judge_invitation(self.competition, invitation.pk,
                organization=self.other_org.as_claim(), user=self.owner)

    def test_new_judge_score_save_inserts_the_immutable_record(self):
        invitation, _ = self.issue()
        score = CompetitionJudgeScore(invitation=invitation,
            round_entry=self.round_entries[0], score=50,
            organization_id=self.org.id, created_by=self.owner)
        score.save(force_insert=True)
        self.assertIsNotNone(score.pk)
