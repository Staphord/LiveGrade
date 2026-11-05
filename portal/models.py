from django.conf import settings
from django.db import models
from django.utils import timezone

from .services import SLUGS, get_service, released_services


class ServiceAccess(models.Model):
    """That a person has chosen to use a service.

    It records a choice, never a permission. Who may do what inside an
    organization is decided by DevPerf's token alone (accounts/access.py); nothing
    here widens or narrows that. The row only lets LiveGrade show a person the
    services they already use, and says when they first and last used each.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='service_access')
    service = models.CharField(max_length=20)
    first_chosen = models.DateTimeField(default=timezone.now)
    last_used = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'service'], name='one_row_per_person_and_service'),
        ]

    def __str__(self):
        return f'{self.user_id} uses {self.service}'


class ServiceInterest(models.Model):
    """Consent to one launch notification for one service, without an account."""

    email = models.EmailField()
    service = models.CharField(max_length=20, choices=[(slug, slug) for slug in SLUGS])
    consent = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['email', 'service'], name='one_interest_per_email_and_service'),
            models.CheckConstraint(condition=models.Q(consent=True), name='interest_requires_consent'),
        ]


def record_choice(user, slug):
    """Note that ``user`` chose the service ``slug``: the first time creates the row,
    every later time only refreshes when it was last used. A slug that is not in the
    registry is refused, so the table can only ever hold real services."""
    if get_service(slug) is None:
        raise ValueError(f'{slug!r} is not a LiveGrade service')
    now = timezone.now()
    access, created = ServiceAccess.objects.get_or_create(
        user=user, service=slug, defaults={'first_chosen': now, 'last_used': now})
    if not created:
        access.last_used = now
        access.save(update_fields=['last_used'])
    return access


def services_for(user):
    """The released services this person already uses, in the registry's order."""
    used = set(ServiceAccess.objects.filter(user=user, service__in=SLUGS).values_list('service', flat=True))
    return [service for service in released_services() if service.slug in used]
