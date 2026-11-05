"""Owner-only, channel-separated monitoring projections for an open Showcase."""

from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Avg, Count
from django.utils import timezone

from .models import ShowcaseJudgeEvaluation, ShowcaseJudgeExhibitAssignment, ShowcaseRating


def showcase_live_activity(event):
    """Return visitor and judge activity separately; never infer a combined award."""
    exhibits = list(event.exhibits.select_related('category').order_by(
        'category__order', 'category__name', 'order', 'pk'))
    exhibit_ids = [item.pk for item in exhibits]
    ratings = {
        row['exhibit_id']: row for row in ShowcaseRating.objects.filter(
            event=event, exhibit_id__in=exhibit_ids)
        .values('exhibit_id').annotate(count=Count('pk'), mean=Avg('stars'))
    }
    assignments = {
        row['exhibit_id']: row for row in ShowcaseJudgeExhibitAssignment.objects.filter(
            event=event, exhibit_id__in=exhibit_ids, active=True,
        ).exclude(conflict_declaration__isnull=False)
        .values('exhibit_id').annotate(assigned=Count('pk'), submitted=Count(
            'evaluation', distinct=True))
    }
    scores = {
        row['assignment__exhibit_id']: row for row in ShowcaseJudgeEvaluation.objects.filter(
            assignment__event=event)
        .values('assignment__exhibit_id').annotate(count=Count('pk'), mean=Avg('score'))
    }

    rows = []
    for exhibit in exhibits:
        audience = ratings.get(exhibit.pk, {})
        judges = scores.get(exhibit.pk, {})
        coverage = assignments.get(exhibit.pk, {})
        audience_mean = audience.get('mean')
        judge_mean = judges.get('mean')
        if audience_mean is not None:
            audience_mean = Decimal(str(audience_mean)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
        if judge_mean is not None:
            judge_mean = Decimal(str(judge_mean)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
        rows.append({
            'exhibit': exhibit.name,
            'category': exhibit.category.name if exhibit.category else '',
            'audience_count': audience.get('count', 0),
            'audience_mean': str(audience_mean) if audience_mean is not None else None,
            'judge_assigned_count': coverage.get('assigned', 0),
            'judge_submitted_count': judges.get('count', 0),
            'judge_mean': str(judge_mean) if judge_mean is not None else None,
        })
    return {
        'event_name': event.name,
        'event_status': event.get_status_display(),
        'updated_at': timezone.now().isoformat(),
        'rows': rows,
    }
