"""The first-session guide follows the real Classroom setup and launch flow."""
from django.test import TestCase
from django.test import RequestFactory
from django.http import Http404
from django.urls import reverse

from accounts.context import CLASSROOM_WALKTHROUGHS_KEY
from accounts.context import _classroom_walkthrough
from accounts.access import ORGS_KEY
from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, GroupMembership, PresentationGroup, RubricCategory, Student,
)
from assessments.views.lecturer_views import (
    assessment_walkthrough_dismiss, assessment_walkthrough_start,
)


class ClassroomWalkthroughTests(TestCase):
    def setUp(self):
        self.org = make_org('Walkthrough University')
        self.owner = make_user('walkthrough-owner')
        sign_in(self.client, self.owner, self.org)
        self.list_url = reverse('assessment_session_list')

    def guide(self, response, step, percent):
        guide = response.context['classroom_walkthrough']
        self.assertIsNotNone(guide)
        self.assertEqual(guide['current_step'], step)
        self.assertEqual(guide['progress_percent'], percent)
        return guide

    def test_opt_in_walkthrough_follows_setup_to_live_and_can_be_dismissed(self):
        page = self.client.get(self.list_url)
        self.assertContains(page, 'Start interactive walkthrough')
        self.assertIsNone(page.context['classroom_walkthrough'])

        self.assertEqual(self.client.get(reverse('assessment_walkthrough_start')).status_code, 405)
        started = self.client.post(reverse('assessment_walkthrough_start'))
        self.assertRedirects(started, reverse('assessment_session_create'))
        guide_page = self.client.get(reverse('assessment_session_create'))
        self.assertContains(guide_page, 'Your first Classroom session')
        self.assertEqual(self.guide(guide_page, 1, 0)['current_action'], 'Create session')

        created = self.client.post(reverse('assessment_session_create'), {
            'name': 'Walkthrough demo', 'identify_by': 'full_name', 'ungraded_penalty': '0',
        })
        session = AssessmentSession.objects.get(name='Walkthrough demo')
        self.assertRedirects(created, reverse('assessment_groups', args=[session.pk]))
        group_page = self.client.get(reverse('assessment_groups', args=[session.pk]))
        guide = self.guide(group_page, 2, 25)
        self.assertEqual(guide['session'], session)
        self.assertEqual(guide['current_action_url'], reverse('assessment_setup', args=[session.pk]))

        student = Student.objects.create(assessment_session=session, full_name='Learner One')
        group = PresentationGroup.objects.create(assessment_session=session, name='Team One')
        GroupMembership.objects.create(student=student, group=group)
        roster_page = self.client.get(reverse('assessment_roster', args=[session.pk]))
        self.assertEqual(self.guide(roster_page, 3, 50)['current_action'], 'Build the rubric')

        RubricCategory.objects.create(
            assessment_session=session, name='Quality', scope='group', weight=100,
        )
        rubric_page = self.client.get(reverse('assessment_rubric', args=[session.pk]))
        self.assertEqual(self.guide(rubric_page, 4, 75)['current_action'], 'Review session')
        self.assertEqual(self.client.post(reverse('assessment_go_live', args=[session.pk])).status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.status, AssessmentSession.Status.LIVE)
        completed_page = self.client.get(reverse('assessment_session_detail', args=[session.pk]))
        completed = self.guide(completed_page, 4, 100)
        self.assertTrue(completed['completed'])
        self.assertContains(completed_page, 'Walkthrough complete')

        self.assertEqual(self.client.get(reverse('assessment_walkthrough_dismiss')).status_code, 405)
        dismissed = self.client.post(reverse('assessment_walkthrough_dismiss'))
        self.assertRedirects(dismissed, self.list_url)
        self.assertIsNone(self.client.get(self.list_url).context['classroom_walkthrough'])

    def test_existing_draft_is_resumed_and_cross_scope_reference_is_not_shown(self):
        draft = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='My existing draft', created_by=self.owner,
        )
        started = self.client.post(reverse('assessment_walkthrough_start'))
        self.assertRedirects(started, reverse('assessment_groups', args=[draft.pk]))
        page = self.client.get(reverse('assessment_groups', args=[draft.pk]))
        self.assertEqual(self.guide(page, 2, 25)['session'], draft)

        other_owner = make_user('walkthrough-other-owner')
        private = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Secret other-owner draft', created_by=other_owner,
            service='panel',
        )
        session = self.client.session
        session[CLASSROOM_WALKTHROUGHS_KEY][str(self.org.pk)] = {'session_id': private.pk}
        session.save()
        page = self.client.get(self.list_url)
        guide = page.context['classroom_walkthrough']
        self.assertIsNone(guide['session'])
        self.assertTrue(guide['session_missing'])
        self.assertContains(page, 'draft linked to this walkthrough is no longer available')
        self.assertNotContains(page, 'Secret other-owner draft')

    def test_walkthrough_state_is_organization_scoped_and_service_scoped(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Private walkthrough draft', created_by=self.owner,
        )
        client_session = self.client.session
        client_session[CLASSROOM_WALKTHROUGHS_KEY] = {
            str(self.org.pk): {'session_id': session.pk},
            str(self.org.pk + 1000): {'session_id': session.pk},
        }
        client_session.save()

        other_org = make_org('Other organization')
        sign_in(self.client, self.owner, self.org, other_org, active=other_org)
        page = self.client.get(self.list_url)
        self.assertIsNone(page.context['classroom_walkthrough'])
        self.assertNotContains(page, 'Private walkthrough draft')

        sign_in(self.client, self.owner, self.org)
        session = self.client.session
        session['active_service'] = 'panel'
        session.save()
        # Service middleware derives scope from the route, so exercise the context
        # processor boundary directly for a non-Classroom request.
        request = RequestFactory().get('/panel/')
        request.user = self.owner
        request.session = self.client.session
        request.service = 'panel'
        organization = {'id': self.org.pk, 'name': self.org.name, 'slug': self.org.slug}
        self.assertIsNone(_classroom_walkthrough(request, organization))

    def test_context_builder_fails_closed_for_ineligible_or_malformed_state(self):
        organization = {'id': self.org.pk, 'name': self.org.name, 'slug': self.org.slug}
        request = RequestFactory().get('/classroom/app/')
        request.user = self.owner
        request.service = 'classroom'
        request.session = {}
        self.assertIsNone(_classroom_walkthrough(request, organization))
        request.session[CLASSROOM_WALKTHROUGHS_KEY] = []
        self.assertIsNone(_classroom_walkthrough(request, organization))
        request.session[CLASSROOM_WALKTHROUGHS_KEY] = {str(self.org.pk): []}
        self.assertIsNone(_classroom_walkthrough(request, organization))
        self.assertIsNone(_classroom_walkthrough(request, None))

    def test_missing_and_closed_walkthrough_states_render_safe_progress(self):
        sign_in(self.client, self.owner, self.org)
        self.client.post(reverse('assessment_walkthrough_start'))
        missing = self.client.get(self.list_url).context['classroom_walkthrough']
        self.assertIsNone(missing['session'])
        self.assertFalse(missing['session_missing'])
        self.assertEqual((missing['current_step'], missing['progress_percent']), (1, 0))

        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Closed walkthrough demo', created_by=self.owner,
            status=AssessmentSession.Status.CLOSED,
        )
        student = Student.objects.create(assessment_session=session, full_name='Learner')
        group = PresentationGroup.objects.create(assessment_session=session, name='Team')
        GroupMembership.objects.create(student=student, group=group)
        RubricCategory.objects.create(
            assessment_session=session, name='Quality', scope='group', weight=100,
        )
        browser = self.client.session
        browser[CLASSROOM_WALKTHROUGHS_KEY][str(self.org.pk)] = {'session_id': session.pk}
        browser.save()
        complete = self.client.get(self.list_url).context['classroom_walkthrough']
        self.assertTrue(complete['completed'])
        self.assertEqual((complete['current_step'], complete['progress_percent']), (4, 100))

    def test_dismiss_handles_malformed_stored_state(self):
        browser = self.client.session
        browser[CLASSROOM_WALKTHROUGHS_KEY] = []
        browser.save()
        response = self.client.post(reverse('assessment_walkthrough_dismiss'))
        self.assertRedirects(response, self.list_url)
        self.assertEqual(self.client.session[CLASSROOM_WALKTHROUGHS_KEY], {})

    def test_actions_reject_non_classroom_service_and_start_normalizes_state(self):
        request = RequestFactory().post('/panel/walkthrough/start/')
        request.user = self.owner
        request.session = {ORGS_KEY: [self.org.as_claim()], CLASSROOM_WALKTHROUGHS_KEY: []}
        request.service = 'panel'
        with self.assertRaises(Http404):
            assessment_walkthrough_start(request)
        with self.assertRaises(Http404):
            assessment_walkthrough_dismiss(request)

        request = RequestFactory().post('/classroom/app/walkthrough/start/')
        request.user = self.owner
        request.session = {ORGS_KEY: [self.org.as_claim()], CLASSROOM_WALKTHROUGHS_KEY: []}
        request.service = 'classroom'
        response = assessment_walkthrough_start(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(request.session[CLASSROOM_WALKTHROUGHS_KEY], {str(self.org.pk): {'session_id': None}})
