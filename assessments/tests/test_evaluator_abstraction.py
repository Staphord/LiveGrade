"""Additive JU-01a evaluator identity expansion; no legacy read switch."""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from accounts.testing import make_org, make_user
from assessments.models import AssessmentSession, Evaluation, Evaluator, Student


class EvaluatorIdentityTests(TestCase):
    def setUp(self):
        self.org = make_org('Evaluator identity organization')
        self.owner = make_user('evaluator-identity-owner')
        self.session = AssessmentSession.objects.create(name='Evaluator class',
            organization_id=self.org.id, created_by=self.owner)
        self.student = self.session.students.create(full_name='Roster evaluator')
        self.other_session = AssessmentSession.objects.create(name='Other class',
            organization_id=self.org.id, created_by=self.owner)
        self.other_student = self.other_session.students.create(full_name='Other evaluator')

    def test_roster_evaluator_is_session_scoped_and_unique(self):
        evaluator = self.student.evaluator_identities.get()
        self.assertEqual(evaluator.assessment_session_id, self.session.pk)
        self.assertEqual(evaluator.kind, Evaluator.Kind.ROSTER)
        evaluator.full_clean()
        self.assertIn('Roster evaluator', str(evaluator))
        duplicate = Evaluator(assessment_session=self.session,
            kind=Evaluator.Kind.ROSTER, student=self.student)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_classroom_student_save_creates_identity_idempotently(self):
        created = self.session.students.create(full_name='New roster row')
        identity = Evaluator.objects.get(student=created)

        created.full_name = 'Updated roster row'
        created.save(update_fields=('full_name',))

        self.assertEqual(identity.assessment_session_id, self.session.pk)
        self.assertEqual(identity.kind, Evaluator.Kind.ROSTER)
        self.assertEqual(created.evaluator_identities.count(), 1)

    def test_student_save_resolves_session_when_only_its_id_was_supplied(self):
        student = Student.objects.create(assessment_session_id=self.session.pk,
            full_name='ID-only roster row')

        self.assertEqual(student.evaluator_identities.get().kind, Evaluator.Kind.ROSTER)

    def test_other_service_student_does_not_receive_unapproved_identity_mapping(self):
        other_service = AssessmentSession.objects.create(name='Competition identity boundary',
            organization_id=self.org.id, created_by=self.owner, service='competition')
        student = other_service.students.create(full_name='Competition participant')

        self.assertFalse(Evaluator.objects.filter(student=student).exists())

    def test_roster_evaluator_requires_a_student_in_its_session(self):
        missing = Evaluator(assessment_session=self.session, kind=Evaluator.Kind.ROSTER)
        with self.assertRaisesMessage(ValidationError, 'Roster evaluators must link'):
            missing.full_clean()
        foreign = Evaluator(assessment_session=self.session,
            kind=Evaluator.Kind.ROSTER, student=self.other_student)
        with self.assertRaisesMessage(ValidationError, 'same session'):
            foreign.full_clean()

    def test_invited_and_open_evaluators_have_no_student_link(self):
        for kind in (Evaluator.Kind.INVITED, Evaluator.Kind.OPEN):
            evaluator = Evaluator(assessment_session=self.session, kind=kind)
            evaluator.full_clean()
            self.assertIn(evaluator.get_kind_display(), str(evaluator))
        invalid = Evaluator(assessment_session=self.session,
            kind=Evaluator.Kind.INVITED, student=self.student)
        with self.assertRaisesMessage(ValidationError, 'cannot link'):
            invalid.full_clean()

    def test_unknown_evaluator_kind_is_rejected(self):
        evaluator = Evaluator(assessment_session=self.session, kind='unknown')
        with self.assertRaisesMessage(ValidationError, 'supported evaluator kind'):
            evaluator.full_clean()

    def test_database_constraints_guard_identity_kind_and_roster_uniqueness(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Evaluator.objects.create(assessment_session=self.session,
                kind=Evaluator.Kind.INVITED, student=self.student)
        self.student.evaluator_identities.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Evaluator.objects.create(assessment_session=self.session,
                kind=Evaluator.Kind.ROSTER, student=self.student)

    def test_existing_evaluation_relation_stays_required_and_new_reference_nullable(self):
        group = self.session.groups.create(name='Scored group')
        turn = self.session.presentation_turns.create(group=group, status='active')
        evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=self.student)
        self.assertEqual(evaluation.evaluator, self.student)
        self.assertIsNone(evaluation.evaluator_record_id)


