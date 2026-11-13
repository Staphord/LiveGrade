from io import StringIO

from django.core.management import call_command, CommandError
from django.test import TestCase

from accounts.testing import make_org, make_user
from assessments.models import AssessmentSession, Evaluator, Evaluation


class EvaluatorIdentityParityCommandTests(TestCase):
    def setUp(self):
        organization = make_org('Parity organization')
        owner = make_user('parity-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=organization.id, created_by=owner,
            service='classroom', name='Parity session')
        self.student = self.session.students.create(full_name='Amina')
        self.group = self.session.groups.create(name='Group 1')
        self.turn = self.session.presentation_turns.create(
            group=self.group, status='closed')

    def run_check(self):
        output = StringIO()
        call_command('check_evaluator_identity_parity', stdout=output)
        return output.getvalue()

    def test_parity_passes_for_complete_roster_and_evaluation(self):
        identity = self.student.evaluator_identities.get()
        Evaluation.objects.create(presentation_turn=self.turn,
            evaluator=self.student, evaluator_record=identity)

        output = self.run_check()

        self.assertIn('classroom_sessions=1', output)
        self.assertIn('classroom_students=1', output)
        self.assertIn('roster_evaluator_identities=1', output)
        self.assertIn('classroom_evaluations=1', output)
        self.assertIn('Evaluator identity parity passed', output)
        self.assertIn('not authorization to switch reads', output)

    def test_parity_fails_for_missing_and_mismatched_identity_rows(self):
        self.student.evaluator_identities.all().delete()
        other_session = AssessmentSession.objects.create(
            organization_id=self.session.organization_id,
            created_by=self.session.created_by,
            service='classroom', name='Other session')
        other_student = other_session.students.create(full_name='Other')
        wrong_identity = other_student.evaluator_identities.get()
        Evaluation.objects.create(presentation_turn=self.turn,
            evaluator=self.student, evaluator_record=wrong_identity)
        Evaluator.objects.create(assessment_session=self.session, kind=Evaluator.Kind.OPEN)

        with self.assertRaises(CommandError) as error:
            self.run_check()

        self.assertIn('students_missing_identity', str(error.exception))
        self.assertIn('invalid_roster_identities', str(error.exception))
        self.assertIn('evaluations_missing_or_mismatched_identity', str(error.exception))

    def test_non_classroom_rows_are_not_part_of_parity_check(self):
        other_session = AssessmentSession.objects.create(
            organization_id=self.session.organization_id,
            created_by=self.session.created_by,
            service='competition', name='Other service')
        other_student = other_session.students.create(full_name='Competition entrant')
        Evaluation.objects.create(presentation_turn=other_session.presentation_turns.create(
            group=other_session.groups.create(name='Track'), status='closed'),
            evaluator=other_student)

        output = self.run_check()

        self.assertIn('classroom_sessions=1', output)
        self.assertIn('classroom_students=1', output)
        self.assertIn('classroom_evaluations=0', output)
        self.assertIn('Evaluator identity parity passed', output)


class EvaluatorBackfillPreflightCommandTests(TestCase):
    def setUp(self):
        organization = make_org('Backfill preflight organization')
        owner = make_user('backfill-preflight-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=organization.id, created_by=owner,
            service='classroom', name='Preflight session')
        self.student = self.session.students.create(full_name='Roster evaluator')
        self.group = self.session.groups.create(name='Group 1')
        self.turn = self.session.presentation_turns.create(
            group=self.group, status='closed')

    def run_preflight(self):
        output = StringIO()
        call_command('check_evaluator_backfill_preflight', stdout=output)
        return output.getvalue()

    def test_preflight_passes_without_cross_session_raters(self):
        Evaluation.objects.create(presentation_turn=self.turn, evaluator=self.student)
        output = self.run_preflight()
        self.assertIn('evaluations_with_cross_session_rater=0', output)
        self.assertIn('preflight passed', output)

    def test_preflight_reports_only_ids_for_cross_session_rater_rows(self):
        other_session = AssessmentSession.objects.create(
            organization_id=self.session.organization_id,
            created_by=self.session.created_by,
            service='classroom', name='Other session')
        foreign_student = other_session.students.create(full_name='Private learner name')
        evaluation = Evaluation.objects.create(
            presentation_turn=self.turn, evaluator=foreign_student)

        output = StringIO()
        with self.assertRaises(CommandError):
            call_command('check_evaluator_backfill_preflight', stdout=output)

        self.assertIn('evaluations_with_cross_session_rater=1', output.getvalue())
        self.assertIn(str(evaluation.pk), output.getvalue())
        self.assertNotIn('Private learner name', output.getvalue())
