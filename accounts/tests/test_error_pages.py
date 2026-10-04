"""404, 403, 500 and an expired form are LiveGrade's own pages - in production, and
with DEBUG on as well."""
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.views import debug

from accounts import error_pages
from accounts.testing import make_org, make_user, sign_in


class FriendlyPagesTests(TestCase):

    def get(self, url='/definitely/not/a/page/', client=None):
        return (client or Client(raise_request_exception=False)).get(url)

    def assertFriendly404(self, response):
        self.assertContains(response, 'Page not found', status_code=404)
        self.assertContains(response, 'Go to LiveGrade', status_code=404)
        for technical in ('URLconf', 'DEBUG = True', 'Request Method', 'assessments/ &lt;int:pk&gt;'):
            self.assertNotContains(response, technical, status_code=404)

    def test_a_mistyped_address_gets_the_friendly_page_with_debug_off_and_on(self):
        for debug_flag in (False, True):
            with self.subTest(debug=debug_flag), override_settings(DEBUG=debug_flag):
                self.assertFriendly404(self.get())

    def test_somebody_elses_session_gets_the_friendly_page_too(self):
        from assessments.models import AssessmentSession

        org = make_org('Uni')
        owner, other = make_user('owner'), make_user('other')
        session = AssessmentSession.objects.create(organization_id=org.pk, name='Private', created_by=owner)
        client = Client(raise_request_exception=False)
        sign_in(client, other, org)
        for debug_flag in (False, True):
            with self.subTest(debug=debug_flag), override_settings(DEBUG=debug_flag):
                self.assertFriendly404(client.get(f'/assessments/{session.pk}/'))

    def test_a_crash_gets_the_friendly_page_and_the_traceback_still_reaches_the_log(self):
        for debug_flag in (False, True):
            with self.subTest(debug=debug_flag), override_settings(DEBUG=debug_flag), \
                    mock.patch('accounts.views.HttpResponse', side_effect=RuntimeError('boom')), \
                    self.assertLogs('django.request', level='ERROR') as logs:
                response = self.get('/healthz')
            self.assertContains(response, 'Something went wrong', status_code=500)
            self.assertNotContains(response, 'RuntimeError', status_code=500)
            self.assertIn('boom', '\n'.join(logs.output))

    def test_a_refused_page_gets_the_friendly_403(self):
        with override_settings(DEBUG=False), mock.patch('accounts.views.HttpResponse', side_effect=PermissionDenied):
            response = self.get('/healthz')
        self.assertContains(response, "You don't have access", status_code=403)

    def test_an_expired_form_gets_a_readable_page_with_a_way_back(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/switch-organization/', {'organization': '1'})
        self.assertContains(response, 'This page has expired', status_code=403)
        self.assertContains(response, 'Reload and try again', status_code=403)
        self.assertContains(response, 'href="/switch-organization/"', status_code=403)
        self.assertNotContains(response, 'CSRF verification failed', status_code=403)


class InstallTests(SimpleTestCase):

    def test_the_two_technical_responses_are_ours(self):
        self.assertIs(debug.technical_404_response, error_pages.friendly_404)
        self.assertIs(debug.technical_500_response, error_pages.friendly_500)

    def test_switched_off_ready_leaves_djangos_pages_alone(self):
        from django.apps import apps

        original = (debug.technical_404_response, debug.technical_500_response)
        try:
            with override_settings(FRIENDLY_ERROR_PAGES=False):
                debug.technical_404_response = debug.technical_500_response = 'django-own'
                apps.get_app_config('accounts').ready()
            self.assertEqual((debug.technical_404_response, debug.technical_500_response), ('django-own', 'django-own'))
        finally:
            debug.technical_404_response, debug.technical_500_response = original

    def test_the_setting_defaults_on_and_can_be_turned_off(self):
        from config.tests_settings import load

        self.assertTrue(load({})['FRIENDLY_ERROR_PAGES'])
        self.assertFalse(load({'FRIENDLY_ERROR_PAGES': 'False'})['FRIENDLY_ERROR_PAGES'])
