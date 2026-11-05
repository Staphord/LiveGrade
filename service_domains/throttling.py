"""Short-lived abuse limits for authenticated-by-link Panel actions."""

import ipaddress

from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac


def allow_panel_examiner_request(invitation):
    """Return True when the invitation is within budget, None if cache fails.

    Only an already-resolved invitation reaches this function. The cache key
    is a salted digest of the stored token digest, not the bearer token or any
    client address. Counters expire quickly and are shared through Django's
    configured cache backend.
    """
    digest = salted_hmac(
        'livegrade-panel-examiner-throttle', invitation.token_digest).hexdigest()
    key = f'panel-examiner:{digest}'
    window = settings.PANEL_EXAMINER_RATE_WINDOW
    try:
        if cache.add(key, 1, timeout=window):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            # The counter expired between the cache lookup and increment.
            return cache.add(key, 1, timeout=window)
        return count <= settings.PANEL_EXAMINER_RATE_LIMIT
    except Exception:
        # Do not accept unthrottled public bearer-link requests when the shared
        # limiter is unavailable.
        return None


def allow_showcase_judge_request(invitation):
    """Rate-limit a resolved Showcase judge bearer without exposing its token."""
    digest = salted_hmac(
        'livegrade-showcase-judge-throttle', invitation.token_digest).hexdigest()
    key = f'showcase-judge:{digest}'
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


def allow_unknown_bearer_request(request, *, service):
    """Throttle unknown public bearer links without persisting the source IP.

    Only the server-observed REMOTE_ADDR is used. Forwarded headers are
    intentionally ignored because their trust depends on ingress configuration.
    The cache key is an HMAC of the canonical address and service namespace.
    """
    remote_addr = request.META.get('REMOTE_ADDR', '')
    try:
        canonical_addr = ipaddress.ip_address(remote_addr).compressed
    except (TypeError, ValueError):
        return None
    digest = salted_hmac(
        f'livegrade-unknown-bearer-source:{service}', canonical_addr).hexdigest()
    key = f'unknown-bearer:{service}:{digest}'
    window = settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW
    try:
        if cache.add(key, 1, timeout=window):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            return cache.add(key, 1, timeout=window)
        return count <= settings.PUBLIC_BEARER_UNKNOWN_RATE_LIMIT
    except Exception:
        return None


def allow_public_event_request(request, *, service, event_id, limit=None, window=None):
    """Throttle a public event link per source and event without storing the IP."""
    remote_addr = request.META.get('REMOTE_ADDR', '')
    try:
        canonical_addr = ipaddress.ip_address(remote_addr).compressed
    except (TypeError, ValueError):
        return None
    digest = salted_hmac(
        f'livegrade-public-event-source:{service}',
        f'{event_id}:{canonical_addr}').hexdigest()
    key = f'public-event:{service}:{digest}'
    window = window or settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW
    limit = limit or settings.PUBLIC_BEARER_UNKNOWN_RATE_LIMIT
    try:
        if cache.add(key, 1, timeout=window):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            return cache.add(key, 1, timeout=window)
        return count <= limit
    except Exception:
        # Public forms fail closed when their shared abuse limiter is unavailable.
        return None
