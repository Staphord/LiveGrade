"""Query scoping for models that belong to an organization.

Organizations live in DevPerf; here an organization is only the id (and name)
DevPerf put in the sign-in token, kept in the session as the lecturer's
*active* organization. Every scoped model carries that id as a plain column -
there is no organization table to join to - and ``objects.for_organization(org)``
filters on it.

The important property is what happens when the organization is missing: the
queryset is empty, never unfiltered. A view that loses its organization shows
nothing, which is a visible bug, rather than showing everything, which is a data
leak nobody notices. Explicit scoping, not a thread-local "current
organization", so Celery tasks (which have no request) behave the same way.
"""

from django.db import models

from .access import active_organization


class OrganizationQuerySet(models.QuerySet):

    def for_organization(self, organization):
        """Rows belonging to ``organization`` (a session organization dict, or
        anything with an ``id``); nothing at all when there is none."""
        if not organization:
            return self.none()
        return self.filter(organization_id=organization['id'])

    def for_request(self, request):
        return self.for_organization(active_organization(request))


class OrganizationManager(models.Manager.from_queryset(OrganizationQuerySet)):
    """Default manager for scoped models. Deliberately not auto-filtering."""


class OrganizationScopedModel(models.Model):
    """Mixin adding DevPerf's organization id and the scoped manager."""

    organization_id = models.BigIntegerField(
        db_index=True,
        help_text="DevPerf's id for the organization this row belongs to.")

    objects = OrganizationManager()

    class Meta:
        abstract = True


def get_scoped_or_404(model, request, **kwargs):
    """Fetch one row from the active organization, or raise 404.

    404 rather than 403 on purpose: a 403 confirms the record exists, which
    tells someone in another organization that a given id is real. Absence is
    the honest answer - as far as this organization is concerned, it is not
    there.
    """
    from django.shortcuts import get_object_or_404

    return get_object_or_404(model.objects.for_request(request), **kwargs)
