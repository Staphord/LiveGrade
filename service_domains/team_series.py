"""Scoped Team series queries, exports, and deletion operations."""

import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime
from io import BytesIO

from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery, Sum, Value
from django.db.models.deletion import CASCADE
from django.db.models.functions import Coalesce
from django.http import QueryDict
from django.utils import timezone

from assessments.models import AssessmentSession
from assessments.pagination import PAGE_SIZE, PAGE_SIZES, paginate

from .models import (
    TrainingCertificate, TrainingCompetency, TrainingCompetencyResult,
    TrainingFeedback, TrainingLearnerReportLink, TrainingParticipant,
    TrainingSeries, TrainingSeriesSession, TrainingSessionCompetency,
)


DELETE_EXPORT_MAX_AGE = 15 * 60
_DELETE_EXPORT_SALT = 'livegrade.team.series-delete-export.v1'
_SERIES_ID = re.compile(r'[1-9][0-9]*\Z')
_INT64_MAX = '9223372036854775807'
_SECRET_EXPORT_FIELDS = {
    'device_digest', 'feedback_public_id', 'hashed_token', 'password',
    'public_id', 'submission_digest', 'submission_key', 'token',
    'token_digest', 'uuid',
}


class TrainingSeriesUnavailable(Exception):
    """The requested series is outside the signed-in owner's workspace."""


class InvalidSeriesSelection(Exception):
    """The submitted series identifiers do not meet the selection limits."""


def organization_id(organization):
    if hasattr(organization, 'pk'):
        return organization.pk
    if isinstance(organization, dict):
        return organization.get('id')
    return organization


def owned_series(organization, user, *, active_only=True):
    queryset = (TrainingSeries.objects.for_organization(organization)
                .owned_by(user))
    if active_only:
        queryset = queryset.filter(active=True)
    return queryset


def get_owned_series(organization, user, series_id, *, active_only=True):
    series = owned_series(organization, user, active_only=active_only).filter(
        pk=series_id).first()
    if series is None:
        raise TrainingSeriesUnavailable
    return series


def _count_annotation(model):
    return Coalesce(Subquery(
        model.objects.filter(series_id=OuterRef('pk')).order_by()
        .values('series_id').annotate(total=Count('pk')).values('total')[:1]),
        Value(0))


def annotated_series(queryset):
    return queryset.annotate(
        participant_count=_count_annotation(TrainingParticipant),
        competency_count=_count_annotation(TrainingCompetency),
        demo_count=_count_annotation(TrainingSeriesSession),
    )


def dashboard_data(organization, user):
    series = annotated_series(owned_series(organization, user)).order_by(
        '-created_at', '-pk')
    totals = series.aggregate(
        total=Count('pk'),
        participants=Coalesce(Sum('participant_count'), Value(0)),
        competencies=Coalesce(Sum('competency_count'), Value(0)),
        demos=Coalesce(Sum('demo_count'), Value(0)),
    )
    total = totals['total'] or 0
    recent_series = list(series[:5])
    roster_series = list(series.filter(participant_count__gt=0).order_by(
        '-participant_count', '-created_at', '-pk')[:5])
    largest_roster = max((item.participant_count for item in roster_series), default=0)
    for item in roster_series:
        item.roster_bar_percent = round(item.participant_count * 100 / largest_roster, 1) \
            if largest_roster else 0

    stage_distribution = ([{
        'key': 'active', 'label': 'Active', 'count': total, 'percentage': 100.0,
    }] if total else [])
    stage_pie_background = 'conic-gradient(var(--success) 0deg 360deg)' if total else ''
    return {
        'dashboard_stats': {
            'total_series': total,
            'active_series': total,
            'total_participants': totals['participants'] or 0,
            'total_competencies': totals['competencies'] or 0,
            'total_demos': totals['demos'] or 0,
        },
        'recent_series': recent_series,
        'roster_series': roster_series,
        'largest_roster': largest_roster,
        'stage_distribution': stage_distribution,
        'stage_pie_background': stage_pie_background,
    }


def series_list_data(request, organization, user):
    base = owned_series(organization, user)
    search_query = (request.GET.get('q') or '').strip()[:100]
    queryset = annotated_series(base)
    if search_query:
        queryset = queryset.filter(name__icontains=search_query)
    queryset = queryset.order_by('-created_at', '-pk')
    paginator, page_obj = paginate(request, queryset)
    query = QueryDict('', mutable=True)
    if search_query:
        query['q'] = search_query
    query['per_page'] = str(paginator.per_page)
    total = base.count()
    return {
        'series': list(page_obj.object_list),
        'status_counts': {'total': total, 'active': total},
        'search_query': search_query,
        'filtered_count': paginator.count,
        'paginator': paginator,
        'page_obj': page_obj,
        'per_page': paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'querystring': query.urlencode(),
    }


def parse_series_ids(raw_ids):
    if not raw_ids or len(raw_ids) > max(PAGE_SIZES):
        raise InvalidSeriesSelection
    values = set()
    for value in raw_ids:
        if (not _SERIES_ID.fullmatch(value) or len(value) > 19
                or (len(value) == 19 and value > _INT64_MAX)):
            raise InvalidSeriesSelection
        values.add(int(value))
    return sorted(values)


def _series_for_selection(organization, user, series_ids, *, lock=False):
    queryset = owned_series(organization, user, active_only=False).filter(
        pk__in=series_ids)
    if lock:
        queryset = queryset.select_for_update()
    selected = list(queryset.order_by('pk'))
    if len(selected) != len(series_ids):
        raise TrainingSeriesUnavailable
    return selected


def selected_series_names(organization, user, series_ids):
    return _series_for_selection(organization, user, sorted(set(series_ids)))


