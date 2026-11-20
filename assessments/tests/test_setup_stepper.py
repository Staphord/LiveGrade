"""The classroom setup screen renders through the shared stepper partial."""
from django.urls import reverse

from assessments.models import AssessmentSession, PanelExaminer
from assessments.tests.test_edges_lecturer_views import LecturerTestCase


def keys(response):
    return [step['key'] for step in response.context['stepper_steps']]


def by_key(response):
    return {step['key']: step for step in response.context['stepper_steps']}


class ClassroomSetupStepperTests(LecturerTestCase):
    def test_new_session_opens_only_the_basics_step(self):
        response = self.client.get(reverse('assessment_session_create'))

        steps = by_key(response)
        self.assertEqual(keys(response), ['basics', 'groups', 'rubric', 'go-live'])
        self.assertTrue(steps['basics']['current'])
        self.assertFalse(steps['basics']['done'])
        for key in ('groups', 'rubric', 'go-live'):
            self.assertTrue(steps[key]['disabled'], key)
        self.assertContains(response, 'class="stepper-nav"')
        self.assertContains(response, 'data-step="basics" data-panel-step="basics" aria-current="step"')
        self.assertNotContains(response, 'setup-stepper-nav')

    def test_draft_basics_edit_reads_completion_from_the_session(self):
        response = self.client.get(reverse('assessment_session_edit', args=[self.session.pk]))

        steps = by_key(response)
        self.assertTrue(steps['basics']['current'])
        self.assertTrue(steps['basics']['done'])
        self.assertEqual(steps['groups']['done'], self.session.groups.exists())
        self.assertEqual(steps['groups']['href'], '#groups-section')
        self.assertFalse(steps['groups']['disabled'])

    def test_setup_step_marks_progress_and_go_live_readiness(self):
        response = self.client.get(self.url('assessment_groups'))

        steps = by_key(response)
        self.assertTrue(steps['groups']['current'])
        self.assertEqual(steps['go-live']['locked'], not self.session.can_go_live())
        self.assertFalse(steps['go-live']['disabled'])
        self.assertTrue(steps['go-live']['final'])
        self.assertEqual(steps['go-live']['icon'], 'bi-rocket-takeoff')

    def test_setup_opens_the_participant_roster_on_the_entries_step(self):
        response = self.client.get(self.url('assessment_setup'), {'step': 'groups'})

        self.assertTrue(response.context['students_open'])
        self.assertContains(response, 'class="collapse show" id="students-body"')
        self.assertContains(response, 'Ann Able')
        self.assertContains(response, 'Cat Cole')

    def test_setup_renders_saved_entries_participants_rubric_and_panelists(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(enable_panel=True)
        self.session.refresh_from_db()
        PanelExaminer.objects.create(
            session=self.session,
            display_name='Dr Example',
            email='examiner@example.test',
            token_digest='d' * 64,
        )

        response = self.client.get(self.url('assessment_setup'), {'step': 'panel'})

        for saved_value in ('Red', 'Blue', 'Ann Able', 'Cat Cole', 'Quality', 'Teamwork',
                            'Dr Example', 'examiner@example.test'):
            self.assertContains(response, saved_value)
        steps = by_key(response)
        self.assertTrue(steps['groups']['done'])
        self.assertTrue(steps['rubric']['done'])
        self.assertTrue(steps['panel']['done'])

    def test_live_session_hides_the_go_live_step(self):
        self.set_status(AssessmentSession.Status.LIVE)

        response = self.client.get(self.url('assessment_groups'))

        self.assertEqual(keys(response), ['basics', 'groups', 'rubric'])
