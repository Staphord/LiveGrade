"""Join / eligibility / submission logic shared by the student-facing views."""

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    Evaluation, EvaluationScore, Evaluator, ParticipationRecord, PresentationTurn,
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


def submission_key(value):
    if value in (None, ''):
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Invalid submission key.', code='invalid')


def submission_digest(group_scores, individual_scores):
    def entries(scores):
        result = []
        for category, raw in scores.items():
            try:
                value = Decimal(str(raw))
                if not value.is_finite() or value < 0 or value > 100:
                    raise ValueError
                rounded = value.quantize(Decimal('0.01'))
                if value != rounded:
                    raise ValueError
                result.append((int(category), format(abs(rounded) if rounded == 0 else rounded, 'f')))
            except (ValueError, TypeError, InvalidOperation):
                raise ValidationError('Scores must be finite numbers.', code='invalid')
        return sorted(result)
    try:
        payload = {'group': entries(group_scores),
                   'individual': sorted((int(student), entries(scores)) for student, scores in individual_scores.items())}
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Invalid score data.', code='invalid')
    return hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode()).hexdigest()


def _retry(key, digest, turn, evaluator):
    existing = Evaluation.objects.filter(submission_key=key).first() if key else None
    if existing is not None:
        if (existing.presentation_turn_id != turn.pk or existing.evaluator_id != evaluator.pk
                or existing.target_student_id is not None or existing.submission_digest != digest):
            raise ValidationError('This retry does not match the original submission.', code='conflict')
        existing.was_replayed = True
    return existing


@transaction.atomic
def submit_evaluation(turn, evaluator, group_scores, individual_scores_by_student, *, key=None):
    """Atomically store one submission; an exact keyed retry returns its receipt.

    Lock this evaluator, not the room: simultaneous evaluators remain independent.
    Database constraints also protect writes by old clients or other processes.
    Confirm accepted retries before checking a now-paused/expired/closed window.
    """
    key = submission_key(key)
    digest = submission_digest(group_scores, individual_scores_by_student) if key else ''
    evaluator = Student.objects.select_for_update(no_key=True).filter(
        pk=evaluator.pk, assessment_session_id=turn.assessment_session_id).first()
    if evaluator is None:
        raise ValidationError('You cannot evaluate this group.', code='ineligible')
    turn = PresentationTurn.objects.select_related('assessment_session', 'group').get(
        pk=turn.pk, assessment_session_id=evaluator.assessment_session_id)
    replay = _retry(key, digest, turn, evaluator)
    if replay is not None:
        return replay
    if (turn.assessment_session.status == 'closed' or turn.status != PresentationTurn.Status.ACTIVE
            or (turn.voting_ends_at is not None and turn.voting_ends_at <= timezone.now())):
        raise ValidationError("Time ran out before your evaluation was submitted, so it wasn't counted.", code='expired')
    if turn.assessment_session.voting_paused:
        raise ValidationError('Voting is currently paused.', code='paused')
    if not turn.is_voting_open:
        raise ValidationError('Voting has not opened yet.', code='not_open')
    if not eligible_to_evaluate(evaluator, turn):
        raise ValidationError('You cannot evaluate this group.', code='ineligible')
    if already_submitted(evaluator, turn):
        raise ValidationError('You have already submitted an evaluation for this group.', code='already_submitted')
    evaluator_identity, _ = Evaluator.objects.get_or_create(
        assessment_session_id=turn.assessment_session_id,
        student=evaluator,
        defaults={'kind': Evaluator.Kind.ROSTER},
    )
    if evaluator_identity.kind != Evaluator.Kind.ROSTER:
        raise ValidationError('This roster evaluator identity is unavailable.', code='ineligible')
    categories = {c.pk: c for c in turn.assessment_session.rubric_categories.all()}
    try:
        # Savepoint permits looking up the winning receipt after a unique-key race.
        with transaction.atomic():
            group_evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=evaluator,
                evaluator_record=evaluator_identity, target_student=None,
                submission_key=key, submission_digest=digest)
            _write_scores(group_evaluation, group_scores, categories, RubricCategory.Scope.GROUP)
            member_ids = turn.group.member_ids()
            for target_id, scores in individual_scores_by_student.items():
                if int(target_id) not in member_ids:
                    raise ValidationError('Unknown presenter submitted.', code='invalid')
                target = Student.objects.get(pk=target_id, assessment_session_id=evaluator.assessment_session_id)
                individual_evaluation = Evaluation.objects.create(
                    presentation_turn=turn, evaluator=evaluator,
                    evaluator_record=evaluator_identity, target_student=target)
                _write_scores(individual_evaluation, scores, categories, RubricCategory.Scope.INDIVIDUAL)
    except IntegrityError:
        replay = _retry(key, digest, turn, evaluator)
        if replay is not None:
            return replay
        if already_submitted(evaluator, turn):
            raise ValidationError('You have already submitted an evaluation for this group.', code='already_submitted')
        raise
    group_evaluation.was_replayed = False
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
