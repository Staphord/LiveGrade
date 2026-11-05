"""The service registry: what the services are and which of them are open."""
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from portal.converters import ServiceSlugConverter
from portal.services import (
    SERVICES, SLUGS, dashboard_url, get_service, is_released, released_services,
)

VOCABULARY_KEYS = {'session', 'participants', 'entries', 'criteria', 'owner'}


class RegistryTests(SimpleTestCase):

    def test_there_are_six_services_in_a_fixed_order(self):
        self.assertEqual(SLUGS, ('classroom', 'panel', 'showcase', 'team', 'competition', 'polls'))
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
            get_service('classroom').vocabulary['session'] = 'x'

    def test_a_service_cannot_be_reassigned(self):
        with self.assertRaises(Exception):
            get_service('classroom').dashboard = 'somewhere'

    def test_each_service_speaks_its_own_language(self):
        self.assertEqual(get_service('classroom').vocabulary['participants'], 'students')
        self.assertEqual(get_service('panel').vocabulary['participants'], 'candidates')
        self.assertEqual(get_service('showcase').vocabulary['entries'], 'exhibits')
        self.assertEqual(get_service('competition').vocabulary['owner'], 'director')
        self.assertEqual(get_service('polls').name, 'Anonymous Polls')


class LookupTests(SimpleTestCase):

    def test_a_registered_slug_is_found(self):
        self.assertEqual(get_service('panel').name, 'Panel')

    def test_anything_else_is_not_a_service(self):
        for value in ('nope', '', 'Classroom', ' classroom', 'classroom/', None, 5, ['classroom']):
            with self.subTest(value=value):
                self.assertIsNone(get_service(value))


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
class ReleaseTests(SimpleTestCase):

    def test_classroom_is_open_by_default(self):
        self.assertTrue(is_released('classroom'))
        self.assertEqual([s.slug for s in released_services()], ['classroom'])

    def test_a_service_that_is_not_switched_on_is_closed(self):
        for slug in ('panel', 'showcase', 'team', 'competition'):
            self.assertFalse(is_released(slug))

    def test_a_service_opens_when_it_has_a_dashboard_and_is_switched_on(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel']):
            self.assertTrue(is_released('panel'))
            self.assertEqual([s.slug for s in released_services()], ['classroom', 'panel'])

    def test_a_service_can_be_switched_off(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=[]):
            self.assertFalse(is_released('classroom'))
            self.assertEqual(released_services(), [])

    def test_an_unknown_name_in_the_setting_opens_nothing(self):
        with override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'bogus']):
            self.assertFalse(is_released('bogus'))
            self.assertFalse(is_released(None))

class DashboardTests(SimpleTestCase):

    def test_classroom_lands_on_the_session_list(self):
        self.assertEqual(dashboard_url(get_service('classroom')), reverse('assessment_session_list'))

    def test_every_service_lands_on_its_existing_workspace(self):
        self.assertEqual({s.slug: dashboard_url(s) for s in SERVICES}, {
            'classroom': reverse('assessment_session_list'),
            'panel': reverse('panel_hearing_list'),
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
        for other in ('', 'class', 'classrooms', 'Classroom', 'assessments', 'oidc', 'assess', 'healthz'):
            self.assertFalse(pattern.match(other), other)

    def test_it_passes_the_value_through_both_ways(self):
        converter = ServiceSlugConverter()
        self.assertEqual(converter.to_python('panel'), 'panel')
        self.assertEqual(converter.to_url('panel'), 'panel')
