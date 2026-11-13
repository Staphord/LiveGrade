"""Participant-facing privacy notice selection and version tracking."""

from .models import PrivacyNotice

NOTICE_SHOWN_SESSION_KEY = 'assessment_privacy_notice_shown'


def active_privacy_notice(session):
    """Return the published notice for this session's service, if configured."""
    return PrivacyNotice.objects.filter(service=session.service, is_active=True).first()


def notice_seen_key(session, student):
    """Opaque session key for a notice served to this participant in this session."""
    return f'{session.pk}:{student.pk}'
