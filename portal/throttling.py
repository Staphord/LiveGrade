"""A shared, short-lived signup limit; neither cache keys nor logs contain addresses."""
from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac


def allow_service_interest(request):
    # REMOTE_ADDR is set by the server. Forwarded headers supplied by callers are
    # deliberately ignored; the trusted proxy/server must establish the client IP.
    digest = salted_hmac('livegrade-service-interest', request.META.get('REMOTE_ADDR', '')).hexdigest()
    key = f'service-interest:{digest}'
    window = settings.LIVEGRADE_INTEREST_RATE_WINDOW
    if cache.add(key, 1, timeout=window):
        return True
    try:
        return cache.incr(key) <= settings.LIVEGRADE_INTEREST_RATE_LIMIT
    except ValueError:
        # The counter expired between add and incr. Start a fresh window if
        # another worker has not already done so.
        return cache.add(key, 1, timeout=window)
