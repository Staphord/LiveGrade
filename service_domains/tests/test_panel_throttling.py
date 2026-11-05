from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service_domains.throttling import (allow_panel_examiner_request,
                                         allow_unknown_bearer_request,
                                         allow_public_event_request)


@override_settings(PANEL_EXAMINER_RATE_LIMIT=2, PANEL_EXAMINER_RATE_WINDOW=30)
class PanelExaminerThrottleTests(SimpleTestCase):
    invitation = SimpleNamespace(token_digest='a' * 64)

    @patch('service_domains.throttling.cache')
    def test_first_request_starts_a_counter_without_raw_token_in_key(self, cache):
        cache.add.return_value = True
        self.assertTrue(allow_panel_examiner_request(self.invitation))
        key = cache.add.call_args.args[0]
        self.assertNotIn(self.invitation.token_digest, key)
        self.assertEqual(cache.add.call_args.kwargs['timeout'], 30)
        cache.incr.assert_not_called()

    @patch('service_domains.throttling.cache')
    def test_existing_counter_allows_within_limit_then_refuses_over_limit(self, cache):
        cache.add.return_value = False
        cache.incr.return_value = 2
        self.assertTrue(allow_panel_examiner_request(self.invitation))
        cache.incr.return_value = 3
        self.assertFalse(allow_panel_examiner_request(self.invitation))

    @patch('service_domains.throttling.cache')
    def test_expired_counter_is_restarted(self, cache):
        cache.add.side_effect = [False, True]
        cache.incr.side_effect = ValueError('counter expired')
        self.assertTrue(allow_panel_examiner_request(self.invitation))
        self.assertEqual(cache.add.call_count, 2)

    @patch('service_domains.throttling.cache')
    def test_cache_error_is_reported_as_unavailable(self, cache):
        cache.add.side_effect = RuntimeError('cache unavailable')
        self.assertIsNone(allow_panel_examiner_request(self.invitation))


@override_settings(PUBLIC_BEARER_UNKNOWN_RATE_LIMIT=2,
                   PUBLIC_BEARER_UNKNOWN_RATE_WINDOW=30)
class UnknownBearerThrottleTests(SimpleTestCase):
    request = SimpleNamespace(META={'REMOTE_ADDR': '203.0.113.8',
                                    'HTTP_X_FORWARDED_FOR': '198.51.100.4'})

    @patch('service_domains.throttling.cache')
    def test_source_address_is_hmac_keyed_and_forwarded_header_is_ignored(self, cache):
        cache.add.return_value = True
        self.assertTrue(allow_unknown_bearer_request(self.request, service='panel-examiner'))
        key = cache.add.call_args.args[0]
        self.assertNotIn('203.0.113.8', key)
        self.assertNotIn('198.51.100.4', key)
        self.assertEqual(cache.add.call_args.kwargs['timeout'], 30)

    @patch('service_domains.throttling.cache')
    def test_counter_enforces_limit_and_restarts_if_it_expires_mid_request(self, cache):
        cache.add.return_value = False
        cache.incr.return_value = 2
        self.assertTrue(allow_unknown_bearer_request(self.request, service='competition-judge'))
        cache.incr.return_value = 3
        self.assertFalse(allow_unknown_bearer_request(self.request, service='competition-judge'))
        cache.add.side_effect = [False, True]
        cache.incr.side_effect = ValueError('expired')
        self.assertTrue(allow_unknown_bearer_request(self.request, service='competition-judge'))

    @patch('service_domains.throttling.cache')
    def test_bad_source_address_and_cache_failure_fail_closed(self, cache):
        self.assertIsNone(allow_unknown_bearer_request(
            SimpleNamespace(META={'REMOTE_ADDR': None}), service='panel-examiner'))
        cache.add.side_effect = RuntimeError('cache unavailable')
        self.assertIsNone(allow_unknown_bearer_request(self.request, service='panel-examiner'))


@override_settings(PUBLIC_BEARER_UNKNOWN_RATE_LIMIT=2,
                   PUBLIC_BEARER_UNKNOWN_RATE_WINDOW=30)
class PublicEventThrottleTests(SimpleTestCase):
    request = SimpleNamespace(META={'REMOTE_ADDR': '203.0.113.8',
                                    'HTTP_X_FORWARDED_FOR': '198.51.100.4'})

    @patch('service_domains.throttling.cache')
    def test_public_counter_is_event_and_source_scoped_without_raw_ip(self, cache):
        cache.add.return_value = True
        self.assertTrue(allow_public_event_request(self.request,
            service='team-feedback', event_id='event-a'))
        first_key = cache.add.call_args.args[0]
        self.assertNotIn('203.0.113.8', first_key)
        self.assertNotIn('198.51.100.4', first_key)
        self.assertTrue(allow_public_event_request(self.request,
            service='team-feedback', event_id='event-b'))
        self.assertNotEqual(first_key, cache.add.call_args.args[0])

    @patch('service_domains.throttling.cache')
    def test_public_counter_enforces_limit_and_fails_closed_when_cache_is_unavailable(self, cache):
        cache.add.return_value = False
        cache.incr.return_value = 3
        self.assertFalse(allow_public_event_request(self.request,
            service='team-feedback', event_id='event-a'))
        cache.add.side_effect = RuntimeError('cache unavailable')
        self.assertIsNone(allow_public_event_request(self.request,
            service='team-feedback', event_id='event-a'))

    @patch('service_domains.throttling.cache')
    def test_showcase_can_use_its_crowd_sized_event_budget(self, cache):
        cache.add.return_value = False
        cache.incr.return_value = 1200
        self.assertTrue(allow_public_event_request(self.request,
            service='showcase-rating', event_id='event-a', limit=2000, window=60))
        self.assertEqual(cache.add.call_args.kwargs['timeout'], 60)
        cache.incr.return_value = 2001
        self.assertFalse(allow_public_event_request(self.request,
            service='showcase-rating', event_id='event-a', limit=2000, window=60))
