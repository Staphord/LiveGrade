"""The one-off copy of sessions out of DevPerf's database.

``devperf`` here is a second in-memory database standing in for DevPerf's. It has
LiveGrade's tables (the schema is the same) plus the one DevPerf table the copy
reads for lecturers' names, ``auth_user``.
"""
import uuid
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.db import connections
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from assessments.models import (AssessmentSession, Evaluation, EvaluationScore,
                                GroupMembership, ParticipationRecord,
                                PresentationGroup, PresentationTurn,
                                RubricCategory, Student)

SRC = 'devperf'


class ImportTestCase(TestCase):
    databases = {'default', SRC}

    def setUp(self):
        with connections[SRC].cursor() as cursor:
            cursor.execute(
                'CREATE TABLE IF NOT EXISTS auth_user (id integer PRIMARY KEY, username text, '
                'first_name text, last_name text, email text)')
            cursor.execute('DELETE FROM auth_user')
            cursor.execute("INSERT INTO auth_user VALUES (7, 'tcher', 'Tea', 'Cher', 'tea@uni.test')")
        self.build_source()

    def build_source(self):
        """A DevPerf-side session with everything hanging off it, owned by user 7."""
        db = SRC
        owner = User.objects.using(db).create(id=7, sub='source-only', username='source-owner')
        self.session = AssessmentSession.objects.using(db).create(
            id=41, organization_id=3, name='FYP Demo', created_by=owner)
        self.uuid = self.session.uuid
        self.ann = Student.objects.using(db).create(
            id=5, assessment_session=self.session, full_name='Ann Able', student_id='S1')
        self.bob = Student.objects.using(db).create(
            id=6, assessment_session=self.session, full_name='Bob Best', student_id='S2')
        self.group = PresentationGroup.objects.using(db).create(
            id=9, assessment_session=self.session, name='Red', order=1)
        GroupMembership.objects.using(db).create(id=1, group=self.group, student=self.ann)
        self.rubric = RubricCategory.objects.using(db).create(
            id=2, assessment_session=self.session, name='Quality',
            scope=RubricCategory.Scope.GROUP, weight=60)
        self.turn = PresentationTurn.objects.using(db).create(
            id=4, assessment_session=self.session, group=self.group)
        ParticipationRecord.objects.using(db).create(id=3, assessment_session=self.session, student=self.ann)
        evaluation = Evaluation.objects.using(db).create(
            id=8, presentation_turn=self.turn, evaluator=self.bob, target_student=self.ann)
        EvaluationScore.objects.using(db).create(
            id=12, evaluation=evaluation, rubric_category=self.rubric, value=Decimal('7.50'))
        AssessmentSession.objects.using(db).filter(pk=41).update(designated_next_group=self.group)

    def run_import(self, *args):
        out = StringIO()
        call_command('import_from_devperf', *args, stdout=out)
        return out.getvalue()


class CopyTests(ImportTestCase):

    def test_everything_is_copied_with_its_ids_and_uuid(self):
        text = self.run_import()
        session = AssessmentSession.objects.get(pk=41)
        self.assertEqual((session.name, session.organization_id, session.uuid), ('FYP Demo', 3, self.uuid))
        self.assertEqual(Student.objects.count(), 2)
        self.assertEqual(GroupMembership.objects.count(), 1)
        self.assertEqual(EvaluationScore.objects.get().value, Decimal('7.50'))
        self.assertEqual(PresentationTurn.objects.get().pk, 4)
        self.assertEqual(session.designated_next_group_id, 9)
        self.assertIn('Check passed', text)

    def test_the_lecturer_is_recognised_by_devperfs_user_id(self):
        self.run_import()
        lecturer = User.objects.get(sub='7')
        self.assertEqual((lecturer.first_name, lecturer.last_name, lecturer.email), ('Tea', 'Cher', 'tea@uni.test'))
        self.assertEqual(lecturer.username, 'devperf-7')
        self.assertEqual(AssessmentSession.objects.get(pk=41).created_by_id, lecturer.pk)

    def test_a_lecturer_missing_from_devperf_still_gets_a_row(self):
        with connections[SRC].cursor() as cursor:
            cursor.execute('DELETE FROM auth_user')
        self.run_import()
        self.assertEqual(User.objects.get(sub='7').first_name, '')

    def test_a_session_with_no_creator_copies_with_none(self):
        AssessmentSession.objects.using(SRC).filter(pk=41).update(created_by=None)
        self.run_import()
        self.assertIsNone(AssessmentSession.objects.get(pk=41).created_by_id)
        self.assertFalse(User.objects.filter(sub='7').exists())

    def test_the_join_url_session_is_the_same_one(self):
        self.run_import()
        self.assertEqual(AssessmentSession.objects.get(uuid=self.uuid).pk, 41)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.run_import()
        text = self.run_import()
        self.assertEqual(AssessmentSession.objects.count(), 1)
        self.assertEqual(Student.objects.count(), 2)
        self.assertIn('0 added', text)

    def test_rows_already_here_are_left_as_they_are(self):
        self.run_import()
        AssessmentSession.objects.filter(pk=41).update(name='Edited here')
        self.run_import()
        self.assertEqual(AssessmentSession.objects.get(pk=41).name, 'Edited here')

    def test_new_rows_after_the_copy_do_not_collide_with_copied_ids(self):
        self.run_import()
        fresh = AssessmentSession.objects.create(organization_id=3, name='New')
        self.assertGreater(fresh.pk, 41)

    def test_devperfs_database_is_not_changed(self):
        before = AssessmentSession.objects.using(SRC).count(), Student.objects.using(SRC).count()
        self.run_import()
        self.assertEqual((AssessmentSession.objects.using(SRC).count(), Student.objects.using(SRC).count()), before)
        self.assertEqual(AssessmentSession.objects.using(SRC).get(pk=41).name, 'FYP Demo')


