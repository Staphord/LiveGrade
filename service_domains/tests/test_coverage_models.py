"""Integrity rules and guards of the domain models, exercised on unsaved records where possible."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.models import AssessmentSession, PresentationGroup
from service_domains.models import (
    CoAssessmentCriterion, CoAssessmentExaminer, CoAssessmentGroup, CoAssessmentParticipant,
    CoAssessmentScorecard, CoAssessmentScoreValue, CoAssessmentSession, CoAssessmentTurn,
    CombinedModerationDecision, CombinedPresentationAssessment, CombinedPresentationMapping,
    Competition, CompetitionCriterion, CompetitionEntry, CompetitionJudgeAbstention,
    CompetitionJudgeInvitation, CompetitionJudgeScore, CompetitionLiveState, CompetitionRound,
    CompetitionRoundEntry, CompetitionTrack, CompetitionWeightedResultAudit, PanelCandidate,
    PanelCriterion, PanelDecision, PanelHearing, PanelInvitation, PanelLiveDisplayLink,
    PanelVote, PanelWrittenReview, ShowcaseCategory, ShowcaseEvent, ShowcaseJudgeConflict,
    ShowcaseJudgeCriterion, TrainingCompetency, TrainingParticipant, TrainingSeries,
    TrainingSeriesSession)


class Invalid(SimpleTestCase):
    def assertInvalid(self, record):
        with self.assertRaises(ValidationError):
            record.clean()


class DisplayNameTests(SimpleTestCase):
    def test_records_print_their_names(self):
        session = AssessmentSession(name='Defence')
        group = CoAssessmentGroup(name='Aurora')
        pairs = [
            (PanelCandidate(display_name='Ada'), 'Ada'),
            (PanelCriterion(name='Rigour'), 'Rigour'),
            (ShowcaseEvent(name='Expo'), 'Expo'),
            (ShowcaseCategory(name='Robots'), 'Robots'),
            (ShowcaseJudgeCriterion(name='Craft'), 'Craft'),
            (TrainingSeries(name='Series'), 'Series'),
            (TrainingCompetency(name='Listening'), 'Listening'),
            (TrainingParticipant(display_name='Lee'), 'Lee'),
            (TrainingSeriesSession(assessment_session=session), 'Defence'),
            (Competition(name='Cup'), 'Cup'),
            (CompetitionCriterion(name='Style'), 'Style'),
            (CompetitionEntry(name='Entry'), 'Entry'),
            (CompetitionRound(name='Final'), 'Final'),
            (CompetitionLiveState(competition=Competition(name='Cup')), 'Cup Live State'),
            (CombinedPresentationAssessment(name='Combined'), 'Combined'),
            (CoAssessmentSession(name='Viva'), 'Viva'),
            (CoAssessmentCriterion(name='Clarity', scope='group', weight=Decimal('40')), 'Clarity (Group'),
            (group, 'Aurora'),
            (CoAssessmentParticipant(full_name='Ada', student_id='S1'), 'Ada'),
            (CoAssessmentParticipant(full_name='', student_id='S1'), 'S1'),
            (CoAssessmentExaminer(display_name='Dr Cho', email='c@example.org'), 'Dr Cho (c@example.org)'),
            (CoAssessmentTurn(group=group, status='active'), 'Aurora : Active'),
        ]
        for record, text in pairs:
            with self.subTest(record=type(record).__name__):
                self.assertIn(text, str(record))


class AppendOnlyGuardTests(SimpleTestCase):
    def assertLocked(self, model):
        record = model()
        record._state.adding = False
        calls = [record.save, record.delete]
        if model is not CompetitionWeightedResultAudit:
            calls += [lambda: model.objects.update(), lambda: model.objects.all().delete()]
        for call in calls:
            with self.subTest(model=model.__name__, call=getattr(call, '__name__', 'queryset')):
                with self.assertRaises(ValidationError):
                    call()

    def test_submitted_records_cannot_be_changed_or_removed(self):
        for model in (PanelWrittenReview, PanelDecision, PanelVote, ShowcaseJudgeConflict,
                      CompetitionWeightedResultAudit, CombinedModerationDecision):
            self.assertLocked(model)

    def test_live_display_links_cannot_be_deleted(self):
        with self.assertRaises(ValidationError):
            PanelLiveDisplayLink().delete()

    def test_combined_policies_and_mappings_refuse_bulk_updates(self):
        for model in (CombinedPresentationAssessment, CombinedPresentationMapping):
            with self.assertRaises(ValidationError):
                model.objects.update()


class PanelRuleTests(Invalid):
    def test_hearing_state_rules(self):
        self.assertInvalid(PanelHearing(pk=1, active_candidate=PanelCandidate(pk=5, hearing_id=2)))
        self.assertInvalid(PanelHearing(status=PanelHearing.Status.SIGNED_OFF))

    def test_candidate_source_group_rules(self):
        def candidate(service, group_org, owner):
            session = AssessmentSession(pk=1, service=service, organization_id=group_org, created_by_id=owner)
            return PanelCandidate(organization_id=1, created_by_id=1,
                                  source_group=PresentationGroup(pk=1, assessment_session=session))
        self.assertInvalid(candidate('team', 1, 1))
        self.assertInvalid(candidate('classroom', 2, 1))
        self.assertInvalid(candidate('classroom', 1, 2))

    def test_written_review_rules(self):
        invitation = PanelInvitation(pk=1, hearing_id=2)
        self.assertInvalid(PanelWrittenReview(invitation=invitation, candidate=PanelCandidate(pk=3, hearing_id=3)))
        self.assertInvalid(PanelWrittenReview(hearing=PanelHearing(pk=1), candidate=PanelCandidate(pk=3, hearing_id=2)))
        self.assertInvalid(PanelWrittenReview(hearing=PanelHearing(pk=1), invitation=invitation))
        self.assertInvalid(PanelWrittenReview(report='  '))
        self.assertInvalid(PanelWrittenReview(report='ok', revision=2, revision_reason=' '))

    def test_vote_rules(self):
        invitation = PanelInvitation(pk=1, hearing_id=2)
        self.assertInvalid(PanelVote(invitation=invitation, candidate=PanelCandidate(pk=3, hearing_id=3)))
        self.assertInvalid(PanelVote(hearing=PanelHearing(pk=1), invitation=invitation))

    def test_live_display_token_must_be_a_digest(self):
        self.assertInvalid(PanelLiveDisplayLink(token_digest='nope'))


class PanelWorkflowRuleTests(TestCase):
    def setUp(self):
        self.org, self.owner = make_org('Rules org'), make_user('rules-owner')

    def hearing(self, workflow):
        return PanelHearing.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name=f'H {workflow}', workflow=workflow)

    def test_decision_rules(self):
        scored = self.hearing(PanelHearing.Workflow.SCORED)
        with self.assertRaises(ValidationError):
            PanelDecision(hearing=scored, organization_id=self.org.pk, created_by=self.owner, rationale='why').clean()
        with self.assertRaises(ValidationError):
            PanelDecision(hearing=PanelHearing(pk=999), candidate=PanelCandidate(pk=3, hearing_id=2)).clean()
        with self.assertRaises(ValidationError):
            PanelDecision(rationale='  ').clean()
        with self.assertRaises(ValidationError):
            PanelDecision(rationale='why', outcome=PanelDecision.Outcome.CONDITIONAL, conditions=' ').clean()

    def test_vote_needs_a_voting_workflow(self):
        scored = self.hearing(PanelHearing.Workflow.SCORED)
        with self.assertRaises(ValidationError):
            PanelVote(hearing=scored, organization_id=self.org.pk, created_by=self.owner).clean()

    def test_display_links_can_be_revoked_once(self):
        hearing = self.hearing(PanelHearing.Workflow.SCORED)
        link = PanelLiveDisplayLink.objects.create(
            organization_id=self.org.pk, created_by=self.owner, hearing=hearing, token_digest='a' * 64)
        link.revoked_at = timezone.now()
        link.save()
        with self.assertRaises(ValidationError):
            link.save()


class ShowcaseConflictRuleTests(Invalid):
    def test_a_conflict_needs_a_reason(self):
        self.assertInvalid(ShowcaseJudgeConflict(reason='  '))


class CompetitionRuleTests(TestCase):
    def test_audience_weight_falls_back_when_the_snapshot_is_unreadable(self):
        competition = Competition(audience_weight=Decimal('30'), workflow_snapshot={'audience_weight': 'abc'})
        self.assertEqual(competition.effective_audience_weight, Decimal('30'))

    def test_competition_clean_rules(self):
        for fields in ({'audience_weight': Decimal('101')}, {'status': Competition.Status.REVEALED}):
            with self.assertRaises(ValidationError):
                Competition(**fields).clean()

    def test_scoring_rules_freeze_when_the_event_starts(self):
        org, owner = make_org('Freeze org'), make_user('freeze-owner')
        draft = Competition.objects.create(organization_id=org.pk, created_by=owner, name='Draft')
        draft.workflow_snapshot = {'audience_weight': '10'}
        with self.assertRaises(ValidationError):
            draft.save()
        live = Competition.objects.create(
            organization_id=org.pk, created_by=owner, name='Live',
            status=Competition.Status.ACTIVE, workflow_snapshot={'audience_weight': '10'})
        live.workflow_snapshot = {'audience_weight': '20'}
        with self.assertRaises(ValidationError):
            live.save()

    def test_criterion_rules(self):
        for fields in ({'name': ' ', 'weight': Decimal('10')}, {'name': 'Style', 'weight': Decimal('0')}):
            with self.assertRaises(ValidationError):
                CompetitionCriterion(**fields).clean()

    def test_weighted_audit_rules(self):
        CompetitionWeightedResultAudit().clean()
        for round_obj in (
                CompetitionRound(pk=1, audience_locked_at=None),
                CompetitionRound(pk=1, audience_locked_at=timezone.now(),
                                 competition=Competition(audience_weight=Decimal('30'))),
        ):
            with self.assertRaises(ValidationError):
                CompetitionWeightedResultAudit(round=round_obj, audience_weight=Decimal('50'),
                                               judge_weight=Decimal('50')).clean()
        locked = CompetitionRound(pk=1, audience_locked_at=timezone.now(),
                                  competition=Competition(audience_weight=Decimal('30'), minimum_audience_ballots_per_track=5))
        with self.assertRaises(ValidationError):
            CompetitionWeightedResultAudit(round=locked, audience_weight=Decimal('30'),
                                           judge_weight=Decimal('70'), audience_ballot_count=1).clean()
        with self.assertRaises(ValidationError):
            CompetitionWeightedResultAudit(round=locked, audience_weight=Decimal('30'), judge_weight=Decimal('70'),
                                           audience_ballot_count=9, method='other').clean()

    def test_round_entry_rules(self):
        track_a, track_b = CompetitionTrack(pk=1, competition_id=1), CompetitionTrack(pk=2, competition_id=2)
        entry = CompetitionEntry(pk=1, track=track_a)
        cases = [
            CompetitionRoundEntry(entry=entry, track=track_b),
            CompetitionRoundEntry(entry=entry, round=CompetitionRound(pk=1, competition_id=9)),
            CompetitionRoundEntry(audience_score=Decimal('5')),
            CompetitionRoundEntry(judge_score=Decimal('5'), round=CompetitionRound(
                pk=1, competition=Competition(audience_weight=Decimal('0')))),
        ]
        for record in cases:
            with self.assertRaises(ValidationError):
                record.clean()

    def test_judge_score_and_abstention_rules(self):
        invitation = CompetitionJudgeInvitation(pk=1, round_id=1, track_id=1, organization_id=1)
        other_entry = CompetitionRoundEntry(pk=2, round_id=2, track_id=1, organization_id=1)
        track_entry = CompetitionRoundEntry(pk=3, round_id=1, track_id=2)
        for record in (
                CompetitionJudgeAbstention(invitation=invitation, round_entry=other_entry, reason='x', organization_id=1),
                CompetitionJudgeAbstention(reason='  '),
                CompetitionJudgeScore(invitation=invitation, round_entry=other_entry, organization_id=1),
                CompetitionJudgeScore(round_entry=CompetitionRoundEntry(pk=3, round_id=1, track_id=2),
                                      criterion=CompetitionCriterion(pk=1, track_id=1)),
                CompetitionJudgeScore(invitation=invitation, organization_id=2),
        ):
            with self.assertRaises(ValidationError):
                record.clean()
        self.assertIsNotNone(track_entry)


class CombinedRuleTests(TestCase):
    def setUp(self):
        self.org, self.owner = make_org('Combined rules'), make_user('combined-rules-owner')

    def assessment(self, **fields):
        base = dict(organization_id=1, created_by_id=1)
        base.update(fields)
        return CombinedPresentationAssessment(**base)

    def test_assessment_source_rules(self):
        session = lambda **kw: AssessmentSession(pk=1, **{'organization_id': 1, 'created_by_id': 1, 'service': 'classroom', **kw})
        hearing = lambda **kw: PanelHearing(pk=1, **{'organization_id': 1, 'created_by_id': 1,
                                                    'workflow': PanelHearing.Workflow.SCORED, **kw})
        cases = [
            (session(organization_id=2), hearing()),
            (session(created_by_id=2), hearing()),
            (session(service='team'), hearing()),
        ]
        for classroom, panel in cases:
            with self.assertRaises(ValidationError):
                self.assessment(classroom_session=classroom, panel_hearing=panel).clean()

    def test_weighted_policy_rules(self):
        weighted = CombinedPresentationAssessment.ResultPolicy.WEIGHTED
        base = dict(classroom_session=AssessmentSession(pk=1, organization_id=1, created_by_id=1),
                    panel_hearing=PanelHearing(pk=1, organization_id=1, created_by_id=1,
                                               workflow=PanelHearing.Workflow.OUTCOME),
                    result_policy=weighted)
        for extra in ({'peer_weight': Decimal('40'), 'panel_weight': Decimal('40'), 'moderation_threshold': 5},
                      {'peer_weight': Decimal('50'), 'panel_weight': Decimal('50'), 'moderation_threshold': None},
                      {'peer_weight': Decimal('50'), 'panel_weight': Decimal('50'), 'moderation_threshold': 5}):
            with self.assertRaises(ValidationError):
                self.assessment(**base, **extra).clean()

    def test_mapping_rules(self):
        CombinedPresentationMapping().clean()
        assessment = self.assessment(pk=1, classroom_session_id=3, panel_hearing_id=4)
        candidate = lambda **kw: PanelCandidate(pk=1, hearing_id=4, organization_id=1, created_by_id=1, **kw)
        cases = [
            dict(group=PresentationGroup(pk=1, assessment_session_id=9), candidate=candidate()),
            dict(group=PresentationGroup(pk=1, assessment_session_id=3),
                 candidate=PanelCandidate(pk=1, hearing_id=8, organization_id=1, created_by_id=1)),
            dict(group=PresentationGroup(pk=1, assessment_session_id=3, is_practice=True), candidate=candidate()),
            dict(group=PresentationGroup(pk=1, assessment_session_id=3), candidate=candidate(source_group_id=7)),
        ]
        for fields in cases:
            with self.assertRaises(ValidationError):
                CombinedPresentationMapping(assessment=assessment, organization_id=1, created_by_id=1, **fields).clean()
        CombinedPresentationMapping(
            assessment=assessment, organization_id=1, created_by_id=1,
            group=PresentationGroup(pk=1, assessment_session_id=3), candidate=candidate()).clean()

    def test_moderation_rules(self):
        mapping = CombinedPresentationMapping(pk=1, organization_id=1)
        previous = CombinedModerationDecision(pk=1, mapping_id=1, revision=1, organization_id=1)
        cases = [
            dict(mapping=mapping, organization_id=1, reason=' '),
            dict(mapping=mapping, organization_id=1, reason='x', supersedes=previous, revision=3),
            dict(mapping=mapping, organization_id=1, reason='x', revision=2),
        ]
        for fields in cases:
            with self.assertRaises(ValidationError):
                CombinedModerationDecision(**fields).clean()
        CombinedModerationDecision(mapping=mapping, organization_id=1, reason='x', supersedes=previous,
                                   revision=2).clean()
        CombinedModerationDecision(mapping=mapping, organization_id=1, reason='x', revision=1).clean()

    def test_empty_assessment_has_nothing_to_validate(self):
        self.assessment().clean()

    def test_published_policy_is_locked_and_undeletable(self):
        hearing = PanelHearing.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='H', workflow=PanelHearing.Workflow.SCORED)
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='S')
        published = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='C', classroom_session=session,
            panel_hearing=hearing, result_policy=CombinedPresentationAssessment.ResultPolicy.WEIGHTED,
            peer_weight=Decimal('50'), panel_weight=Decimal('50'), moderation_threshold=Decimal('5'))
        CombinedPresentationAssessment.objects.filter(pk=published.pk)
        published.status = CombinedPresentationAssessment.Status.ACTIVE
        published.save()
        published.panel_weight = Decimal('60')
        with self.assertRaises(ValidationError):
            published.save()
        with self.assertRaises(ValidationError):
            published.clean()
        published.refresh_from_db()
        published.status = CombinedPresentationAssessment.Status.DRAFT
        with self.assertRaises(ValidationError):
            published.save()
        published.refresh_from_db()
        with self.assertRaises(ValidationError):
            published.delete()
        with self.assertRaises(ValidationError):
            CombinedPresentationAssessment.objects.filter(pk=published.pk).delete()

    def test_mappings_lock_when_the_assessment_is_published(self):
        hearing = PanelHearing.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='H2', workflow=PanelHearing.Workflow.SCORED)
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='S2')
        assessment = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='C2', classroom_session=session,
            panel_hearing=hearing)
        group = PresentationGroup.objects.create(assessment_session=session, name='G')
        candidate = PanelCandidate.objects.create(
            organization_id=self.org.pk, created_by=self.owner, hearing=hearing, display_name='C', order=1)
        mapping = CombinedPresentationMapping.objects.create(
            organization_id=self.org.pk, created_by=self.owner, assessment=assessment, group=group,
            candidate=candidate)
        self.assertEqual(mapping.group_member_ids_snapshot, [])
        assessment.status = CombinedPresentationAssessment.Status.ACTIVE
        assessment.save()
        with self.assertRaises(ValidationError):
            mapping.save()
        with self.assertRaises(ValidationError):
            mapping.delete()
        with self.assertRaises(ValidationError):
            CombinedPresentationMapping.objects.all().delete()
        draft = CombinedPresentationAssessment.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='C3',
            classroom_session=AssessmentSession.objects.create(
                organization_id=self.org.pk, created_by=self.owner, name='S3'),
            panel_hearing=PanelHearing.objects.create(
                organization_id=self.org.pk, created_by=self.owner, name='H3', workflow=PanelHearing.Workflow.SCORED))
        CombinedPresentationAssessment.objects.filter(pk=draft.pk).delete()
        self.assertFalse(CombinedPresentationAssessment.objects.filter(pk=draft.pk).exists())


class CoAssessmentScorecardRuleTests(Invalid):
    def test_scorecard_rules(self):
        turn = CoAssessmentTurn(pk=1, group_id=5)
        self.assertInvalid(CoAssessmentScorecard(turn=turn, evaluator_tier=CoAssessmentScorecard.Tier.PEER))
        self.assertInvalid(CoAssessmentScorecard(
            turn=turn, evaluator_tier=CoAssessmentScorecard.Tier.PEER,
            peer_evaluator=CoAssessmentParticipant(pk=1, group_id=5)))
        self.assertInvalid(CoAssessmentScorecard(turn=turn, evaluator_tier=CoAssessmentScorecard.Tier.PANEL))

    def test_valid_scorecards_and_unknown_tiers_pass(self):
        turn = CoAssessmentTurn(pk=1, group_id=5)
        CoAssessmentScorecard(turn=turn, evaluator_tier=CoAssessmentScorecard.Tier.PEER,
                              peer_evaluator=CoAssessmentParticipant(pk=1, group_id=6)).clean()
        CoAssessmentScorecard(turn=turn, evaluator_tier=CoAssessmentScorecard.Tier.PANEL,
                              examiner_evaluator=CoAssessmentExaminer(pk=1)).clean()
        CoAssessmentScorecard(turn=turn, evaluator_tier='other').clean()

    def test_valid_score_values_pass(self):
        group = CoAssessmentCriterion(pk=1, scope=CoAssessmentCriterion.Scope.GROUP)
        individual = CoAssessmentCriterion(pk=2, scope=CoAssessmentCriterion.Scope.INDIVIDUAL)
        CoAssessmentScoreValue().clean()
        CoAssessmentScoreValue(criterion=group).clean()
        CoAssessmentScoreValue(criterion=individual, target_participant=CoAssessmentParticipant(pk=1)).clean()

    def test_score_value_target_rules(self):
        group = CoAssessmentCriterion(pk=1, scope=CoAssessmentCriterion.Scope.GROUP)
        individual = CoAssessmentCriterion(pk=2, scope=CoAssessmentCriterion.Scope.INDIVIDUAL)
        self.assertInvalid(CoAssessmentScoreValue(criterion=group, target_participant=CoAssessmentParticipant(pk=1)))
        self.assertInvalid(CoAssessmentScoreValue(criterion=individual))
