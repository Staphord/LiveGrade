from decimal import Decimal

from accounts.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.testing import make_org, make_user
from assessments.models import (
    AssessmentSession, Evaluation, EvaluationScore, GroupMembership, PresentationGroup,
    PresentationTurn, RubricCategory, Student,
)
from assessments.scoring import group_score_percent, individual_score_percent
from assessments.services import (
    already_submitted, eligible_to_evaluate, match_student, submit_evaluation,
)


class AssessmentTestBase(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer,
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID)
        self.group_cat = RubricCategory.objects.create(
            assessment_session=self.session, name='Technical implementation',
            scope=RubricCategory.Scope.GROUP, weight=Decimal('60'))
        self.individual_cat = RubricCategory.objects.create(
            assessment_session=self.session, name='Communication',
            scope=RubricCategory.Scope.INDIVIDUAL, weight=Decimal('40'))
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        self.student_a = Student.objects.create(assessment_session=self.session,
            full_name='Student A', student_id='A1')
        self.student_b = Student.objects.create(assessment_session=self.session,
            full_name='Student B', student_id='B1')
        self.student_c = Student.objects.create(assessment_session=self.session,
            full_name='Student C', student_id='C1')
        GroupMembership.objects.create(group=self.group1, student=self.student_a)
        GroupMembership.objects.create(group=self.group1, student=self.student_b)
        self.turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group1,
            status=PresentationTurn.Status.ACTIVE)


class ScoreBoundsTests(AssessmentTestBase):
    def test_score_within_bounds_ok(self):
        submit_evaluation(self.turn, self.student_c,
            {self.group_cat.pk: Decimal('48')},
            {self.student_a.pk: {self.individual_cat.pk: Decimal('36')},
             self.student_b.pk: {self.individual_cat.pk: Decimal('28')}})
        self.assertEqual(EvaluationScore.objects.count(), 3)

    def test_score_above_the_category_weight_rejected(self):
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.student_c,
                {self.group_cat.pk: Decimal('60.01')}, {})

    def test_a_score_equal_to_the_weight_is_full_marks(self):
        submit_evaluation(self.turn, self.student_c, {self.group_cat.pk: Decimal('60')}, {})
        self.assertEqual(group_score_percent(self.group1, self.session), Decimal('60.0'))

    def test_a_category_with_no_weight_cannot_be_scored(self):
        RubricCategory.objects.filter(pk=self.group_cat.pk).update(weight=Decimal('0'))
        with self.assertRaisesMessage(ValidationError, 'no weight to score against'):
            submit_evaluation(self.turn, self.student_c, {self.group_cat.pk: Decimal('0')}, {})
        self.assertFalse(EvaluationScore.objects.exists())

    def test_a_score_row_without_its_own_scale_takes_the_categorys_current_weight(self):
        evaluation = Evaluation.objects.create(presentation_turn=self.turn, evaluator=self.student_c)
        score = EvaluationScore.objects.create(
            evaluation=evaluation, rubric_category=self.group_cat, value=Decimal('12'))
        self.assertEqual(score.max_value, Decimal('60'))

    def test_the_score_remembers_the_weight_it_was_given_out_of(self):
        submit_evaluation(self.turn, self.student_c, {self.group_cat.pk: Decimal('30')}, {})
        self.assertEqual(EvaluationScore.objects.get().max_value, Decimal('60.00'))

    def test_negative_score_rejected(self):
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.student_c,
                {self.group_cat.pk: Decimal('-1')}, {})


class SelfEvaluationBlockedTests(AssessmentTestBase):
    def test_group_member_cannot_evaluate_own_group(self):
        self.assertFalse(eligible_to_evaluate(self.student_a, self.turn))
        self.assertFalse(eligible_to_evaluate(self.student_b, self.turn))

    def test_outsider_can_evaluate(self):
        self.assertTrue(eligible_to_evaluate(self.student_c, self.turn))

    def test_submit_raises_for_own_group(self):
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.student_a,
                {self.group_cat.pk: Decimal('5')}, {})


class EvaluationUniquenessTests(AssessmentTestBase):
    def test_double_submission_blocked(self):
        submit_evaluation(self.turn, self.student_c,
            {self.group_cat.pk: Decimal('8')}, {})
        self.assertTrue(already_submitted(self.student_c, self.turn))
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.student_c,
                {self.group_cat.pk: Decimal('5')}, {})


class RubricScopeDefaultTests(AssessmentTestBase):
    def test_new_category_defaults_to_group_scope(self):
        category = RubricCategory.objects.create(
            assessment_session=self.session, name='Innovation')
        self.assertEqual(category.scope, RubricCategory.Scope.GROUP)


