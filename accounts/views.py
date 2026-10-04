from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .access import ACTIVE_KEY, ORGS_KEY, active_organization, lecturer_required


def home(request):
    """A visitor sees what LiveGrade is and how to start; somebody signed in goes
    straight to their sessions."""
    if not request.user.is_authenticated:
        return render(request, 'landing.html')
    if active_organization(request) is None:
        return render(request, 'accounts/no_access.html', status=403)
    return redirect('assessment_session_list')


def no_access(request):
    """Shown to somebody DevPerf signed in but who may not run LiveGrade."""
    return render(request, 'accounts/no_access.html', status=403)


@lecturer_required
@require_POST
def switch_organization(request):
    """Work in another of the lecturer's organizations, if it is theirs."""
    try:
        wanted = int(request.POST.get('organization', ''))
    except ValueError:
        wanted = None
    if any(org['id'] == wanted for org in request.session.get(ORGS_KEY, [])):
        request.session[ACTIVE_KEY] = wanted
    return redirect('home')


def health(request):
    return HttpResponse('ok', content_type='text/plain')


def csrf_failure(request, reason=''):
    """The security token on a form did not match: almost always a page left open
    across a sign-out or sign-in elsewhere. Not the person's fault, and nothing
    was changed - the page says so and offers a fresh start."""
    return render(request, 'errors/csrf.html', {'reload_url': request.get_full_path()}, status=403)
