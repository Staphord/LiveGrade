"""Service choice, service-local navigation, and a useful first Classroom session."""
from io import BytesIO

from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from accounts.access import ACTIVE_KEY
from accounts.testing import make_org, make_user, sign_in
from assessments.views.setup_views import onboarding_sample
from portal.models import record_choice
from portal.services import SERVICES


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
class LandingTests(TestCase):

    def test_every_service_has_a_card_and_its_entry_address(self):
        response = self.client.get('/')
        for service in SERVICES:
            with self.subTest(service=service.slug):
                self.assertContains(response, service.name)
                if service.slug == 'classroom':
                    self.assertContains(response, f'href="/{service.slug}/"')
                else:
                    self.assertNotContains(response, f'href="/{service.slug}/"')
        self.assertContains(response, 'Open Classroom', count=1)
        release_state = {card['service'].slug: card['released'] for card in response.context['service_cards']}
        self.assertEqual(release_state, {service.slug: service.slug == 'classroom' for service in SERVICES})
        self.assertContains(response, 'href="/oidc/authenticate/?signup=1"')
        self.assertNotContains(response, 'Sign up for Classroom')

    def test_a_returning_person_sees_only_their_released_service_shortcuts(self):
        org, user = make_org('Uni'), make_user('ann')
        record_choice(user, 'classroom')
        record_choice(user, 'panel')
        sign_in(self.client, user, org)
        response = self.client.get('/')
        self.assertContains(response, 'Continue to Classroom')
        self.assertNotContains(response, 'Continue to Panel')
        self.assertEqual([s.slug for s in response.context['used_services']], ['classroom'])

    def test_service_shortcuts_do_not_leak_to_another_person(self):
        org = make_org('Uni')
        record_choice(make_user('ann'), 'classroom')
        sign_in(self.client, make_user('ben'), org)
        self.assertNotContains(self.client.get('/'), 'Continue with your services')

    def test_cards_follow_the_release_configuration(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=[]):
            response = self.client.get('/')
        release_state = {card['service'].slug: card['released'] for card in response.context['service_cards']}
        self.assertEqual(release_state, {service.slug: False for service in SERVICES})

    def test_enabled_service_cards_link_to_real_service_entries(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=[service.slug for service in SERVICES]):
            response = self.client.get('/')
        for service in SERVICES:
            self.assertContains(response, f'Open {service.name}')
        self.assertNotContains(response, 'Sign up for Panel')
        self.assertNotContains(response, 'Sign up for Competition')
        self.assertNotContains(response, 'Sign up for Team and Training')
        self.assertNotContains(response, 'Sign up for Showcase')
        self.assertContains(response, 'href="/panel/"')

    def test_coassessment_landing_promotion_is_removed_but_service_card_remains(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=[service.slug for service in SERVICES]):
            response = self.client.get('/')

        self.assertContains(response, 'Open Co-Assessment')
        self.assertNotContains(response, 'combined-workflow-heading')
        self.assertNotContains(response, 'Need both peer and panel grading?')
        self.assertNotContains(response, 'Run a synchronized co-assessment with shared rubrics')


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
class ClassroomShellTests(TestCase):

    def setUp(self):
        self.org, self.other = make_org('Uni'), make_org('College')
        self.user = make_user('ann')
        sign_in(self.client, self.user, self.org, self.other)

    def test_shell_names_only_its_service_and_keeps_the_switcher_in_that_service(self):
        response = self.client.get(reverse('assessment_dashboard'))
        self.assertContains(response, 'Classroom dashboard')
        self.assertContains(response, 'action="/classroom/switch-organization/"')
        for slug in ('panel', 'showcase', 'team', 'competition'):
            self.assertNotContains(response, f'href="/{slug}/"')
        self.assertNotContains(response, 'Continue with your services')
        self.assertEqual(response.context['current_service'].slug, 'classroom')
        self.assertEqual(response.context['service_vocabulary']['participants'], 'students')

    def test_switching_organization_returns_to_the_same_service(self):
        response = self.client.post('/classroom/switch-organization/', {'organization': self.other.pk})
        self.assertRedirects(response, '/classroom/app/', fetch_redirect_response=False)
        self.assertEqual(self.client.session[ACTIVE_KEY], self.other.pk)

    def test_a_foreign_organization_cannot_be_selected(self):
        foreign = make_org('Foreign')
        self.client.post('/classroom/switch-organization/', {'organization': foreign.pk})
        self.assertNotEqual(self.client.session.get(ACTIVE_KEY), foreign.pk)

    def test_an_unreleased_service_cannot_change_the_active_organization(self):
        response = self.client.post('/panel/switch-organization/', {'organization': self.other.pk})
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(self.client.session.get(ACTIVE_KEY), self.other.pk)

    def test_empty_dashboard_links_to_both_synthetic_sample_files(self):
        response = self.client.get(reverse('assessment_session_list'))
        for name in ('assessment_group_onboarding_sample', 'assessment_rubric_onboarding_sample'):
            self.assertContains(response, reverse(name))

    def test_sample_workbooks_are_valid_and_contain_only_synthetic_examples(self):
        for name, first_header in (('assessment_group_onboarding_sample', 'Group Name'),
                                   ('assessment_rubric_onboarding_sample', 'Category')):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertIn('attachment;', response['Content-Disposition'])
                workbook = load_workbook(BytesIO(response.content))
                self.assertEqual(workbook.active['A1'].value, first_header)
                workbook.close()

    def test_sample_download_refuses_missing_service_scope(self):
        request = RequestFactory().get('/')
        request.user = self.user
        request.session = self.client.session
        from django.http import Http404
        with self.assertRaises(Http404):
            onboarding_sample(request, 'groups')
