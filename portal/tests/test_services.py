"""The service registry: what the services are and which of them are open."""
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from portal.converters import ServiceSlugConverter
from portal.services import (
    SERVICES, SLUGS, dashboard_url, get_service, is_released, released_services,
)

VOCABULARY_KEYS = {'session', 'participants', 'entries', 'criteria', 'owner'}


class RegistryTests(SimpleTestCase):

    def test_there_are_five_services_in_a_fixed_order(self):
        self.assertEqual(SLUGS, ('assessment', 'showcase', 'team', 'competition', 'polls'))
        self.assertEqual([s.slug for s in SERVICES], list(SLUGS))

    def test_every_service_is_fully_described(self):
        for service in SERVICES:
            with self.subTest(service=service.slug):
                self.assertTrue(service.name)
                self.assertTrue(service.promise.endswith('.'))
                self.assertTrue(service.audience)
                self.assertEqual(set(service.vocabulary), VOCABULARY_KEYS)
                self.assertTrue(all(service.vocabulary.values()))

    def test_slugs_are_safe_to_put_in_an_address(self):
        for slug in SLUGS:
            self.assertRegex(slug, r'^[a-z]+$')

    def test_the_vocabulary_cannot_be_changed_at_run_time(self):
        with self.assertRaises(TypeError):
            get_service('assessment').vocabulary['session'] = 'x'

    def test_a_service_cannot_be_reassigned(self):
        with self.assertRaises(Exception):
            get_service('assessment').dashboard = 'somewhere'

    def test_each_service_speaks_its_own_language(self):
        self.assertEqual(get_service('assessment').vocabulary['participants'], 'participants')
        self.assertEqual(get_service('showcase').vocabulary['entries'], 'exhibits')
        self.assertEqual(get_service('competition').vocabulary['owner'], 'director')
        self.assertEqual(get_service('polls').name, 'Anonymous Polls')


class LookupTests(SimpleTestCase):

    def test_a_registered_slug_is_found(self):
        self.assertEqual(get_service('assessment').name, 'Assessments')

    def test_anything_else_is_not_a_service(self):
        for value in ('nope', '', 'Assessment', ' assessment', 'assessment/', None, 5, ['assessment']):
            with self.subTest(value=value):
                self.assertIsNone(get_service(value))


@override_settings(LIVEGRADE_LIVE_SERVICES=['assessment'])
class ReleaseTests(SimpleTestCase):

    def test_assessment_is_open_by_default(self):
        self.assertTrue(is_released('assessment'))
        self.assertEqual([s.slug for s in released_services()], ['assessment'])

    def test_a_service_that_is_not_switched_on_is_closed(self):
        for slug in ('showcase', 'team', 'competition', 'polls'):
            self.assertFalse(is_released(slug))

    def test_a_service_opens_when_it_has_a_dashboard_and_is_switched_on(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=['assessment', 'polls']):
            self.assertTrue(is_released('polls'))
            self.assertEqual([s.slug for s in released_services()], ['assessment', 'polls'])

    def test_a_service_can_be_switched_off(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=[]):
            self.assertFalse(is_released('assessment'))
            self.assertEqual(released_services(), [])

    def test_an_unknown_name_in_the_setting_opens_nothing(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=['assessment', 'bogus']):
            self.assertFalse(is_released('bogus'))
            self.assertFalse(is_released(None))

class DashboardTests(SimpleTestCase):

    def test_assessment_lands_on_its_dashboard(self):
        self.assertEqual(dashboard_url(get_service('assessment')), reverse('assessment_dashboard'))

    def test_every_service_lands_on_its_existing_workspace(self):
        self.assertEqual({s.slug: dashboard_url(s) for s in SERVICES}, {
            'assessment': reverse('assessment_dashboard'),
            'showcase': reverse('showcase_event_list'),
            'team': reverse('training_series_list'),
            'competition': reverse('competition_list'),
            'polls': reverse('standalone_poll_list'),
        })


class ConverterTests(SimpleTestCase):

    def test_it_matches_exactly_the_registered_slugs(self):
        import re

        pattern = re.compile(f'^(?:{ServiceSlugConverter.regex})$')
        for slug in SLUGS:
            self.assertTrue(pattern.match(slug))
        for other in ('', 'class', 'classrooms', 'Assessment', 'assessments', 'oidc', 'assess', 'healthz'):
            self.assertFalse(pattern.match(other), other)

    def test_it_passes_the_value_through_both_ways(self):
        converter = ServiceSlugConverter()
        self.assertEqual(converter.to_python('assessment'), 'assessment')
        self.assertEqual(converter.to_url('assessment'), 'assessment')
