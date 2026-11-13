"""Redact bearer-capability URL segments from Django application log records."""

import logging
import re
import traceback


BEARER_PATH_PATTERN = re.compile(
    r'(?i)(/(?:panel/(?:examiner|results|display)|competition/judge|showcase/judge|team/(?:feedback|results)|assess/(?:poll-invitation|polls)|polls/(?:vote|live|qr))/)'
    r'([^/?\s"\'<>]+)'
)

SESSION_PATH_PATTERN = re.compile(
    r'(?i)(/assess/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=/|$)')


def redact_bearer_paths(value):
    """Replace capability values while retaining a useful route description."""
    return SESSION_PATH_PATTERN.sub(r'\1[redacted]',
        BEARER_PATH_PATTERN.sub(r'\1[redacted]', value))


class BearerUrlRedactionFilter(logging.Filter):
    """Ensure messages, exception text, and request objects do not expose tokens."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = redact_bearer_paths(message)
        record.args = ()

        if hasattr(record, 'request'):
            # Structured handlers must not serialize the original HttpRequest,
            # whose path and resolver kwargs contain the bearer capability.
            record.request = None

        if record.exc_info:
            exception_text = ''.join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact_bearer_paths(exception_text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_bearer_paths(record.exc_text)
        return True
