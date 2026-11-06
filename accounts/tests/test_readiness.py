from unittest.mock import patch
from django.test import TestCase


class DependencyReadinessTests(TestCase):
    def test_ready_is_separate_from_liveness_and_not_cached(self):
        self.assertEqual(self.client.get('/healthz').content, b'ok')
        response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ready'})
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_dependency_error_fails_closed_without_details(self):
        with patch('django.core.cache.cache.set', side_effect=RuntimeError('private connection detail')):
            response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertEqual(self.client.post('/readyz').status_code, 405)
