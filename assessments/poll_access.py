"""Shared budgets for anonymous poll pages, including refresh and submission."""
from functools import wraps
from django.conf import settings
from django.http import HttpResponse
from service_domains.throttling import allow_public_event_request


def public_poll_budget(view):
    @wraps(view)
    def wrapped(request, poll_uuid, *args, **kwargs):
        submitting = request.method == 'POST'
        limit = (settings.POLL_PUBLIC_SUBMISSION_RATE_LIMIT if submitting
                 else settings.POLL_PUBLIC_READ_RATE_LIMIT)
        allowed = allow_public_event_request(request,
            service='poll-submit' if submitting else 'poll-read', event_id=poll_uuid,
            limit=limit, window=60)
        if allowed is None:
            response = HttpResponse('Polls are temporarily unavailable. Please try again.', status=503)
        elif not allowed:
            response = HttpResponse('Too many requests. Please try again shortly.', status=429)
            response['Retry-After'] = '60'
        else:
            return view(request, poll_uuid, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response
    return wrapped
