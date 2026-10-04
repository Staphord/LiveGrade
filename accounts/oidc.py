"""Sign-in with DevPerf.

DevPerf signs a token naming the person and listing the organizations they
belong to, each marked with whether they may run LiveGrade there. Everything
here trusts that list and nothing else: who may do what is decided once, in
DevPerf, never reimplemented in this service.
"""
from urllib.parse import urlencode

from django.conf import settings
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView

from .access import ORGS_KEY, usable_orgs
from .models import User


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


class CallbackView(OIDCAuthenticationCallbackView):
    """Remembers which organizations DevPerf said this person may use."""

    def login_success(self):
        response = super().login_success()
        self.request.session[ORGS_KEY] = usable_orgs(
            self.user.devperf_claims.get('orgs'))
        return response


def devperf_logout_url(request):
    """Where to send somebody so signing out here ends their DevPerf session
    too, returning them to LiveGrade afterwards. The return address is the one
    registered in DevPerf; anything else it refuses."""
    query = {
        'id_token_hint': request.session.get('oidc_id_token', ''),
        'client_id': settings.OIDC_RP_CLIENT_ID,
        'post_logout_redirect_uri': f'{settings.LIVEGRADE_BASE_URL}/',
    }
    return f'{settings.OIDC_OP_LOGOUT_ENDPOINT}?{urlencode(query)}'
