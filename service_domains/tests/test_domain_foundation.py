from datetime import timedelta
from decimal import Decimal
import secrets
import time
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.models import AssessmentSession, SessionTemplate, TemplateRubricCategory
from service_domains.models import (
    Competition, CompetitionAdvancementDecision, CompetitionEntry, CompetitionRound,
    CompetitionRoundEntry, CompetitionTrack, PanelCandidate, PanelCriterion,
    PanelAuditEvent, PanelConflict, PanelHearing, PanelInvitation, PanelScore,
    ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit,
    ShowcaseRating, TrainingCompetency, TrainingParticipant, TrainingSeries, TrainingSeriesSession,
)
from service_domains.operations import (
    activate_panel_candidate, compile_panel_results, confirm_panel_signoff,
    competition_advancement, competition_ranked_choice,
    create_next_competition_round,
    declare_panel_conflict, issue_panel_invitation, owned_rows, panel_aggregate,
    release_panel_results, revoke_panel_invitation, submit_panel_score,
    resolve_panel_invitation, showcase_awards, submit_showcase_rating,
    create_next_training_session,
)
from service_domains.rules import aggregate_anonymous_feedback, advance_entries, aggregate_scores, ranked_choice_result, training_trend
from service_domains.onboarding import service_onboarding


class ServiceDomainFixture(TestCase):
    def setUp(self):
        self.org = make_org('Domain org')
        self.other_org = make_org('Other org')
        self.owner = make_user('domain-owner')
        self.other = make_user('domain-other')

    def base(self, model, **values):
        return model(organization_id=self.org.id, created_by=self.owner, **values)


class ServiceOnboardingTests(TestCase):
    def test_guide_heading_and_kicker_match_first_use_or_existing_work(self):
        for service, expected_title in (
                ('panel', 'Your Panel workspace'),
                ('competition', 'Your Competition workspace'),
                ('team', 'Your Team and Training workspace'),
                ('showcase', 'Your Showcase workspace')):
            with self.subTest(service=service):
                first_use = service_onboarding(service)
                self.assertEqual(first_use['kicker'], 'Getting started')
                self.assertIn('first', first_use['title'])

                existing_work = service_onboarding(
                    service, resume_url='/workspace/1/', resume_name='Existing work')
                self.assertEqual(existing_work['kicker'], 'Your workflow')
                self.assertEqual(existing_work['title'], expected_title)
                self.assertEqual(existing_work['resume_url'], '/workspace/1/')


class ScopeAndModelValidationTests(ServiceDomainFixture):
    def test_fail_closed_owner_scope(self):
        mine = self.base(PanelHearing, name='Mine')
        mine.save()
        theirs = PanelHearing.objects.create(organization_id=self.org.id, created_by=self.other, name='Theirs')
        self.assertEqual(list(owned_rows(PanelHearing, self.org.as_claim(), self.owner)), [mine])
        self.assertFalse(owned_rows(PanelHearing, None, self.owner).exists())
        self.assertFalse(owned_rows(PanelHearing, self.org.as_claim(), None).exists())
        self.assertNotIn(theirs, owned_rows(PanelHearing, self.org.as_claim(), self.owner))

    def test_parent_records_must_share_organization_and_owner(self):
        hearing = self.base(PanelHearing, name='Hearing')
        hearing.save()
        same = self.base(PanelCandidate, hearing=hearing, display_name='Candidate')
        same.full_clean()
        wrong_org = PanelCandidate(organization_id=self.other_org.id, created_by=self.owner,
                                   hearing=hearing, display_name='Candidate')
        with self.assertRaisesMessage(ValidationError, 'same organization'):
            wrong_org.full_clean()
        wrong_owner = PanelCandidate(organization_id=self.org.id, created_by=self.other,
                                     hearing=hearing, display_name='Candidate')
        with self.assertRaisesMessage(ValidationError, 'same owner'):
            wrong_owner.full_clean()
        optional_event = self.base(ShowcaseEvent, name='Optional link')
        optional_event.save()
        self.base(ShowcaseExhibit, event=optional_event, name='No category').full_clean()

    def test_panel_status_requires_signed_off_and_release_times(self):
        signed = self.base(PanelHearing, name='H', status=PanelHearing.Status.SIGNED_OFF)
        with self.assertRaises(ValidationError):
            signed.full_clean()
        signed.signed_off_at = timezone.now()
        signed.full_clean()
        released = self.base(PanelHearing, name='H', status=PanelHearing.Status.RELEASED,
                             signed_off_at=timezone.now())
        with self.assertRaises(ValidationError):
            released.full_clean()
        released.released_at = timezone.now()
        released.full_clean()

    def test_showcase_event_dates_and_minor_consent_are_validated(self):
        start = timezone.now()
        bad_dates = self.base(ShowcaseEvent, name='Bad', starts_at=start,
                              ends_at=start - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            bad_dates.full_clean()
        minors = self.base(ShowcaseEvent, name='Minors', status=ShowcaseEvent.Status.OPEN,
                           includes_minors=True)
        with self.assertRaisesMessage(ValidationError, 'consent'):
            minors.full_clean()
        minors.consent_confirmed_at = start
        minors.full_clean()

    def test_showcase_rating_checks_event_and_exhibit_and_status(self):
        event = self.base(ShowcaseEvent, name='Open', status=ShowcaseEvent.Status.OPEN)
        event.save()
        exhibit = self.base(ShowcaseExhibit, event=event, name='Exhibit')
        exhibit.save()
        key = ShowcaseRating.issue_device_token(event)
        self.assertEqual(len(ShowcaseRating.digest_device_key(key, event)), 64)
        with self.assertRaises(ValidationError):
            ShowcaseRating.digest_device_key('short', event)
        signer = signing.TimestampSigner(salt='livegrade.showcase.device.v1')
        malformed = signer.sign_object({'event': str(event.public_id), 'device': 'short'})
        with self.assertRaises(ValidationError):
            ShowcaseRating.digest_device_key(malformed, event)
        non_object = signer.sign_object(['not', 'a', 'device', 'object'])
        with self.assertRaises(ValidationError):
            ShowcaseRating.digest_device_key(non_object, event)
        expired_payload = {'event': str(event.public_id), 'device': secrets.token_urlsafe(32)}
        with patch('django.core.signing.time.time', return_value=time.time() - 8 * 24 * 60 * 60):
            expired_token = signer.sign_object(expired_payload)
        with self.assertRaisesMessage(ValidationError, 'expired'):
            ShowcaseRating.digest_device_key(expired_token, event)
        valid_digest = ShowcaseRating.digest_device_key(key, event)
        valid = self.base(ShowcaseRating, event=event, exhibit=exhibit,
                          device_digest=valid_digest, stars=5)
        valid.full_clean()
        valid.device_digest = 'bad'
        with self.assertRaises(ValidationError):
            valid.full_clean()
        valid.device_digest = valid_digest
        wrong_event = self.base(ShowcaseEvent, name='Other', status=ShowcaseEvent.Status.OPEN)
        wrong_event.save()
        foreign_category = self.base(ShowcaseCategory, event=wrong_event, name='Foreign')
        foreign_category.save()
        exhibit.category = foreign_category
        with self.assertRaisesMessage(ValidationError, 'must belong to this showcase'):
            exhibit.full_clean()
        exhibit.category = None
        other_token = ShowcaseRating.issue_device_token(wrong_event)
        with self.assertRaises(ValidationError):
            ShowcaseRating.digest_device_key(other_token, event)
        invalid = self.base(ShowcaseRating, event=wrong_event, exhibit=exhibit,
                            device_digest=ShowcaseRating.digest_device_key(other_token, wrong_event), stars=4)
        with self.assertRaisesMessage(ValidationError, 'must belong'):
            invalid.full_clean()
        event.status = ShowcaseEvent.Status.CLOSED
        event.save()
        with self.assertRaisesMessage(ValidationError, 'only while'):
            valid.full_clean()

    def test_team_series_session_refuses_non_team_sessions(self):
        series = self.base(TrainingSeries, name='Training')
        series.save()
        missing = self.base(TrainingSeriesSession, series=series, assessment_session=None, sequence=1)
        with self.assertRaises(ValidationError):
            missing.full_clean()
        classroom = AssessmentSession.objects.create(organization_id=self.org.id, created_by=self.owner,
                                                     service='classroom', name='Classroom')
        link = self.base(TrainingSeriesSession, series=series, assessment_session=classroom, sequence=1)
        with self.assertRaisesMessage(ValidationError, 'only Team'):
            link.full_clean()
        team = AssessmentSession.objects.create(organization_id=self.org.id, created_by=self.owner,
                                               service='team', name='Team')
        link.assessment_session = team
        link.full_clean()
        foreign_org_session = AssessmentSession.objects.create(organization_id=self.other_org.id,
            created_by=self.owner, service='team', name='Foreign org')
        link.assessment_session = foreign_org_session
        with self.assertRaisesMessage(ValidationError, 'same organization'):
            link.full_clean()
        foreign_owner_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.other, service='team', name='Foreign owner')
        link.assessment_session = foreign_owner_session
        with self.assertRaisesMessage(ValidationError, 'same owner'):
            link.full_clean()

    def test_referenced_domain_choices_validate(self):
        series = self.base(TrainingSeries, name='Training')
        series.save()
        competency = self.base(TrainingCompetency, series=series, name='Evidence', passing_score=75)
        competency.full_clean()
        competition = self.base(Competition, name='Challenge')
        competition.save()
        track = self.base(CompetitionTrack, competition=competition, name='Open')
        track.full_clean()
        track.save()
        track.advancement_value = Decimal('1.5')
        with self.assertRaises(ValidationError):
            track.full_clean()
        track.advancement_rule = 'threshold'
        track.advancement_value = Decimal('101')
        with self.assertRaises(ValidationError):
            track.full_clean()
        entry = self.base(CompetitionEntry, track=track, name='Project', reference='P-1')
        entry.full_clean()


