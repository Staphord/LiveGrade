"""Verify Classroom's additive evaluator identity backfill before a read switch."""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q

from assessments.models import AssessmentSession, Evaluation, Evaluator, Student


class Command(BaseCommand):
    help = (
        'Read-only check that each Classroom roster/evaluation has a matching '
        'expanded evaluator identity. Does not switch application reads.'
    )

    def handle(self, *args, **options):
        sessions = AssessmentSession.objects.filter(service='classroom')
        students = Student.objects.filter(assessment_session__in=sessions)
        evaluators = Evaluator.objects.filter(assessment_session__in=sessions)
        evaluations = Evaluation.objects.filter(
            presentation_turn__assessment_session__in=sessions)

        counts = {
            'classroom_sessions': sessions.count(),
            'classroom_students': students.count(),
            'roster_evaluator_identities': evaluators.count(),
            'classroom_evaluations': evaluations.count(),
            'students_missing_identity': students.filter(
                evaluator_identities__isnull=True).count(),
            'invalid_roster_identities': evaluators.filter(
                ~Q(kind='roster') | Q(student__isnull=True)
                | ~Q(student__assessment_session_id=F('assessment_session_id'))
            ).count(),
            'evaluations_missing_or_mismatched_identity': evaluations.filter(
                Q(evaluator_record__isnull=True)
                | ~Q(evaluator_record__kind='roster')
                | ~Q(evaluator_record__assessment_session_id=F(
                    'presentation_turn__assessment_session_id'))
                | ~Q(evaluator_record__student_id=F('evaluator_id'))
            ).count(),
        }
        for name, count in counts.items():
            self.stdout.write(f'{name}={count}')

        problems = [name for name in (
            'students_missing_identity', 'invalid_roster_identities',
            'evaluations_missing_or_mismatched_identity') if counts[name]]
        if problems:
            raise CommandError('Evaluator identity parity failed: ' + ', '.join(problems))
        self.stdout.write(self.style.SUCCESS(
            'Evaluator identity parity passed. This is evidence for review, '
            'not authorization to switch reads.'
        ))
