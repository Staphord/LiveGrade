from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [('service_domains', '0002_allow_anonymous_service_records')]
AFTER = [('service_domains', '0003_showcase_event_public_id')]


class ShowcaseEventPublicIdMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_existing_events_receive_public_ids_and_survive_rollback(self):
        old = self.migrate(BEFORE)
        Event = old.get_model('service_domains', 'ShowcaseEvent')
        row = Event.objects.create(organization_id=987, name='Existing event', status='draft')

        current = self.migrate(AFTER)
        NewEvent = current.get_model('service_domains', 'ShowcaseEvent')
        migrated = NewEvent.objects.get(pk=row.pk)
        self.assertIsNotNone(migrated.public_id)
        self.assertNotEqual(str(migrated.public_id), '')

        rolled_back = self.migrate(BEFORE)
        PreservedEvent = rolled_back.get_model('service_domains', 'ShowcaseEvent')
        preserved = PreservedEvent.objects.get(pk=row.pk)
        self.assertEqual(preserved.name, 'Existing event')