def _field_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _record(instance):
    values = {'id': instance.pk}
    for field in instance._meta.concrete_fields:
        if field.name in _SECRET_EXPORT_FIELDS:
            continue
        values[field.attname] = _field_value(field.value_from_object(instance))
    return values


def _assessment_cascade_records(sessions):
    frontier = defaultdict(list)
    frontier[AssessmentSession].extend(sessions)
    seen = set()
    exported = []
    while frontier:
        current, frontier = frontier, defaultdict(list)
        for model, objects in current.items():
            new_objects = []
            for instance in objects:
                identity = (model._meta.label_lower, instance.pk)
                if identity in seen:
                    continue
                seen.add(identity)
                new_objects.append(instance)
                exported.append({'model': model._meta.label_lower, 'record': _record(instance)})
            if not new_objects:
                continue
            parent_ids = [instance.pk for instance in new_objects]
            for relation in model._meta.related_objects:
                if relation.on_delete is not CASCADE:
                    continue
                field = relation.field
                if not (field.many_to_one or field.one_to_one):
                    continue
                related_model = relation.related_model
                children = related_model._base_manager.filter(
                    **{f'{field.attname}__in': parent_ids})
                frontier[related_model].extend(children.iterator())
    return exported


@transaction.atomic
def build_deletion_export(organization, user, raw_ids):
    series_ids = parse_series_ids(raw_ids)
    selected = _series_for_selection(organization, user, series_ids, lock=True)
    id_set = [item.pk for item in selected]
    links = list(TrainingSeriesSession.objects.for_organization(organization)
        .owned_by(user).filter(series_id__in=id_set).select_related('assessment_session'))
    sessions = [link.assessment_session for link in links]
    result_queryset = TrainingCompetencyResult.objects.filter(
        criterion__series_session__series_id__in=id_set).select_related(
            'criterion', 'student')
    data = {
        'format': 'LiveGrade Team series deletion export',
        'version': 1,
        'exported_at': timezone.now().isoformat(),
        'series': [_record(item) for item in selected],
        'team_members': [_record(item) for item in TrainingParticipant.objects.filter(
            series_id__in=id_set).order_by('series_id', 'order', 'pk')],
        'competencies': [_record(item) for item in TrainingCompetency.objects.filter(
            series_id__in=id_set).order_by('series_id', 'order', 'pk')],
        'demos': [
            {'series_link': _record(link), 'assessment_session': _record(link.assessment_session)}
            for link in links
        ],
        'learner_reports': [_record(item) for item in TrainingLearnerReportLink.objects.filter(
            series_session__series_id__in=id_set).order_by('pk')],
        'anonymous_feedback': [_record(item) for item in TrainingFeedback.objects.filter(
            series_session__series_id__in=id_set).order_by('submitted_at', 'pk')],
        'demo_competencies': [_record(item) for item in TrainingSessionCompetency.objects.filter(
            series_session__series_id__in=id_set).order_by('series_session_id', 'order', 'pk')],
        'competency_results': [_record(item) for item in result_queryset.order_by(
            'criterion_id', 'student_id', 'revision')],
        'certificates': [_record(item) for item in TrainingCertificate.objects.filter(
            result__criterion__series_session__series_id__in=id_set).order_by('issued_at', 'pk')],
        'linked_assessment_records': _assessment_cascade_records(sessions),
    }
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('team-series-export.json', json.dumps(
            data, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
    token = signing.TimestampSigner(salt=_DELETE_EXPORT_SALT).sign_object({
        'series_ids': id_set,
        'organization_id': organization_id(organization),
        'user_id': user.pk,
    })
    return stream.getvalue(), token, id_set


def verify_deletion_export_token(token, organization, user, series_ids):
    if not token:
        return False
    try:
        payload = signing.TimestampSigner(salt=_DELETE_EXPORT_SALT).unsign_object(
            token, max_age=DELETE_EXPORT_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
        return False
    return (isinstance(payload, dict)
            and payload.get('organization_id') == organization_id(organization)
            and payload.get('user_id') == user.pk
            and payload.get('series_ids') == sorted(set(series_ids)))


@transaction.atomic
def delete_series_selection(organization, user, series_ids):
    selected = _series_for_selection(organization, user, sorted(set(series_ids)), lock=True)
    selected_ids = [item.pk for item in selected]
    linked_session_ids = list(TrainingSeriesSession.objects.for_organization(organization)
        .owned_by(user).filter(series_id__in=selected_ids)
        .values_list('assessment_session_id', flat=True))
    result_ids = TrainingCompetencyResult.objects.filter(
        criterion__series_session__series_id__in=selected_ids).values_list('pk', flat=True)
    TrainingCertificate.objects.filter(result_id__in=result_ids).delete()
    TrainingLearnerReportLink.objects.filter(
        series_session__series_id__in=selected_ids).delete()
    TrainingFeedback.objects.filter(
        series_session__series_id__in=selected_ids).delete_for_series_removal()

    remaining = TrainingCompetencyResult.objects.filter(pk__in=result_ids)
    while remaining.exists():
        leaves = remaining.filter(corrections__isnull=True).distinct()
        if not leaves.exists():
            raise RuntimeError('Training result revisions contain an unresolved dependency.')
        leaves.delete_for_series_removal()

    TrainingSeries.objects.filter(pk__in=selected_ids).delete()
    if linked_session_ids:
        (AssessmentSession.objects.for_organization(organization)
         .owned_by(user).filter(service='team', pk__in=linked_session_ids).delete())
    return len(selected)


def deletion_redirect_query(post_data):
    params = QueryDict('', mutable=True)
    query = (post_data.get('q') or '').strip()[:100]
    if query:
        params['q'] = query
    try:
        per_page = int(post_data.get('per_page', PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE
    params['per_page'] = str(per_page)
    return params.urlencode()
