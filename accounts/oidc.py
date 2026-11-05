"""Sign-in with DevPerf.

DevPerf signs a token naming the person and listing the organizations they
belong to, each marked with whether they may run LiveGrade there. Everything
here trusts that list and nothing else: who may do what is decided once, in
DevPerf, never reimplemented in this service.
"""
from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings
from django.urls import reverse
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView, OIDCAuthenticationRequestView

from .access import ORGS_KEY, usable_orgs
from .directory import record_sign_in
from .models import User
from portal.models import record_choice
from portal.services import CHOICE_KEY, dashboard_url, get_service, is_released


class DevPerfBackend(OIDCAuthenticationBackend):

    def describe_user_by_claims(self, claims):
        return f"sub {claims.get('sub')}"

    def verify_claims(self, claims):
        """Let somebody in only when DevPerf says they may run LiveGrade
        somewhere. The claims come from DevPerf's userinfo endpoint, over the
        access token this sign-in just earned, so they are DevPerf's own."""
        return bool(claims.get('sub')) and bool(usable_orgs(claims.get('orgs')))

    def filter_users_by_claims(self, claims):
        return User.objects.filter(sub=str(claims['sub']))

    def create_user(self, claims):
        sub = str(claims['sub'])
        user = User(sub=sub, username=f'devperf-{sub}')
        return self.update_user(user, claims)

    def update_user(self, user, claims):
        """Refresh the person's name and email from DevPerf at every sign-in."""
        user.first_name = claims.get('given_name', '')[:150]
        user.last_name = claims.get('family_name', '')[:150]
        user.email = claims.get('email', '')
        user.save()
        # Carried to the callback view, which puts it in the session once the
        # login has settled (logging in can replace the session).
        user.devperf_claims = claims
        return user


class SignInStartView(OIDCAuthenticationRequestView):
    """Sends the browser to DevPerf to sign in - or, with ``?signup=1``, to create
    an account there first (the standard ``screen_hint=signup`` request parameter;
    DevPerf answers it with its sign-up page and then carries on to sign in)."""

    def get(self, request):
        selected_slug = request.session.pop(CHOICE_KEY, None)
        response = super().get(request)
        # Bind the destination to this OIDC transaction. Another tab can start a
        # different sign-in without changing where this callback will land.
        state = parse_qs(urlsplit(response.url).query)['state'][0]
        states = request.session['oidc_states']
        states[state][CHOICE_KEY] = selected_slug if is_released(selected_slug) else None
        request.session['oidc_states'] = states
        request.session.pop('oidc_login_next', None)
        return response

    def get_extra_params(self, request):
        params = dict(super().get_extra_params(request))
        if request.GET.get('signup') == '1':
            params['screen_hint'] = 'signup'
        return params


class CallbackView(OIDCAuthenticationCallbackView):
    """Remembers DevPerf access and returns the person to their chosen service."""

    def get(self, request):
        # The library verifies and consumes the state before calling login_success.
        # Capture its service here because the library removes the state record.
        state = request.session.get('oidc_states', {}).get(request.GET.get('state'), {})
        slug = state.get(CHOICE_KEY)
        self.chosen_service = get_service(slug) if is_released(slug) else None
        return super().get(request)

    @property
    def success_url(self):
        return dashboard_url(self.chosen_service) if self.chosen_service else reverse('home')

    def login_success(self):
        response = super().login_success()
        organizations = usable_orgs(self.user.devperf_claims.get('orgs'))
        self.request.session[ORGS_KEY] = organizations
        # So colleagues can hand this person a session (accounts.directory).
        record_sign_in(self.user, organizations)

        if self.chosen_service:
            record_choice(self.user, self.chosen_service.slug)
        return response


SWITCH_ACCOUNT_STATE = 'switch-account'


def devperf_logout_url(request, state=None):
    """Where to send somebody so signing out here ends their DevPerf session
    too, returning them to LiveGrade afterwards. The return address is the one
    registered in DevPerf; anything else it refuses. ``state`` comes back on the
    return address untouched."""
    query = {
        'id_token_hint': request.session.get('oidc_id_token', ''),
        'client_id': settings.OIDC_RP_CLIENT_ID,
        'post_logout_redirect_uri': f'{settings.LIVEGRADE_BASE_URL}/',
    }
    if state:
        query['state'] = state
    # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string (Django, not Flask: a URL built from a setting and urlencode())
    return f'{settings.OIDC_OP_LOGOUT_ENDPOINT}?{urlencode(query)}'
