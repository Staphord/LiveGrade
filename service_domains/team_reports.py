"""Explicit, owner-controlled handoff of private learner competency outcomes."""
import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import TrainingCompetencyResult, TrainingLearnerReportLink, TrainingSeriesSession
from .throttling import allow_unknown_bearer_request


def _owned_link(series_session, organization, user):
    link = (TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
        .select_for_update().select_related('assessment_session', 'series')
        .filter(pk=series_session.pk, assessment_session__service='team').first())
    if link is None:
        raise PermissionDenied
    return link


@transaction.atomic
def release_learner_report(series_session, student_id, *, organization, user):
    link = _owned_link(series_session, organization, user)
    if link.assessment_session.status != 'closed':
        raise ValidationError('Close this demo before releasing learner results.')
    try:
        student = link.assessment_session.students.filter(pk=int(student_id)).first()
    except (TypeError, ValueError):
        student = None
    if student is None:
        raise PermissionDenied
    latest = {}
    for result in (TrainingCompetencyResult.objects.for_organization(organization).owned_by(user)
            .filter(criterion__series_session=link, student=student)
            .select_related('criterion').order_by('criterion_id', '-revision')):
        latest.setdefault(result.criterion_id, result)
    if not latest:
        raise ValidationError('Record competency outcomes for this learner before releasing results.')
    now = timezone.now()
    TrainingLearnerReportLink.objects.filter(series_session=link, student=student,
        revoked_at__isnull=True).update(revoked_at=now)
    token = secrets.token_urlsafe(32)
    TrainingLearnerReportLink.objects.create(series_session=link, student=student,
        organization_id=link.organization_id, created_by=user,
        token_digest=hashlib.sha256(token.encode()).hexdigest(), expires_at=now + timedelta(days=90),
        snapshot={'learner': student.full_name, 'session': link.assessment_session.name,
                  'series': link.series.name, 'released_at': now.isoformat(),
                  'outcomes': [{'criterion': result.criterion.name, 'score': str(result.score),
                                'passing_score': str(result.criterion.passing_score),
                                'passed': result.passed, 'evidence': result.evidence,
                                'revision': result.revision} for result in latest.values()]})
    return token


@transaction.atomic
def revoke_learner_report(series_session, student_id, *, organization, user):
    link = _owned_link(series_session, organization, user)
    try:
        student_id = int(student_id)
    except (TypeError, ValueError):
        raise PermissionDenied
    if not link.assessment_session.students.filter(pk=student_id).exists():
        raise PermissionDenied
    TrainingLearnerReportLink.objects.filter(series_session=link, student_id=student_id,
        revoked_at__isnull=True).update(revoked_at=timezone.now())


@require_safe
@never_cache
def training_learner_report(request, token):
    if getattr(request, 'service', None) != 'team':
        raise Http404
    allowed = allow_unknown_bearer_request(request, service='team-private-results')
    if allowed is not True:
        response = HttpResponse('Please try again shortly.', status=503 if allowed is None else 429)
        response['Cache-Control'] = 'private, no-store'
        response['Retry-After'] = '60'
        return response
    if not token or len(token) > 256:
        raise Http404
    link = TrainingLearnerReportLink.objects.filter(
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        revoked_at__isnull=True, expires_at__gt=timezone.now()).first()
    if link is None:
        raise Http404
    response = render(request, 'service_domains/team_learner_report.html', {'report': link.snapshot})
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response
