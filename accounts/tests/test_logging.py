import logging
from types import SimpleNamespace

from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase

from accounts.apps import AccountsConfig
from accounts.logging import BearerUrlRedactionFilter, redact_bearer_paths


class BearerUrlRedactionTests(SimpleTestCase):
    def test_redacts_panel_competition_and_team_capability_routes(self):
        message = (
            'GET /assess/poll-invitation/poll-secret/ 302; '
            'GET /panel/examiner/panel-secret/ 404; '
            'GET /panel/results/result-secret/ 200; '
            'GET /competition/judge/judge-secret/ 403; '
            'GET /team/feedback/123e4567-e89b-12d3-a456-426614174000/ 200'
        )

        redacted = redact_bearer_paths(message)

        for secret in ('poll-secret', 'panel-secret', 'result-secret', 'judge-secret',
                       '123e4567-e89b-12d3-a456-426614174000'):
            self.assertNotIn(secret, redacted)
        self.assertEqual(redacted.count('[redacted]'), 5)

    def test_filter_redacts_message_exception_and_removes_structured_request(self):
        record = logging.LogRecord('django.request', logging.ERROR, __file__, 1,
            'Not Found: %s', ('/panel/examiner/example-secret/',), None)
        record.request = SimpleNamespace(path='/panel/examiner/example-secret/')
        try:
            raise RuntimeError('failed at /team/feedback/feedback-secret/')
        except RuntimeError:
            record.exc_info = __import__('sys').exc_info()

        self.assertTrue(BearerUrlRedactionFilter().filter(record))

        self.assertNotIn('example-secret', record.getMessage())
        self.assertNotIn('feedback-secret', record.exc_text)
        self.assertIsNone(record.request)
        self.assertIsNone(record.exc_info)

    def test_filter_safely_formats_non_string_messages_and_existing_exception_text(self):
        record = logging.LogRecord('django.server', logging.WARNING, __file__, 1,
            17, (), None)
        record.exc_text = 'Request /competition/judge/secret-17/ was refused.'

        self.assertTrue(BearerUrlRedactionFilter().filter(record))

        self.assertEqual(record.getMessage(), '17')
        self.assertNotIn('secret-17', record.exc_text)

    def test_filter_falls_back_when_a_log_format_has_missing_arguments(self):
        record = logging.LogRecord('django.request', logging.WARNING, __file__, 1,
            'Request %s failed for %s', ('/panel/examiner/private-capability/',), None)

        self.assertTrue(BearerUrlRedactionFilter().filter(record))

        self.assertNotIn('private-capability', record.getMessage())
        self.assertNotIn('/panel/examiner/', record.getMessage())

    def test_accounts_app_installs_filter_once_on_django_request_and_server(self):
        app = AccountsConfig('accounts', __import__('accounts'))
        app.ready()
        app.ready()

        for name in ('django.request', 'django.server'):
            filters = [item for item in logging.getLogger(name).filters
                       if isinstance(item, BearerUrlRedactionFilter)]
            self.assertEqual(len(filters), 1)


class BearerUrlRequestLogTests(TestCase):
    def test_unknown_panel_invitation_token_is_not_in_django_request_log(self):
        token = 'unissued-panel-capability-should-not-be-logged'
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            with self.assertLogs('django.request', level='WARNING') as captured:
                response = Client().get(f'/panel/examiner/{token}/')

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(token, '\n'.join(captured.output))
        self.assertTrue(all(getattr(record, 'request', None) is None
                            for record in captured.records))
