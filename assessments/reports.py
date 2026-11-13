"""Downloadable, privacy-preserving Classroom result reports."""

import csv
from io import StringIO

from django.db.models import Count

from .models import Evaluation
from .spreadsheet_safety import spreadsheet_safe_text


def _rater_counts(session):
    """Return aggregate counts only; evaluator identities never enter reports."""
    base = Evaluation.objects.filter(
        presentation_turn__assessment_session=session,
        presentation_turn__group__is_practice=False,
    )
    group = {
        row['presentation_turn__group_id']: row['count']
        for row in base.filter(target_student__isnull=True)
        .values('presentation_turn__group_id')
        .annotate(count=Count('evaluator', distinct=True))
    }
    individual = {
        row['target_student_id']: row['count']
        for row in base.filter(target_student__isnull=False)
        .values('target_student_id')
        .annotate(count=Count('evaluator', distinct=True))
    }
    return group, individual


def results_report_data(session, results):
    """Project shared scores into a renderer-neutral, identity-safe report.

    Names and rubric text are plain strings for a renderer to display as text.
    This structure intentionally contains no evaluator identity or raw
    evaluator-to-score rows.
    """
    group_raters, individual_raters = _rater_counts(session)
    criteria = session.rubric_categories.prefetch_related('level_descriptors').all()
    return {
        'session_name': session.name,
        'session_status': session.status,
        'closed_at': session.closed_at,
        'scoring_rules': {
            'peer_contribution_percent': 100,
            'final_score_formula': 'Group points + individual points - participation penalty, floored at 0.',
            'ungraded_penalty_per_other_group': session.ungraded_penalty,
        },
        'criteria': [
            {
                'name': category.name,
                'description': category.description,
                'scope': category.get_scope_display(),
                'weight': category.weight,
                'levels': [
                    {'score': level.score, 'description': level.description}
                    for level in category.level_descriptors.all()
                ],
            }
            for category in criteria
        ],
        'groups': [
            {
                'name': row['group'].name,
                'score': row['percent'],
                'rater_count': group_raters.get(row['group'].pk, 0),
            }
            for row in results['group_rows']
        ],
        'students': [
            {
                'name': row['student'].full_name or row['student'].student_id,
                'group': row['group'].name,
                'group_score': row['group_percent'],
                'individual_score': row['individual_percent'],
                'ungraded_groups': row['ungraded'],
                'penalty': row['penalty'],
                'final_score': row['final_percent'],
                'group_raters': group_raters.get(row['group'].pk, 0),
                'individual_raters': individual_raters.get(row['student'].pk, 0),
            }
            for row in results['student_rows']
        ],
    }


def export_results_csv(session, results):
    """Create one row per roster student, matching the on-screen final marks.

    Text columns are safe to open in spreadsheet applications, while numeric
    marks remain numeric. The count columns disclose only the number of raters,
    never who submitted a score.
    """
    report = results_report_data(session, results)
    output = StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow([
        'Student', 'Group', 'Group points', 'Individual points',
        'Penalty (points)', 'Final score', 'Group raters', 'Individual raters',
    ])
    for row in report['students']:
        writer.writerow([
            spreadsheet_safe_text(row['name']),
            spreadsheet_safe_text(row['group']),
            str(row['group_score']) if row['group_score'] is not None else '',
            str(row['individual_score']) if row['individual_score'] is not None else '',
            str(row['penalty']),
            str(row['final_score']) if row['final_score'] is not None else '',
            row['group_raters'],
            row['individual_raters'],
        ])
    return output.getvalue().encode('utf-8-sig')
