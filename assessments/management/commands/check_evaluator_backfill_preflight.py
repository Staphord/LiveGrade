"""Check legacy evaluation relationships before the evaluator identity migration."""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from assessments.models import Evaluation


class Command(BaseCommand):
    help = (
        'Read-only preflight for the evaluator identity backfill. Checks that '
        'each existing evaluation rater belongs to the session scored.'
    )

    def handle(self, *args, **options):
        mismatched = (Evaluation.objects
            .exclude(presentation_turn__assessment_session_id=F(
                'evaluator__assessment_session_id'))
            .order_by('pk'))
        count = mismatched.count()
        self.stdout.write(f'evaluations_with_cross_session_rater={count}')
        if count:
            sample = list(mismatched.values_list('pk', flat=True)[:20])
            self.stdout.write(f'example_evaluation_ids={sample}')
            raise CommandError(
                'Evaluator identity backfill is unsafe until these legacy '
                'relationships are investigated and repaired without deleting scores.')
        self.stdout.write(self.style.SUCCESS(
            'Evaluator backfill preflight passed. No data was changed.'))
