"""Tests for the Electron desktop OIDC handoff flow."""

import hashlib
from urllib.parse import parse_qs, urlsplit

from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.access import ORGS_KEY
from accounts.models import User
from accounts.oidc import DESKTOP_SWITCH_ACCOUNT_STATE, CallbackView


@override_settings(SESSION_COOKIE_SECURE=False)
class DesktopAuthHandoffTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_desktop_login_redirects_to_oidc_with_desktop_flag(self):
        resp = self.client.get(reverse('desktop_login'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], f"{reverse('oidc_authentication_init')}?desktop=1")

        signup_resp = self.client.get(f"{reverse('desktop_login')}?signup=1")
        self.assertEqual(signup_resp.status_code, 302)
        self.assertEqual(signup_resp['Location'], f"{reverse('oidc_authentication_init')}?desktop=1&signup=1")

    def test_desktop_handoff_renders_deep_link_and_no_store(self):
        resp = self.client.get(f"{reverse('desktop_handoff')}?code=test-abc")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Cache-Control'], 'no-store')
        self.assertContains(resp, 'livegrade://auth?code=test-abc')
        self.assertContains(resp, 'no-sidebar')
        self.assertNotContains(resp, '<div class="sidebar">')

    def test_desktop_exchange_consumes_single_use_code_and_logs_in(self):
        user = User.objects.create(sub='sub-123', username='devperf-sub-123', email='org@example.com')
        orgs = [{'id': 1, 'name': 'Demo Org', 'role': 'owner', 'public': False}]
        raw_code = 'valid-one-time-code'
        digest = hashlib.sha256(raw_code.encode('utf-8')).hexdigest()
        cache.set(
            f'desktop_auth_handoff:{digest}',
            {'user_id': user.pk, 'orgs': orgs, 'next_url': '/assessment/app/'},
            timeout=60,
        )

        resp = self.client.get(f"{reverse('desktop_exchange')}?code={raw_code}")
        self.assertRedirects(resp, '/assessment/app/', fetch_redirect_response=False)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        self.assertEqual(self.client.session[ORGS_KEY], orgs)

        # Second attempt with the same code is rejected (single-use)
        second = self.client.get(f"{reverse('desktop_exchange')}?code={raw_code}")
        self.assertRedirects(second, reverse('home'), fetch_redirect_response=False)

    def test_desktop_exchange_rejects_missing_or_expired_code(self):
        resp = self.client.get(reverse('desktop_exchange'))
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_callback_login_success_redirects_to_desktop_handoff(self):
        user = User.objects.create(
            sub='sub-desktop-1',
            username='devperf-sub-desktop-1',
            email='desktop@example.com',
        )
        user.devperf_claims = {'orgs': [{'id': 7, 'name': 'Desktop Org', 'role': 'owner', 'public': False}]}
        request = RequestFactory().get('/oidc/callback/')
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()

        view = CallbackView()
        view.request = request
        view.user = user
        view.chosen_service = None
        view.is_desktop = True
        view.desktop_handoff_code = None

        response = view.login_success()
        self.assertEqual(response.status_code, 302)
        parsed = urlsplit(response['Location'])
        self.assertEqual(parsed.path, reverse('desktop_handoff'))
        code = parse_qs(parsed.query).get('code', [''])[0]
        self.assertTrue(code)
        digest = hashlib.sha256(code.encode('utf-8')).hexdigest()
        self.assertIsNotNone(cache.get(f'desktop_auth_handoff:{digest}'))

    def test_authenticated_desktop_login_prompts_to_continue_or_switch(self):
        user = User.objects.create(
            sub='sub-confirm-1',
            username='devperf-sub-confirm-1',
            email='lecturer@example.com',
            first_name='Ada',
            last_name='Lovelace',
        )
        self.client.force_login(user)

        resp = self.client.get(reverse('desktop_login'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Cache-Control'], 'no-store')
        self.assertContains(resp, 'Continue as Ada Lovelace')
        self.assertContains(resp, 'Use a different account')
        self.assertContains(resp, 'no-sidebar')
        self.assertNotContains(resp, '<div class="sidebar">')

    def test_landing_page_shows_sign_out_when_authenticated(self):
        from accounts.testing import make_org, sign_in
        user = User.objects.create(
            sub='sub-landing-1',
            username='devperf-sub-landing-1',
            email='landing@example.com',
        )
        org = make_org('Landing Org')
        sign_in(self.client, user, org)

        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'action="{reverse("oidc_logout")}"')
        self.assertContains(resp, 'Sign out')

    def test_authenticated_desktop_login_continue_issues_handoff_code(self):
        user = User.objects.create(
            sub='sub-continue-1',
            username='devperf-sub-continue-1',
            email='continue@example.com',
        )
        orgs = [{'id': 3, 'name': 'Org Three', 'role': 'owner', 'public': False}]
        self.client.force_login(user)
        session = self.client.session
        session[ORGS_KEY] = orgs
        session.save()

        resp = self.client.post(reverse('desktop_login'), {'action': 'continue'})
        self.assertEqual(resp.status_code, 302)
        parsed = urlsplit(resp['Location'])
        self.assertEqual(parsed.path, reverse('desktop_handoff'))
        code = parse_qs(parsed.query).get('code', [''])[0]
        self.assertTrue(code)
        digest = hashlib.sha256(code.encode('utf-8')).hexdigest()
        payload = cache.get(f'desktop_auth_handoff:{digest}')
        self.assertEqual(payload['user_id'], user.pk)
        self.assertEqual(payload['orgs'], orgs)

    @override_settings(
        OIDC_RP_CLIENT_ID='lg-client',
        OIDC_OP_LOGOUT_ENDPOINT='https://devperf.example.test/o/logout/',
        LIVEGRADE_BASE_URL='https://lg.example.test',
    )
    def test_authenticated_desktop_login_switch_logs_out_and_returns_to_desktop_flow(self):
        user = User.objects.create(
            sub='sub-switch-1',
            username='devperf-sub-switch-1',
            email='switch@example.com',
        )
        self.client.force_login(user)

        resp = self.client.post(reverse('desktop_login'), {'action': 'switch'})
        self.assertEqual(resp.status_code, 302)
        parsed = urlsplit(resp['Location'])
        self.assertEqual(f'{parsed.scheme}://{parsed.netloc}{parsed.path}', 'https://devperf.example.test/o/logout/')
        self.assertEqual(parse_qs(parsed.query).get('state'), [DESKTOP_SWITCH_ACCOUNT_STATE])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unauthenticated_desktop_login_post_redirects_back_to_login(self):
        resp = self.client.post(reverse('desktop_login'), {'action': 'continue'})
        self.assertRedirects(resp, reverse('desktop_login'), fetch_redirect_response=False)

    def test_home_redirects_desktop_switch_state_back_to_desktop_oidc(self):
        resp = self.client.get(f"{reverse('home')}?state={DESKTOP_SWITCH_ACCOUNT_STATE}")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], f"{reverse('oidc_authentication_init')}?desktop=1")

    @override_settings(
        OIDC_RP_CLIENT_ID='lg-client',
        OIDC_OP_LOGOUT_ENDPOINT='https://devperf.example.test/o/logout/',
        LIVEGRADE_BASE_URL='https://lg.example.test',
    )
    def test_desktop_user_agent_logout_stays_in_app_and_redirects_home(self):
        user = User.objects.create(
            sub='sub-logout-1',
            username='devperf-sub-logout-1',
            email='logout@example.com',
        )
        self.client.force_login(user)

        resp = self.client.post(
            reverse('oidc_logout'),
            HTTP_USER_AGENT='Mozilla/5.0 LiveGradeDesktop/1.0.0',
        )
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(
        OIDC_RP_CLIENT_ID='lg-client',
        OIDC_OP_LOGOUT_ENDPOINT='https://devperf.example.test/o/logout/',
        LIVEGRADE_BASE_URL='https://lg.example.test',
    )
    def test_browser_user_agent_logout_still_redirects_to_devperf(self):
        user = User.objects.create(
            sub='sub-logout-2',
            username='devperf-sub-logout-2',
            email='browser@example.com',
        )
        self.client.force_login(user)

        resp = self.client.post(
            reverse('oidc_logout'),
            HTTP_USER_AGENT='Mozilla/5.0 Chrome/130.0.0.0',
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('https://devperf.example.test/o/logout/'))

