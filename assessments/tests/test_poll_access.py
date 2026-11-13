from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from assessments.poll_access import public_poll_budget


@public_poll_budget
def sample_view(request, poll_uuid):
    return HttpResponse('ok')


class PublicPollBudgetTests(SimpleTestCase):
    def call(self, method, allowed):
        request = getattr(RequestFactory(), method)('/poll/')
        with patch('assessments.poll_access.allow_public_event_request', return_value=allowed) as budget:
            response = sample_view(request, 'abc')
        return response, budget

    def test_allowed_requests_reach_the_view(self):
        response, budget = self.call('get', True)
        self.assertEqual(response.content, b'ok')
        self.assertEqual(budget.call_args.kwargs['service'], 'poll-read')

    def test_submissions_use_their_own_budget(self):
        response, budget = self.call('post', True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(budget.call_args.kwargs['service'], 'poll-submit')

    def test_exhausted_budget_is_a_429_with_retry_after(self):
        response, _ = self.call('get', False)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '60')
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_unavailable_budget_store_is_a_503(self):
        response, _ = self.call('post', None)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
