from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [('service_domains', '0018_showcasecertificate')]
AFTER = [('service_domains', '0019_trainingseries_feedback_minimum_responses_and_more')]


class TrainingFeedbackPublicIdMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        # Assessment migrations are independent and remain at their current leaf.
        # Include that leaf when rendering historical service-domain models.
        target = [*target, *executor.loader.graph.leaf_nodes('assessments')]
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_existing_team_sessions_get_distinct_public_feedback_ids(self):
        old = self.migrate(BEFORE)
        Series = old.get_model('service_domains', 'TrainingSeries')
        SeriesSession = old.get_model('service_domains', 'TrainingSeriesSession')
        AssessmentSession = old.get_model('assessments', 'AssessmentSession')
        series = Series.objects.create(organization_id=9234, name='Existing series')
        links = []
        for sequence in (1, 2):
            session = AssessmentSession.objects.create(organization_id=9234,
                service='team', name=f'Existing demo {sequence}')
            links.append(SeriesSession.objects.create(organization_id=9234,
                series=series, assessment_session=session, sequence=sequence))

        current = self.migrate(AFTER)
        NewSeriesSession = current.get_model('service_domains', 'TrainingSeriesSession')
        ids = list(NewSeriesSession.objects.filter(pk__in=[row.pk for row in links])
            .values_list('feedback_public_id', flat=True))
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), 2)

        rolled_back = self.migrate(BEFORE)
        Preserved = rolled_back.get_model('service_domains', 'TrainingSeriesSession')
        self.assertEqual(Preserved.objects.filter(series_id=series.pk).count(), 2)
