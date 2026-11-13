from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [('service_domains', '0044_alter_panelauditevent_action')]
AFTER = [('service_domains', '0045_competition_public_leaderboard_enabled_and_more')]


class CompetitionDisplayMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_only_previously_revealed_competitions_keep_public_display_enabled(self):
        old = self.migrate(BEFORE)
        Competition = old.get_model('service_domains', 'Competition')
        revealed = Competition.objects.create(organization_id=987, name='Revealed',
            status='revealed', revealed_at='2026-10-27T10:00:00Z')
        complete = Competition.objects.create(organization_id=987, name='Complete',
            status='complete')

        current = self.migrate(AFTER)
        NewCompetition = current.get_model('service_domains', 'Competition')
        self.assertTrue(NewCompetition.objects.get(pk=revealed.pk).public_leaderboard_enabled)
        self.assertFalse(NewCompetition.objects.get(pk=complete.pk).public_leaderboard_enabled)
