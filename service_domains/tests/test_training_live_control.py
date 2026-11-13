"""Team Training Live Workshop Console tests."""
from decimal import Decimal
import uuid
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, Student
from service_domains.models import (
    TrainingCompetency,
    TrainingCompetencyResult,
    TrainingFeedback,
    TrainingParticipant,
    TrainingSeries,
    TrainingSeriesSession,
    TrainingSessionCompetency,
)


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'polls', 'team', 'showcase', 'competition'])
class TrainingLiveControlTests(TestCase):

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.org = make_org(f'Workshop Org {suffix}')
        self.other_org = make_org(f'Other Workshop Org {suffix}')
        self.owner = make_user(f'workshop-owner-{suffix}')
        self.other = make_user(f'workshop-other-{suffix}')
        sign_in(self.client, self.owner, self.org)

        self.series = TrainingSeries.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Executive Crisis Simulation',
            feedback_minimum_responses=1,
        )
        self.participant1 = TrainingParticipant.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            series=self.series,
            display_name='Alice Smith',
            group_name='Pod Alpha',
            order=1,
        )
        self.participant2 = TrainingParticipant.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            series=self.series,
            display_name='Bob Jones',
            group_name='Pod Beta',
            order=2,
        )
        self.assess_session = AssessmentSession.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            service='team',
            name='Emergency Operations Drill',
            status='live',
        )
        self.student1 = Student.objects.create(
            assessment_session=self.assess_session,
            full_name='Alice Smith',
            student_id='ALICE',
        )
        self.student2 = Student.objects.create(
            assessment_session=self.assess_session,
            full_name='Bob Jones',
            student_id='BOB',
        )
        self.session = TrainingSeriesSession.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            series=self.series,
            assessment_session=self.assess_session,
            sequence=1,
        )
        self.criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            series_session=self.session,
            name='Incident Triage',
            passing_score=Decimal('60.00'),
            order=1,
        )

    def test_training_session_live_renders_workshop_console(self):
        url = reverse('training_session_live', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Executive Crisis Simulation')
        self.assertContains(res, 'Emergency Operations Drill')
        self.assertContains(res, 'Pod Alpha')
        self.assertContains(res, 'Pod Beta')
        self.assertContains(res, 'Alice Smith')
        self.assertContains(res, 'Incident Triage')

    def test_training_session_live_state_endpoint_returns_json_and_fragments(self):
        state_url = reverse('training_session_live_state', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })
        res = self.client.get(state_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('session_status'), 'live')
        self.assertEqual(data.get('active_drill_phase'), 'briefing')
        self.assertIn('hero_html', data)
        self.assertIn('competencies_html', data)
        self.assertNotIn('feedback_html', data)
        self.assertEqual(data['drill_timer']['seconds'], 900)
        self.assertIn('pods_html', data)

    def test_live_actions_dispatch_and_mutate_workshop_state(self):
        action_url = reverse('training_session_live_action', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })

        # Action: set drill phase
        res = self.client.post(action_url, {'action': 'set_drill_phase', 'phase': 'simulation'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('active_drill_phase'), 'simulation')
        self.session.refresh_from_db()
        self.assertEqual(self.session.workflow_snapshot.get('drill_phase'), 'simulation')

        # Action: adjust drill timer
        res = self.client.post(action_url, {'action': 'adjust_drill_timer', 'delta': 300})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreaterEqual(data.get('drill_seconds_remaining'), 1200)

        # Action: toggle drill pause
        res = self.client.post(action_url, {'action': 'toggle_drill_pause'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('timer_paused'))

        # Action: mark pod visited
        res = self.client.post(action_url, {'action': 'mark_pod_visited', 'pod_name': 'Pod Alpha'})
        self.assertEqual(res.status_code, 200)
        self.session.refresh_from_db()
        self.assertIn('Pod Alpha', self.session.workflow_snapshot.get('visited_pods', []))

        # Action: save observation note
        res = self.client.post(action_url, {
            'action': 'save_observation_note',
            'key': str(self.participant1.pk),
            'note': 'Demonstrated calm communication under pressure.',
        })
        self.assertEqual(res.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(
            self.session.workflow_snapshot.get('notes', {}).get(str(self.participant1.pk)),
            'Demonstrated calm communication under pressure.'
        )

        # Action: record competency score
        res = self.client.post(action_url, {
            'action': 'record_competency_score',
            'criterion_id': str(self.criterion.pk),
            'student_id': str(self.student1.pk),
            'score': '66.00',
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            TrainingCompetencyResult.objects.filter(
                criterion=self.criterion, student=self.student1, score=Decimal('66.00')
            ).exists()
        )

    def test_tenant_isolation_denies_cross_organization_access(self):
        other_client = self.client_class()
        sign_in(other_client, self.other, self.other_org)

        url = reverse('training_session_live', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })
        res = other_client.get(url)
        self.assertEqual(res.status_code, 404)

    def test_conclude_session_action_closes_session_and_returns_redirect(self):
        action_url = reverse('training_session_live_action', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })
        # AJAX submission
        res = self.client.post(action_url, {'action': 'conclude_session'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('status'), 'closed')
        self.assertIn('redirect', data)
        self.assess_session.refresh_from_db()
        self.assertEqual(self.assess_session.status, 'closed')

    def test_live_session_allows_anonymous_feedback_submission(self):
        # Configure series feedback minimum
        self.series.feedback_minimum_responses = 1
        self.series.save(update_fields=('feedback_minimum_responses',))
        self.assess_session.status = 'live'
        self.assess_session.save(update_fields=('status',))

        feedback_url = reverse('training_anonymous_feedback', kwargs={
            'feedback_public_id': self.session.feedback_public_id,
        })
        # Live workshop in progress shows informative waiting state instead of 404
        live_res = self.client.get(feedback_url)
        self.assertEqual(live_res.status_code, 200)
        self.assertContains(live_res, 'Workshop in Progress')

        # Conclude session to open feedback collection
        self.assess_session.status = 'closed'
        self.assess_session.save(update_fields=('status',))

        get_res = self.client.get(feedback_url)
        self.assertEqual(get_res.status_code, 200)
        self.assertContains(get_res, 'How useful was this demo?')

        post_res = self.client.post(feedback_url, {'score': '5'})
        self.assertEqual(post_res.status_code, 200)
        self.assertTrue(
            TrainingFeedback.objects.filter(
                series_session=self.session, score=5
            ).exists()
        )

    def test_training_session_projector_renders_venue_display_without_notes_or_scoring(self):
        projector_url = reverse('training_session_projector', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })
        res = self.client.get(projector_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Venue Display')
        self.assertContains(res, 'Executive Crisis Simulation')
        self.assertContains(res, 'Emergency Operations Drill')
        self.assertContains(res, 'proj-clock')
        # Ensure coach scoring buttons and private notes are omitted from room projector
        self.assertNotContains(res, 'data-training-score')
        self.assertNotContains(res, 'data-training-note')

    def test_training_session_feedback_qr_png_format(self):
        qr_url = reverse('training_session_feedback_qr', kwargs={
            'series_id': self.series.pk,
            'session_id': self.session.pk,
        })
        res = self.client.get(qr_url + '?format=png')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')
        self.assertTrue(res.content.startswith(b'\x89PNG\r\n\x1a\n'))
