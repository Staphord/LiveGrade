"""The page for an account DevPerf signed in but LiveGrade refused: it must offer a way out."""
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.oidc import devperf_switch_account_url
from accounts.testing import make_user, sign_in


@override_settings(DEVPERF_URL='https://devperf.example.test', LIVEGRADE_BASE_URL='https://lg.example.test',
                   OIDC_RP_CLIENT_ID='lg-client',
                   OIDC_OP_LOGOUT_ENDPOINT='https://devperf.example.test/o/logout/')
class NoAccessPageTests(TestCase):

    def test_somebody_refused_at_sign_in_is_not_shown_a_sign_out_button_that_does_nothing(self):
        response = self.client.get(reverse('no_access'))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, 'sidebar-logout-link', status_code=403)
        self.assertNotContains(response, 'Signed in with DevPerf', status_code=403)

    def test_they_can_leave_the_wrong_account_and_still_try_again(self):
        response = self.client.get(reverse('no_access'))
        self.assertContains(response, 'Use a different account', status_code=403)
        self.assertContains(response, 'Sign in again', status_code=403)
        self.assertContains(response, devperf_switch_account_url().replace('&', '&amp;'), status_code=403)

    def test_the_switch_link_goes_through_devperf_and_back_to_livegrade(self):
        parts = urlsplit(devperf_switch_account_url())
        self.assertEqual(f'{parts.scheme}://{parts.netloc}{parts.path}', 'https://devperf.example.test/o/logout/')
        query = parse_qs(parts.query)
        self.assertEqual(query['client_id'], ['lg-client'])
        self.assertEqual(query['post_logout_redirect_uri'], ['https://lg.example.test/'])

    def test_somebody_signed_in_without_any_organization_keeps_the_sign_out_button_and_gets_the_switch(self):
        sign_in(self.client, make_user('ann'))
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'sidebar-logout-link', status_code=403)
        self.assertContains(response, 'signout-modal', status_code=403)
        self.assertContains(response, 'Use a different account', status_code=403)
