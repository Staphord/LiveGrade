"""request.service: taken from the address alone, and only for an open service."""
from django.test import RequestFactory, SimpleTestCase, override_settings

from portal.middleware import ServiceMiddleware, service_for_path


@override_settings(LIVEGRADE_LIVE_SERVICES=['assessment'])
class ServiceForPathTests(SimpleTestCase):

    def test_a_service_entry_address_belongs_to_that_service(self):
        for path in ('/assessment/', '/assessment', '/assessment/app/', '/assessment/app/5/edit/'):
            with self.subTest(path=path):
                self.assertEqual(service_for_path(path), 'assessment')

    def test_legacy_classroom_addresses_do_not_gain_assessment_scope(self):
        for path in ('/assessments/', '/assessments', '/classroom/', '/panel/', '/coassessment/'):
            with self.subTest(path=path):
                self.assertIsNone(service_for_path(path))

    def test_everything_else_belongs_to_no_service(self):
        for path in ('/', '', '/assess/00000000-0000-4000-8000-000000000000/join/', '/oidc/callback/',
                     '/healthz', '/no-access/', '/ws/assessments/x/', '/nope/'):
            with self.subTest(path=path):
                self.assertIsNone(service_for_path(path))

    def test_a_service_that_is_not_open_has_no_scope(self):
        for path in ('/showcase/', '/team/', '/competition/'):
            with self.subTest(path=path):
                self.assertIsNone(service_for_path(path))

    def test_only_an_exact_slug_counts(self):
        for path in ('/Assessment/', '/ASSESSMENT/', '/assessments/', '//assessment/', '/ assessment/',
                     'assessment/', '/assessment%2F/'):
            with self.subTest(path=path):
                self.assertIsNone(service_for_path(path))

    def test_dot_segments_do_not_change_the_first_segment(self):
        self.assertEqual(service_for_path('/assessment/../showcase/'), 'assessment')
        self.assertIsNone(service_for_path('/oidc/../assessment/'))

    @override_settings(LIVEGRADE_LIVE_SERVICES=[])
    def test_switching_the_service_off_removes_the_scope(self):
        self.assertIsNone(service_for_path('/assessment/'))
        self.assertIsNone(service_for_path('/assessment/app/'))
        self.assertIsNone(service_for_path('/assessments/'))


class MiddlewareTests(SimpleTestCase):

    def run_middleware(self, path):
        seen = {}

        def get_response(request):
            seen['service'] = request.service
            return 'response'

        result = ServiceMiddleware(get_response)(RequestFactory().get(path))
        return seen['service'], result

    def test_the_service_is_set_before_the_view_runs(self):
        service, result = self.run_middleware('/assessment/app/')
        self.assertEqual(service, 'assessment')
        self.assertEqual(result, 'response')

    def test_the_attribute_always_exists_even_when_it_is_empty(self):
        service, _ = self.run_middleware('/assess/anything/')
        self.assertIsNone(service)

    def test_a_query_string_cannot_set_the_service(self):
        service, _ = self.run_middleware('/oidc/authenticate/?service=assessment')
        self.assertIsNone(service)

    def test_a_header_cannot_set_the_service(self):
        request = RequestFactory().get('/oidc/authenticate/', HTTP_X_SERVICE='assessment')
        ServiceMiddleware(lambda r: setattr(self, 'got', r.service))(request)
        self.assertIsNone(self.got)
