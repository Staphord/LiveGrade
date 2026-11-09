from unittest.mock import patch
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, Student
from service_domains.models import TrainingSeries, TrainingSeriesSession, TrainingSessionCompetency
from service_domains.operations import record_training_competency_result
from service_domains.team_reports import release_learner_report, revoke_learner_report


class PrivateLearnerReportTests(TestCase):
    def setUp(self):
        self.org = make_org('Private training')
        self.owner = make_user('report-owner')
        self.session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Demo', status='closed')
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Practice')
        self.link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=self.session, sequence=1)
        self.student = Student.objects.create(assessment_session=self.session, full_name='Learner One')
        self.other = Student.objects.create(assessment_session=self.session, full_name='Private Other')
        self.criterion = TrainingSessionCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series_session=self.link, name='Communication', passing_score=70)
        record_training_competency_result(self.session, self.criterion, self.student, 80,
            'Clear and concise delivery.', organization=self.org.as_claim(), user=self.owner)

    def test_explicit_release_hides_other_learners_and_correction_revokes_snapshot(self):
        token = release_learner_report(self.link, self.student.pk,
            organization=self.org.as_claim(), user=self.owner)
        url = reverse('training_learner_report', args=(token,))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            response = self.client.get(url)
            self.assertContains(response, 'Clear and concise delivery.')
            self.assertNotContains(response, 'Private Other')
            self.assertEqual(response['Referrer-Policy'], 'no-referrer')
            record_training_competency_result(self.session, self.criterion, self.student, 85,
                'Updated evidence.', correction_reason='Reviewed the recording.',
                organization=self.org.as_claim(), user=self.owner)
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_release_is_owner_scoped_and_revoke_ends_access(self):
        with self.assertRaises(PermissionDenied):
            release_learner_report(self.link, self.student.pk,
                organization=self.org.as_claim(), user=make_user('report-stranger'))
        token = release_learner_report(self.link, self.student.pk,
            organization=self.org.as_claim(), user=self.owner)
        revoke_learner_report(self.link, self.student.pk,
            organization=self.org.as_claim(), user=self.owner)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            self.assertEqual(self.client.get(reverse('training_learner_report', args=(token,))).status_code, 404)

    def test_owner_can_release_and_copy_link_from_outcomes_screen(self):
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            response = self.client.post(reverse('training_session_competencies', args=(self.session.pk,)),
                {'action': 'release_learner', 'student_id': self.student.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'learner-report-link')
        self.assertTrue(response.context['learner_report_url'])

    def test_owner_revoke_and_error_handling_from_outcomes_screen(self):
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            revoke_resp = self.client.post(
                reverse('training_session_competencies', args=(self.session.pk,)),
                {'action': 'revoke_learner', 'student_id': self.student.pk})
            self.assertEqual(revoke_resp.status_code, 200)

            err_resp = self.client.post(
                reverse('training_session_competencies', args=(self.session.pk,)),
                {'action': 'release_learner', 'student_id': 99999})
            self.assertEqual(err_resp.status_code, 200)
            self.assertIn('This learner is unavailable', err_resp.context['certificate_error'])


    def test_release_and_revoke_edge_cases(self):
        from django.core.exceptions import ValidationError
        # Attempt release when session is not closed
        self.session.status = 'live'
        self.session.save()
        with self.assertRaises(ValidationError):
            release_learner_report(self.link, self.student.pk, organization=self.org.as_claim(), user=self.owner)
        self.session.status = 'closed'
        self.session.save()

        # Non-existent student ID
        with self.assertRaises(PermissionDenied):
            release_learner_report(self.link, 'not-a-number', organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            release_learner_report(self.link, 99999, organization=self.org.as_claim(), user=self.owner)

        # Release when no competencies recorded
        fresh_student = Student.objects.create(assessment_session=self.session, full_name='Unrated Learner')
        with self.assertRaises(ValidationError):
            release_learner_report(self.link, fresh_student.pk, organization=self.org.as_claim(), user=self.owner)

        # Revoke non-existent student ID
        with self.assertRaises(PermissionDenied):
            revoke_learner_report(self.link, 'invalid', organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            revoke_learner_report(self.link, 99999, organization=self.org.as_claim(), user=self.owner)

        # Rate-limiting / throttling check on bearer token endpoint
        token = release_learner_report(self.link, self.student.pk, organization=self.org.as_claim(), user=self.owner)
        url = reverse('training_learner_report', args=(token,))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            with patch('service_domains.team_reports.allow_unknown_bearer_request', return_value=False):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 429)
            with patch('service_domains.team_reports.allow_unknown_bearer_request', return_value=None):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 503)

        # Overlong bearer token
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            res = self.client.get(reverse('training_learner_report', args=('x' * 300,)))
            self.assertEqual(res.status_code, 404)

        # Service scoping boundary (non-team service)
        from django.test import RequestFactory
        from django.http import Http404
        from service_domains.team_reports import training_learner_report
        rf = RequestFactory()
        req = rf.get(url)
        req.service = 'classroom'
        with self.assertRaises(Http404):
            training_learner_report(req, token)
