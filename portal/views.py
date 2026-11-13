from urllib.parse import urlencode

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from django.views.decorators.http import require_POST
from redis.exceptions import RedisError

from accounts.access import active_organization
from accounts.views import no_access

from .forms import ServiceInterestForm
from .models import ServiceInterest, record_choice
from .services import CHOICE_KEY, dashboard_url, get_service, is_released
from .throttling import allow_service_interest


@require_safe
@never_cache
def entry(request, slug):
    """A service's entry address. Opening it is the act of choosing the service.

    - A service that is not open shows its coming-soon page, to anybody.
    - A visitor is sent to sign in (or register) and then to the dashboard.
    - A signed-in person is noted as using the service and sent to its dashboard.

    The destination comes from the registry, never from the request, so there is no
    address a visitor can supply to be redirected to. The only state written is the
    harmless, idempotent note that the person chose a released service.
    """
    service = get_service(slug)
    if service is None:  # the route only matches registered slugs; this keeps the view safe on its own
        raise Http404
    if not is_released(slug):
        return render(request, 'portal/coming_soon.html', {
            'service': service, 'interest_form': ServiceInterestForm()})

    destination = dashboard_url(service)
    if not request.user.is_authenticated:
        request.session[CHOICE_KEY] = slug
        # The callback chooses the destination from this server-side session value.
        # Do not let an OIDC ``next`` parameter decide which service opens.
        sign_in_url = reverse('oidc_authentication_init')
        if request.GET.get('signup') == '1':
            sign_in_url += '?' + urlencode({'signup': '1'})
        return redirect(sign_in_url)
    if active_organization(request) is None:
        return no_access(request)
    record_choice(request.user, slug)
    return redirect(destination)


@require_POST
@never_cache
def service_interest(request, slug):
    service = get_service(slug)
    if service is None or is_released(slug):
        raise Http404
    form = ServiceInterestForm(request.POST)
    try:
        allowed = allow_service_interest(request)
    except RedisError:
        form.add_error(None, 'Notifications are temporarily unavailable. Please try again later.')
        response = render(request, 'portal/coming_soon.html', {
            'service': service, 'interest_form': form}, status=503)
        response['Retry-After'] = '60'
        return response
    if not allowed:
        form.add_error(None, 'Too many requests. Please try again later.')
        response = render(request, 'portal/coming_soon.html', {
            'service': service, 'interest_form': form}, status=429)
        response['Retry-After'] = str(settings.LIVEGRADE_INTEREST_RATE_WINDOW)
        return response
    if form.is_valid():
        ServiceInterest.objects.get_or_create(
            email=form.cleaned_data['email'], service=slug, defaults={'consent': True})
        return render(request, 'portal/coming_soon.html', {'service': service, 'subscribed': True})
    return render(request, 'portal/coming_soon.html', {
        'service': service, 'interest_form': form}, status=400)
