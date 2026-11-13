"""Launch-interest schema changes preserve existing accounts, choices, and sessions."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from accounts.testing import make_user
from assessments.models import AssessmentSession
from portal.models import ServiceAccess


class InterestMigrationTests(TransactionTestCase):

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([('portal', target)])
        return executor.loader.project_state([('portal', target)]).apps

    def test_forward_and_reverse_leave_existing_choices_and_sessions_intact(self):
        self.migrate('0001_initial')
        user = make_user('migration-owner')
        choice = ServiceAccess.objects.create(user=user, service='classroom')
        session = AssessmentSession.objects.create(
            organization_id=7, name='Preserved Classroom', created_by=user)
        self.assertNotIn('portal_serviceinterest', connection.introspection.table_names())

        apps = self.migrate('0002_serviceinterest')
        interest = apps.get_model('portal', 'ServiceInterest')
        interest.objects.create(email='synthetic@example.test', service='panel', consent=True)
        self.assertEqual(interest.objects.count(), 1)
        self.assertTrue(ServiceAccess.objects.filter(pk=choice.pk, user_id=user.pk).exists())
        self.assertTrue(AssessmentSession.objects.filter(pk=session.pk, created_by_id=user.pk).exists())

        self.migrate('0001_initial')
        self.assertNotIn('portal_serviceinterest', connection.introspection.table_names())
        self.assertTrue(ServiceAccess.objects.filter(pk=choice.pk, service='classroom').exists())
        self.assertEqual(AssessmentSession.objects.get(pk=session.pk).name, 'Preserved Classroom')
