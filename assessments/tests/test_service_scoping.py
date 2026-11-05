"""A session belongs to one service, and no owner page crosses services.

The same person, in the same organization, who owns a session of another service
still cannot reach it from Classroom: it is as absent as another organization's.
Participants are different on purpose: they hold a session link, not an account, and
the link works whatever the service.
"""
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.access import ORGS_KEY
from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, GroupMembership, PresentationGroup, RubricCategory,
                                SessionTemplate, Student)
from assessments.views import assessment_session_create
from assessments.views.lecturer_views import _session
from assessments.tests.test_access_matrix import GET, LECTURER_ROW_ROUTES, POST

OTHER = 'panel'  # a service with no owner pages yet: its sessions exist only as rows


class ServiceTestCase(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.ann = make_user('ann')
        self.classroom = AssessmentSession.objects.create(organization_id=self.org.pk, name='Ann classroom', created_by=self.ann)
        self.hearing = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='ANN-PANEL-HEARING', created_by=self.ann, service=OTHER)
        sign_in(self.client, self.ann, self.org)

    def url(self, name, session):
        return reverse(name, args=[session.pk])


class DefaultServiceTests(ServiceTestCase):

    def test_a_session_is_classroom_unless_it_says_otherwise(self):
        self.assertEqual(self.classroom.service, 'classroom')

    def test_the_service_is_stored_and_indexed(self):
        field = AssessmentSession._meta.get_field('service')
        self.assertEqual(field.max_length, 20)
        self.assertTrue(field.db_index)
        self.assertEqual(AssessmentSession.objects.get(pk=self.hearing.pk).service, OTHER)


class ListTests(ServiceTestCase):

    def test_the_list_shows_only_this_services_sessions(self):
        page = self.client.get(reverse('assessment_session_list'))
        self.assertContains(page, 'Ann classroom')
        self.assertNotContains(page, 'ANN-PANEL-HEARING')

    def test_a_person_whose_only_sessions_are_in_another_service_sees_an_empty_list(self):
        self.classroom.delete()
        page = self.client.get(reverse('assessment_session_list'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'ANN-PANEL-HEARING')


class EveryOwnerPageTests(ServiceTestCase):
    """The page for a session of another service answers exactly as for an id that is not there."""

    GET = ('assessment_template_save', 'assessment_session_duplicate', 'assessment_session_detail', 'assessment_session_edit', 'assessment_live_state', 'assessment_roster',
           'assessment_rubric', 'assessment_groups', 'assessment_participation', 'assessment_results',
           'assessment_results_export', 'assessment_results_export_csv', 'assessment_qr',
           'assessment_transfer', 'assessment_setup')
    # Delete is last: were it ever to succeed, the rows after it would be refused only because they are gone.
    POST = ('assessment_template_save', 'assessment_session_duplicate', 'assessment_go_live', 'assessment_close_session', 'assessment_toggle_pause',
            'assessment_toggle_joining', 'assessment_close_turn', 'assessment_session_delete',
            'assessment_public_results')

    def test_every_read_page_is_not_found(self):
        missing = AssessmentSession(pk=999999)
        for name in self.GET:
            with self.subTest(name=name):
                got = self.client.get(self.url(name, self.hearing))
                self.assertEqual(got.status_code, 404)
                self.assertEqual(got.status_code, self.client.get(self.url(name, missing)).status_code)
                self.assertNotContains(got, 'ANN-PANEL-HEARING', status_code=404)

    def test_every_action_is_refused_and_changes_nothing(self):
        for name in self.POST:
            with self.subTest(name=name):
                self.assertEqual(self.client.post(self.url(name, self.hearing)).status_code, 404)
        row = AssessmentSession.objects.get(pk=self.hearing.pk)
        self.assertEqual((row.status, row.voting_paused, row.joining_locked, row.name),
                         (AssessmentSession.Status.DRAFT, False, False, 'ANN-PANEL-HEARING'))

    def test_the_classroom_session_beside_it_still_opens(self):
        self.assertEqual(self.client.get(self.url('assessment_session_detail', self.classroom)).status_code, 200)

    def test_a_session_of_another_service_cannot_be_deleted_by_its_own_owner_from_classroom(self):
        self.client.post(self.url('assessment_session_delete', self.hearing))
        self.assertTrue(AssessmentSession.objects.filter(pk=self.hearing.pk).exists())

    def test_a_session_of_another_service_cannot_release_a_classroom_leaderboard(self):
        response = self.client.post(self.url('assessment_public_results', self.hearing),
            {'action': 'release'})
        self.assertEqual(response.status_code, 404)
        self.hearing.refresh_from_db()
        self.assertIsNone(self.hearing.public_results_released_at)

    def test_every_owner_session_route_fails_closed_for_a_cross_service_session(self):
        student = Student.objects.create(assessment_session=self.hearing, full_name='Panel learner')
        group = PresentationGroup.objects.create(assessment_session=self.hearing, name='Panel group')
        GroupMembership.objects.create(group=group, student=student)
        category = RubricCategory.objects.create(
            assessment_session=self.hearing, name='Panel criterion', weight=100)
        template = SessionTemplate.objects.create(
            name='Panel template', organization_id=self.org.pk, created_by=self.ann, service=OTHER)
        from assessments.tests.test_access_matrix import named_patterns
        patterns = dict(named_patterns())

        def route_url(name):
            converters = patterns[name].pattern.converters
            values = {
                'pk': self.hearing.pk,
                'student_pk': student.pk,
                'group_pk': group.pk,
                'category_pk': category.pk,
                'template_pk': template.pk,
            }
            return reverse(name, kwargs={key: values[key] for key in converters})

        session_routes = {
            name: method for name, method in LECTURER_ROW_ROUTES.items()
            if 'pk' in patterns[name].pattern.converters
        }
        template_routes = {
            name for name in LECTURER_ROW_ROUTES
            if 'template_pk' in patterns[name].pattern.converters
        }
        self.assertEqual(template_routes, {'assessment_template_delete'})

        for name, method in session_routes.items():
            with self.subTest(name=name, method=method):
                response = getattr(self.client, method)(route_url(name))
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(route_url('assessment_template_delete')).status_code, 404)
        self.assertTrue(AssessmentSession.objects.filter(pk=self.hearing.pk).exists())
        self.assertTrue(SessionTemplate.objects.filter(pk=template.pk).exists())