class DryRunAndReplaceTests(ImportTestCase):

    def test_a_dry_run_reports_and_keeps_nothing(self):
        text = self.run_import('--dry-run')
        self.assertIn('DRY RUN', text)
        self.assertIn('Check passed', text)
        self.assertEqual(AssessmentSession.objects.count(), 0)
        self.assertFalse(User.objects.filter(sub='7').exists())

    def test_replace_needs_confirmation(self):
        with self.assertRaisesMessage(CommandError, '--yes'):
            self.run_import('--replace')

    def test_replace_empties_this_side_first_then_copies_everything(self):
        AssessmentSession.objects.create(organization_id=3, name='Stale local row')
        self.run_import('--replace', '--yes')
        self.assertEqual(list(AssessmentSession.objects.values_list('name', flat=True)), ['FYP Demo'])

    def test_a_replace_dry_run_needs_no_confirmation_and_keeps_the_old_data(self):
        AssessmentSession.objects.create(organization_id=3, name='Stale local row')
        self.run_import('--replace', '--dry-run')
        self.assertEqual(list(AssessmentSession.objects.values_list('name', flat=True)), ['Stale local row'])


class RefusalTests(TestCase):

    def test_the_source_cannot_be_livegrades_own_database(self):
        with self.assertRaisesMessage(CommandError, "not LiveGrade's own"):
            call_command('import_from_devperf', '--source', 'default')

    def test_an_unconfigured_source_says_how_to_configure_it(self):
        with self.assertRaisesMessage(CommandError, 'DEVPERF_DB_NAME'):
            call_command('import_from_devperf', '--source', 'nowhere')


class VerificationTests(ImportTestCase):

    def test_a_missing_row_is_reported_and_fails_the_command(self):
        from unittest import mock

        from assessments.management.commands import import_from_devperf as module

        real = module.Command.copy_model

        def drop_memberships(self, src, model, users):
            if model is GroupMembership:
                return 0
            return real(self, src, model, users)

        out = StringIO()
        with mock.patch.object(module.Command, 'copy_model', drop_memberships), \
                self.assertRaisesMessage(CommandError, 'nothing should be cut over'):
            call_command('import_from_devperf', stdout=out)
        self.assertIn('PROBLEM: assessments.GroupMembership: 1 of 1 rows missing', out.getvalue())

    def test_grades_that_do_not_add_up_are_reported(self):
        from unittest import mock

        from assessments.management.commands import import_from_devperf as module

        real = module.Command.copy_model

        def wrong_value(self, src, model, users):
            result = real(self, src, model, users)
            if model is EvaluationScore:
                EvaluationScore.objects.update(value=Decimal('1.00'))
            return result

        out = StringIO()
        with mock.patch.object(module.Command, 'copy_model', wrong_value), \
                self.assertRaises(CommandError):
            call_command('import_from_devperf', stdout=out)
        self.assertIn('EvaluationScore values differ: DevPerf 7.5, here 1', out.getvalue())

    def test_an_empty_source_copies_nothing_and_passes(self):
        AssessmentSession.objects.using(SRC).all().delete()
        User.objects.using(SRC).all().delete()
        text = self.run_import()
        self.assertIn('Check passed', text)
        self.assertEqual(AssessmentSession.objects.count(), 0)
