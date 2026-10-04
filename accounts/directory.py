"""Who else may run LiveGrade in an organization.

Filled at each sign-in from DevPerf's token (``record_sign_in``) and read when a
lecturer hands a session to a colleague (``colleagues``).
"""
from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils import timezone

from .models import OrganizationLecturer, User


@transaction.atomic
def record_sign_in(user, organizations):
    """Remember that ``user`` may run LiveGrade in exactly ``organizations``
    (the token's usable ones): add or refresh those, forget any other."""
    now = timezone.now()
    for organization in organizations:
        OrganizationLecturer.objects.update_or_create(
            user=user, organization_id=organization['id'],
            defaults={'organization_name': organization.get('name', ''), 'last_seen': now})
    (OrganizationLecturer.objects.filter(user=user)
     .exclude(organization_id__in=[o['id'] for o in organizations]).delete())


def colleagues(organization_id, excluding=None):
    """The people known to run LiveGrade in the organization, by name,
    optionally leaving out one person (the one asking)."""
    people = User.objects.filter(organization_roles__organization_id=organization_id)
    if excluding is not None:
        people = people.exclude(pk=excluding.pk)
    # By the name a person is shown under: their first name, or their username
    # when they have none, ignoring case.
    return people.annotate(
        sort_name=Lower(Coalesce(NullIf('first_name', Value('')), 'username'))
    ).order_by('sort_name', 'last_name', 'pk')
