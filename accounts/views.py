from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .access import ACTIVE_KEY, ORGS_KEY, lecturer_required


@lecturer_required
def home(request):
    """LiveGrade has one place to start: the lecturer's sessions."""
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
