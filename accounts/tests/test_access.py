from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.access import ACTIVE_KEY, ORGS_KEY, active_organization, can_oversee, usable_orgs
from accounts.context import livegrade
from accounts.models import User

ACME = {'id': 1, 'slug': 'acme', 'name': 'Acme'}
GLOBEX = {'id': 2, 'slug': 'globex', 'name': 'Globex'}


def signed_in(client, orgs=(ACME,), sub='7', **session):
    user = User.objects.create(sub=sub, username=f'devperf-{sub}', first_name='Tea', last_name='Cher')
    client.force_login(user)
    stored = client.session
    stored[ORGS_KEY] = list(orgs)
    for key, value in session.items():
        stored[key] = value
    stored.save()
    return user


class UsableOrgsTests(TestCase):

    def test_only_organizations_where_assessments_may_be_run_are_kept(self):
        orgs = [
            {'id': 1, 'slug': 'a', 'name': 'A', 'can_run_assessments': True},
            {'id': 2, 'slug': 'b', 'name': 'B', 'can_run_assessments': False},
        ]
        self.assertEqual(usable_orgs(orgs), [{'id': 1, 'slug': 'a', 'name': 'A', 'public': False, 'can_oversee': False}])

    def test_a_claim_that_is_not_a_list_grants_nothing(self):
        for claim in (None, 'acme', {'id': 1, 'can_run_assessments': True}, 5):
            with self.subTest(claim=claim):
                self.assertEqual(usable_orgs(claim), [])

    def test_malformed_entries_are_ignored_rather_than_trusted(self):
        for entry in ('x', None, {'can_run_assessments': True}, {'id': '1', 'can_run_assessments': True},
                      {'id': True, 'can_run_assessments': True}, {'id': 1, 'can_run_assessments': 'yes'},
                      {'id': 1, 'can_run_assessments': 1}, {'id': 1}):
            with self.subTest(entry=entry):
                self.assertEqual(usable_orgs([entry]), [])

    def test_oversight_is_claimed_only_with_a_literal_true(self):
        def oversee(value):
            entry = {'id': 1, 'can_run_assessments': True, 'can_oversee_assessments': value}
            return usable_orgs([entry])[0]['can_oversee']

        self.assertTrue(oversee(True))
        for not_true in (False, None, 'yes', 1, 'true', [True]):
            with self.subTest(value=not_true):
                self.assertFalse(oversee(not_true))

    def test_a_missing_oversight_claim_means_no_oversight(self):
        self.assertFalse(usable_orgs([{'id': 1, 'can_run_assessments': True}])[0]['can_oversee'])

    def test_oversight_never_stands_in_for_the_right_to_run_livegrade(self):
        self.assertEqual(usable_orgs([{'id': 1, 'can_run_assessments': False,
                                       'can_oversee_assessments': True}]), [])

    def test_a_missing_name_or_slug_becomes_empty_text(self):
        self.assertEqual(usable_orgs([{'id': 3, 'can_run_assessments': True}]),
                         [{'id': 3, 'slug': '', 'name': '', 'public': False, 'can_oversee': False}])


class CanOverseeTests(TestCase):

    def request(self, *orgs, active=None):
        request = RequestFactory().get('/')
        request.session = {ORGS_KEY: list(orgs)}
        if active is not None:
            request.session[ACTIVE_KEY] = active
        return request

    def test_it_follows_the_active_organization_only(self):
        acme = {'id': 1, 'slug': 'a', 'name': 'A', 'can_oversee': True}
        globex = {'id': 2, 'slug': 'g', 'name': 'G', 'can_oversee': False}
        self.assertTrue(can_oversee(self.request(acme, globex, active=1)))
        self.assertFalse(can_oversee(self.request(acme, globex, active=2)))

    def test_somebody_with_no_organization_oversees_nothing(self):
        self.assertFalse(can_oversee(self.request()))

    def test_a_session_from_before_oversight_existed_means_no_oversight(self):
        self.assertFalse(can_oversee(self.request({'id': 1, 'slug': 'a', 'name': 'A'})))


class ActiveOrganizationTests(TestCase):

    def request(self, **session):
        request = RequestFactory().get('/')
        request.session = session
        return request

    def test_the_first_is_used_when_none_was_chosen(self):
        self.assertEqual(active_organization(self.request(**{ORGS_KEY: [ACME, GLOBEX]})), ACME)

    def test_the_chosen_one_is_used_when_it_is_still_theirs(self):
        request = self.request(**{ORGS_KEY: [ACME, GLOBEX], ACTIVE_KEY: 2})
        self.assertEqual(active_organization(request), GLOBEX)

    def test_a_choice_that_is_no_longer_theirs_falls_back_to_the_first(self):
        request = self.request(**{ORGS_KEY: [ACME], ACTIVE_KEY: 99})
        self.assertEqual(active_organization(request), ACME)

    def test_nobody_without_organizations_has_one(self):
        self.assertIsNone(active_organization(self.request()))
        self.assertIsNone(active_organization(self.request(**{ORGS_KEY: []})))


