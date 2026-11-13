"""The entry address: opening /classroom/ is the act of choosing Classroom."""
from django.conf import settings
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from portal import views
from portal.models import ServiceAccess
from portal.views import CHOICE_KEY

SIGN_IN = '/oidc/authenticate/'
UNRELEASED = ('panel', 'showcase', 'team', 'competition')


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
class EntryTestCase(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.ann = make_user('ann')


class VisitorTests(EntryTestCase):

    def test_a_visitor_is_sent_to_sign_in_and_on_to_the_dashboard(self):
        response = self.client.get('/classroom/')
        self.assertRedirects(response, SIGN_IN, fetch_redirect_response=False)

    def test_the_choice_is_remembered_for_the_sign_in_that_follows(self):
        self.client.get('/classroom/')
        self.assertEqual(self.client.session[CHOICE_KEY], 'classroom')

    def test_registration_keeps_the_choice_and_passes_only_the_signup_hint(self):
        response = self.client.get('/classroom/?signup=1&next=https://evil.example/')
        self.assertRedirects(response, '/oidc/authenticate/?signup=1', fetch_redirect_response=False)
        self.assertEqual(self.client.session[CHOICE_KEY], 'classroom')

    def test_nothing_is_recorded_for_somebody_who_is_not_signed_in(self):
        self.client.get('/classroom/')
        self.assertEqual(ServiceAccess.objects.count(), 0)

    def test_the_address_works_with_the_name_from_the_url_helper(self):
        self.assertEqual(reverse('service_entry', kwargs={'slug': 'classroom'}), '/classroom/')

    def test_a_returned_address_in_the_query_is_ignored(self):
        for hostile in ('https://evil.example/', '//evil.example/', '/assess/x/', 'javascript:alert(1)'):
            with self.subTest(hostile=hostile):
                response = self.client.get('/classroom/', {'next': hostile})
                self.assertEqual(response['Location'], SIGN_IN)

    def test_the_destination_never_comes_from_the_request(self):
        response = self.client.get('/classroom/?service=panel&slug=panel', HTTP_REFERER='https://evil.example/')
        self.assertEqual(response['Location'], SIGN_IN)
        self.assertEqual(self.client.session[CHOICE_KEY], 'classroom')


class SignedInTests(EntryTestCase):

    def test_a_lecturer_goes_straight_to_the_dashboard(self):
        sign_in(self.client, self.ann, self.org)
        response = self.client.get('/classroom/')
        self.assertRedirects(response, '/classroom/app/', fetch_redirect_response=False)

    def test_the_choice_is_recorded(self):
        sign_in(self.client, self.ann, self.org)
        self.client.get('/classroom/')
        access = ServiceAccess.objects.get()
        self.assertEqual((access.user, access.service), (self.ann, 'classroom'))

    def test_coming_back_refreshes_the_one_row(self):
        sign_in(self.client, self.ann, self.org)
        self.client.get('/classroom/')
        first = ServiceAccess.objects.get()
        self.client.get('/classroom/')
        self.assertEqual(ServiceAccess.objects.count(), 1)
        self.assertGreaterEqual(ServiceAccess.objects.get().last_used, first.last_used)

    def test_the_dashboard_it_leads_to_opens(self):
        sign_in(self.client, self.ann, self.org)
        self.assertEqual(self.client.get('/classroom/', follow=True).status_code, 200)

    def test_somebody_with_no_right_to_run_livegrade_is_refused_and_nothing_is_recorded(self):
        sign_in(self.client, self.ann)
        response = self.client.get('/classroom/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ServiceAccess.objects.count(), 0)


class ComingSoonTests(EntryTestCase):

    def test_a_service_that_is_not_open_shows_its_own_page_to_a_visitor(self):
        for slug in UNRELEASED:
            with self.subTest(slug=slug):
                response = self.client.get(f'/{slug}/')
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, 'portal/coming_soon.html')
                self.assertContains(response, 'is coming soon')

    def test_it_names_the_service_and_says_what_it_will_do(self):
        response = self.client.get('/panel/')
        self.assertContains(response, 'Panel is coming soon')
        self.assertContains(response, 'invite examiners')

    def test_a_signed_in_person_sees_the_same_page_and_nothing_is_recorded(self):
        sign_in(self.client, self.ann, self.org)
        for slug in UNRELEASED:
            self.assertContains(self.client.get(f'/{slug}/'), 'is coming soon')
        self.assertEqual(ServiceAccess.objects.count(), 0)

    def test_no_choice_is_remembered_for_a_service_that_cannot_be_entered(self):
        self.client.get('/panel/')
        self.assertNotIn(CHOICE_KEY, self.client.session)

    @override_settings(LIVEGRADE_LIVE_SERVICES=[])
    def test_classroom_too_shows_coming_soon_when_it_is_switched_off(self):
        self.assertContains(self.client.get('/classroom/'), 'Classroom is coming soon')

    @override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
    def test_a_service_dashboard_remains_closed_until_its_release_switch_is_set(self):
        self.assertContains(self.client.get('/panel/'), 'Panel is coming soon')

    def test_the_page_offers_a_way_back_and_names_no_other_service(self):
        response = self.client.get('/panel/')
        self.assertContains(response, 'href="/"')
        for other in ('Classroom', 'Showcase', 'Competition'):
            self.assertNotContains(response, other)


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'competition', 'team', 'showcase'])
class ServiceEntryAvailabilityTests(EntryTestCase):

    def test_signed_in_owner_can_enter_real_service_dashboard_from_service_card(self):
        sign_in(self.client, self.ann, self.org)
        response = self.client.get('/panel/', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], '/panel/app/hearings/')
        self.assertContains(response, 'Panel assessments')
        self.assertTrue(ServiceAccess.objects.filter(user=self.ann, service='panel').exists())

    def test_anonymous_service_choice_survives_sign_in_redirect(self):
        response = self.client.get('/showcase/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/oidc/authenticate/', response['Location'])
        self.assertEqual(self.client.session[CHOICE_KEY], 'showcase')

    def test_each_service_entry_reaches_its_real_owner_workspace(self):
        sign_in(self.client, self.ann, self.org)
        expected = {
            'panel': ('/panel/app/hearings/', 'Panel assessments'),
            'competition': ('/competition/app/', 'Your competitions'),
            'team': ('/team/series/', 'Your training series'),
            'showcase': ('/showcase/organizer/', 'Showcase'),
        }
        for slug, (path, heading) in expected.items():
            with self.subTest(service=slug):
                response = self.client.get(f'/{slug}/', follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.redirect_chain[-1][0], path)
                self.assertContains(response, heading)


