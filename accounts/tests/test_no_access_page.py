"""The page for an account DevPerf signed in but LiveGrade refused: it must offer a way out."""
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.oidc import SWITCH_ACCOUNT_STATE, devperf_logout_url
from accounts.testing import make_user, sign_in

SETTINGS = dict(DEVPERF_URL='https://devperf.example.test', LIVEGRADE_BASE_URL='https://lg.example.test',
                OIDC_RP_CLIENT_ID='lg-client', OIDC_OP_LOGOUT_ENDPOINT='https://devperf.example.test/o/logout/')


@override_settings(**SETTINGS)
class NoAccessPageTests(TestCase):

    def test_somebody_refused_at_sign_in_is_not_shown_a_sign_out_button_that_does_nothing(self):
        response = self.client.get(reverse('no_access'))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, 'sidebar-logout-link', status_code=403)
        self.assertNotContains(response, 'class="sidebar"', status_code=403)
        self.assertNotContains(response, 'Signed in with DevPerf', status_code=403)

    def test_the_only_choices_are_to_switch_account_or_go_home(self):
        response = self.client.get(reverse('no_access'))
        self.assertContains(response, f'action="{reverse("switch_account")}"', status_code=403)
        self.assertContains(response, 'Use a different account', status_code=403)
        self.assertContains(response, 'Back to the home page', status_code=403)
        self.assertNotContains(response, 'Sign in again', status_code=403)

    def test_somebody_signed_in_without_any_organization_gets_the_switch_but_no_home_button(self):
        sign_in(self.client, make_user('ann'))
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, f'action="{reverse("switch_account")}"', status_code=403)
        self.assertNotContains(response, 'Back to the home page', status_code=403)


@override_settings(**SETTINGS)
class SwitchAccountTests(TestCase):

    def test_it_ends_the_session_and_sends_devperf_the_signed_hint_so_nobody_is_asked(self):
        sign_in(self.client, make_user('ann'))
        session = self.client.session
        session['oidc_id_token'] = 'signed-hint'
        session.save()
        response = self.client.post(reverse('switch_account'))
        parts = urlsplit(response['Location'])
        self.assertEqual(f'{parts.scheme}://{parts.netloc}{parts.path}', 'https://devperf.example.test/o/logout/')
        query = parse_qs(parts.query)
        self.assertEqual(query['id_token_hint'], ['signed-hint'])
        self.assertEqual(query['client_id'], ['lg-client'])
        self.assertEqual(query['post_logout_redirect_uri'], ['https://lg.example.test/'])
        self.assertEqual(query['state'], [SWITCH_ACCOUNT_STATE])
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotIn('oidc_id_token', self.client.session)

    def test_somebody_who_was_never_signed_in_here_can_still_switch(self):
        session = self.client.session
        session['oidc_id_token'] = 'signed-hint'
        session.save()
        response = self.client.post(reverse('switch_account'))
        self.assertEqual(parse_qs(urlsplit(response['Location']).query)['id_token_hint'], ['signed-hint'])
        self.assertNotIn('oidc_id_token', self.client.session)

    def test_a_plain_link_cannot_do_it(self):
        self.assertEqual(self.client.get(reverse('switch_account')).status_code, 405)

    def test_coming_back_from_devperf_goes_straight_to_the_sign_in_page(self):
        response = self.client.get(reverse('home'), {'state': SWITCH_ACCOUNT_STATE})
        self.assertRedirects(response, reverse('oidc_authentication_init'), fetch_redirect_response=False)

    def test_an_ordinary_visit_to_the_home_page_still_shows_the_landing_page(self):
        self.assertContains(self.client.get(reverse('home'), {'state': 'something-else'}), 'Grade presentations')

    def test_an_ordinary_sign_out_link_carries_no_marker(self):
        session = self.client.session
        session['oidc_id_token'] = 'signed-hint'
        session.save()
        request = self.client.get(reverse('home')).wsgi_request
        self.assertNotIn('state=', devperf_logout_url(request))
