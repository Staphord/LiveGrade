"""A public launch-notification signup is consented, bounded, and private."""
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from redis.exceptions import ConnectionError

from portal.models import ServiceAccess, ServiceInterest
from portal.throttling import allow_service_interest


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'], LIVEGRADE_INTEREST_RATE_LIMIT=5,
                   LIVEGRADE_INTEREST_RATE_WINDOW=600)
class InterestTests(TestCase):

    def setUp(self):
        cache.clear()
        self.url = '/panel/notify/'
        self.fields = {'email': 'Ann@Example.test', 'consent': 'on'}

    def tearDown(self):
        cache.clear()

    def test_the_coming_soon_page_has_a_consented_signup_for_its_own_service(self):
        response = self.client.get('/panel/')
        self.assertContains(response, 'action="/panel/notify/"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'Email me once when this service opens.')
        self.assertContains(response, 'name="consent" required')

    def test_a_visitor_can_subscribe_without_an_account(self):
        response = self.client.post(self.url, self.fields)
        self.assertContains(response, "You're on the notification list.")
        row = ServiceInterest.objects.get()
        self.assertEqual((row.email, row.service, row.consent), ('ann@example.test', 'panel', True))
        self.assertEqual(ServiceAccess.objects.count(), 0)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_duplicate_has_the_same_confirmation_and_does_not_replace_the_consent_time(self):
        first_response = self.client.post(self.url, self.fields)
        first = ServiceInterest.objects.get()
        response = self.client.post(self.url, {**self.fields, 'email': 'ann@example.test'})
        self.assertEqual(ServiceInterest.objects.count(), 1)
        self.assertEqual(ServiceInterest.objects.get().created_at, first.created_at)
        self.assertEqual(response.status_code, first_response.status_code)
        self.assertEqual(response.context['subscribed'], first_response.context['subscribed'])

    def test_invalid_email_and_missing_consent_never_create_an_interest(self):
        for fields in ({'email': 'bad', 'consent': 'on'}, {'email': 'ann@example.test'}, {}):
            with self.subTest(fields=fields):
                self.assertEqual(self.client.post(self.url, fields).status_code, 400)
        self.assertEqual(ServiceInterest.objects.count(), 0)

    def test_submitted_service_and_redirect_fields_cannot_change_the_service_or_destination(self):
        response = self.client.post(self.url, {
            **self.fields, 'service': 'competition', 'next': 'https://evil.example/'})
        self.assertEqual(ServiceInterest.objects.get().service, 'panel')
        self.assertNotIn('Location', response)
        self.assertNotContains(response, 'evil.example')

    def test_csrf_is_required_even_for_anonymous_signups(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(self.url, self.fields)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ServiceInterest.objects.count(), 0)

    def test_a_released_or_unknown_service_cannot_accept_interest(self):
        for url in ('/classroom/notify/', '/unknown/notify/'):
            self.assertEqual(self.client.post(url, self.fields).status_code, 404)
        self.assertEqual(ServiceInterest.objects.count(), 0)

    @override_settings(LIVEGRADE_INTEREST_RATE_LIMIT=2)
    def test_limit_is_shared_across_services_and_cannot_be_bypassed_with_a_forwarded_header(self):
        self.client.post(self.url, self.fields)
        self.client.post('/showcase/notify/', self.fields)
        response = self.client.post('/competition/notify/', self.fields, HTTP_X_FORWARDED_FOR='192.0.2.50')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '600')
        self.assertEqual(ServiceInterest.objects.count(), 2)
        other = self.client.post('/competition/notify/', self.fields, REMOTE_ADDR='192.0.2.51')
        self.assertEqual(other.status_code, 200)

    def test_rate_limiter_failure_cannot_store_an_unbounded_signup(self):
        with mock.patch('portal.views.allow_service_interest', side_effect=ConnectionError('unavailable')):
            response = self.client.post(self.url, self.fields)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(ServiceInterest.objects.count(), 0)
        self.assertEqual(response['Retry-After'], '60')

    def test_database_refuses_an_unconsented_interest(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ServiceInterest.objects.create(email='ann@example.test', service='panel', consent=False)

    def test_cache_key_does_not_retain_the_raw_client_address(self):
        request = RequestFactory().post(self.url, REMOTE_ADDR='192.0.2.100')
        with mock.patch('portal.throttling.cache.add', return_value=True) as add:
            self.assertTrue(allow_service_interest(request))
        self.assertNotIn('192.0.2.100', add.call_args.args[0])

    def test_expiring_counter_can_restart_and_respects_another_workers_restart(self):
        request = RequestFactory().post(self.url)
        for restarted in (True, False):
            with self.subTest(restarted=restarted), \
                    mock.patch('portal.throttling.cache.add', side_effect=[False, restarted]), \
                    mock.patch('portal.throttling.cache.incr', side_effect=ValueError):
                self.assertEqual(allow_service_interest(request), restarted)
