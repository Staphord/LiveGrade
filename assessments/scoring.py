"""Result calculation — the only code allowed to read EvaluationScore for
lecturer-facing output. Every query here aggregates; none joins an
evaluator identity to a raw score in the same row (see models.py docstring).
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count, Sum

from .models import EvaluationScore, RubricCategory


def fraction_averages(score_qs, *keys):
    """``{key tuple: average of value / max_value}`` over ``score_qs``, in Decimal.

    A score is judged against the weight it was given out of (``max_value``), so a
    weight edited later rescales past scores instead of breaking them. The database
    only sums and counts, grouped by ``max_value`` too (one bucket unless the weight
    was edited mid-session); the division is exact Decimal arithmetic here, because
    dividing two decimal columns in SQL is integer division on SQLite.
    """
    totals = {}
    for row in (score_qs.filter(max_value__gt=0)
                .values(*keys, 'max_value')
                .annotate(total=Sum('value'), n=Count('pk'))):
        key = tuple(row[k] for k in keys)
        fraction_sum, count = totals.get(key, (Decimal('0'), 0))
        totals[key] = (fraction_sum + row['total'] / row['max_value'], count + row['n'])
    return {key: fraction_sum / count for key, (fraction_sum, count) in totals.items()}


def _category_percent(evaluation_qs, category):
    """Average of a category's scores, as a percent of full marks."""
    scores = EvaluationScore.objects.filter(evaluation__in=evaluation_qs, rubric_category=category)
    avg = fraction_averages(scores, 'rubric_category_id').get((category.pk,))
    return None if avg is None else avg * 100


def group_score_percent(group, assessment_session):
    """This group's score in points - each category's `weight` is its real share
    of the 100-point final score (see `RubricCategory.weight`), so this sums
    straight to a value in [0, the group categories' weight total] with no
    further renormalizing needed at result time."""
    from .models import Evaluation

    categories = list(assessment_session.rubric_categories.filter(
        scope=RubricCategory.Scope.GROUP))
    if not categories:
        return None
    evaluations = Evaluation.objects.filter(
        presentation_turn__group=group,
        presentation_turn__assessment_session=assessment_session,
        target_student__isnull=True)
    weighted_sum = Decimal('0')
    any_scored = False
    for category in categories:
        pct = _category_percent(evaluations, category)
        if pct is None:
            continue
        any_scored = True
        weighted_sum += pct * category.weight
    if not any_scored:
        return None
    return (weighted_sum / 100).quantize(Decimal('0.1'), ROUND_HALF_UP)


def individual_score_percent(student, assessment_session):
    """This student's score in points - see `group_score_percent` above for why
    no renormalization against the categories' own weight total is needed."""
    from .models import Evaluation

    categories = list(assessment_session.rubric_categories.filter(
        scope=RubricCategory.Scope.INDIVIDUAL))
    if not categories:
        return None
    evaluations = Evaluation.objects.filter(
        target_student=student,
        presentation_turn__assessment_session=assessment_session)
    weighted_sum = Decimal('0')
    any_scored = False
    for category in categories:
        pct = _category_percent(evaluations, category)
        if pct is None:
            continue
        any_scored = True
        weighted_sum += pct * category.weight
    if not any_scored:
        return None
    return (weighted_sum / 100).quantize(Decimal('0.1'), ROUND_HALF_UP)


def ungraded_group_count(student, group, assessment_session):
    """How many *other* groups this student never submitted a group-scope
    evaluation for — the basis for the per-skipped-group deduction below.
    A student's own group is excluded since they're never eligible to grade
    it in the first place (see services.eligible_to_evaluate)."""
    from .models import Evaluation

    other_group_ids = set(assessment_session.groups.exclude(
        pk=group.pk if group else None).values_list('pk', flat=True))
    if not other_group_ids:
        return 0
    voted_group_ids = set(Evaluation.objects.filter(
        presentation_turn__assessment_session=assessment_session,
        evaluator=student, target_student__isnull=True,
    ).values_list('presentation_turn__group_id', flat=True))
    return len(other_group_ids - voted_group_ids)


def final_score_percent(student, group, assessment_session):
    """Combined score, minus the session's `ungraded_penalty` for every other
    group this student didn't grade. `group_score_percent` and
    `individual_score_percent` are already in points of the 100-point final
    score (each category's weight is its real share of it), so there's
    nothing left to recombine - just add them."""
    group_pct = group_score_percent(group, assessment_session) or Decimal('0')
    individual_pct = individual_score_percent(student, assessment_session) or Decimal('0')
    combined = group_pct + individual_pct
    penalty = ungraded_group_count(student, group, assessment_session) * assessment_session.ungraded_penalty
    final = combined - penalty
    if final < 0:
        final = Decimal('0')
    return final.quantize(Decimal('0.1'), ROUND_HALF_UP)


