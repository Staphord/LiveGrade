from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [('service_domains', '0019_trainingseries_feedback_minimum_responses_and_more')]
AFTER = [('service_domains', '0020_competition_public_id_competitionaudienceballot')]


class CompetitionPublicIdMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_existing_competitions_get_distinct_public_ballot_ids(self):
        old = self.migrate(BEFORE)
        Competition = old.get_model('service_domains', 'Competition')
        rows = [Competition.objects.create(organization_id=987, name=f'Existing competition {i}')
                for i in (1, 2)]

        current = self.migrate(AFTER)
        NewCompetition = current.get_model('service_domains', 'Competition')
        identifiers = list(NewCompetition.objects.filter(pk__in=[row.pk for row in rows])
            .values_list('public_id', flat=True))
        self.assertEqual(len(identifiers), 2)
        self.assertTrue(all(identifiers))
        self.assertEqual(len(set(identifiers)), 2)

        rolled_back = self.migrate(BEFORE)
        Preserved = rolled_back.get_model('service_domains', 'Competition')
        self.assertEqual(Preserved.objects.filter(pk__in=[row.pk for row in rows]).count(), 2)