class CompetitionAdvancementOperationTests(ServiceDomainFixture):
    def setUp(self):
        super().setUp()
        self.competition = self.base(Competition, name='Challenge', status=Competition.Status.ACTIVE)
        self.competition.save()
        self.track = self.base(CompetitionTrack, competition=self.competition,
            name='Open', advancement_rule='top_n', advancement_value=Decimal('2'))
        self.track.save()
        self.entries = []
        for index, name in enumerate(('A', 'B', 'C'), start=1):
            entry = self.base(CompetitionEntry, track=self.track, name=name, order=index)
            entry.save()
            self.entries.append(entry)
        self.source = self.base(CompetitionRound, competition=self.competition,
            name='Round 1', order=1)
        self.source.save()
        for entry, score in zip(self.entries, (90, 80, 40)):
            row = self.base(CompetitionRoundEntry, round=self.source, entry=entry,
                track=self.track, score=Decimal(score))
            row.save()

    def advance(self, **kwargs):
        return create_next_competition_round(self.competition, self.source, 'Final',
            organization=self.org.as_claim(), user=self.owner, **kwargs)

    def test_top_n_advancement_persists_exact_entrants_and_rule(self):
        next_round, selected = self.advance()
        self.assertEqual(selected, {self.track.pk: [self.entries[0].pk, self.entries[1].pk]})
        self.assertEqual(list(next_round.round_entries.values_list('entry_id', flat=True)),
            [self.entries[0].pk, self.entries[1].pk])
        decision = CompetitionAdvancementDecision.objects.get(source_round=self.source)
        self.assertEqual(decision.selected_entry_ids, [self.entries[0].pk, self.entries[1].pk])
        self.assertEqual(decision.rule_value, Decimal('2'))
        self.assertIsNotNone(CompetitionRound.objects.get(pk=self.source.pk).advancement_locked_at)

    def test_audience_weighted_advancement_waits_for_frozen_combined_scores(self):
        self.competition.audience_weight = Decimal('25')
        self.competition.save(update_fields=('audience_weight', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'Finalize and lock weighted audience results'):
            self.advance()
        self.assertEqual(CompetitionRound.objects.count(), 1)

    def test_cutoff_tie_requires_reasoned_head_judge_and_is_atomic(self):
        self.source.round_entries.filter(entry__in=self.entries[1:]).update(score=80)
        with self.assertRaisesMessage(ValidationError, 'tie at the advancement boundary'):
            self.advance()
        self.assertEqual(CompetitionRound.objects.count(), 1)
        next_round, selected = self.advance(
            head_judge_decisions={self.track.pk: [self.entries[1].pk]},
            head_judge_reasons={self.track.pk: 'Published head-judge ruling'})
        self.assertEqual(selected[self.track.pk], [self.entries[0].pk, self.entries[1].pk])
        decision = CompetitionAdvancementDecision.objects.get(next_round=next_round)
        self.assertEqual(decision.head_judge_decision, [self.entries[1].pk])
        self.assertEqual(decision.head_judge_reason, 'Published head-judge ruling')

    def test_cutoff_tie_with_missing_reason_creates_no_round(self):
        self.source.round_entries.filter(entry__in=self.entries[1:]).update(score=80)
        with self.assertRaisesMessage(ValidationError, 'needs a reason'):
            self.advance(head_judge_decisions={self.track.pk: [self.entries[1].pk]})
        self.assertEqual(CompetitionRound.objects.count(), 1)

    def test_missing_score_and_wrong_scope_are_refused(self):
        self.source.round_entries.filter(entry=self.entries[0]).update(score=None)
        with self.assertRaisesMessage(ValidationError, 'needs a score'):
            self.advance()
        with self.assertRaises(PermissionDenied):
            create_next_competition_round(self.competition, self.source, 'Final',
                organization=self.other_org.as_claim(), user=self.owner)

    def test_round_entry_cannot_claim_another_track(self):
        other_track = self.base(CompetitionTrack, competition=self.competition, name='Other')
        other_track.save()
        row = self.base(CompetitionRoundEntry, round=self.source, entry=self.entries[0],
            track=other_track, score=90)
        with self.assertRaisesMessage(ValidationError, 'use the entry track'):
            row.full_clean()

    def test_round_entry_and_decision_reject_cross_competition_references(self):
        other_competition = self.base(Competition, name='Other')
        other_competition.save()
        other_track = self.base(CompetitionTrack, competition=other_competition, name='Other')
        other_track.save()
        other_round = self.base(CompetitionRound, competition=other_competition,
            name='Other round', order=1)
        other_round.save()
        row = self.base(CompetitionRoundEntry, round=other_round, entry=self.entries[0],
            track=self.track, score=90)
        with self.assertRaises(ValidationError):
            row.full_clean()
        decision = self.base(CompetitionAdvancementDecision, source_round=self.source,
            next_round=other_round, track=self.track, rule='top_n', rule_value=1,
            selected_entry_ids=[])
        with self.assertRaisesMessage(ValidationError, 'same competition'):
            decision.full_clean()
        decision.next_round = self.source
        decision.track = other_track
        with self.assertRaisesMessage(ValidationError, 'track'):
            decision.full_clean()

    def test_decision_validates_json_and_head_judge_reason_and_is_immutable(self):
        next_round = self.base(CompetitionRound, competition=self.competition,
            name='Final', order=2)
        next_round.save()
        decision = self.base(CompetitionAdvancementDecision, source_round=self.source,
            next_round=next_round, track=self.track, rule='top_n', rule_value=2,
            tie_break_order='bad', selected_entry_ids=[])
        with self.assertRaisesMessage(ValidationError, 'must be lists'):
            decision.full_clean()
        decision.tie_break_order = []
        decision.head_judge_decision = [self.entries[1].pk]
        with self.assertRaisesMessage(ValidationError, 'Record why'):
            decision.full_clean()
        decision.head_judge_reason = 'Tie ruled'
        decision.full_clean()
        decision.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            decision.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            decision.delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            CompetitionAdvancementDecision.objects.filter(pk=decision.pk).delete()

    def test_decision_model_rejects_invalid_relationship_and_selection_shapes(self):
        next_round = self.base(CompetitionRound, competition=self.competition,
            name='Final', order=2)
        next_round.save()
        other_competition = self.base(Competition, name='Elsewhere')
        other_competition.save()
        other_round = self.base(CompetitionRound, competition=other_competition,
            name='Elsewhere round', order=1)
        other_round.save()
        decision = self.base(CompetitionAdvancementDecision, source_round=self.source,
            next_round=next_round, track=self.track, rule='top_n', rule_value=2,
            selected_entry_ids=[])
        decision.next_round = other_round
        with self.assertRaisesMessage(ValidationError, 'same competition'):
            decision.clean()
        decision.next_round = next_round
        decision.track = None
        with self.assertRaisesMessage(ValidationError, 'must be a list'):
            decision.selected_entry_ids = 'bad'
            decision.clean()
        decision.selected_entry_ids = []
        other_track = self.base(CompetitionTrack, competition=other_competition, name='Elsewhere track')
        other_track.save()
        decision.track_id = other_track.pk
        decision.next_round = next_round
        with self.assertRaisesMessage(ValidationError, 'track'):
            decision.clean()
        decision.track = self.track
        decision.next_round = None
        decision.clean()

    def test_invalid_state_payloads_missing_track_and_already_advanced_are_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'active competition'):
            self.competition.status = Competition.Status.DRAFT
            self.competition.save()
            self.advance()
        self.competition.status = Competition.Status.ACTIVE
        self.competition.save()
        with self.assertRaisesMessage(ValidationError, 'grouped by track'):
            self.advance(tie_break_orders=[])
        with self.assertRaisesMessage(ValidationError, 'unknown track'):
            self.advance(tie_break_orders={'not-a-track': []})
        with self.assertRaisesMessage(ValidationError, 'unknown track'):
            self.advance(tie_break_orders={999999: []})
        CompetitionRound.objects.create(competition=self.competition, organization_id=self.org.id,
            created_by=self.owner, name='Later', order=2)
        with self.assertRaisesMessage(ValidationError, 'latest competition round'):
            self.advance()
        CompetitionRound.objects.filter(order=2).delete()
        self.source.advancement_locked_at = timezone.now()
        self.source.save()
        with self.assertRaisesMessage(ValidationError, 'already advanced'):
            self.advance()

    def test_invalid_round_name_and_duplicate_tie_instructions_are_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'next-round name'):
            create_next_competition_round(self.competition, self.source, ' ',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'not both'):
            self.advance(tie_break_orders={self.track.pk: [self.entries[0].pk]},
                head_judge_decisions={self.track.pk: [self.entries[1].pk]})
        with self.assertRaisesMessage(ValidationError, 'requires a head-judge decision'):
            self.advance(head_judge_reasons={self.track.pk: 'Stray reason'})

    def test_track_without_entries_and_empty_threshold_advancement_are_rejected(self):
        second_track = self.base(CompetitionTrack, competition=self.competition,
            name='Second', advancement_rule='threshold', advancement_value=99)
        second_track.save()
        with self.assertRaisesMessage(ValidationError, 'Every competition track'):
            self.advance()
        # All scores remain below the second track threshold, with one valid entry
        # to satisfy its roster requirement.
        second_entry = self.base(CompetitionEntry, track=second_track, name='Second entry')
        second_entry.save()
        self.base(CompetitionRoundEntry, round=self.source, entry=second_entry,
            track=second_track, score=50).save()
        self.track.advancement_rule = 'threshold'
        self.track.advancement_value = 99
        self.track.save()
        with self.assertRaisesMessage(ValidationError, 'advanced no entries'):
            self.advance()

    def test_missing_tracks_source_round_and_corrupt_track_rows_fail_closed(self):
        with self.assertRaises(PermissionDenied):
            create_next_competition_round(self.competition, self.base(CompetitionRound,
                competition=self.competition, name='Fake', order=5), 'Final',
                organization=self.org.as_claim(), user=self.owner)
        empty_competition = self.base(Competition, name='No tracks', status=Competition.Status.ACTIVE)
        empty_competition.save()
        empty_round = self.base(CompetitionRound, competition=empty_competition,
            name='Round 1', order=1)
        empty_round.save()
        with self.assertRaisesMessage(ValidationError, 'at least one competition track'):
            create_next_competition_round(empty_competition, empty_round, 'Final',
                organization=self.org.as_claim(), user=self.owner)
        rows = list(self.source.round_entries.all())
        other_track = self.base(CompetitionTrack, competition=self.competition, name='Other')
        other_track.save()
        row = rows[0]
        row.track = other_track
        row.save(update_fields=('track', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'wrong competition track'):
            self.advance()

    def test_advancement_decision_is_append_only(self):
        self.advance()
        decision = CompetitionAdvancementDecision.objects.get(source_round=self.source)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            CompetitionAdvancementDecision.objects.filter(pk=decision.pk).update(rule='threshold')


class PanelOperationTests(ServiceDomainFixture):
    def setUp(self):
        super().setUp()
        self.hearing = self.base(PanelHearing, name='Defence', status=PanelHearing.Status.OPEN)
        self.hearing.save()
        self.candidate = self.base(PanelCandidate, hearing=self.hearing, display_name='Candidate A')
        self.candidate.save()
        self.criterion = self.base(PanelCriterion, hearing=self.hearing, name='Evidence')
        self.criterion.save()
        self.expires = timezone.now() + timedelta(hours=1)
        self.invitation, self.token = issue_panel_invitation(self.hearing, 'examiner@example.test',
            self.expires, organization=self.org.as_claim(), user=self.owner)

    def test_invitation_persists_only_hash_and_token_expires_or_revokes(self):
        expires = timezone.now() + timedelta(hours=1)
        invitation, token = issue_panel_invitation(self.hearing, 'token-check@example.test', expires,
                                                   organization=self.org.as_claim(), user=self.owner)
        self.assertNotEqual(invitation.token_digest, token)
        self.assertEqual(resolve_panel_invitation(token), invitation)
        self.assertIsNone(resolve_panel_invitation(''))
        self.assertIsNone(resolve_panel_invitation('x' * 257))
        invitation.accepted_at = timezone.now()
        self.assertFalse(invitation.usable_at())
        invitation.save()
        self.assertIsNone(resolve_panel_invitation(token))
        invitation.accepted_at = None
        invitation.revoked_at = timezone.now()
        self.assertFalse(invitation.usable_at())
        invitation.revoked_at = None
        self.assertFalse(invitation.usable_at(expires + timedelta(seconds=1)))

    def test_expired_invitation_cannot_be_created(self):
        invitation = PanelInvitation(hearing=self.hearing, organization_id=self.org.id,
            created_by=self.owner, email='expired@example.test',
            token_digest=PanelInvitation.digest_token('expired'),
            expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            invitation.full_clean()

    def test_invitation_requires_owner_and_open_state(self):
        expires = timezone.now() + timedelta(hours=1)
        with self.assertRaises(PermissionDenied):
            issue_panel_invitation(self.hearing, 'x@example.test', expires,
                                   organization=self.other_org.as_claim(), user=self.owner)
        self.hearing.status = PanelHearing.Status.RELEASED
        self.hearing.signed_off_at = timezone.now()
        self.hearing.released_at = timezone.now()
        self.hearing.save()
        with self.assertRaisesMessage(ValidationError, 'not accepting'):
            issue_panel_invitation(self.hearing, 'x@example.test', expires,
                                   organization=self.org.as_claim(), user=self.owner)

    def test_signoff_and_release_are_ordered_and_owner_scoped(self):
        with self.assertRaisesMessage(ValidationError, 'under review'):
            confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        activate_panel_candidate(self.hearing, self.candidate,
            organization=self.org.as_claim(), user=self.owner)
        submit_panel_score(self.token, self.candidate, self.criterion, '87.50')
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.other)
        with self.assertRaises(PermissionDenied):
            release_panel_results(self.hearing, organization=self.org.as_claim(), user=self.other)
        with self.assertRaisesMessage(ValidationError, 'signed off'):
            release_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        signed = confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(signed.status, PanelHearing.Status.SIGNED_OFF)
        self.assertIsNotNone(signed.signed_off_at)
        released = release_panel_results(signed, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(released.status, PanelHearing.Status.RELEASED)
        self.assertIsNotNone(released.released_at)
        audit = list(PanelAuditEvent.objects.filter(action__in=(
            PanelAuditEvent.Action.RESULTS_SIGNED_OFF, PanelAuditEvent.Action.RESULTS_RELEASED)))
        self.assertEqual([row.action for row in audit], [
            PanelAuditEvent.Action.RESULTS_SIGNED_OFF, PanelAuditEvent.Action.RESULTS_RELEASED])
        self.assertTrue(all(row.actor_user_id == self.owner.pk and row.reason for row in audit))

    def test_panel_aggregation_uses_exact_decimal_rules(self):
        self.assertEqual(panel_aggregate(['70', '80']), Decimal('75'))
        self.assertEqual(panel_aggregate(['10', '20', '80'], 'median'), Decimal('20'))
        self.assertEqual(panel_aggregate(['0', '10', '20', '30', '100'], 'trimmed_mean', '0.2'), Decimal('20'))

    def test_invited_examiner_can_score_once_per_candidate_criterion(self):
        activate_panel_candidate(self.hearing, self.candidate,
            organization=self.org.as_claim(), user=self.owner)
        score = submit_panel_score(self.token, self.candidate, self.criterion, '87.50', 'Clear evidence')
        self.assertEqual(score.score, Decimal('87.50'))
        self.assertEqual(score.comment, 'Clear evidence')
        self.assertEqual(PanelScore.objects.count(), 1)
        audit = PanelAuditEvent.objects.filter(action=PanelAuditEvent.Action.SCORE_SUBMITTED).get()
        self.assertEqual(audit.invitation_id, self.invitation.pk)
        self.assertEqual(audit.candidate_id, self.candidate.pk)
        self.assertEqual(audit.reason, 'Criterion score submitted.')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            audit.reason = 'changed'
            audit.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelAuditEvent.objects.filter(pk=audit.pk).update(reason='changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            audit.delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelAuditEvent.objects.filter(pk=audit.pk).delete()
        with self.assertRaises(ValidationError):
            submit_panel_score(self.token, self.candidate, self.criterion, '90')

    def test_conflict_declaration_is_reasoned_and_blocks_scoring(self):
        activate_panel_candidate(self.hearing, self.candidate,
            organization=self.org.as_claim(), user=self.owner)
        conflict = declare_panel_conflict(self.token, self.candidate, 'I supervised this candidate.')
        self.assertEqual(conflict.reason, 'I supervised this candidate.')
        self.assertEqual(conflict.invitation_id, self.invitation.pk)
        audit = PanelAuditEvent.objects.filter(action=PanelAuditEvent.Action.CONFLICT_DECLARED).get()
        self.assertEqual(audit.reason, conflict.reason)
        self.assertIsNone(audit.actor_user_id)
        with self.assertRaisesMessage(PermissionDenied, 'recused'):
            submit_panel_score(self.token, self.candidate, self.criterion, '90')
        self.assertFalse(PanelScore.objects.exists())

    def test_panel_score_rejects_foreign_candidate_or_criterion(self):
        activate_panel_candidate(self.hearing, self.candidate,
            organization=self.org.as_claim(), user=self.owner)
        other_hearing = self.base(PanelHearing, name='Other hearing', status=PanelHearing.Status.OPEN)
        other_hearing.save()
        other_candidate = self.base(PanelCandidate, hearing=other_hearing, display_name='Other')
        other_candidate.save()
        other_criterion = self.base(PanelCriterion, hearing=other_hearing, name='Other criterion')
        other_criterion.save()
        with self.assertRaises(PermissionDenied):
            submit_panel_score(self.token, other_candidate, self.criterion, '90')
        with self.assertRaises(PermissionDenied):
            submit_panel_score(self.token, self.candidate, other_criterion, '90')
        with self.assertRaises(ValidationError):
            submit_panel_score(self.token, self.candidate, self.criterion, '100.01')
        self.assertFalse(PanelScore.objects.exists())

    def test_panel_record_validation_rejects_foreign_hearing_and_blank_conflict_reason(self):
        other_hearing = self.base(PanelHearing, name='Other hearing', status=PanelHearing.Status.OPEN)
        other_hearing.save()
        other_candidate = self.base(PanelCandidate, hearing=other_hearing, display_name='Other')
        other_candidate.save()
        other_criterion = self.base(PanelCriterion, hearing=other_hearing, name='Other criterion')
        other_criterion.save()
        conflict = self.base(PanelConflict, invitation=self.invitation,
            candidate=other_candidate, reason='Conflict')
        with self.assertRaisesMessage(ValidationError, 'invited hearing'):
            conflict.full_clean()
        conflict.candidate = self.candidate
        conflict.reason = '   '
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            conflict.full_clean()
        score = self.base(PanelScore, invitation=self.invitation,
            candidate=other_candidate, criterion=self.criterion, score='80')
        with self.assertRaisesMessage(ValidationError, 'invited hearing'):
            score.full_clean()
        score.candidate = self.candidate
        score.criterion = other_criterion
        with self.assertRaisesMessage(ValidationError, 'criterion must belong'):
            score.full_clean()

    def test_conflict_declaration_rejects_missing_or_foreign_invitation_context(self):
        with self.assertRaises(PermissionDenied):
            declare_panel_conflict('', self.candidate, 'Conflict')
        with self.assertRaises(PermissionDenied):
            declare_panel_conflict('unknown token', self.candidate, 'Conflict')
        other_hearing = self.base(PanelHearing, name='Other hearing', status=PanelHearing.Status.OPEN)
        other_hearing.save()
        other_candidate = self.base(PanelCandidate, hearing=other_hearing, display_name='Other')
        other_candidate.save()
        with self.assertRaises(PermissionDenied):
            declare_panel_conflict(self.token, other_candidate, 'Conflict')

    def test_score_submission_rejects_missing_token(self):
        with self.assertRaises(PermissionDenied):
            submit_panel_score('', self.candidate, self.criterion, '80')

    def test_panel_submission_denies_if_hearing_disappears_before_lock(self):
        with patch('service_domains.operations.PanelHearing.objects.select_for_update') as lock_hearing:
            lock_hearing.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                submit_panel_score(self.token, self.candidate, self.criterion, '80')

    def test_owner_can_revoke_invitation_with_reason_and_audit(self):
        with self.assertRaises(PermissionDenied):
            revoke_panel_invitation(self.hearing, self.invitation, 'Wrong owner.',
                organization=self.org.as_claim(), user=self.other)
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            revoke_panel_invitation(self.hearing, self.invitation, '   ',
                organization=self.org.as_claim(), user=self.owner)
        revoked = revoke_panel_invitation(self.hearing, self.invitation, 'Examiner withdrew.',
            organization=self.org.as_claim(), user=self.owner)
        self.assertIsNotNone(revoked.revoked_at)
        self.assertIsNone(resolve_panel_invitation(self.token))
        event = PanelAuditEvent.objects.get(action=PanelAuditEvent.Action.INVITATION_REVOKED)
        self.assertEqual(event.reason, 'Examiner withdrew.')
        self.assertEqual(event.actor_user_id, self.owner.pk)
        self.assertEqual(event.invitation_id, self.invitation.pk)
        with self.assertRaisesMessage(ValidationError, 'already been revoked'):
            revoke_panel_invitation(self.hearing, self.invitation, 'Again.',
                organization=self.org.as_claim(), user=self.owner)
        another = self.base(PanelHearing, name='Another hearing')
        another.save()
        with self.assertRaises(PermissionDenied):
            revoke_panel_invitation(another, self.invitation, 'Wrong hearing.',
                organization=self.org.as_claim(), user=self.owner)

    def test_audit_events_require_same_hearing_and_a_reason(self):
        other_hearing = self.base(PanelHearing, name='Other')
        other_hearing.save()
        other_candidate = self.base(PanelCandidate, hearing=other_hearing, display_name='Other')
        other_candidate.save()
        audit = self.base(PanelAuditEvent, hearing=self.hearing,
            invitation=self.invitation, candidate=other_candidate,
            action=PanelAuditEvent.Action.SCORE_SUBMITTED, reason='Score submitted.')
        self.assertNotEqual(self.invitation.hearing_id, other_hearing.pk)
        audit.invitation = issue_panel_invitation(other_hearing, 'other@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner)[0]
        with self.assertRaisesMessage(ValidationError, 'invitation must belong'):
            audit.full_clean()
        audit.invitation = self.invitation
        with self.assertRaisesMessage(ValidationError, 'candidate must belong'):
            audit.full_clean()
        audit.candidate = self.candidate
        audit.reason = ' '
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            audit.full_clean()

    def test_revoked_or_expired_invitation_cannot_submit(self):
        self.invitation.revoked_at = timezone.now()
        self.invitation.save(update_fields=('revoked_at',))
        with self.assertRaises(PermissionDenied):
            submit_panel_score(self.token, self.candidate, self.criterion, '90')

    def test_hearing_review_closes_invitation_and_scoring(self):
        self.hearing.status = PanelHearing.Status.REVIEW
        self.hearing.save(update_fields=('status',))
        with self.assertRaisesMessage(ValidationError, 'not accepting scores'):
            submit_panel_score(self.token, self.candidate, self.criterion, '90')
        with self.assertRaisesMessage(ValidationError, 'not accepting examiner'):
            issue_panel_invitation(self.hearing, 'new@example.test', self.expires,
                                   organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'not accepting conflict'):
            declare_panel_conflict(self.token, self.candidate, 'Conflict')
        self.assertFalse(PanelScore.objects.exists())
        self.invitation.revoked_at = None
        self.invitation.expires_at = timezone.now() - timedelta(seconds=1)
        self.invitation.save(update_fields=('revoked_at', 'expires_at'))
        with self.assertRaises(PermissionDenied):
            submit_panel_score(self.token, self.candidate, self.criterion, '90')


class ShowcaseOperationTests(ServiceDomainFixture):
    def setUp(self):
        super().setUp()
        self.event = self.base(ShowcaseEvent, name='Fair', status=ShowcaseEvent.Status.OPEN)
        self.event.save()
        self.category = self.base(ShowcaseCategory, event=self.event, name='Science')
        self.category.save()
        self.exhibit = self.base(ShowcaseExhibit, event=self.event, category=self.category, name='Solar')
        self.exhibit.save()

    def test_rating_accepts_once_with_digest_and_rejects_duplicate(self):
        key = ShowcaseRating.issue_device_token(self.event)
        rating = submit_showcase_rating(self.event, self.exhibit, key, 5)
        self.assertEqual(len(rating.device_digest), 64)
        self.assertNotIn(key, rating.device_digest)
        with self.assertRaisesMessage(ValidationError, 'already rated'):
            submit_showcase_rating(self.event, self.exhibit, key, 3)

    def test_rating_rejects_closed_event_and_foreign_exhibit(self):
        other = self.base(ShowcaseEvent, name='Other', status=ShowcaseEvent.Status.OPEN)
        other.save()
        alien = self.base(ShowcaseExhibit, event=other, name='Other')
        alien.save()
        with self.assertRaisesMessage(ValidationError, 'not part'):
            submit_showcase_rating(self.event, alien, 'x' * 40, 3)
        self.event.status = ShowcaseEvent.Status.CLOSED
        self.event.save(update_fields=('status',))
        with self.assertRaisesMessage(ValidationError, 'not currently'):
            submit_showcase_rating(self.event, self.exhibit, ShowcaseRating.issue_device_token(self.event), 3)

    def test_rating_reloads_event_inside_transaction(self):
        missing = self.base(ShowcaseEvent, id=999999, name='Deleted', status=ShowcaseEvent.Status.OPEN)
        with self.assertRaisesMessage(ValidationError, 'not currently'):
            submit_showcase_rating(missing, self.exhibit,
                ShowcaseRating.issue_device_token(self.event), 4)

    def test_rating_requires_minor_consent_and_active_window(self):
        key = ShowcaseRating.issue_device_token(self.event)
        self.event.includes_minors = True
        self.event.save(update_fields=('includes_minors',))
        with self.assertRaisesMessage(ValidationError, 'consent'):
            submit_showcase_rating(self.event, self.exhibit, key, 5)
        self.event.consent_confirmed_at = timezone.now()
        self.event.starts_at = timezone.now() + timedelta(minutes=1)
        self.event.save(update_fields=('consent_confirmed_at', 'starts_at'))
        with self.assertRaisesMessage(ValidationError, 'not opened'):
            submit_showcase_rating(self.event, self.exhibit, key, 5)
        self.event.starts_at = None
        self.event.ends_at = timezone.now() - timedelta(seconds=1)
        self.event.save(update_fields=('starts_at', 'ends_at'))
        with self.assertRaisesMessage(ValidationError, 'window has ended'):
            submit_showcase_rating(self.event, self.exhibit, key, 5)

    def test_rating_database_conflict_is_reported_as_duplicate(self):
        with patch.object(ShowcaseRating, 'save', side_effect=IntegrityError('unique race')):
            with self.assertRaisesMessage(ValidationError, 'already rated'):
                submit_showcase_rating(self.event, self.exhibit, ShowcaseRating.issue_device_token(self.event), 4)

    def test_rating_validation_errors_other_than_duplicates_are_preserved(self):
        with self.assertRaises(ValidationError):
            submit_showcase_rating(self.event, self.exhibit, ShowcaseRating.issue_device_token(self.event), 9)

    def test_awards_require_public_release_and_use_manual_known_answer(self):
        with self.assertRaises(PermissionDenied):
            showcase_awards(self.event)
        other = self.base(ShowcaseExhibit, event=self.event, category=self.category, name='Wind')
        other.save()
        for stars in (5, 4, 5):
            submit_showcase_rating(self.event, self.exhibit, ShowcaseRating.issue_device_token(self.event), stars)
        submit_showcase_rating(self.event, other, ShowcaseRating.issue_device_token(self.event), 5)
        submit_showcase_rating(self.event, other, ShowcaseRating.issue_device_token(self.event), 4)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save()
        result = showcase_awards(self.event)
        self.assertIsNone(result['categories'][0]['winner'])
        self.assertIsNone(result['categories'][0]['mean'])
        self.assertEqual(result['categories'][0]['judge_count'], 0)
        self.assertEqual(result['audience_favorite'], self.exhibit)
        self.assertEqual(result['audience_favorite_mean'], Decimal('14') / Decimal('3'))

    def test_empty_category_and_event_have_no_award(self):
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save()
        empty_event = self.base(ShowcaseEvent, name='Empty', status=ShowcaseEvent.Status.PUBLISHED,
                                public_results=True)
        empty_event.save()
        self.assertIsNone(showcase_awards(empty_event)['audience_favorite'])
        empty_category = self.base(ShowcaseCategory, event=empty_event, name='Empty category')
        empty_category.save()
        self.base(ShowcaseExhibit, event=empty_event, category=empty_category, name='Unrated').save()
        self.assertIsNone(showcase_awards(empty_event)['categories'][0]['winner'])

    def test_equal_showcase_scores_are_returned_as_ties_not_hidden_name_order(self):
        second = self.base(ShowcaseExhibit, event=self.event, category=self.category, name='Second')
        second.save()
        submit_showcase_rating(self.event, self.exhibit, ShowcaseRating.issue_device_token(self.event), 4)
        submit_showcase_rating(self.event, second, ShowcaseRating.issue_device_token(self.event), 4)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save()
        result = showcase_awards(self.event)
        self.assertIsNone(result['categories'][0]['winner'])
        self.assertEqual(result['categories'][0]['tied'], [])
        self.assertIsNone(result['audience_favorite'])
        self.assertEqual(set(result['audience_favorite_tied']), {self.exhibit, second})


class TeamNextSessionTests(ServiceDomainFixture):
    def setUp(self):
        super().setUp()
        self.series = self.base(TrainingSeries, name='Demo series')
        self.series.save()
        self.template = SessionTemplate.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='team',
            name='Team demo rubric', identify_by=AssessmentSession.IdentifyBy.FULL_NAME)
        TemplateRubricCategory.objects.create(template=self.template, name='Evidence',
            scope='group', weight=100, order=0)
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=self.series, display_name='Amina', reference='A-1', group_name='Team A', order=1)
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=self.series, display_name='Juma', reference='J-1', group_name='Team A', order=2)

    def create_next(self, name='Week 1', template=None, user=None):
        return create_next_training_session(self.series, template or self.template, name,
            organization=self.org.as_claim(), user=user or self.owner)

    def test_next_session_reuses_roster_and_rubric_and_starts_clean(self):
        session = self.create_next()
        self.assertEqual(session.service, 'team')
        self.assertEqual(session.status, AssessmentSession.Status.DRAFT)
        self.assertEqual(list(session.students.values_list('full_name', flat=True)), ['Amina', 'Juma'])
        self.assertEqual(session.groups.count(), 1)
        self.assertEqual(session.groups.get().name, 'Team A')
        self.assertEqual(session.groups.get().memberships.count(), 2)
        self.assertEqual(list(session.rubric_categories.values_list('name', 'weight')),
                         [('Evidence', Decimal('100.00'))])
        self.assertFalse(session.presentation_turns.exists())
        self.assertFalse(session.presentation_turns.filter(evaluations__isnull=False).exists())
        link = TrainingSeriesSession.objects.get(assessment_session=session)
        self.assertEqual((link.series, link.sequence), (self.series, 1))
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.other,
            series=self.series, display_name='Other owner', reference='X-1', group_name='Private group')
        second = self.create_next('Week 2')
        self.assertEqual(TrainingSeriesSession.objects.get(assessment_session=second).sequence, 2)
        self.assertEqual(second.students.count(), 2)
        self.assertEqual(second.groups.count(), 1)

    def test_empty_roster_missing_id_and_wrong_template_fail_without_partial_session(self):
        self.series.participants.all().delete()
        with self.assertRaisesMessage(ValidationError, 'at least one'):
            self.create_next()
        self.assertFalse(AssessmentSession.objects.filter(service='team').exists())

        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=self.series, display_name='No reference', group_name='Team B')
        id_template = SessionTemplate.objects.create(organization_id=self.org.id, created_by=self.owner,
            service='team', name='ID template', identify_by=AssessmentSession.IdentifyBy.STUDENT_ID)
        with self.assertRaisesMessage(ValidationError, 'needs an identifier'):
            self.create_next(template=id_template)
        wrong_service = SessionTemplate.objects.create(organization_id=self.org.id, created_by=self.owner,
            service='classroom', name='Class template')
        with self.assertRaisesMessage(ValidationError, 'Team template'):
            self.create_next(template=wrong_service)
        self.assertFalse(AssessmentSession.objects.filter(service='team').exists())

    def test_template_without_criteria_does_not_create_empty_training_session(self):
        empty_template = SessionTemplate.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='team', name='Empty rubric')
        with self.assertRaisesMessage(ValidationError, 'at least one criterion'):
            self.create_next(template=empty_template)
        self.assertFalse(AssessmentSession.objects.filter(service='team').exists())

    def test_only_active_series_owner_can_create_next_session(self):
        with self.assertRaises(PermissionDenied):
            self.create_next(user=self.other)
        self.series.active = False
        self.series.save()
        with self.assertRaises(PermissionDenied):
            self.create_next()
        self.assertFalse(AssessmentSession.objects.filter(service='team').exists())