def _weighted_percent(categories, avg_by_category):
    """The same sum `group_score_percent` / `individual_score_percent` build,
    from category averages that were already fetched (in the categories' own
    order, so the Decimal arithmetic is identical)."""
    weighted_sum = Decimal('0')
    any_scored = False
    for category in categories:
        avg = avg_by_category.get(category.pk)
        if avg is None:
            continue
        any_scored = True
        weighted_sum += avg * 100 * category.weight
    if not any_scored:
        return None
    return (weighted_sum / 100).quantize(Decimal('0.1'), ROUND_HALF_UP)


def group_score_percents(assessment_session, groups):
    """``{group.pk: group_score_percent(group)}`` for every group given, from one
    grouped query instead of one per group and category."""
    group_cats = list(assessment_session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP))
    if not group_cats:
        return {group.pk: None for group in groups}
    avgs = {}
    for (group_id, category_id), avg in fraction_averages(
            EvaluationScore.objects.filter(
                evaluation__presentation_turn__assessment_session=assessment_session,
                evaluation__target_student__isnull=True, rubric_category__in=group_cats),
            'evaluation__presentation_turn__group_id', 'rubric_category_id').items():
        avgs.setdefault(group_id, {})[category_id] = avg
    return {group.pk: _weighted_percent(group_cats, avgs.get(group.pk, {})) for group in groups}


def group_vote_counts(assessment_session):
    """``{group_id: distinct students who submitted a group-scope evaluation}``."""
    from .models import Evaluation

    return {row['presentation_turn__group_id']: row['n'] for row in Evaluation.objects.filter(
        presentation_turn__assessment_session=assessment_session, target_student__isnull=True
    ).values('presentation_turn__group_id').annotate(n=Count('evaluator', distinct=True))}


def turn_vote_counts(turn_ids):
    """``{turn_id: distinct students who submitted a group-scope evaluation}``."""
    from .models import Evaluation

    return {row['presentation_turn_id']: row['n'] for row in Evaluation.objects.filter(
        presentation_turn_id__in=turn_ids, target_student__isnull=True
    ).values('presentation_turn_id').annotate(n=Count('evaluator', distinct=True))}


def session_results(assessment_session):
    """Full results table: one row per student, plus per-group summary.

    Same numbers as calling `group_score_percent`, `individual_score_percent`,
    `ungraded_group_count` and `final_score_percent` one student at a time,
    but read in a handful of grouped queries instead of hundreds."""
    from .models import Evaluation

    groups = list(assessment_session.groups.prefetch_related('memberships__student'))
    individual_cats = list(assessment_session.rubric_categories.filter(
        scope=RubricCategory.Scope.INDIVIDUAL))

    group_percents = group_score_percents(assessment_session, groups)
    individual_avgs = {}
    if individual_cats:
        for (student_id, category_id), avg in fraction_averages(
                EvaluationScore.objects.filter(
                    evaluation__presentation_turn__assessment_session=assessment_session,
                    evaluation__target_student__isnull=False, rubric_category__in=individual_cats),
                'evaluation__target_student_id', 'rubric_category_id').items():
            individual_avgs.setdefault(student_id, {})[category_id] = avg

    all_group_ids = {group.pk for group in groups}
    voted_by_student = {}
    for evaluator_id, group_id in Evaluation.objects.filter(
            presentation_turn__assessment_session=assessment_session, target_student__isnull=True,
    ).values_list('evaluator_id', 'presentation_turn__group_id'):
        voted_by_student.setdefault(evaluator_id, set()).add(group_id)

    per_group_penalty = assessment_session.ungraded_penalty
    group_rows = []
    student_rows = []
    for group in groups:
        g_pct = group_percents[group.pk]
        group_rows.append({'group': group, 'percent': g_pct})
        other_group_ids = all_group_ids - {group.pk}
        for membership in group.memberships.all():
            student = membership.student
            i_pct = (_weighted_percent(individual_cats, individual_avgs.get(student.pk, {}))
                     if individual_cats else None)
            ungraded = (len(other_group_ids - voted_by_student.get(student.pk, set()))
                        if other_group_ids else 0)
            penalty = ungraded * per_group_penalty
            final = (g_pct or Decimal('0')) + (i_pct or Decimal('0')) - penalty
            if final < 0:
                final = Decimal('0')
            student_rows.append({
                'student': student, 'group': group,
                'individual_percent': i_pct, 'group_percent': g_pct,
                'ungraded': ungraded, 'penalty': penalty, 'final_percent': final.quantize(Decimal('0.1'), ROUND_HALF_UP),
            })
    return {'group_rows': group_rows, 'student_rows': student_rows}
