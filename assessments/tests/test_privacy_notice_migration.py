"""Migration 0011 seeds a versioned notice without rewriting participation rows."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [('assessments', '0010_leveldescriptor_templateleveldescriptor')]
AFTER = [('assessments', '0011_participant_privacy_notice')]
PUBLIC_RESULTS_NOTICE = [('assessments', '0015_classroom_public_results_notice')]
BEFORE_PUBLIC_RESULTS_NOTICE = [('assessments', '0014_assessmentsession_public_results_released_at_and_more')]


class PrivacyNoticeMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_initial_notice_is_seeded_and_legacy_participation_is_preserved(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        Student = old.get_model('assessments', 'Student')
        ParticipationRecord = old.get_model('assessments', 'ParticipationRecord')
        session = Session.objects.create(organization_id=781, name='Existing class')
        student = Student.objects.create(assessment_session=session, full_name='Learner')
        record = ParticipationRecord.objects.create(assessment_session=session, student=student)

        new = self.migrate(AFTER)
        Notice = new.get_model('assessments', 'PrivacyNotice')
        Record = new.get_model('assessments', 'ParticipationRecord')
        notice = Notice.objects.get(service='classroom', version='1', is_active=True)
        preserved = Record.objects.get(pk=record.pk)
        self.assertEqual((preserved.student_id, preserved.assessment_session_id),
                         (student.pk, session.pk))
        self.assertIsNone(preserved.privacy_notice_id)
        self.assertIn('which learner submitted each score', notice.body)

    def test_rollback_preserves_participation_when_no_notice_was_recorded(self):
        new = self.migrate(AFTER)
        Session = new.get_model('assessments', 'AssessmentSession')
        Student = new.get_model('assessments', 'Student')
        Record = new.get_model('assessments', 'ParticipationRecord')
        session = Session.objects.create(organization_id=782, name='Unused notice class')
        student = Student.objects.create(assessment_session=session, full_name='Learner')
        record = Record.objects.create(assessment_session=session, student=student)

        old = self.migrate(BEFORE)
        self.assertEqual(old.get_model('assessments', 'ParticipationRecord').objects.get(pk=record.pk).student_id,
                         student.pk)

    def test_rollback_is_refused_after_a_notice_version_was_recorded(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        Student = old.get_model('assessments', 'Student')
        Record = old.get_model('assessments', 'ParticipationRecord')
        session = Session.objects.create(organization_id=783, name='Shown notice class')
        student = Student.objects.create(assessment_session=session, full_name='Learner')
        record = Record.objects.create(assessment_session=session, student=student)

        new = self.migrate(AFTER)
        Notice = new.get_model('assessments', 'PrivacyNotice')
        Record = new.get_model('assessments', 'ParticipationRecord')
        notice = Notice.objects.get(service='classroom', version='1')
        Record.objects.filter(pk=record.pk).update(privacy_notice_id=notice.pk)
        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(RuntimeError, 'Privacy notice versions have been accepted'):
            executor.migrate(BEFORE)
        self.assertTrue(Notice.objects.filter(pk=notice.pk).exists())

    def test_public_results_notice_v2_is_activated_without_rewriting_v1_and_rolls_back_safely(self):
        self.migrate(BEFORE)
        current = self.migrate(PUBLIC_RESULTS_NOTICE)
        Notice = current.get_model('assessments', 'PrivacyNotice')
        old_notice = Notice.objects.get(service='classroom', version='1')
        new_notice = Notice.objects.get(service='classroom', version='2')
        self.assertFalse(old_notice.is_active)
        self.assertIn('which learner submitted each score', old_notice.body)
        self.assertTrue(new_notice.is_active)
        self.assertIn('public room screen', new_notice.body)

        rolled_back = self.migrate(BEFORE_PUBLIC_RESULTS_NOTICE)
        NoticeAfterRollback = rolled_back.get_model('assessments', 'PrivacyNotice')
        self.assertTrue(NoticeAfterRollback.objects.get(
            service='classroom', version='1').is_active)
        self.assertFalse(NoticeAfterRollback.objects.get(
            service='classroom', version='2').is_active)

    def test_live_rankings_notice_upgrade_preserves_release_and_notice_history(self):
        old = self.migrate(PUBLIC_RESULTS_NOTICE)
        Session = old.get_model('assessments', 'AssessmentSession')
        Notice = old.get_model('assessments', 'PrivacyNotice')
        from django.utils import timezone
        released = Session.objects.create(organization_id=784, name='Released classroom',
            public_results_released_at=timezone.now())
        private = Session.objects.create(organization_id=784, name='Private classroom')
        # TransactionTestCase flushes seeded rows between migration rehearsals.
        previous_notice, _ = Notice.objects.get_or_create(service='classroom', version='2',
            defaults={'title': 'Previous notice', 'body': 'Previous public-results notice.', 'is_active': True})
        new = self.migrate([('assessments', '0017_live_rankings_privacy_notice')])
        NewSession = new.get_model('assessments', 'AssessmentSession')
        NewNotice = new.get_model('assessments', 'PrivacyNotice')
        self.assertTrue(NewSession.objects.get(pk=released.pk).public_leaderboard_show_scores)
        self.assertFalse(NewSession.objects.get(pk=private.pk).public_leaderboard_show_scores)
        current = NewNotice.objects.get(service='classroom', version='3', is_active=True)
        self.assertIn('as groups finish voting', current.body)
        self.assertIn('Classroom polls', current.body)
        self.assertTrue(NewNotice.objects.filter(pk=previous_notice.pk).exists())
        self.assertFalse(NewNotice.objects.get(pk=previous_notice.pk).is_active)
        rolled_back = self.migrate(PUBLIC_RESULTS_NOTICE)
        self.assertTrue(rolled_back.get_model('assessments', 'PrivacyNotice').objects.get(
            service='classroom', version='2').is_active)
