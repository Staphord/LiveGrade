"""Join / eligibility / submission logic shared by the student-facing views."""

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    Evaluation, EvaluationScore, ParticipationRecord, PresentationTurn,
    RubricCategory, Student,
)

SESSION_COOKIE_SALT = 'assessments.student-session'
STUDENT_SESSION_KEY = 'assessment_student_ids'


def match_student(assessment_session, typed_value):
    """Find the roster row matching what the student typed, or None.

    Matches case-/whitespace-insensitively against whichever field the
    session identifies by, per the lecturer's choice.
    """
    typed_value = (typed_value or '').strip()
    if not typed_value:
        return None
    field = 'student_id' if assessment_session.identify_by == 'student_id' else 'full_name'
    return assessment_session.students.filter(
        **{f'{field}__iexact': typed_value}).first()


def record_join(request, student):
    """Mark participation and remember this student in the session (cookie)."""
    ParticipationRecord.objects.get_or_create(
        assessment_session=student.assessment_session, student=student)
    key = STUDENT_SESSION_KEY
    ids = request.session.get(key, [])
    if student.pk not in ids:
        ids.append(student.pk)
    request.session[key] = ids


def student_from_request(request, assessment_session):
    """The Student this browser joined as for this session, or None."""
    ids = request.session.get(STUDENT_SESSION_KEY, [])
    if not ids:
        return None
    return assessment_session.students.filter(pk__in=ids).first()


def eligible_to_evaluate(student, turn):
    """A student may evaluate a turn unless they're a member of the presenting group."""
    if turn is None or not turn.is_voting_open:
        return False
    return student.pk not in turn.group.member_ids()


def already_submitted(student, turn):
    return Evaluation.objects.filter(presentation_turn=turn, evaluator=student).exists()


@transaction.atomic
def submit_evaluation(turn, evaluator, group_scores, individual_scores_by_student):
    """Record one full submission for a turn.

    group_scores: {rubric_category_id: value}
    individual_scores_by_student: {student_id: {rubric_category_id: value}}
    Raises ValidationError on any bound violation or duplicate — nothing is
    partially written.
    """
    if turn.assessment_session.voting_paused:
        raise ValidationError('Voting is currently paused.')
    if not eligible_to_evaluate(evaluator, turn):
        raise ValidationError('You cannot evaluate this group.')
    if already_submitted(evaluator, turn):
        raise ValidationError('You have already submitted an evaluation for this group.')

    categories = {c.pk: c for c in turn.assessment_session.rubric_categories.all()}

    group_evaluation = Evaluation.objects.create(
        presentation_turn=turn, evaluator=evaluator, target_student=None)
    _write_scores(group_evaluation, group_scores, categories, RubricCategory.Scope.GROUP)

    for target_id, scores in individual_scores_by_student.items():
        target = Student.objects.get(pk=target_id)
        individual_evaluation = Evaluation.objects.create(
            presentation_turn=turn, evaluator=evaluator, target_student=target)
        _write_scores(individual_evaluation, scores, categories,
                      RubricCategory.Scope.INDIVIDUAL)

    return group_evaluation


def _write_scores(evaluation, scores, categories, expected_scope):
    for category_id, value in scores.items():
        category = categories.get(int(category_id))
        if category is None or category.scope != expected_scope:
            raise ValidationError('Unknown rubric category submitted.')
        score = EvaluationScore(evaluation=evaluation, rubric_category=category, value=value,
                                max_value=category.weight)
        score.full_clean()
        score.save()
