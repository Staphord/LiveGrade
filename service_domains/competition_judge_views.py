"""Bearer-link scoring workspace for invited Competition judges."""

from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.middleware.csrf import get_token
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .competition_forms import CompetitionJudgeScorecardForm
from .models import CompetitionCriterion
from .operations import (resolve_competition_judge_invitation,
                         submit_competition_judge_scores)
from .throttling import allow_unknown_bearer_request


def allow_competition_judge_request(invitation):
    """Fail closed when the shared limiter is unavailable; keep tokens out of keys."""
    digest = salted_hmac('livegrade-competition-judge-throttle',
        invitation.token_digest).hexdigest()
    key = f'competition-judge:{digest}'
    window = settings.PANEL_EXAMINER_RATE_WINDOW
    try:
        if cache.add(key, 1, timeout=window):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            return cache.add(key, 1, timeout=window)
        return count <= settings.PANEL_EXAMINER_RATE_LIMIT
    except Exception:
        return None


@require_http_methods(['GET', 'POST'])
@never_cache
def competition_judge_scorecard(request, token):
    if getattr(request, 'service', None) != 'competition':
        raise Http404
    invitation = resolve_competition_judge_invitation(token)
    if invitation is None:
        unknown_allowed = allow_unknown_bearer_request(request, service='competition-judge')
        if unknown_allowed is None:
            return HttpResponse('The judge service is temporarily unavailable.', status=503)
        if not unknown_allowed:
            response = HttpResponse('Too many requests for judge links.', status=429)
            response['Retry-After'] = str(settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
            return response
        raise Http404
    allowed = allow_competition_judge_request(invitation)
    if allowed is None:
        return HttpResponse('The judge service is temporarily unavailable.', status=503)
    if not allowed:
        response = HttpResponse('Too many requests for this judge link.', status=429)
        response['Retry-After'] = str(settings.PANEL_EXAMINER_RATE_WINDOW)
        return response

    wants_json = 'application/json' in request.headers.get('Accept', '')
    if request.method == 'GET' and wants_json:
        return JsonResponse({
            'status': 'submitted' if invitation.submitted_at else 'open',
            'csrf_token': get_token(request),
        })

    entries = list(invitation.round.round_entries.filter(track=invitation.track)
        .select_related('entry').order_by('entry__order', 'entry_id'))
    criteria = list(CompetitionCriterion.objects.filter(track=invitation.track).order_by('order', 'pk'))
    form = CompetitionJudgeScorecardForm(
        request.POST if request.method == 'POST' else None, entries=entries, criteria=criteria)
    score_rows = [{'entry': row.entry, 'score_fields': row.score_fields} for row in entries]
    error = ''
    submitted = invitation.submitted_at is not None
    if request.method == 'POST':
        if form.is_valid():
            try:
                submit_competition_judge_scores(token, form.score_payload())
            except PermissionDenied:
                if not wants_json:
                    raise Http404
                response = JsonResponse({
                    'status': 'rejected', 'code': 'invitation_unavailable',
                    'message': 'This invitation can no longer accept a scorecard.',
                }, status=403)
                response['Referrer-Policy'] = 'same-origin'
                return response
            except ValidationError as exc:
                error = '; '.join(exc.messages)
                submitted = False
            else:
                submitted = True
        elif invitation.submitted_at is not None:
            error = 'This scorecard was already submitted; no further changes are accepted.'
            submitted = False

    response = render(request, 'service_domains/competition_judge_scorecard.html', {
        'invitation': invitation, 'score_rows': score_rows, 'form': form,
        'error': error, 'submitted': submitted, 'invitation_token': token,
    }, status=400 if error or (request.method == 'POST' and not form.is_valid()) else 200)
    if wants_json and request.method == 'POST':
        if submitted:
            response = JsonResponse({'status': 'accepted', 'next_url': request.path})
        else:
            message = error or '; '.join(
                message for field_errors in form.errors.values() for message in field_errors)
            response = JsonResponse({
                'status': 'rejected', 'code': 'invalid_scorecard',
                'message': message or 'The scorecard was not accepted.',
            }, status=400)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response
