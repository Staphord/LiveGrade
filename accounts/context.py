from django.conf import settings

from .access import ORGS_KEY, active_organization


PERSONAL = 'Personal'


def livegrade(request):
    """The pieces every page's header needs.

    The shared workspace for self-registered lecturers is not an organization
    to them: on its own it is not shown at all, and next to a real one it is
    called "Personal".
    """
    organizations = request.session.get(ORGS_KEY) or []
    return {
        'devperf_url': settings.DEVPERF_URL,
        'active_org': active_organization(request),
        'my_orgs': [{**org, 'display_name': PERSONAL if org.get('public') else org['name']}
                    for org in organizations],
        'only_personal': bool(organizations) and all(org.get('public') for org in organizations),
    }
