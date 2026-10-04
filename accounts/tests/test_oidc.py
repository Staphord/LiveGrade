from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.access import ORGS_KEY
from accounts.models import User
from accounts.oidc import DevPerfBackend, devperf_logout_url

CLAIMS = {
    'sub': '42', 'email': 'tea@uni.test', 'name': 'Tea Cher', 'given_name': 'Tea',
    'family_name': 'Cher', 'preferred_username': 'tea',
    'orgs': [
        {'id': 1, 'slug': 'acme', 'name': 'Acme', 'can_run_assessments': True},
        {'id': 2, 'slug': 'globex', 'name': 'Globex', 'can_run_assessments': False},
    ],
}
OP = 'https://devperf.example.test'


class BackendTests(TestCase):

    def setUp(self):
        self.backend = DevPerfBackend()

    def test_somebody_who_may_run_livegrade_somewhere_is_let_in(self):
        self.assertTrue(self.backend.verify_claims(CLAIMS))

    def test_somebody_who_may_not_run_it_anywhere_is_refused(self):
        denied = {**CLAIMS, 'orgs': [{'id': 2, 'can_run_assessments': False}]}
        self.assertFalse(self.backend.verify_claims(denied))
        self.assertFalse(self.backend.verify_claims({**CLAIMS, 'orgs': []}))

    def test_a_token_without_the_organizations_is_refused(self):
        without = {k: v for k, v in CLAIMS.items() if k != 'orgs'}
        self.assertFalse(self.backend.verify_claims(without))

    def test_a_token_without_a_subject_is_refused(self):
        without = {k: v for k, v in CLAIMS.items() if k != 'sub'}
        self.assertFalse(self.backend.verify_claims(without))

    def test_a_first_sign_in_creates_the_lecturer_from_the_claims(self):
        user = self.backend.create_user(CLAIMS)
        user.refresh_from_db()
        self.assertEqual((user.sub, user.username), ('42', 'devperf-42'))
        self.assertEqual((user.first_name, user.last_name, user.email), ('Tea', 'Cher', 'tea@uni.test'))
        self.assertFalse(user.has_usable_password())

    def test_a_returning_lecturer_is_found_by_subject_not_email(self):
        existing = User.objects.create(sub='42', username='devperf-42', email='old@uni.test')
        User.objects.create(sub='99', username='devperf-99', email='tea@uni.test')
        self.assertEqual(list(self.backend.filter_users_by_claims(CLAIMS)), [existing])

    def test_an_unknown_subject_matches_nobody(self):
        self.assertEqual(list(self.backend.filter_users_by_claims(CLAIMS)), [])

    def test_each_sign_in_refreshes_the_name_and_email(self):
        existing = User.objects.create(sub='42', username='devperf-42', email='old@uni.test')
        user = self.backend.update_user(existing, {**CLAIMS, 'email': 'new@uni.test', 'given_name': 'T'})
        existing.refresh_from_db()
        self.assertEqual((existing.email, existing.first_name), ('new@uni.test', 'T'))
        self.assertIs(user, existing)

    def test_missing_names_become_empty(self):
        user = self.backend.create_user({'sub': '5', 'orgs': CLAIMS['orgs']})
        self.assertEqual((user.first_name, user.last_name, user.email), ('', '', ''))

    def test_the_log_line_names_the_subject(self):
        self.assertEqual(self.backend.describe_user_by_claims(CLAIMS), 'sub 42')


@override_settings(DEVPERF_URL=OP, LIVEGRADE_BASE_URL='https://livegrade.example.test',
                   OIDC_RP_CLIENT_ID='client-1', OIDC_OP_AUTHORIZATION_ENDPOINT=f'{OP}/o/authorize/',
                   OIDC_OP_LOGOUT_ENDPOINT=f'{OP}/o/logout/')
