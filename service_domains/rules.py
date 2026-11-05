"""Deterministic calculations shared by the planned service workflows."""

from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError


def _decimal_scores(values):
    try:
        scores = [Decimal(value) for value in values]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('Scores must be valid decimal numbers.') from exc
    if not scores:
        raise ValidationError('At least one score is required.')
    if any(not score.is_finite() or score < 0 or score > 100 for score in scores):
        raise ValidationError('Scores must be finite values from 0 to 100.')
    return scores


def aggregate_scores(values, method='mean', trim_fraction=None):
    """Return an exact Decimal mean, median, or symmetric trimmed mean."""
    scores = sorted(_decimal_scores(values))
    if method == 'mean':
        return sum(scores, Decimal('0')) / Decimal(len(scores))
    if method == 'median':
        middle = len(scores) // 2
        if len(scores) % 2:
            return scores[middle]
        return (scores[middle - 1] + scores[middle]) / Decimal('2')
    if method == 'trimmed_mean':
        if trim_fraction is None:
            raise ValidationError('Trimmed mean requires an explicitly configured trim fraction.')
        try:
            fraction = Decimal(trim_fraction)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError('Trim fraction must be a decimal from 0 to less than 0.5.') from exc
        if not fraction.is_finite() or fraction < 0 or fraction >= Decimal('0.5'):
            raise ValidationError('Trim fraction must be a decimal from 0 to less than 0.5.')
        count = int(Decimal(len(scores)) * fraction)
        retained = scores[count:len(scores) - count] if count else scores
        return sum(retained, Decimal('0')) / Decimal(len(retained))
    raise ValidationError('Unknown aggregation method.')


def showcase_judge_score(criteria, raw_scores):
    """Normalize configured Showcase scales and apply their percentage weights."""
    if not criteria or not isinstance(raw_scores, dict):
        raise ValidationError('Judge criteria and scores are required.')
    expected = {str(item.pk) for item in criteria}
    if set(raw_scores) != expected:
        raise ValidationError('Score every configured criterion exactly once.')
    if sum((Decimal(item.weight) for item in criteria), Decimal('0')) != Decimal('100'):
        raise ValidationError('Judge criterion weights must total exactly 100.')

    total = Decimal('0')
    for criterion in criteria:
        value = raw_scores[str(criterion.pk)]
        if criterion.scale == 'points':
            if isinstance(value, bool):
                raise ValidationError('Point scores must be valid numbers.')
            try:
                points = Decimal(str(value))
                maximum = Decimal(criterion.max_points)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError('Point scores must be valid numbers.') from exc
            if (not points.is_finite() or points < 0 or not maximum.is_finite()
                    or maximum <= 0 or points > maximum):
                raise ValidationError('Point scores must be between zero and the criterion maximum.')
            fraction = points / maximum
        elif criterion.scale == 'stars':
            if isinstance(value, bool) or not str(value).isdigit():
                raise ValidationError('Star scores must be whole numbers from 1 to 5.')
            stars = int(value)
            if stars < 1 or stars > 5:
                raise ValidationError('Star scores must be whole numbers from 1 to 5.')
            fraction = Decimal(stars) / Decimal('5')
        elif criterion.scale == 'pass_fail':
            if value not in (True, False, 'pass', 'fail'):
                raise ValidationError('Pass/fail scores must be pass or fail.')
            fraction = Decimal('1') if value in (True, 'pass') else Decimal('0')
        elif criterion.scale == 'checklist':
            items = criterion.checklist_items
            if not isinstance(items, list) or not items:
                raise ValidationError('Checklist criteria need at least one configured item.')
            if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
                raise ValidationError('Checklist scores must identify completed items.')
            selected = set()
            for item in value:
                if isinstance(item, bool):
                    raise ValidationError('Checklist selections must be valid item indexes.')
                try:
                    index = int(item)
                except (TypeError, ValueError) as exc:
                    raise ValidationError('Checklist selections must be valid item indexes.') from exc
                if str(index) != str(item) or not 0 <= index < len(items):
                    raise ValidationError('Checklist selections must reference configured items.')
                selected.add(index)
            fraction = Decimal(len(selected)) / Decimal(len(items))
        else:
            raise ValidationError('Choose a supported Showcase judge scale.')
        total += fraction * Decimal(criterion.weight)
    return total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def ranked_choice_result(ballots, candidate_ids, tie_break_order=()):
    """Instant-runoff tally with explicit deterministic tie-breaking.

    Each ballot is an ordered iterable of candidate IDs. Exhausted ballots are
    ignored in later rounds. A complete tie is resolved using the supplied
    published order; without one it is returned as a tie for human decision.
    """
    candidates = list(dict.fromkeys(candidate_ids))
    if len(candidates) < 2:
        raise ValidationError('Ranked choice requires at least two candidates.')
    allowed = set(candidates)
    normalized = []
    for ballot in ballots:
        ranking = list(ballot)
        if len(ranking) != len(set(ranking)) or any(item not in allowed for item in ranking):
            raise ValidationError('Each ballot must rank known candidates at most once.')
        normalized.append(ranking)
    tie_order = list(dict.fromkeys(tie_break_order))
    if any(item not in allowed for item in tie_order):
        raise ValidationError('Tie-break order contains an unknown candidate.')
    rounds = []
    remaining = set(candidates)
    while True:
        tally = Counter(next((item for item in ballot if item in remaining), None)
                        for ballot in normalized)
        tally.pop(None, None)
        rounds.append({candidate: tally.get(candidate, 0) for candidate in candidates if candidate in remaining})
        if len(remaining) == 1:
            return {'winner': next(iter(remaining)), 'tied': [], 'rounds': rounds, 'resolved_by': 'majority_or_last_remaining'}
        total = sum(tally.values())
        winner, count = max(tally.items(), key=lambda pair: pair[1], default=(None, 0))
        if winner is not None and count * 2 > total:
            return {'winner': winner, 'tied': [], 'rounds': rounds, 'resolved_by': 'majority'}
        lowest = min((tally.get(candidate, 0) for candidate in remaining), default=0)
        eliminate = [candidate for candidate in remaining if tally.get(candidate, 0) == lowest]
        if len(eliminate) > 1:
            ordered = [candidate for candidate in tie_order if candidate in eliminate]
            if ordered:
                eliminate = [ordered[-1]]
            elif len(eliminate) == len(remaining):
                return {'winner': None, 'tied': eliminate, 'rounds': rounds, 'resolved_by': 'head_judge_required'}
            else:
                # Do not make a hidden arbitrary choice when lower candidates tie.
                return {'winner': None, 'tied': eliminate, 'rounds': rounds, 'resolved_by': 'head_judge_required'}
        remaining.remove(eliminate[0])
