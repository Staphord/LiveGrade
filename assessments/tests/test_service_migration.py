"""0027 moves legacy Classroom service records into the Assessment service."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('assessments', '0026_assessmentsession_avoid_reciprocal_pairs_and_more')]
AFTER = [('assessments', '0027_migrate_classroom_service_to_assessment')]
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

    def test_every_legacy_classroom_session_moves_to_assessment(self):
        old = self.migrate(BEFORE)
        Session = old.get_model('assessments', 'AssessmentSession')
        ids = [Session.objects.create(organization_id=org, name=name, service='classroom').pk
               for org, name in ((1, 'First'), (1, 'Second'), (2, 'Third'))]
        Template = old.get_model('assessments', 'SessionTemplate')
        template_id = Template.objects.create(
            organization_id=1, name='Legacy template', service='classroom').pk
        PrivacyNotice = old.get_model('assessments', 'PrivacyNotice')
        notice_id = PrivacyNotice.objects.create(
            service='classroom', version='legacy', title='Legacy notice', body='Old service copy',
        ).pk
        self.assertIn('service', self.columns())

        new = self.migrate(AFTER)
        Session = new.get_model('assessments', 'AssessmentSession')
        self.assertIn('service', self.columns())
        self.assertEqual(Session.objects.count(), 3)
        self.assertEqual(set(Session.objects.filter(pk__in=ids).values_list('service', flat=True)), {'assessment'})

        Template = new.get_model('assessments', 'SessionTemplate')
        PrivacyNotice = new.get_model('assessments', 'PrivacyNotice')
        self.assertEqual(Template.objects.get(pk=template_id).service, 'assessment')
        self.assertEqual(PrivacyNotice.objects.get(pk=notice_id).service, 'assessment')

    def test_a_session_made_after_the_migration_without_a_service_defaults_to_assessment(self):
        new = self.migrate(AFTER)
        Session = new.get_model('assessments', 'AssessmentSession')
        self.assertEqual(Session.objects.create(organization_id=1, name='Later').service, 'assessment')

    def test_the_column_is_indexed(self):
        self.migrate(AFTER)
        with connection.cursor() as cursor:
            indexes = connection.introspection.get_constraints(cursor, TABLE)
        self.assertTrue(any(info['index'] and info['columns'] == ['service'] for info in indexes.values()))

    def test_it_reverses_and_keeps_the_sessions(self):
        new = self.migrate(AFTER)
        new.get_model('assessments', 'AssessmentSession').objects.create(organization_id=1, name='Keep me')
        old = self.migrate(BEFORE)
        self.assertIn('service', self.columns())
        session = old.get_model('assessments', 'AssessmentSession').objects.get()
        self.assertEqual(session.name, 'Keep me')
        self.assertEqual(session.service, 'classroom')
