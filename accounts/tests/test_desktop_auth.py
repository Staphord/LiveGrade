"""Tests for the Electron desktop OIDC handoff flow."""

import hashlib
from urllib.parse import parse_qs, urlsplit

from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.access import ORGS_KEY
from accounts.models import User
from accounts.oidc import CallbackView


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