class RouteTests(EntryTestCase):

    def test_an_address_that_is_not_a_service_is_not_found(self):
        for path in ('/nope/', '/Classroom/', '/classrooms/', '/class/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_entry_cannot_be_posted_to(self):
        self.assertEqual(self.client.post('/classroom/').status_code, 405)
        self.assertEqual(self.client.post('/panel/').status_code, 405)

    def test_the_address_without_its_trailing_slash_is_sent_to_the_one_with_it(self):
        response = self.client.get('/classroom')
        self.assertRedirects(response, '/classroom/', status_code=301, fetch_redirect_response=False)

    def test_a_head_request_is_answered_like_a_get(self):
        self.assertEqual(self.client.head('/classroom/').status_code, 302)

    def test_the_answers_are_not_cached(self):
        for path in ('/classroom/', '/panel/'):
            with self.subTest(path=path):
                self.assertIn('no-store', self.client.get(path)['Cache-Control'])

    def test_the_other_routes_are_not_shadowed(self):
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get('/no-access/').status_code, 403)
        self.assertTrue(self.client.get('/oidc/authenticate/')['Location'].startswith(settings.OIDC_OP_AUTHORIZATION_ENDPOINT))
        self.assertEqual(self.client.get('/assess/00000000-0000-4000-8000-000000000000/join/').status_code, 404)

    def test_the_view_refuses_a_slug_the_registry_does_not_know(self):
        # The route only matches registered slugs; the view does not rely on that alone.
        with self.assertRaises(Http404):
            views.entry(RequestFactory().get('/nope/'), 'nope')

    def test_the_standard_security_headers_are_on_the_answers(self):
        response = self.client.get('/panel/')
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        policy_header = ('Content-Security-Policy' if settings.CSP_ENFORCE
                         else 'Content-Security-Policy-Report-Only')
        self.assertIn('frame-ancestors', response[policy_header])


class LegacyClassroomRouteTests(EntryTestCase):

    def test_every_owner_route_has_a_method_preserving_legacy_redirect(self):
        from assessments.urls import urlpatterns

        for pattern in urlpatterns:
            arguments = {name: 123 for name in pattern.pattern.converters}
            destination = reverse(pattern.name, kwargs=arguments)
            legacy = destination.replace('/classroom/app/', '/assessments/', 1)
            with self.subTest(route=pattern.name):
                response = self.client.get(legacy)
                self.assertEqual(response.status_code, 308)
                self.assertEqual(response['Location'], destination)

    def test_an_arbitrary_legacy_suffix_cannot_choose_a_new_destination(self):
        for path in ('/assessments/../panel/', '/assessments/https://evil.example/',
                     '/assessments//evil.example/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_old_classroom_paths_permanently_redirect_and_keep_the_query(self):
        for old_path, new_path in (
            ('/assessments/', '/classroom/app/'),
            ('/assessments/12/results/?page=2&status=full',
             '/classroom/app/12/results/?page=2&status=full'),
            ('/assessments?scope=all', '/classroom/app/?scope=all'),
        ):
            with self.subTest(old_path=old_path):
                response = self.client.get(old_path)
                self.assertEqual(response.status_code, 308)
                self.assertEqual(response['Location'], new_path)

    def test_post_redirect_preserves_the_method_body_and_query(self):
        from assessments.models import AssessmentSession

        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Demo day', created_by=self.ann)
        sign_in(self.client, self.ann, self.org)
        response = self.client.post(
            f'/assessments/{session.pk}/set-timer/?source=legacy',
            {'duration_seconds': '300'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain,
                         [(f'/classroom/app/{session.pk}/set-timer/?source=legacy', 308)])
        session.refresh_from_db()
        self.assertEqual(session.default_turn_seconds, 300)
