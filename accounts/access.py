"""Who may use LiveGrade, and for which organization.

DevPerf's token lists the person's organizations, each with
``can_run_assessments``. The usable ones are kept in the session at sign-in;
the page works on one of them at a time - the *active* organization - and every
query LiveGrade makes is scoped to it.
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render

ORGS_KEY = 'livegrade_orgs'
ACTIVE_KEY = 'livegrade_active_org'


def usable_orgs(orgs):
    """The organizations in a token's ``orgs`` claim where LiveGrade may be run.

    Defensive about shape: the claim comes from another service, so anything
    that is not a well-formed entry is ignored rather than trusted.
    """
    if not isinstance(orgs, list):
        return []
    return [
        {'id': org['id'], 'slug': str(org.get('slug', '')), 'name': str(org.get('name', ''))}
        for org in orgs
        if isinstance(org, dict) and org.get('can_run_assessments') is True
        and isinstance(org.get('id'), int) and not isinstance(org.get('id'), bool)
    ]


def active_organization(request):
    """The organization this lecturer is working in, or None.

    The one they chose if it is still one of theirs, otherwise their first.
    """
    orgs = request.session.get(ORGS_KEY) or []
    chosen = request.session.get(ACTIVE_KEY)
    for org in orgs:
        if org['id'] == chosen:
            return org
    return orgs[0] if orgs else None


def lecturer_required(view):
    """Signed in through DevPerf, with at least one organization to work in."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        organization = active_organization(request)
        if organization is None:
            return render(request, 'accounts/no_access.html', status=403)
        request.organization = organization
        return view(request, *args, **kwargs)
    return wrapper
