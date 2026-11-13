"""0005 gives every existing session a service, and can be undone."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('assessments', '0004_flexible_weights_penalty')]
AFTER = [('assessments', '0005_session_service')]
TABLE = 'assessments_assessmentsession'


class SessionServiceMigrationTests(TransactionTestCase):

    def tearDown(self):
        # Leave the test database at the latest schema, as every later test expects it.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def columns(self):
        with connection.cursor() as cursor:
            return {column.name for column in connection.introspection.get_table_description(cursor, TABLE)}

    def test_every_session_that_already_exists_lands_in_classroom(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        ids = [Session.objects.create(organization_id=org, name=name).pk
               for org, name in ((1, 'First'), (1, 'Second'), (2, 'Third'))]
        self.assertNotIn('service', self.columns())

        new = self.migrate(AFTER)
        Session = new.get_model('assessments', 'AssessmentSession')
        self.assertIn('service', self.columns())
        self.assertEqual(Session.objects.count(), 3)
        self.assertEqual(set(Session.objects.filter(pk__in=ids).values_list('service', flat=True)), {'classroom'})

    def test_a_session_made_after_the_migration_without_a_service_is_classroom_too(self):
        new = self.migrate(AFTER)
        Session = new.get_model('assessments', 'AssessmentSession')
        self.assertEqual(Session.objects.create(organization_id=1, name='Later').service, 'classroom')

    def test_the_column_is_indexed(self):
        self.migrate(AFTER)
        with connection.cursor() as cursor:
            indexes = connection.introspection.get_constraints(cursor, TABLE)
        self.assertTrue(any(info['index'] and info['columns'] == ['service'] for info in indexes.values()))

    def test_it_reverses_and_keeps_the_sessions(self):
        new = self.migrate(AFTER)
        new.get_model('assessments', 'AssessmentSession').objects.create(organization_id=1, name='Keep me')
        old = self.migrate(BEFORE)
        self.assertNotIn('service', self.columns())
        self.assertEqual(old.get_model('assessments', 'AssessmentSession').objects.get().name, 'Keep me')