class SignInFlowTests(TestCase):
    """The browser's journey through LiveGrade's sign-in, with DevPerf's
    answers stubbed at the network edge."""

    def begin(self):
        response = self.client.get(reverse('oidc_authentication_init'))
        self.assertEqual(response.status_code, 302)
        return response

    def finish(self, claims=CLAIMS):
        state = parse_qs(urlsplit(self.begin()['Location']).query)['state'][0]
        with mock.patch.object(DevPerfBackend, 'get_token',
                               return_value={'id_token': 'id.jwt', 'access_token': 'at'}), \
                mock.patch.object(DevPerfBackend, 'verify_token', return_value={'sub': claims['sub']}), \
                mock.patch.object(DevPerfBackend, 'get_userinfo', return_value=claims):
            return self.client.get(reverse('oidc_authentication_callback'),
                                   {'code': 'abc', 'state': state})

    def test_sign_in_sends_the_browser_to_devperf_with_a_pkce_challenge(self):
        location = self.begin()['Location']
        self.assertTrue(location.startswith(f'{OP}/o/authorize/?'))
        query = parse_qs(urlsplit(location).query)
        self.assertEqual(query['client_id'], ['client-1'])
        self.assertEqual(query['response_type'], ['code'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertIn('code_challenge', query)
        self.assertEqual(query['scope'], ['openid profile email livegrade'])
        self.assertTrue(query['redirect_uri'][0].endswith('/oidc/callback/'))

    def test_a_good_sign_in_creates_the_lecturer_and_remembers_where_they_may_work(self):
        response = self.finish()
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(User.objects.get().sub, '42')
        self.assertEqual(self.client.session[ORGS_KEY],
                         [{'id': 1, 'slug': 'acme', 'name': 'Acme', 'public': False, 'can_oversee': False}])
        self.assertContains(self.client.get('/assessments/'), 'Tea Cher')

    def test_a_sign_in_adds_the_person_to_the_colleague_directory(self):
        from accounts.models import OrganizationLecturer

        self.finish()
        row = OrganizationLecturer.objects.get()
        self.assertEqual((row.user.sub, row.organization_id, row.organization_name), ('42', 1, 'Acme'))

    def test_signing_in_again_without_that_organization_removes_the_row(self):
        from accounts.models import OrganizationLecturer

        self.finish()
        self.client.logout()
        self.finish({**CLAIMS, 'orgs': [{'id': 9, 'slug': 'new', 'name': 'New', 'can_run_assessments': True}]})
        self.assertEqual(list(OrganizationLecturer.objects.values_list('organization_id', flat=True)), [9])

    def test_oversight_travels_into_the_session_when_devperf_grants_it(self):
        orgs = [{'id': 1, 'slug': 'acme', 'name': 'Acme', 'can_run_assessments': True,
                 'can_oversee_assessments': True}]
        self.finish({**CLAIMS, 'orgs': orgs})
        self.assertEqual(self.client.session[ORGS_KEY][0]['can_oversee'], True)
        self.assertContains(self.client.get('/assessments/?scope=all'), 'All sessions in Acme')

    def test_without_the_claim_there_is_no_oversight(self):
        self.finish()
        self.assertNotContains(self.client.get('/assessments/?scope=all'), 'All sessions in')

    def test_only_the_organizations_where_they_may_run_it_are_kept(self):
        self.finish()
        self.assertNotIn(2, [org['id'] for org in self.client.session[ORGS_KEY]])

    def test_signing_in_again_updates_rather_than_duplicates(self):
        self.finish()
        self.client.logout()
        self.finish({**CLAIMS, 'email': 'changed@uni.test'})
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.get().email, 'changed@uni.test')

    def test_somebody_who_may_not_run_it_is_not_signed_in(self):
        response = self.finish({**CLAIMS, 'orgs': [{'id': 2, 'can_run_assessments': False}]})
        self.assertRedirects(response, '/no-access/', fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), 0)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_forged_state_is_refused(self):
        self.begin()
        response = self.client.get(reverse('oidc_authentication_callback'),
                                   {'code': 'abc', 'state': 'forged'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signing_out_ends_the_devperf_session_too_and_returns_here(self):
        self.finish()
        response = self.client.post(reverse('oidc_logout'))
        self.assertEqual(response.status_code, 302)
        target = urlsplit(response['Location'])
        self.assertEqual(f'{target.scheme}://{target.netloc}{target.path}', f'{OP}/o/logout/')
        query = parse_qs(target.query)
        self.assertEqual(query['id_token_hint'], ['id.jwt'])
        self.assertEqual(query['client_id'], ['client-1'])
        self.assertEqual(query['post_logout_redirect_uri'], ['https://livegrade.example.test/'])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signing_out_while_not_signed_in_just_goes_home(self):
        response = self.client.post(reverse('oidc_logout'))
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_signing_out_needs_a_post(self):
        self.assertEqual(self.client.get(reverse('oidc_logout')).status_code, 405)


class LogoutUrlTests(SimpleTestCase):

    @override_settings(OIDC_RP_CLIENT_ID='c', LIVEGRADE_BASE_URL='https://lg.example.test',
                       OIDC_OP_LOGOUT_ENDPOINT=f'{OP}/o/logout/')
    def test_the_return_address_is_livegrades_home(self):
        request = mock.Mock(session={'oidc_id_token': 'tok'})
        url = devperf_logout_url(request)
        self.assertIn('post_logout_redirect_uri=https%3A%2F%2Flg.example.test%2F', url)
        self.assertIn('id_token_hint=tok', url)

    @override_settings(OIDC_RP_CLIENT_ID='c', LIVEGRADE_BASE_URL='https://lg.example.test',
                       OIDC_OP_LOGOUT_ENDPOINT=f'{OP}/o/logout/')
    def test_a_session_without_a_stored_token_sends_an_empty_hint(self):
        self.assertIn('id_token_hint=&', devperf_logout_url(mock.Mock(session={})))
