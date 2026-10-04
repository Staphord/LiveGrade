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
    that is not a well-formed entry is ignored rather than trusted. An entry
    carries ``can_oversee`` only when DevPerf said so with a literal true; it
    never stands in for the right to run LiveGrade, which is checked first.
    """
    if not isinstance(orgs, list):
        return []
    return [
        {'id': org['id'], 'slug': str(org.get('slug', '')), 'name': str(org.get('name', '')),
         'public': org.get('is_public_workspace') is True,
         # Nobody oversees the shared workspace: everybody in it is a stranger to
         # everybody else, and each only ever sees their own sessions.
         'can_oversee': (org.get('can_oversee_assessments') is True
                         and org.get('is_public_workspace') is not True)}
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


def in_public_workspace(request):
    """Whether the active organization is the shared one self-registered
    lecturers use: no organization to show, no colleagues to hand sessions to."""
    organization = active_organization(request)
    return bool(organization and organization.get('public'))


def can_oversee(request):
    """Whether this person may see every lecturer's session in the active
    organization (read-only, plus handing one over), as DevPerf last said."""
    organization = active_organization(request)
    # Never in the shared workspace, however the flag got into the session.
    return bool(organization and organization.get('can_oversee') and not organization.get('public'))


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
