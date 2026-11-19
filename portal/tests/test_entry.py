"""Service entry behavior for the current service registry and route layout."""
from django.conf import settings
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from portal import views
from portal.models import ServiceAccess
from portal.services import CHOICE_KEY, SERVICES

SIGN_IN = reverse('oidc_authentication_init')
ALL_SERVICES = [service.slug for service in SERVICES]
RELEASED_BY_DEFAULT = ['assessment', 'polls']
NOT_RELEASED_BY_DEFAULT = ['showcase', 'team', 'competition']


@override_settings(LIVEGRADE_LIVE_SERVICES=RELEASED_BY_DEFAULT)
class EntryTestCase(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.user = make_user('ann')

    def entry_url(self, slug='assessment'):
        return reverse('service_entry', kwargs={'slug': slug})


class VisitorTests(EntryTestCase):

    def test_a_visitor_is_sent_to_sign_in_from_the_selected_service(self):
        response = self.client.get(self.entry_url())
        self.assertRedirects(response, SIGN_IN, fetch_redirect_response=False)

    def test_the_service_choice_is_remembered_for_sign_in(self):
        self.client.get(self.entry_url('polls'))
        self.assertEqual(self.client.session[CHOICE_KEY], 'polls')

    def test_registration_keeps_the_choice_and_ignores_a_supplied_destination(self):
        response = self.client.get(self.entry_url() + '?signup=1&next=https://evil.example/')
        self.assertRedirects(response, SIGN_IN + '?signup=1', fetch_redirect_response=False)
        self.assertEqual(self.client.session[CHOICE_KEY], 'assessment')

    def test_a_return_address_in_the_query_is_ignored(self):
        for hostile in ('https://evil.example/', '//evil.example/', '/assess/x/',
                        'javascript:alert(1)'):
            with self.subTest(hostile=hostile):
                response = self.client.get(self.entry_url(), {'next': hostile})
                self.assertEqual(response['Location'], SIGN_IN)

    def test_nothing_is_recorded_for_a_visitor(self):
        self.client.get(self.entry_url())
        self.assertEqual(ServiceAccess.objects.count(), 0)


class SignedInTests(EntryTestCase):

    def setUp(self):
        super().setUp()
        sign_in(self.client, self.user, self.org)

    def test_a_lecturer_goes_to_the_assessment_dashboard(self):
        response = self.client.get(self.entry_url())
        self.assertRedirects(response, reverse('assessment_dashboard'), fetch_redirect_response=False)

    def test_the_service_choice_is_recorded(self):
        self.client.get(self.entry_url())
        access = ServiceAccess.objects.get()
        self.assertEqual((access.user, access.service), (self.user, 'assessment'))

    def test_returning_to_the_service_refreshes_its_existing_record(self):
        self.client.get(self.entry_url())
        first = ServiceAccess.objects.get()
        self.client.get(self.entry_url())
        self.assertEqual(ServiceAccess.objects.count(), 1)
        self.assertGreaterEqual(ServiceAccess.objects.get().last_used, first.last_used)

    def test_the_destination_dashboard_opens(self):
        response = self.client.get(self.entry_url(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], reverse('assessment_dashboard'))

    def test_a_person_without_a_livegrade_organization_is_refused(self):
        other_client = self.client_class()
        sign_in(other_client, self.user)
        response = other_client.get(self.entry_url())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ServiceAccess.objects.count(), 0)


