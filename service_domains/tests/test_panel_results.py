from datetime import timedelta
from decimal import Decimal
import hashlib
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.models import (AssessmentSession, Evaluation, EvaluationScore,
                                PresentationGroup, PresentationTurn, RubricCategory, Student)
from assessments.scoring import group_score_percent
from service_domains.models import (
    PanelAbstention, PanelAuditEvent, PanelCandidate, PanelCommentRedaction, PanelConflict, PanelCriterion,
    PanelEntrantResultLink, PanelHearing,
    PanelInvitation, PanelResultAdjustment, PanelResultSnapshot, PanelScore,
    PanelScoringPanel,
)
from service_domains.forms import (PanelCommentRedactionForm, PanelCommentReleaseForm, PanelResultAdjustmentForm,
                                   PanelScoringPanelForm)
from service_domains.operations import (
    abstain_panel_score, activate_panel_candidate, adjust_panel_result, compile_panel_results, confirm_panel_signoff,
    declare_panel_conflict, issue_panel_invitation,
    issue_panel_entrant_result_link, redact_panel_comment, release_panel_comment,
    release_panel_results, revoke_panel_entrant_result_link,
    resolve_panel_entrant_result_link, submit_panel_score,
)


class PanelResultCompilationTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel results org')
        self.owner = make_user('panel-results-owner')
        self.other = make_user('panel-results-other')
        self.hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Final review', status=PanelHearing.Status.OPEN,
            minimum_examiners=1)
        self.candidate_a = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Candidate A', order=0)
        self.candidate_b = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Candidate B', order=1)
        self.criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Evidence', order=0)
        self.tokens = []
        for number in (1, 2):
            _, token = issue_panel_invitation(self.hearing, f'examiner{number}@example.test',
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
            self.tokens.append(token)

    def score(self, token, candidate, value, comment='Private comment.'):
        activate_panel_candidate(self.hearing, candidate,
            organization=self.org.as_claim(), user=self.owner)
        return submit_panel_score(token, candidate, self.criterion, value, comment)

    def test_compilation_excludes_recused_scores_and_persists_aggregate_only_snapshots(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[1], self.candidate_a, 90)
        self.score(self.tokens[0], self.candidate_b, 20)
        self.score(self.tokens[1], self.candidate_b, 70)
        conflict = PanelConflict(invitation=self.hearing.invitations.order_by('pk').first(),
            candidate=self.candidate_b, reason='Prior supervisory relationship.',
            organization_id=self.org.id, created_by=self.owner)
        conflict.full_clean()
        conflict.save()

        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        self.hearing.refresh_from_db()
        self.assertEqual(self.hearing.status, PanelHearing.Status.REVIEW)
        by_candidate = {row.candidate_id: row for row in snapshots}
        self.assertEqual(by_candidate[self.candidate_a.pk].score, Decimal('85.000000'))
        self.assertEqual(by_candidate[self.candidate_a.pk].rater_count, 2)
        self.assertEqual(by_candidate[self.candidate_b.pk].score, Decimal('70.000000'))
        self.assertEqual(by_candidate[self.candidate_b.pk].rater_count, 1)
        self.assertEqual(by_candidate[self.candidate_a.pk].aggregation, PanelHearing.Aggregation.MEAN)
        self.assertFalse(hasattr(by_candidate[self.candidate_a.pk], 'invitation_id'))
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.RESULTS_COMPILED).count(), 1)

    def test_default_panel_mean_matches_classroom_result_for_identical_scores(self):
        scores = (Decimal('80'), Decimal('90'), Decimal('91.5'))
        classroom = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Classroom parity fixture', status='closed')
        group = PresentationGroup.objects.create(assessment_session=classroom, name='Candidate A')
        category = RubricCategory.objects.create(assessment_session=classroom,
            name='Evidence', scope=RubricCategory.Scope.GROUP, weight=100)
        turn = PresentationTurn.objects.create(assessment_session=classroom, group=group,
            status=PresentationTurn.Status.CLOSED)
        _, third_token = issue_panel_invitation(self.hearing, 'parity-examiner-3@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        for index, (score, token) in enumerate(zip(scores, (*self.tokens, third_token))):
            evaluator = Student.objects.create(assessment_session=classroom,
                full_name=f'Classroom evaluator {index}')
            evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=evaluator)
            EvaluationScore.objects.create(evaluation=evaluation, rubric_category=category,
                value=score, max_value=100)
            self.score(token, self.candidate_a, score)
            self.score(token, self.candidate_b, score)

        classroom_result = group_score_percent(group, classroom)
        panel_results = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        panel_snapshot = next(row for row in panel_results
            if row.candidate_id == self.candidate_a.pk)

        self.assertEqual(classroom_result, Decimal('87.2'))
        self.assertEqual(panel_snapshot.score, classroom_result)
        self.assertEqual(panel_snapshot.rater_count, 3)
        self.assertEqual(panel_snapshot.panel_methodology[0]['score'], '87.166667')

    def test_default_panel_matches_classroom_at_zero_and_half_up_rounding_boundary(self):
        cases = (
            (self.candidate_a, 'Zero boundary', (Decimal('0'), Decimal('0.10'), Decimal('0.20'))),
            (self.candidate_b, 'Midpoint boundary',
             (Decimal('80.00'), Decimal('80.00'), Decimal('80.15'))),
        )
        classroom = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Boundary parity fixture', status='closed')
        category = RubricCategory.objects.create(assessment_session=classroom,
            name='Evidence', scope=RubricCategory.Scope.GROUP, weight=100)
        _, third_token = issue_panel_invitation(self.hearing, 'boundary-examiner-3@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        tokens = (*self.tokens, third_token)
        classroom_results = {}

        for candidate, group_name, values in cases:
            group = PresentationGroup.objects.create(assessment_session=classroom,
                name=group_name)
            turn = PresentationTurn.objects.create(assessment_session=classroom, group=group,
                status=PresentationTurn.Status.CLOSED)
            for index, (value, token) in enumerate(zip(values, tokens)):
                evaluator = Student.objects.create(assessment_session=classroom,
                    full_name=f'{group_name} evaluator {index}')
                evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=evaluator)
                EvaluationScore.objects.create(evaluation=evaluation, rubric_category=category,
                    value=value, max_value=100)
                self.score(token, candidate, value)
            classroom_results[candidate.pk] = group_score_percent(group, classroom)

        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        panel_results = {row.candidate_id: row for row in snapshots}
        self.assertEqual(classroom_results[self.candidate_a.pk], Decimal('0.1'))
        self.assertEqual(classroom_results[self.candidate_b.pk], Decimal('80.1'))
        for candidate, _, _ in cases:
            with self.subTest(candidate=candidate.display_name):
                self.assertEqual(panel_results[candidate.pk].score,
                    classroom_results[candidate.pk])

    def test_below_minimum_results_compile_with_a_durable_warning_flag(self):
        self.hearing.minimum_examiners = 2
        self.hearing.save(update_fields=('minimum_examiners', 'updated_at'))
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[1], self.candidate_a, 90)
        self.score(self.tokens[0], self.candidate_b, 70)
        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        by_candidate = {row.candidate_id: row for row in snapshots}
        self.assertFalse(by_candidate[self.candidate_a.pk].below_minimum)
        self.assertTrue(by_candidate[self.candidate_b.pk].below_minimum)
        self.assertEqual(by_candidate[self.candidate_b.pk].rater_count, 1)
        self.assertEqual(by_candidate[self.candidate_b.pk].panel_methodology[0]['minimum_examiners'], 2)
        self.assertTrue(by_candidate[self.candidate_b.pk].panel_methodology[0]['below_minimum'])
        self.hearing.refresh_from_db()
        self.assertEqual(self.hearing.status, PanelHearing.Status.REVIEW)
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.RESULTS_COMPILED).count(), 1)

    def test_abstentions_do_not_count_toward_minimum_rater_coverage(self):
        self.hearing.minimum_examiners = 2
        self.hearing.save(update_fields=('minimum_examiners', 'updated_at'))
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[1], self.candidate_a, 90)
        activate_panel_candidate(self.hearing, self.candidate_b,
            organization=self.org.as_claim(), user=self.owner)
        abstain_panel_score(self.tokens[0], self.candidate_b, self.criterion, reason='Unable to assess this criterion.')
        self.score(self.tokens[1], self.candidate_b, 70)
        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(PanelAbstention.objects.count(), 1)
        candidate_b_result = next(row for row in snapshots if row.candidate_id == self.candidate_b.pk)
        self.assertTrue(candidate_b_result.below_minimum)

    def test_result_compilation_requires_open_configured_hearing(self):
        empty = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Empty open hearing', status=PanelHearing.Status.OPEN)
        with self.assertRaisesMessage(ValidationError, 'needs candidates and the required criteria'):
            compile_panel_results(empty, organization=self.org.as_claim(), user=self.owner)
        self.hearing.status = PanelHearing.Status.REVIEW
        self.hearing.save(update_fields=('status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'Only an open hearing'):
            compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)

    def test_trimmed_method_and_snapshot_relationships_are_validated(self):
        unconfigured = PanelHearing(organization_id=self.org.id, created_by=self.owner,
            name='Trimmed but unconfigured', aggregation=PanelHearing.Aggregation.TRIMMED_MEAN)
        with self.assertRaisesMessage(ValidationError, 'explicit trim fraction'):
            unconfigured.full_clean()
        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other hearing')
        foreign_candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, display_name='Foreign')
        wrong_candidate = PanelResultSnapshot(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, candidate=foreign_candidate, criterion=self.criterion,
            score=50, rater_count=1, aggregation=PanelHearing.Aggregation.MEAN)
        with self.assertRaisesMessage(ValidationError, 'must belong to this hearing'):
            wrong_candidate.full_clean()
        wrong_criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, name='Foreign criterion')
        wrong_snapshot = PanelResultSnapshot(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, candidate=self.candidate_a, criterion=wrong_criterion,
            score=50, rater_count=1, aggregation=PanelHearing.Aggregation.MEAN)
        with self.assertRaisesMessage(ValidationError, 'must belong to this hearing'):
            wrong_snapshot.full_clean()
        missing_trim = PanelResultSnapshot(organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, candidate=self.candidate_a, criterion=self.criterion,
            score=50, rater_count=1, aggregation=PanelHearing.Aggregation.TRIMMED_MEAN)
        with self.assertRaisesMessage(ValidationError, 'configured trim fraction'):
            missing_trim.full_clean()

    def test_trimmed_mean_snapshot_discloses_explicit_rule(self):
        self.hearing.aggregation = PanelHearing.Aggregation.TRIMMED_MEAN
        self.hearing.trim_fraction = Decimal('0.200')
        self.hearing.minimum_examiners = 5
        self.hearing.save(update_fields=('aggregation', 'trim_fraction', 'minimum_examiners', 'updated_at'))
        tokens = self.tokens[:]
        for number in (3, 4, 5):
            _, token = issue_panel_invitation(self.hearing, f'examiner{number}@example.test',
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
            tokens.append(token)
        for token, value in zip(tokens, (0, 10, 20, 30, 100)):
            self.score(token, self.candidate_a, value)
            self.score(token, self.candidate_b, value)
        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(snapshots[0].score, Decimal('20.000000'))
        self.assertEqual(snapshots[0].trim_fraction, Decimal('0.200'))
        self.assertEqual(snapshots[0].rater_count, 5)

    def test_panel_shares_aggregate_independently_then_combine_and_disclose(self):
        first_panel = self.hearing.panels.get(name='Default panel')
        first_panel.share = Decimal('50.00')
        first_panel.configuration_managed = True
        first_panel.save(update_fields=('share', 'configuration_managed', 'updated_at'))
        second_panel = PanelScoringPanel.objects.create(
            organization_id=self.org.id, created_by=self.owner, hearing=self.hearing,
            name='External panel', share=Decimal('50.00'),
            aggregation=PanelHearing.Aggregation.MEAN, minimum_examiners=2,
            configuration_managed=True)
        self.criterion.panels.add(second_panel)
        second_tokens = [issue_panel_invitation(self.hearing, f'external{number}@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=second_panel)[1] for number in (1, 2)]
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[1], self.candidate_a, 100)
        for token, value in zip(second_tokens, (20, 40)):
            submit_panel_score(token, self.candidate_a, self.criterion, value)
        self.score(self.tokens[0], self.candidate_b, 60)
        self.score(self.tokens[1], self.candidate_b, 80)
        for token, value in zip(second_tokens, (40, 60)):
            submit_panel_score(token, self.candidate_b, self.criterion, value)

        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)

        by_candidate = {row.candidate_id: row for row in snapshots}
        result = by_candidate[self.candidate_a.pk]
        self.assertEqual(result.score, Decimal('60.000000'))
        self.assertEqual(result.rater_count, 4)
        self.assertEqual(len(result.panel_methodology), 2)
        self.assertEqual(result.panel_methodology[0]['share'], '50.00')
        self.assertEqual(Decimal(result.panel_methodology[0]['score']), Decimal('90'))
        self.assertEqual(Decimal(result.panel_methodology[1]['score']), Decimal('30'))

    def test_trimmed_scoring_is_unavailable_in_form_and_model_requires_fraction(self):
        form = PanelScoringPanelForm(data={
            'name': 'Trimmed panel', 'admission_method': 'invited', 'share': '100.00',
            'aggregation': PanelHearing.Aggregation.TRIMMED_MEAN,
            'minimum_examiners': '3', 'trim_fraction': '',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('trimmed_mean is not one of the available choices', str(form.errors))
        panel = PanelScoringPanel(hearing=self.hearing, organization_id=self.org.id,
            created_by=self.owner, name='Trimmed panel',
            aggregation=PanelHearing.Aggregation.TRIMMED_MEAN)
        with self.assertRaisesMessage(ValidationError, 'explicit trim fraction'):
            panel.full_clean()

    def test_compilation_requires_exact_shares_and_at_least_one_criterion_per_panel(self):
        panel = self.hearing.panels.get(name='Default panel')
        panel.share = Decimal('99.99')
        panel.configuration_managed = True
        panel.save(update_fields=('share', 'configuration_managed', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'total exactly 100%'):
            compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        panel.share = Decimal('100.00')
        panel.save(update_fields=('share', 'updated_at'))
        self.criterion.panels.clear()
        with self.assertRaisesMessage(ValidationError, 'Assign at least one criterion'):
            compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)

    def test_panel_specific_minimum_rater_rule_is_enforced(self):
        panel = self.hearing.panels.get(name='Default panel')
        panel.configuration_managed = True
        panel.minimum_examiners = 2
        panel.save(update_fields=('configuration_managed', 'minimum_examiners', 'updated_at'))
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        snapshots = compile_panel_results(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        self.assertTrue(next(row for row in snapshots
            if row.candidate_id == self.candidate_a.pk).below_minimum)

    def test_result_compilation_still_blocks_a_candidate_with_no_eligible_scores(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        with self.assertRaisesMessage(ValidationError,
                'no eligible examiner scored: Candidate B / Evidence / Default panel'):
            compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(PanelResultSnapshot.objects.exists())

    def test_invitation_rejects_panel_from_another_hearing(self):
        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other hearing')
        foreign_panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, name='Other panel')
        invalid_invitation = PanelInvitation(hearing=self.hearing, panel=foreign_panel,
            organization_id=self.org.id, created_by=self.owner, email='x@example.test',
            token_digest=PanelInvitation.digest_token('invalid-panel'),
            expires_at=timezone.now() + timedelta(hours=1))
        with self.assertRaisesMessage(ValidationError, 'must belong to the invited hearing'):
            invalid_invitation.full_clean()
        with self.assertRaises(PermissionDenied):
            issue_panel_invitation(self.hearing, 'wrong-panel@example.test',
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
                user=self.owner, panel=foreign_panel)

    def test_examiner_cannot_score_a_criterion_outside_their_panel_assignment(self):
        other_panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Other panel')
        _, token = issue_panel_invitation(self.hearing, 'other-panel@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=other_panel)
        with self.assertRaisesMessage(PermissionDenied, 'not assigned to the invited panel'):
            submit_panel_score(token, self.candidate_a, self.criterion, 80)

    def test_unassigned_criterion_prevents_result_snapshot_creation(self):
        default = self.hearing.panels.get(name='Default panel')
        default.share = Decimal('50.00')
        default.configuration_managed = True
        default.save(update_fields=('share', 'configuration_managed', 'updated_at'))
        other = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Other panel',
            share=Decimal('50.00'), configuration_managed=True)
        next_criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Other evidence', order=1)
        next_criterion.panels.add(other)
        unassigned = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Unassigned evidence', order=2)
        other_tokens = [issue_panel_invitation(self.hearing, f'other{index}@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=other)[1] for index in (1, 2)]
        for candidate, first_value, second_value in (
            (self.candidate_a, 80, 70), (self.candidate_b, 60, 50)):
            self.score(self.tokens[0], candidate, first_value)
            self.score(self.tokens[1], candidate, first_value)
            for token in other_tokens:
                submit_panel_score(token, candidate, next_criterion, second_value)
        with self.assertRaisesMessage(ValidationError, 'must be assigned to a panel'):
            compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(PanelResultSnapshot.objects.exists())

    def test_snapshots_are_immutable_and_signoff_release_are_ordered(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        snapshot = PanelResultSnapshot.objects.get(candidate=self.candidate_a)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            snapshot.score = 99
            snapshot.save()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            PanelResultSnapshot.objects.filter(pk=snapshot.pk).update(score=99)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            snapshot.delete()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            PanelResultSnapshot.objects.filter(pk=snapshot.pk).delete()

        signed = confirm_panel_signoff(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(signed.status, PanelHearing.Status.SIGNED_OFF)
        released = release_panel_results(signed,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(released.status, PanelHearing.Status.RELEASED)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.RESULTS_SIGNED_OFF).count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.RESULTS_RELEASED).count(), 1)

    def test_operation_rejects_wrong_owner_and_review_page_hides_rater_identity(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        with self.assertRaises(PermissionDenied):
            compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.other)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)


    def test_owner_can_redact_comment_in_review_without_changing_score_and_audit_once(self):
        score = self.score(self.tokens[0], self.candidate_a, 80, 'Sensitive identity comment.')
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        redact_panel_comment(self.hearing, score, 'Contains identifying information.',
            organization=self.org.as_claim(), user=self.owner)
        score.refresh_from_db()
        self.assertEqual(score.comment, '')
        self.assertEqual(score.score, Decimal('80.00'))
        redaction = PanelCommentRedaction.objects.get(score=score)
        self.assertEqual(redaction.original_comment_digest,
            hashlib.sha256(b'Sensitive identity comment.').hexdigest())
        self.assertEqual(redaction.reason, 'Contains identifying information.')
        self.assertEqual(redaction.moderated_by, self.owner)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.COMMENT_REDACTED).count(), 1)

    def test_owner_can_approve_comment_for_entrant_with_reason_and_single_audit(self):
        score = self.score(self.tokens[0], self.candidate_a, 80, 'Useful anonymous feedback.')
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            release_panel_comment(self.hearing, score, '  ',
                organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.COMMENT_RELEASED).exists())
        release_panel_comment(self.hearing, score, 'Feedback checked for identifying details.',
            organization=self.org.as_claim(), user=self.owner)
        score.refresh_from_db()
        self.assertTrue(score.comment_released_to_entrant)
        event = PanelAuditEvent.objects.get(hearing=self.hearing,
            action=PanelAuditEvent.Action.COMMENT_RELEASED)
        self.assertEqual(event.reason, 'Feedback checked for identifying details.')
        with self.assertRaisesMessage(ValidationError, 'already approved'):
            release_panel_comment(self.hearing, score, 'Duplicate.',
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.COMMENT_RELEASED).count(), 1)


    def test_comment_release_operation_rejects_wrong_owner_and_after_signoff(self):
        score = self.score(self.tokens[0], self.candidate_a, 80, 'Anonymous text.')
        empty_comment = self.score(self.tokens[0], self.candidate_b, 70, '')
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            release_panel_comment(self.hearing, score, 'Unauthorized.',
                organization=self.org.as_claim(), user=self.other)
        with self.assertRaises(PermissionDenied):
            release_panel_comment(self.hearing, PanelScore(pk=999999), 'Missing score.',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'no comment available'):
            release_panel_comment(self.hearing, empty_comment, 'No text.',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            release_panel_comment(self.hearing, score, None,
                organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'only while results are under review'):
            release_panel_comment(self.hearing, score, 'Too late.',
                organization=self.org.as_claim(), user=self.owner)

    def test_comment_release_form_requires_a_reason(self):
        self.assertFalse(PanelCommentReleaseForm({'reason': '  '}).is_valid())
        self.assertTrue(PanelCommentReleaseForm({'reason': 'Reviewed.'}).is_valid())


    def test_comment_redaction_rejects_empty_reason_duplicate_and_post_signoff(self):
        score = self.score(self.tokens[0], self.candidate_a, 80, 'Sensitive note.')
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            redact_panel_comment(self.hearing, score, '   ',
                organization=self.org.as_claim(), user=self.owner)
        record = redact_panel_comment(self.hearing, score, 'Approved moderation.',
            organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'no comment available'):
            redact_panel_comment(self.hearing, score, 'Duplicate request.',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'only while results are under review'):
            confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
            redact_panel_comment(self.hearing, score, 'After sign-off.',
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(PanelCommentRedaction.objects.get(), record)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelCommentRedaction.objects.filter(pk=record.pk).update(reason='Changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            record.delete()

    def test_comment_redaction_fails_closed_for_wrong_owner(self):
        score = self.score(self.tokens[0], self.candidate_a, 80, 'Sensitive note.')
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            redact_panel_comment(self.hearing, score, 'Unauthorized.',
                organization=self.org.as_claim(), user=self.other)
        self.assertEqual(PanelCommentRedaction.objects.count(), 0)
        score.refresh_from_db()
        self.assertEqual(score.comment, 'Sensitive note.')

    def test_comment_redaction_form_and_model_reject_invalid_values(self):
        form = PanelCommentRedactionForm({'reason': '   '})
        self.assertFalse(form.is_valid())
        self.assertIn('A reason is required', form.errors['reason'][0])

        score = self.score(self.tokens[0], self.candidate_a, 80, 'Sensitive note.')
        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Different hearing')
        wrong_hearing = PanelCommentRedaction(hearing=other_hearing, score=score,
            original_comment_digest=hashlib.sha256(b'Sensitive note.').hexdigest(),
            reason='Reason.', moderated_by=self.owner,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            wrong_hearing.full_clean()
        self.assertIn('score', caught.exception.message_dict)

        invalid_reason = PanelCommentRedaction(hearing=self.hearing, score=score,
            original_comment_digest=hashlib.sha256(b'Sensitive note.').hexdigest(), reason='  ',
            moderated_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            invalid_reason.full_clean()
        self.assertIn('reason', caught.exception.message_dict)

        invalid_digest = PanelCommentRedaction(hearing=self.hearing, score=score,
            original_comment_digest='invalid', reason='Reason.', moderated_by=self.owner,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            invalid_digest.full_clean()
        self.assertIn('original_comment_digest', caught.exception.message_dict)

    def test_redaction_rejects_unknown_score_and_reason_type_and_is_append_only(self):
        score = self.score(self.tokens[0], self.candidate_a, 80, 'Sensitive note.')
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            redact_panel_comment(self.hearing, PanelScore(pk=999999), 'Missing score.',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            redact_panel_comment(self.hearing, score, None,
                organization=self.org.as_claim(), user=self.owner)
        record = redact_panel_comment(self.hearing, score, 'Approved.',
            organization=self.org.as_claim(), user=self.owner)
        record.reason = 'Changed.'
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            record.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelCommentRedaction.objects.filter(pk=record.pk).delete()



    def test_entrant_result_link_model_preserves_revocation_history_and_validation(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        release_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        link, token = issue_panel_entrant_result_link(self.hearing, self.candidate_a,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIsNone(resolve_panel_entrant_result_link('bad-token'))
        self.assertIsNone(resolve_panel_entrant_result_link(token + 'x'))
        with self.assertRaisesMessage(ValidationError, 'revoked once'):
            link.save()
        with self.assertRaisesMessage(ValidationError, 'retained as revocation history'):
            link.delete()
        with self.assertRaisesMessage(ValidationError, 'revocation history'):
            PanelEntrantResultLink.objects.filter(pk=link.pk).delete()
        with self.assertRaisesMessage(ValidationError, 'audited operation'):
            PanelEntrantResultLink.objects.filter(pk=link.pk).update(revoked_at=timezone.now())

    def test_entrant_link_guards_reject_foreign_candidate_invalid_revocation_and_unknown_owner_targets(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        release_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        self.hearing.refresh_from_db()
        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other released hearing')
        foreign_candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, display_name='Foreign entrant')
        with self.assertRaises(PermissionDenied):
            issue_panel_entrant_result_link(self.hearing, self.candidate_a,
                organization=self.org.as_claim(), user=self.other)
        with self.assertRaises(PermissionDenied):
            issue_panel_entrant_result_link(self.hearing, foreign_candidate,
                organization=self.org.as_claim(), user=self.owner)

        link, token = issue_panel_entrant_result_link(self.hearing, self.candidate_a,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIsNone(resolve_panel_entrant_result_link('x' * 129))
        with self.assertRaisesMessage(ValidationError, 'revocation reason'):
            revoke_panel_entrant_result_link(self.hearing, link, ' ',
                organization=self.org.as_claim(), user=self.owner)
        missing = PanelEntrantResultLink(pk=999999)
        with self.assertRaises(PermissionDenied):
            revoke_panel_entrant_result_link(self.hearing, missing, 'Reason.',
                organization=self.org.as_claim(), user=self.owner)

        bad_candidate = PanelEntrantResultLink(hearing=self.hearing,
            candidate=foreign_candidate, token_digest='a' * 64,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'entrant must belong'):
            bad_candidate.full_clean()
        bad_digest = PanelEntrantResultLink(hearing=self.hearing,
            candidate=self.candidate_a, token_digest='invalid',
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'digest is invalid'):
            bad_digest.full_clean()
        draft_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Draft release')
        draft_candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=draft_hearing, display_name='Draft entrant')
        unreleased_link = PanelEntrantResultLink(hearing=draft_hearing,
            candidate=draft_candidate, token_digest='b' * 64,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'only after results are released'):
            unreleased_link.full_clean()


    def test_post_signoff_adjustment_appends_original_value_actor_and_reason(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        snapshot = PanelResultSnapshot.objects.get(candidate=self.candidate_a)
        with self.assertRaisesMessage(ValidationError, 'only after sign-off'):
            adjust_panel_result(snapshot, 81, 'Reason.', organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        first = adjust_panel_result(snapshot, '82.5', 'Corrected an arithmetic transcription.',
            organization=self.org.as_claim(), user=self.owner)
        second = adjust_panel_result(snapshot, '83', 'Approved second review.',
            organization=self.org.as_claim(), user=self.owner)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.score, Decimal('80.000000'))
        self.assertEqual((first.revision, first.supersedes_id), (1, None))
        self.assertEqual((second.revision, second.supersedes), (2, first))
        self.assertEqual(second.adjusted_by, self.owner)
        self.assertEqual(second.reason, 'Approved second review.')
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.RESULT_ADJUSTED).count(), 2)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelResultAdjustment.objects.filter(pk=first.pk).update(reason='changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelResultAdjustment.objects.filter(pk=first.pk).delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            first.delete()


    def test_adjustments_require_valid_reason_score_and_owner_scope(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        snapshot = PanelResultSnapshot.objects.get(candidate=self.candidate_a)
        for score, reason in (('101', 'Out of range.'), ('70', '  '), ('bad', 'Invalid score.')):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                adjust_panel_result(snapshot, score, reason,
                    organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            adjust_panel_result(snapshot, 70, 'Foreign owner.',
                organization=self.org.as_claim(), user=self.other)
        self.assertFalse(PanelResultAdjustment.objects.exists())

    def test_adjustment_model_rejects_broken_revision_chains_and_mutation(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        first_snapshot = PanelResultSnapshot.objects.get(candidate=self.candidate_a)
        other_snapshot = PanelResultSnapshot.objects.get(candidate=self.candidate_b)
        first = adjust_panel_result(first_snapshot, 81, 'Correction one.',
            organization=self.org.as_claim(), user=self.owner)

        invalid_root = PanelResultAdjustment(snapshot=first_snapshot, adjusted_score=82,
            reason='Skipped root revision.', revision=2, organization_id=self.org.id,
            created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'first adjustment revision'):
            invalid_root.full_clean()
        wrong_parent = PanelResultAdjustment(snapshot=other_snapshot, adjusted_score=82,
            reason='Wrong result chain.', revision=2, supersedes=first,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'supersede the latest same-result'):
            wrong_parent.full_clean()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            first.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            first.delete()

    def test_adjustment_form_rejects_whitespace_reason(self):
        form = PanelResultAdjustmentForm({'adjusted_score': '80', 'reason': '   '})
        self.assertFalse(form.is_valid())
        self.assertIn('reason', form.errors)

    def test_adjustment_operation_fails_closed_for_missing_hearing_or_snapshot(self):
        self.score(self.tokens[0], self.candidate_a, 80)
        self.score(self.tokens[0], self.candidate_b, 70)
        compile_panel_results(self.hearing, organization=self.org.as_claim(), user=self.owner)
        confirm_panel_signoff(self.hearing, organization=self.org.as_claim(), user=self.owner)
        snapshot = PanelResultSnapshot.objects.get(candidate=self.candidate_a)
        with patch('service_domains.operations.PanelHearing.objects.for_organization') as scoped:
            scoped.return_value.owned_by.return_value.select_for_update.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                adjust_panel_result(snapshot, 81, 'Missing hearing.',
                    organization=self.org.as_claim(), user=self.owner)
        with patch('service_domains.operations.PanelResultSnapshot.objects.for_organization') as scoped:
            scoped.return_value.owned_by.return_value.filter.return_value.values_list.return_value.first.return_value = self.hearing.pk
            scoped.return_value.owned_by.return_value.select_for_update.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                adjust_panel_result(snapshot, 81, 'Missing snapshot.',
                    organization=self.org.as_claim(), user=self.owner)