class WeightValidationTests(AssessmentTestBase):
    def test_can_go_live_false_when_category_weights_dont_sum_to_100(self):
        self.assertEqual(self.session.rubric_weight_total(), Decimal('100'))
        RubricCategory.objects.create(
            assessment_session=self.session, name='Extra',
            scope=RubricCategory.Scope.GROUP, weight=Decimal('50'))
        PresentationGroup.objects.get_or_create(assessment_session=self.session, name='Group 1')
        self.assertFalse(self.session.can_go_live())

    def test_can_go_live_needs_exactly_100_combined_whatever_the_split(self):
        for group_weight, individual_weight, ready in (
                (60, 40, True), (87, 13, True), (100, None, True), (None, 100, True),
                (60, 39, False), (60, 41, False), (50, None, False)):
            with self.subTest(split=(group_weight, individual_weight)):
                session = AssessmentSession.objects.create(
                    organization_id=self.org.pk, name='Split', created_by=self.lecturer)
                PresentationGroup.objects.create(assessment_session=session, name='G1')
                if group_weight:
                    RubricCategory.objects.create(
                        assessment_session=session, name='G', weight=Decimal(group_weight),
                        scope=RubricCategory.Scope.GROUP)
                if individual_weight:
                    RubricCategory.objects.create(
                        assessment_session=session, name='I', weight=Decimal(individual_weight),
                        scope=RubricCategory.Scope.INDIVIDUAL)
                self.assertEqual(session.can_go_live(), ready)

    def test_can_go_live_needs_a_group(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='No groups', created_by=self.lecturer)
        RubricCategory.objects.create(assessment_session=session, name='Only', weight=Decimal('100'))
        self.assertFalse(session.can_go_live())

    def test_the_group_and_individual_shares_are_the_sums_of_their_weights(self):
        self.assertEqual(self.session.group_weight_total(), Decimal('60'))
        self.assertEqual(self.session.individual_weight_total(), Decimal('40'))


class VotingPausedTests(AssessmentTestBase):
    def test_no_submission_while_paused(self):
        self.session.voting_paused = True
        self.session.save()
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.student_c,
                {self.group_cat.pk: Decimal('5')}, {})


class MatchStudentTests(AssessmentTestBase):
    def test_case_insensitive_match_by_student_id(self):
        matched = match_student(self.session, 'a1')
        self.assertEqual(matched, self.student_a)

    def test_no_match_returns_none(self):
        self.assertIsNone(match_student(self.session, 'nope'))


class ScoringAggregationTests(AssessmentTestBase):
    def test_group_score_is_the_average_fraction_of_the_weight_in_points(self):
        submit_evaluation(self.turn, self.student_c,
            {self.group_cat.pk: Decimal('48')}, {})
        self.assertEqual(group_score_percent(self.group1, self.session), Decimal('48.0'))

    def test_group_score_averages_several_voters(self):
        voter_d = Student.objects.create(assessment_session=self.session, full_name='D', student_id='D1')
        submit_evaluation(self.turn, self.student_c, {self.group_cat.pk: Decimal('60')}, {})
        submit_evaluation(self.turn, voter_d, {self.group_cat.pk: Decimal('30')}, {})
        self.assertEqual(group_score_percent(self.group1, self.session), Decimal('45.0'))

    def test_a_weight_edited_later_rescales_past_scores_proportionally(self):
        # 48 out of 60 is 80% of the category. After the weight is changed to 50
        # the same vote is still 80%, i.e. 40 of the new 50 points.
        submit_evaluation(self.turn, self.student_c, {self.group_cat.pk: Decimal('48')}, {})
        self.group_cat.weight = Decimal('50')
        self.group_cat.save()
        self.assertEqual(group_score_percent(self.group1, self.session), Decimal('40.0'))

    def test_votes_given_before_and_after_a_weight_edit_are_averaged_as_fractions(self):
        voter_d = Student.objects.create(assessment_session=self.session, full_name='D', student_id='D1')
        submit_evaluation(self.turn, self.student_c, {self.group_cat.pk: Decimal('60')}, {})  # 100%
        self.group_cat.weight = Decimal('20')
        self.group_cat.save()
        submit_evaluation(self.turn, voter_d, {self.group_cat.pk: Decimal('10')}, {})  # 50%
        self.assertEqual(group_score_percent(self.group1, self.session), Decimal('15.0'))  # 75% of 20

    def test_individual_score(self):
        submit_evaluation(self.turn, self.student_c,
            {self.group_cat.pk: Decimal('48')},
            {self.student_a.pk: {self.individual_cat.pk: Decimal('20')}})
        self.assertEqual(
            individual_score_percent(self.student_a, self.session), Decimal('20.0'))
