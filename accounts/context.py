from django.conf import settings

from .access import ORGS_KEY, active_organization


def livegrade(request):
    """The pieces every page's header needs."""
    return {
        'devperf_url': settings.DEVPERF_URL,
        'active_org': active_organization(request),
        'my_orgs': request.session.get(ORGS_KEY) or [],
    }
