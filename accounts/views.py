from django.contrib.auth import logout
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .access import ACTIVE_KEY, ORGS_KEY, active_organization, lecturer_required
from .oidc import SWITCH_ACCOUNT_STATE, devperf_logout_url
from portal.models import services_for
from portal.services import SERVICES, dashboard_url, get_service, is_released


def home(request):
    """Choose a service here; returning people also see their existing services."""
    if not request.user.is_authenticated:
        if request.GET.get('state') == SWITCH_ACCOUNT_STATE:
            return redirect('oidc_authentication_init')
    elif active_organization(request) is None:
        return no_access(request)
    return render(request, 'landing.html', {
        'service_cards': [{'service': service, 'released': is_released(service.slug)}
                          for service in SERVICES],
        'released_service_slugs': [service.slug for service in SERVICES
                                   if is_released(service.slug)],
        'used_services': services_for(request.user) if request.user.is_authenticated else [],
    })


def no_access(request):
    """Shown to somebody DevPerf signed in but who may not run LiveGrade."""
    return render(request, 'accounts/no_access.html', status=403)


@require_POST
def switch_account(request):
    """Leave the account that was refused, here and in DevPerf, and go straight
    back to sign-in to pick another. DevPerf is given the signed hint from this
    visit, so it ends its session without asking; it then returns the person here
    carrying a marker that sends them on to the sign-in page."""
    url = devperf_logout_url(request, state=SWITCH_ACCOUNT_STATE)
    logout(request)
    return redirect(url)


@lecturer_required
@require_POST
def switch_organization(request, slug=None):
    """Work in another of the lecturer's organizations, if it is theirs."""
    service = get_service(getattr(request, 'service', None))
    if slug is not None and (not service or service.slug != slug or not is_released(slug)):
        raise Http404
    try:
        wanted = int(request.POST.get('organization', ''))
    except ValueError:
        wanted = None
    if any(org['id'] == wanted for org in request.session.get(ORGS_KEY, [])):
        request.session[ACTIVE_KEY] = wanted
    return redirect(dashboard_url(service) if service else 'home')


def health(request):
    return HttpResponse('ok', content_type='text/plain')


def readiness(request):
    """Dependency readiness, separate from process liveness; never expose details."""
    import secrets
    from django.core.cache import cache
    from django.db import connections

    if request.method not in ('GET', 'HEAD'):
        return HttpResponse(status=405, headers={'Allow': 'GET, HEAD'})
    key = 'readiness:' + secrets.token_hex(12)
    ready = False
    try:
        with connections['default'].cursor() as cursor:
            cursor.execute('SELECT 1')
            database_ready = cursor.fetchone()[0] == 1
        cache.set(key, 'ready', timeout=5)
        ready = database_ready and cache.get(key) == 'ready'
        cache.delete(key)
    except Exception:
        pass
    response = JsonResponse({'status': 'ready' if ready else 'unavailable'}, status=200 if ready else 503)
    response['Cache-Control'] = 'no-store'
    return response


def csrf_failure(request, reason=''):
    """The security token on a form did not match: almost always a page left open
    across a sign-out or sign-in elsewhere. Not the person's fault, and nothing
    was changed - the page says so and offers a fresh start."""
    from urllib.parse import urlsplit
    from django.urls import reverse
    from django.utils.http import url_has_allowed_host_and_scheme

    reload_url = request.get_full_path()
    if request.path == reverse('oidc_logout'):
        referer = request.META.get('HTTP_REFERER', '')
        if referer and url_has_allowed_host_and_scheme(
                referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            parts = urlsplit(referer)
            reload_url = f'{parts.path}?{parts.query}' if parts.query else (parts.path or reverse('home'))
        else:
            reload_url = reverse('home')
    return render(request, 'errors/csrf.html', {'reload_url': reload_url}, status=403)


def desktop_login(request):
    """Start OIDC sign-in from the system browser and return via livegrade://auth."""
    from urllib.parse import urlencode
    from django.urls import reverse

    params = {'desktop': '1'}
    if request.GET.get('signup') == '1':
        params['signup'] = '1'
    return redirect(f"{reverse('oidc_authentication_init')}?{urlencode(params)}")


def desktop_handoff(request):
    """Browser handoff page that triggers livegrade://auth?code=..."""
    code = (request.GET.get('code') or '').strip()
    response = render(request, 'accounts/desktop_handoff.html', {'code': code})
    response['Cache-Control'] = 'no-store'
    return response


def desktop_exchange(request):
    """Exchange a single-use handoff code for an authenticated Electron session."""
    import hashlib
    from django.contrib import messages
    from django.contrib.auth import login
    from django.core.cache import cache
    from .models import User

    raw_code = (request.GET.get('code') or '').strip()
    if not raw_code:
        messages.error(request, 'Sign-in link expired or invalid. Please sign in again.')
        return redirect('home')

    digest = hashlib.sha256(raw_code.encode('utf-8')).hexdigest()
    cache_key = f'desktop_auth_handoff:{digest}'
    payload = cache.get(cache_key)
    cache.delete(cache_key)
    if not isinstance(payload, dict) or not payload.get('user_id'):
        messages.error(request, 'Sign-in link expired or already used. Please sign in again.')
        return redirect('home')

    user = User.objects.filter(pk=payload['user_id'], is_active=True).first()
    if user is None:
        messages.error(request, 'Account not found. Please sign in again.')
        return redirect('home')

    login(request, user, backend='accounts.oidc.DevPerfBackend')
    request.session[ORGS_KEY] = payload.get('orgs') or []
    return redirect(payload.get('next_url') or 'home')