class CreateTests(ServiceTestCase):

    def test_a_session_created_in_classroom_is_a_classroom_session(self):
        response = self.client.post(reverse('assessment_session_create'),
                                    {'name': 'New one', 'identify_by': 'full_name', 'ungraded_penalty': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AssessmentSession.objects.get(name='New one').service, 'classroom')

    def test_the_form_page_opens(self):
        self.assertEqual(self.client.get(reverse('assessment_session_create')).status_code, 200)

    def test_without_a_service_nothing_is_created(self):
        request = RequestFactory().post('/anywhere/', {'name': 'x'})
        request.user = self.ann
        request.session = {ORGS_KEY: [self.org.as_claim()]}
        with self.assertRaises(Http404):
            assessment_session_create(request)
        self.assertFalse(AssessmentSession.objects.filter(name='x').exists())


class OversightTests(TestCase):

    def setUp(self):
        self.org = make_org('Uni', oversee=True)
        self.boss = make_user('boss')
        self.ann = make_user('ann')
        self.classroom = AssessmentSession.objects.create(organization_id=self.org.pk, name='ANN-CLASS', created_by=self.ann)
        self.hearing = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='ANN-PANEL-HEARING', created_by=self.ann, service=OTHER)
        sign_in(self.client, self.boss, self.org)

    def test_an_overseer_sees_every_colleagues_session_in_this_service_only(self):
        page = self.client.get(reverse('assessment_session_list') + '?scope=all')
        self.assertContains(page, 'ANN-CLASS')
        self.assertNotContains(page, 'ANN-PANEL-HEARING')

    def test_an_overseer_cannot_open_a_colleagues_session_of_another_service(self):
        for name in ('assessment_results', 'assessment_participation', 'assessment_transfer'):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.hearing.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('assessment_results', args=[self.classroom.pk])).status_code, 200)


class FailClosedTests(ServiceTestCase):

    def request(self, **extra):
        request = RequestFactory().get('/')
        request.user = self.ann
        request.session = {ORGS_KEY: [self.org.as_claim()]}
        for key, value in extra.items():
            setattr(request, key, value)
        return request

    def test_a_request_with_no_service_finds_no_session(self):
        with self.assertRaises(Http404):
            _session(self.request(), self.classroom.pk)

    def test_a_request_with_an_empty_service_finds_no_session(self):
        with self.assertRaises(Http404):
            _session(self.request(service=None), self.classroom.pk)

    def test_the_right_service_finds_it(self):
        self.assertEqual(_session(self.request(service='classroom'), self.classroom.pk), self.classroom)

    def test_the_wrong_service_does_not(self):
        with self.assertRaises(Http404):
            _session(self.request(service=OTHER), self.classroom.pk)

    def test_the_queryset_filter_on_nothing_returns_nothing(self):
        for empty in (None, '', 0):
            with self.subTest(empty=empty):
                self.assertEqual(list(AssessmentSession.objects.for_service(empty)), [])

    def test_the_queryset_filter_returns_one_services_rows(self):
        self.assertEqual(list(AssessmentSession.objects.for_service('classroom')), [self.classroom])
        self.assertEqual(list(AssessmentSession.objects.for_service(OTHER)), [self.hearing])

    def test_it_chains_with_the_organization_and_the_owner(self):
        rows = (AssessmentSession.objects.for_organization(self.org.as_claim())
                .for_service('classroom').owned_by(self.ann))
        self.assertEqual(list(rows), [self.classroom])
        other_org = make_org('Elsewhere')
        self.assertEqual(list(AssessmentSession.objects.for_organization(other_org.as_claim()).for_service('classroom')), [])


class ParticipantsAreNotScopedTests(TestCase):
    """A session link works whatever the service: AM-08, participants never meet a service."""

    def test_a_participant_reaches_a_session_of_any_service_by_its_link(self):
        org = make_org('Uni')
        for service in ('classroom', 'panel', 'showcase'):
            session = AssessmentSession.objects.create(organization_id=org.pk, name=service, service=service)
            with self.subTest(service=service):
                self.assertNotEqual(self.client.get(reverse('student_join', args=[session.uuid])).status_code, 404)