class ComingSoonTests(EntryTestCase):

    def test_registered_services_without_a_release_switch_show_coming_soon(self):
        for slug in NOT_RELEASED_BY_DEFAULT:
            with self.subTest(slug=slug):
                response = self.client.get(self.entry_url(slug))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, 'portal/coming_soon.html')
                self.assertContains(response, 'is coming soon')

    def test_a_signed_in_person_sees_the_same_page_without_a_service_record(self):
        sign_in(self.client, self.user, self.org)
        for slug in NOT_RELEASED_BY_DEFAULT:
            with self.subTest(slug=slug):
                self.assertContains(self.client.get(self.entry_url(slug)), 'is coming soon')
        self.assertEqual(ServiceAccess.objects.count(), 0)

    def test_no_choice_is_saved_for_a_service_that_is_not_open(self):
        self.client.get(self.entry_url('showcase'))
        self.assertNotIn(CHOICE_KEY, self.client.session)

    @override_settings(LIVEGRADE_LIVE_SERVICES=[])
    def test_an_open_service_shows_coming_soon_when_switched_off(self):
        response = self.client.get(self.entry_url())
        self.assertContains(response, 'Assessments is coming soon')

    def test_the_page_links_back_to_livegrade(self):
        response = self.client.get(self.entry_url('showcase'))
        self.assertContains(response, 'href="/"')
        self.assertContains(response, 'Showcase is coming soon')


@override_settings(LIVEGRADE_LIVE_SERVICES=ALL_SERVICES)
class ServiceEntryAvailabilityTests(EntryTestCase):

    def test_every_released_service_reaches_its_registered_dashboard(self):
        sign_in(self.client, self.user, self.org)
        for service in SERVICES:
            with self.subTest(service=service.slug):
                response = self.client.get(self.entry_url(service.slug), follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.redirect_chain[-1][0], reverse(service.dashboard))
                self.assertTrue(ServiceAccess.objects.filter(
                    user=self.user, service=service.slug).exists())

    def test_an_enabled_service_entry_is_not_chosen_from_request_data(self):
        sign_in(self.client, self.user, self.org)
        response = self.client.get(
            self.entry_url('assessment') + '?service=competition&slug=competition',
            HTTP_REFERER='https://evil.example/')
        self.assertRedirects(response, reverse('assessment_dashboard'), fetch_redirect_response=False)
        self.assertTrue(ServiceAccess.objects.filter(user=self.user, service='assessment').exists())
        self.assertFalse(ServiceAccess.objects.filter(user=self.user, service='competition').exists())


class RouteTests(EntryTestCase):

    def test_only_registered_service_slugs_are_entry_routes(self):
        self.assertEqual(set(ALL_SERVICES), {'assessment', 'showcase', 'team', 'competition', 'polls'})
        for path in ('/nope/', '/Assessment/', '/assessments/', '/classroom/', '/panel/',
                     '/coassessment/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_entry_address_does_not_accept_post(self):
        self.assertEqual(self.client.post(self.entry_url()).status_code, 405)

    def test_the_entry_address_accepts_head_like_get(self):
        self.assertEqual(self.client.head(self.entry_url()).status_code, 302)

    def test_the_address_without_its_trailing_slash_is_canonicalized(self):
        self.assertRedirects(self.client.get('/assessment'), '/assessment/',
                             status_code=301, fetch_redirect_response=False)

    def test_service_entry_responses_are_not_cached(self):
        for path in (self.entry_url(), self.entry_url('showcase')):
            with self.subTest(path=path):
                self.assertIn('no-store', self.client.get(path)['Cache-Control'])

    def test_other_application_routes_are_not_shadowed(self):
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get(reverse('no_access')).status_code, 403)
        self.assertTrue(self.client.get(SIGN_IN)['Location'].startswith(
            settings.OIDC_OP_AUTHORIZATION_ENDPOINT))
        self.assertEqual(
            self.client.get('/assess/00000000-0000-4000-8000-000000000000/join/').status_code,
            404)

    def test_the_view_rejects_a_slug_outside_the_registry(self):
        with self.assertRaises(Http404):
            views.entry(RequestFactory().get('/nope/'), 'nope')

    def test_standard_security_headers_are_on_entry_responses(self):
        response = self.client.get(self.entry_url('showcase'))
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        policy_header = ('Content-Security-Policy' if settings.CSP_ENFORCE
                         else 'Content-Security-Policy-Report-Only')
        self.assertIn('frame-ancestors', response[policy_header])