def advance_entries(scores, rule, value, *, tie_break_order=(), head_judge_decision=None):
    """Return entries in deterministic score order for top-N or threshold rules."""
    normalized = {}
    for entry, score in scores.items():
        try:
            number = Decimal(score)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError('Entry scores must be valid decimals.') from exc
        if not number.is_finite():
            raise ValidationError('Entry scores must be finite.')
        normalized[entry] = number
    if rule == 'top_n':
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError('Top-N advancement needs a positive whole number.') from exc
        if count < 1 or Decimal(str(value)) != count:
            raise ValidationError('Top-N advancement needs a positive whole number.')
        ordered = sorted(normalized.items(), key=lambda pair: (-pair[1], str(pair[0])))
        if count >= len(ordered):
            return [entry for entry, _ in ordered]
        cutoff = ordered[count - 1][1]
        certain = [entry for entry, score in ordered if score > cutoff]
        tied = [entry for entry, score in ordered if score == cutoff]
        slots = count - len(certain)
        if len(tied) <= slots:
            return certain + tied
        if head_judge_decision is not None:
            selected = list(head_judge_decision)
            if len(selected) != slots or len(set(selected)) != len(selected) or any(item not in tied for item in selected):
                raise ValidationError('Head-judge advancement decision must select exactly the tied places.')
            return certain + selected
        if tie_break_order:
            ordered_ties = [item for item in tie_break_order if item in tied]
            if len(set(ordered_ties)) != len(tied):
                raise ValidationError('Configured tie-break order must cover every tied entry exactly once.')
            return certain + ordered_ties[:slots]
        raise ValidationError('A top-N tie at the advancement boundary requires an approved policy or head-judge decision.')
    if rule == 'threshold':
        try:
            threshold = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError('Threshold advancement needs a valid decimal.') from exc
        if not threshold.is_finite():
            raise ValidationError('Threshold advancement needs a finite decimal.')
        return [entry for entry, score in sorted(normalized.items(), key=lambda pair: (-pair[1], str(pair[0])))
                if score >= threshold]
    raise ValidationError('Unknown advancement rule.')


def training_trend(session_results, learner_key):
    """Return the learner's source marks across three or more distinct sessions."""
    rows = []
    seen = set()
    for session_key, learner_scores in session_results:
        if session_key in seen:
            raise ValidationError('A training series cannot count the same session twice.')
        seen.add(session_key)
        if learner_key not in learner_scores:
            raise ValidationError('Every source session must contain this learner result.')
        scores = _decimal_scores([learner_scores[learner_key]])
        rows.append((session_key, scores[0]))
    if len(rows) < 3:
        raise ValidationError('At least three source sessions are required for a trend.')
    return {
        'sessions': rows,
        'first_score': rows[0][1],
        'latest_score': rows[-1][1],
        'change': rows[-1][1] - rows[0][1],
    }


def aggregate_anonymous_feedback(values, *, minimum_responses):
    """Return aggregate-only feedback after the caller's approved privacy floor."""
    try:
        floor = int(minimum_responses)
    except (TypeError, ValueError) as exc:
        raise ValidationError('A positive privacy response floor is required.') from exc
    if floor < 2:
        raise ValidationError('The privacy response floor must be at least two.')
    scores = _decimal_scores(values)
    if len(scores) < floor:
        return None
    return {
        'response_count': len(scores),
        'mean': aggregate_scores(scores, 'mean'),
        'median': aggregate_scores(scores, 'median'),
    }
