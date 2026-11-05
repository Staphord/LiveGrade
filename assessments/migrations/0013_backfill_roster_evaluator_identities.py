"""Backfill roster identities and keep legacy and expanded references aligned."""

from collections import defaultdict

from django.db import migrations
from django.db.models import F


BATCH_SIZE = 1000


def backfill_roster_evaluators(apps, schema_editor):
    Student = apps.get_model('assessments', 'Student')
    Evaluator = apps.get_model('assessments', 'Evaluator')
    Evaluation = apps.get_model('assessments', 'Evaluation')
    database = schema_editor.connection.alias

    # The legacy schema does not enforce that a rater belongs to the same
    # session as the turn being scored. Detect corrupt/cross-session rows
    # before inserting identities so the migration fails atomically with a
    # repair path instead of raising a KeyError partway through the backfill.
    mismatched = Evaluation.objects.using(database).exclude(
        presentation_turn__assessment_session_id=F(
            'evaluator__assessment_session_id'))
    mismatch_count = mismatched.count()
    if mismatch_count:
        raise RuntimeError(
            f'Cannot backfill evaluator identities: {mismatch_count} legacy '
            'evaluation row(s) reference a rater from a different session. '
            'Run `manage.py check_evaluator_backfill_preflight`, repair the '
            'reported rows without deleting scores, and retry this migration.')

    # Every roster participant may act as an evaluator, including those who
    # have not yet submitted. Create bounded batches to keep migration memory
    # usage predictable on larger existing rosters.
    sessions = Student.objects.using(database).order_by().values_list(
        'assessment_session_id', flat=True).distinct().iterator(chunk_size=BATCH_SIZE)
    for session_id in sessions:
        students = Student.objects.using(database).filter(
            assessment_session_id=session_id).order_by('pk').iterator(chunk_size=BATCH_SIZE)
        while True:
            batch = []
            for student in students:
                batch.append(Evaluator(assessment_session_id=session_id,
                    student_id=student.pk, kind='roster'))
                if len(batch) == BATCH_SIZE:
                    Evaluator.objects.using(database).bulk_create(batch, batch_size=BATCH_SIZE)
                    batch = []
            if batch:
                Evaluator.objects.using(database).bulk_create(batch, batch_size=BATCH_SIZE)
            break

    # Update Evaluation rows in bounded batches. All existing Classroom
    # evaluations are roster evaluations and retain their legacy Student FK.
    evaluations = Evaluation.objects.using(database).select_related(
        'presentation_turn').order_by('pk').iterator(chunk_size=BATCH_SIZE)
    while True:
        batch = []
        for evaluation in evaluations:
            batch.append(evaluation)
            if len(batch) == BATCH_SIZE:
                _attach_identity_batch(batch, Evaluator, Evaluation, database)
                batch = []
        if batch:
            _attach_identity_batch(batch, Evaluator, Evaluation, database)
        break


def _attach_identity_batch(rows, Evaluator, Evaluation, database):
    by_session = defaultdict(set)
    for row in rows:
        by_session[row.presentation_turn.assessment_session_id].add(row.evaluator_id)
    identity_ids = {}
    for session_id, student_ids in by_session.items():
        identity_ids.update({(session_id, student_id): identity_id
            for student_id, identity_id in Evaluator.objects.using(database).filter(
                assessment_session_id=session_id, student_id__in=student_ids
            ).values_list('student_id', 'pk')})
    for row in rows:
        row.evaluator_record_id = identity_ids[(
            row.presentation_turn.assessment_session_id, row.evaluator_id)]
    Evaluation.objects.using(database).bulk_update(rows, ['evaluator_record'], batch_size=BATCH_SIZE)


def reverse_backfill(apps, schema_editor):
    Evaluator = apps.get_model('assessments', 'Evaluator')
    Evaluation = apps.get_model('assessments', 'Evaluation')
    database = schema_editor.connection.alias
    Evaluation.objects.using(database).filter(evaluator_record__isnull=False).update(
        evaluator_record=None)
    Evaluator.objects.using(database).filter(kind='roster').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('assessments', '0012_evaluator_evaluation_evaluator_record_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_roster_evaluators, reverse_backfill),
    ]