class ContextTests(TestCase):

    def test_the_header_gets_the_organizations_and_devperf_address(self):
        request = RequestFactory().get('/')
        request.session = {ORGS_KEY: [ACME, GLOBEX], ACTIVE_KEY: 2}
        with self.settings(DEVPERF_URL='https://devperf.example.test'):
            context = livegrade(request)
        self.assertEqual(context['active_org'], GLOBEX)
        self.assertEqual([o['display_name'] for o in context['my_orgs']], ['Acme', 'Globex'])
        self.assertFalse(context['only_personal'])
        self.assertEqual(context['devperf_url'], 'https://devperf.example.test')

    def test_a_visitor_with_no_session_data_gets_empty_values(self):
        request = RequestFactory().get('/')
        request.session = {}
        context = livegrade(request)
        self.assertIsNone(context['active_org'])
        self.assertEqual(context['my_orgs'], [])


class HomeAccessTests(TestCase):

    def test_a_visitor_sees_the_public_landing_page_with_both_ways_in(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Grade presentations')
        self.assertContains(response, reverse('oidc_authentication_init') + '?signup=1')
        self.assertContains(response, 'Log in')

    def test_a_protected_page_still_sends_a_visitor_to_sign_in_and_back(self):
        response = self.client.get(reverse('assessment_session_list'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(reverse('oidc_authentication_init')))
        self.assertIn('next=', response['Location'])

    def test_a_lecturer_is_taken_to_their_sessions(self):
        signed_in(self.client)
        self.assertRedirects(self.client.get(reverse('home')), reverse('assessment_session_list'))

    def test_the_page_header_names_the_lecturer_and_organization(self):
        signed_in(self.client)
        response = self.client.get(reverse('assessment_session_list'))
        self.assertContains(response, 'Tea Cher')
        self.assertContains(response, 'Acme')
        self.assertEqual(response.wsgi_request.organization, ACME)

    def test_the_organization_switcher_appears_only_for_several(self):
        signed_in(self.client, orgs=[ACME, GLOBEX])
        self.assertContains(self.client.get(reverse('assessment_session_list')), 'name="organization"')

    def test_one_organization_is_shown_without_a_switcher(self):
        signed_in(self.client)
        self.assertNotContains(self.client.get(reverse('assessment_session_list')), 'name="organization"')

    def test_somebody_signed_in_without_an_organization_is_refused(self):
        signed_in(self.client, orgs=())
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'not available for this account', status_code=403)

    def test_the_no_access_page_opens_for_anybody(self):
        response = self.client.get(reverse('no_access'))
        self.assertContains(response, 'Sign in again', status_code=403)

    def test_the_health_check_needs_no_sign_in(self):
        self.assertEqual(self.client.get(reverse('health')).content, b'ok')

    def test_a_signed_in_name_falls_back_to_the_username(self):
        user = User(sub='1', username='devperf-1')
        self.assertEqual(user.display_name(), 'devperf-1')


class SwitchOrganizationTests(TestCase):

    def test_choosing_another_of_their_organizations_switches_to_it(self):
        signed_in(self.client, orgs=[ACME, GLOBEX])
        response = self.client.post(reverse('switch_organization'), {'organization': '2'})
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)
        self.assertEqual(self.client.session[ACTIVE_KEY], 2)

    def test_somebody_elses_organization_is_ignored(self):
        signed_in(self.client, orgs=[ACME])
        self.client.post(reverse('switch_organization'), {'organization': '2'})
        self.assertNotIn(ACTIVE_KEY, self.client.session)

    def test_nonsense_is_ignored(self):
        signed_in(self.client, orgs=[ACME])
        self.client.post(reverse('switch_organization'), {'organization': 'abc'})
        self.client.post(reverse('switch_organization'), {})
        self.assertNotIn(ACTIVE_KEY, self.client.session)

    def test_it_only_accepts_a_post(self):
        signed_in(self.client)
        self.assertEqual(self.client.get(reverse('switch_organization')).status_code, 405)

    def test_a_visitor_is_sent_to_sign_in(self):
        response = self.client.post(reverse('switch_organization'), {'organization': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(reverse('oidc_authentication_init')))


class UserModelTests(TestCase):

    def test_a_user_never_has_a_password(self):
        user = User.objects.create(sub='1', username='devperf-1')
        user.set_password('anything')
        user.save()
        self.assertFalse(user.has_usable_password())

    def test_saving_again_keeps_the_same_unusable_password_so_sessions_survive(self):
        user = User.objects.create(sub='1', username='devperf-1')
        before = user.password
        user.first_name = 'Changed'
        user.save()
        self.assertEqual(User.objects.get(pk=user.pk).password, before)

    def test_the_devperf_id_is_unique(self):
        User.objects.create(sub='1', username='devperf-1')
        with self.assertRaises(Exception):
            User.objects.create(sub='1', username='devperf-other')