class ServiceRuleTests(TestCase):
    def test_revealed_competition_requires_timestamp(self):
        draft = Competition(organization_id=1, name='Draft')
        draft.full_clean()
        row = Competition(organization_id=1, name='Cup', status=Competition.Status.REVEALED)
        with self.assertRaises(ValidationError):
            row.full_clean()
        row.revealed_at = timezone.now()
        row.full_clean()

    def test_score_aggregation_mean_median_and_trimmed_mean(self):
        self.assertEqual(aggregate_scores(['70', '80']), Decimal('75'))
        self.assertEqual(aggregate_scores(['1', '2'], 'median'), Decimal('1.5'))
        self.assertEqual(aggregate_scores(['1', '2', '3']), Decimal('2'))
        self.assertEqual(aggregate_scores(['1', '2', '3', '4']), Decimal('2.5'))
        self.assertEqual(aggregate_scores(['0', '10', '20', '30', '100'], 'trimmed_mean', '0.2'), Decimal('20'))
        for scores, method, fraction in (([], 'mean', '0.1'), (['101'], 'mean', '0.1'),
                                        (['NaN'], 'mean', '0.1'), (['2'], 'unknown', '0.1'),
                                        (['1', '2'], 'trimmed_mean', '0.5'),
                                        (['1', '2'], 'trimmed_mean', 'bad'),
                                        (['1', '2'], 'trimmed_mean', None), (['bad'], 'mean', '0.1')):
            with self.subTest(scores=scores, method=method, fraction=fraction), self.assertRaises(ValidationError):
                aggregate_scores(scores, method, fraction)

    def test_top_n_and_threshold_advancement(self):
        scores = {'a': '90', 'b': '90', 'c': '70'}
        self.assertEqual(advance_entries(scores, 'top_n', 2), ['a', 'b'])
        self.assertEqual(advance_entries(scores, 'top_n', 5), ['a', 'b', 'c'])
        self.assertEqual(advance_entries(scores, 'threshold', '90'), ['a', 'b'])
        boundary_tie = {'a': '90', 'b': '80', 'c': '80', 'd': '70'}
        with self.assertRaisesMessage(ValidationError, 'advancement boundary'):
            advance_entries(boundary_tie, 'top_n', 2)
        self.assertEqual(advance_entries(boundary_tie, 'top_n', 2,
                         tie_break_order=['b', 'c']), ['a', 'b'])
        self.assertEqual(advance_entries(boundary_tie, 'top_n', 2,
                         head_judge_decision=['c']), ['a', 'c'])
        with self.assertRaises(ValidationError):
            advance_entries(boundary_tie, 'top_n', 2, head_judge_decision=['x'])
        with self.assertRaises(ValidationError):
            advance_entries(boundary_tie, 'top_n', 2, tie_break_order=['b'])
        for rule, value in (('top_n', 0), ('top_n', 1.5), ('top_n', None),
                            ('threshold', 'bad'), ('threshold', 'NaN'), ('other', 1)):
            with self.subTest(rule=rule, value=value), self.assertRaises(ValidationError):
                advance_entries(scores, rule, value)
        with self.assertRaises(ValidationError):
            advance_entries({'a': 'Infinity'}, 'top_n', 1)
        with self.assertRaises(ValidationError):
            advance_entries({'a': 'bad'}, 'top_n', 1)

    def test_ranked_choice_known_winner_tie_and_invalid_ballot(self):
        ballots = [['a', 'b', 'c'], ['a', 'b'], ['b', 'a'], ['c', 'b'], ['b', 'a']]
        result = competition_ranked_choice(ballots, ['a', 'b', 'c'])
        self.assertEqual(result['winner'], 'b')
        # Hand tally: first preferences are A=2, B=2, C=1; eliminate C,
        # transfer its ballot to B, and finish at A=2, B=3.
        self.assertEqual(result['rounds'], [
            {'a': 2, 'b': 2, 'c': 1},
            {'a': 2, 'b': 3},
        ])
        tied = competition_ranked_choice([], ['a', 'b'])
        self.assertIsNone(tied['winner'])
        self.assertEqual(tied['resolved_by'], 'head_judge_required')
        chosen = competition_ranked_choice([], ['a', 'b'], ['a', 'b'])
        self.assertIn(chosen['winner'], ('a', 'b'))
        for ballots_, candidates, order in (([], ['a'], []), ([['a', 'a']], ['a', 'b'], []),
                                            ([['x']], ['a', 'b'], []), ([], ['a', 'b'], ['x'])):
            with self.subTest(ballots=ballots_, candidates=candidates), self.assertRaises(ValidationError):
                ranked_choice_result(ballots_, candidates, order)

    def test_ranked_choice_defers_partial_low_tie_to_head_judge(self):
        ballots = [['a']] * 3 + [['b']] * 2 + [['c']] * 2 + [['d']]
        result = ranked_choice_result(ballots, ['a', 'b', 'c', 'd'])
        self.assertIsNone(result['winner'])
        self.assertEqual(set(result['tied']), {'b', 'c'})
        self.assertEqual(result['resolved_by'], 'head_judge_required')

    def test_competition_advancement_adapter(self):
        self.assertEqual(competition_advancement({'x': '4', 'y': '3'}, 'top_n', 1), ['x'])

    def test_training_trend_keeps_each_source_result_and_needs_three_sessions(self):
        rows = [('one', {'learner': '60'}), ('two', {'learner': '70'}), ('three', {'learner': '85'})]
        trend = training_trend(rows, 'learner')
        self.assertEqual(trend['sessions'], [('one', Decimal('60')), ('two', Decimal('70')), ('three', Decimal('85'))])
        self.assertEqual(trend['change'], Decimal('25'))
        for invalid in (rows[:2], rows + [rows[0]], [('one', {'other': 5}), *rows[1:]]):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                training_trend(invalid, 'learner')

    def test_anonymous_feedback_is_hidden_below_explicit_privacy_floor(self):
        self.assertIsNone(aggregate_anonymous_feedback(['3', '4'], minimum_responses=3))
        self.assertEqual(aggregate_anonymous_feedback(['3', '4', '5'], minimum_responses=3),
                         {'response_count': 3, 'mean': Decimal('4'), 'median': Decimal('4')})
        for floor in (0, 1, 'bad'):
            with self.subTest(floor=floor), self.assertRaises(ValidationError):
                aggregate_anonymous_feedback(['4'], minimum_responses=floor)