BEFORE = [('assessments', '0011_participant_privacy_notice')]
EXPANDED = [('assessments', '0012_evaluator_evaluation_evaluator_record_and_more')]
AFTER = [('assessments', '0013_backfill_roster_evaluator_identities')]


class EvaluatorExpansionMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_backfill_links_existing_evaluations_and_rollback_preserves_legacy_data(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        Student = old.get_model('assessments', 'Student')
        Group = old.get_model('assessments', 'PresentationGroup')
        Turn = old.get_model('assessments', 'PresentationTurn')
        EvaluationRow = old.get_model('assessments', 'Evaluation')
        RubricCategoryRow = old.get_model('assessments', 'RubricCategory')
        EvaluationScoreRow = old.get_model('assessments', 'EvaluationScore')
        session = Session.objects.create(organization_id=891, name='Legacy evaluator session')
        student = Student.objects.create(assessment_session=session, full_name='Existing evaluator')
        group = Group.objects.create(assessment_session=session, name='Existing group')
        turn = Turn.objects.create(assessment_session=session, group=group, status='closed')
        evaluation = EvaluationRow.objects.create(presentation_turn=turn, evaluator=student)
        category = RubricCategoryRow.objects.create(assessment_session=session,
            name='Legacy criterion', weight=100)
        EvaluationScoreRow.objects.create(evaluation=evaluation,
            rubric_category=category, value='77.00', max_value='100.00')

        expanded_state = self.migrate(EXPANDED)
        ExpandedEvaluation = expanded_state.get_model('assessments', 'Evaluation')
        self.assertEqual(ExpandedEvaluation.objects.get(pk=evaluation.pk).evaluator_record_id, None)
        new = self.migrate(AFTER)
        EvaluationRow = new.get_model('assessments', 'Evaluation')
        EvaluatorRow = new.get_model('assessments', 'Evaluator')
        expanded = EvaluationRow.objects.get(pk=evaluation.pk)
        self.assertEqual(expanded.evaluator_id, student.pk)
        self.assertEqual(expanded.evaluator_record.kind, 'roster')
        self.assertEqual(expanded.evaluator_record.student_id, student.pk)
        self.assertEqual(expanded.evaluator_record.assessment_session_id, session.pk)
        self.assertEqual(EvaluationRow.objects.count(), 1)
        self.assertEqual(EvaluatorRow.objects.count(), 1)
        LegacyScore = new.get_model('assessments', 'EvaluationScore')
        self.assertEqual(list(LegacyScore.objects.filter(evaluation__evaluator_id=student.pk)
            .values_list('rubric_category_id', 'value')),
            list(LegacyScore.objects.filter(evaluation__evaluator_record__student_id=student.pk)
                .values_list('rubric_category_id', 'value')))
        self.assertEqual(str(LegacyScore.objects.get(evaluation_id=evaluation.pk).value), '77.00')

        old_again = self.migrate(EXPANDED)
        LegacyEvaluation = old_again.get_model('assessments', 'Evaluation')
        preserved = LegacyEvaluation.objects.get(pk=evaluation.pk)
        self.assertEqual(preserved.evaluator_id, student.pk)
        self.assertIsNone(preserved.evaluator_record_id)
        self.assertEqual(str(old_again.get_model('assessments', 'EvaluationScore')
            .objects.get(evaluation_id=evaluation.pk).value), '77.00')
        self.assertEqual(old_again.get_model('assessments', 'Evaluator').objects.count(), 0)
        with connection.cursor() as cursor:
            table_names = connection.introspection.table_names(cursor)
        self.assertIn('assessments_evaluator', table_names)

    def test_backfill_refuses_cross_session_legacy_rater_before_writing(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        Student = old.get_model('assessments', 'Student')
        Group = old.get_model('assessments', 'PresentationGroup')
        Turn = old.get_model('assessments', 'PresentationTurn')
        EvaluationRow = old.get_model('assessments', 'Evaluation')
        session = Session.objects.create(organization_id=892, name='Scored session')
        other_session = Session.objects.create(organization_id=892, name='Rater session')
        foreign_rater = Student.objects.create(
            assessment_session=other_session, full_name='Foreign legacy rater')
        group = Group.objects.create(assessment_session=session, name='Scored group')
        turn = Turn.objects.create(assessment_session=session, group=group, status='closed')
        evaluation = EvaluationRow.objects.create(
            presentation_turn=turn, evaluator=foreign_rater)

        expanded = self.migrate(EXPANDED)
        with self.assertRaisesRegex(RuntimeError, 'different session'):
            self.migrate(AFTER)

        ExpandedEvaluation = expanded.get_model('assessments', 'Evaluation')
        EvaluatorRow = expanded.get_model('assessments', 'Evaluator')
        self.assertEqual(EvaluatorRow.objects.count(), 0)
        self.assertIsNone(ExpandedEvaluation.objects.get(pk=evaluation.pk).evaluator_record_id)
        # The fixture is intentionally invalid; remove it so tearDown can
        # migrate the test database back to its normal leaf state.
        ExpandedEvaluation.objects.filter(pk=evaluation.pk).delete()

    def test_backfill_crosses_batch_boundary_and_preserves_existing_scores(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        Student = old.get_model('assessments', 'Student')
        Group = old.get_model('assessments', 'PresentationGroup')
        Turn = old.get_model('assessments', 'PresentationTurn')
        EvaluationRow = old.get_model('assessments', 'Evaluation')
        RubricCategoryRow = old.get_model('assessments', 'RubricCategory')
        EvaluationScoreRow = old.get_model('assessments', 'EvaluationScore')
        session = Session.objects.create(organization_id=893, name='Batch session')
        group = Group.objects.create(assessment_session=session, name='Batch group')
        turn = Turn.objects.create(assessment_session=session, group=group, status='closed')
        category = RubricCategoryRow.objects.create(
            assessment_session=session, name='Shared criterion', weight=100)
        count = 1007
        students = Student.objects.bulk_create([
            Student(assessment_session=session, full_name=f'Learner {index}',
                student_id=f'B-{index}') for index in range(count)])
        evaluations = EvaluationRow.objects.bulk_create([
            EvaluationRow(presentation_turn=turn, evaluator=student)
            for student in students])
        EvaluationScoreRow.objects.bulk_create([
            EvaluationScoreRow(evaluation=evaluation, rubric_category=category,
                value='77.00', max_value='100.00') for evaluation in evaluations])

        self.migrate(EXPANDED)
        new = self.migrate(AFTER)
        ExpandedEvaluation = new.get_model('assessments', 'Evaluation')
        EvaluatorRow = new.get_model('assessments', 'Evaluator')
        LegacyScore = new.get_model('assessments', 'EvaluationScore')
        self.assertEqual(EvaluatorRow.objects.filter(
            assessment_session_id=session.pk, kind='roster').count(), count)
        self.assertEqual(ExpandedEvaluation.objects.filter(
            presentation_turn_id=turn.pk, evaluator_record__isnull=False).count(), count)
        self.assertEqual(LegacyScore.objects.filter(
            evaluation__presentation_turn_id=turn.pk,
            evaluation__evaluator__assessment_session_id=session.pk).count(), count)
        self.assertEqual(LegacyScore.objects.filter(
            evaluation__presentation_turn_id=turn.pk,
            evaluation__evaluator_record__student__assessment_session_id=session.pk,
            value='77.00').count(), count)
