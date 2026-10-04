from decimal import Decimal

from accounts.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.testing import make_org, make_user
from assessments.models import (
    AssessmentSession, EvaluationScore, GroupMembership, PresentationGroup,
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
            scope=RubricCategory.Scope.GROUP, max_points=Decimal('10'), weight=Decimal('100'))
        self.individual_cat = RubricCategory.objects.create(
            assessment_session=self.session, name='Communication',
            scope=RubricCategory.Scope.INDIVIDUAL, max_points=Decimal('10'), weight=Decimal('100'))
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
            {self.group_cat.pk: Decimal('8')},
            {self.student_a.pk: {self.individual_cat.pk: Decimal('9')},
             self.student_b.pk: {self.individual_cat.pk: Decimal('7')}})
        self.assertEqual(EvaluationScore.objects.count(), 3)

    def test_score_above_max_points_rejected(self):
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.student_c,
                {self.group_cat.pk: Decimal('11')}, {})

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
    def test_session_weights_must_sum_to_100(self):
        self.session.group_weight_percent = Decimal('70')
        self.session.individual_weight_percent = Decimal('40')
        with self.assertRaises(ValidationError):
            self.session.full_clean()

    def test_can_go_live_false_when_category_weights_dont_sum(self):
        RubricCategory.objects.create(
            assessment_session=self.session, name='Extra',
            scope=RubricCategory.Scope.GROUP, weight=Decimal('50'))
        self.assertFalse(self.session.can_go_live())

    def test_can_go_live_requires_matching_the_configured_split_not_100(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Split 60/40', created_by=self.lecturer,
            group_weight_percent=Decimal('60'), individual_weight_percent=Decimal('40'))
        group = PresentationGroup.objects.create(assessment_session=session, name='G1')
        # Weight=100 was the old flat cap - now over this session's 60%
        # group share, so it must NOT be considered ready.
        RubricCategory.objects.create(
            assessment_session=session, name='Only category',
            scope=RubricCategory.Scope.GROUP, weight=Decimal('100'))
        self.assertFalse(session.can_go_live())

        session.rubric_categories.all().delete()
        RubricCategory.objects.create(
            assessment_session=session, name='Only category',
            scope=RubricCategory.Scope.GROUP, weight=Decimal('60'))
        self.assertTrue(session.can_go_live())


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
    def test_group_score_percent_is_average_over_max(self):
        submit_evaluation(self.turn, self.student_c,
            {self.group_cat.pk: Decimal('8')}, {})
        self.assertEqual(group_score_percent(self.group1, self.session), Decimal('80.0'))

    def test_group_score_percent_scales_to_the_category_weight_not_100(self):
        # A category weighted at 60 (this session's group share) scoring
        # 80% of its max must land at 48.0 (80% of 60), not 80.0 - the old
        # normalize-to-100-then-recombine behavior this replaces.
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Weighted', created_by=self.lecturer,
            group_weight_percent=Decimal('60'), individual_weight_percent=Decimal('40'))
        category = RubricCategory.objects.create(
            assessment_session=session, name='Technical', scope=RubricCategory.Scope.GROUP,
            max_points=Decimal('10'), weight=Decimal('60'))
        group = PresentationGroup.objects.create(assessment_session=session, name='G1')
        student = Student.objects.create(assessment_session=session, full_name='Outsider', student_id='O1')
        turn = PresentationTurn.objects.create(
            assessment_session=session, group=group, status=PresentationTurn.Status.ACTIVE)
        submit_evaluation(turn, student, {category.pk: Decimal('8')}, {})
        self.assertEqual(group_score_percent(group, session), Decimal('48.0'))

    def test_individual_score_percent(self):
        submit_evaluation(self.turn, self.student_c,
            {self.group_cat.pk: Decimal('8')},
            {self.student_a.pk: {self.individual_cat.pk: Decimal('5')}})
        self.assertEqual(
            individual_score_percent(self.student_a, self.session), Decimal('50.0'))
