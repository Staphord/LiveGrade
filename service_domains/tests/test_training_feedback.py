import uuid
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession
from service_domains.models import TrainingFeedback, TrainingSeries, TrainingSeriesSession
from service_domains.operations import (submit_training_feedback,
                                        training_series_feedback_summary)
from service_domains.team_forms import TrainingFeedbackPrivacyForm


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class TrainingAnonymousFeedbackTests(TestCase):
    def setUp(self):
        self.org = make_org('Anonymous feedback organization')
        self.other_org = make_org('Other feedback organization')
        self.owner = make_user('feedback-owner')
        self.other_owner = make_user('feedback-other-owner')
        self.series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Practice demos', feedback_minimum_responses=3)
        self.session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Closed demo',
            status=AssessmentSession.Status.CLOSED)
        self.link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=self.session, sequence=1)
        self.url = reverse('training_anonymous_feedback',
            kwargs={'feedback_public_id': self.link.feedback_public_id})

    def request(self, client, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            return getattr(client, method)(url, *args, **kwargs)

    def submit_from_new_browser(self, score):
        client = Client()
        response = self.request(client, 'get', self.url)
        self.assertEqual(response.status_code, 200)
        cookie_name = f'livegrade_team_feedback_{self.link.feedback_public_id.hex}'
        self.assertTrue(response.cookies[cookie_name]['httponly'])
        self.assertEqual(response.cookies[cookie_name]['samesite'], 'Lax')
        self.assertIn('private, no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        return client, self.request(client, 'post', self.url, {'score': str(score)})

    def test_feedback_is_anonymous_unique_per_browser_and_aggregated_only_at_floor(self):
        client_a, submitted_a = self.submit_from_new_browser(5)
        self.assertEqual(submitted_a.status_code, 200)
        self.assertContains(submitted_a, 'recorded')
        first = TrainingFeedback.objects.get()
        self.assertIsNone(first.created_by_id)
        self.assertEqual(len(first.device_digest), 64)
        self.assertNotIn('feedback-owner', str(first.device_digest))
        duplicate = self.request(client_a, 'post', self.url, {'score': '2'})
        self.assertEqual(duplicate.status_code, 400)

        client_b, submitted_b = self.submit_from_new_browser(3)
        self.assertEqual(submitted_b.status_code, 200)
        before_floor = training_series_feedback_summary(self.series,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(before_floor[0]['response_count'], None)
        self.assertIsNone(before_floor[0]['aggregate'])

        _, submitted_c = self.submit_from_new_browser(4)
        self.assertEqual(submitted_c.status_code, 200)
        after_floor = training_series_feedback_summary(self.series,
            organization=self.org.as_claim(), user=self.owner)
        aggregate = after_floor[0]['aggregate']
        self.assertEqual(after_floor[0]['response_count'], 3)
        self.assertEqual(str(aggregate['mean']), '4')
        self.assertEqual(str(aggregate['median']), '4')

        sign_in(self.client, self.owner, self.org)
        trends_url = reverse('training_series_trends', args=(self.series.pk,))
        response = self.request(self.client, 'get', trends_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['feedback_summaries'][0]['aggregate']['mean'], 4)
        self.assertNotContains(response, '5 out of 5')
        self.assertEqual(set(response.context['feedback_summaries'][0]['aggregate']),
            {'response_count', 'mean', 'median'})

    def test_feedback_route_fails_closed_for_unconfigured_active_or_foreign_context(self):
        active = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Active demo')
        active_link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=active, sequence=2)
        active_url = reverse('training_anonymous_feedback',
            kwargs={'feedback_public_id': active_link.feedback_public_id})
        self.assertEqual(self.request(self.client, 'get', active_url).status_code, 404)
        disabled = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='No feedback floor')
        disabled_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Disabled feedback demo',
            status=AssessmentSession.Status.CLOSED)
        disabled_link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=disabled, assessment_session=disabled_session, sequence=1)
        disabled_url = reverse('training_anonymous_feedback',
            kwargs={'feedback_public_id': disabled_link.feedback_public_id})
        self.assertEqual(self.request(self.client, 'get', disabled_url).status_code, 404)
        self.assertEqual(self.request(self.client, 'get', reverse('training_anonymous_feedback',
            kwargs={'feedback_public_id': uuid.uuid4()})).status_code, 404)

        with self.assertRaisesMessage(ValidationError, 'invalid or expired'):
            submit_training_feedback(self.link.feedback_public_id,
                TrainingFeedback.issue_device_token(active_link), 4)

    def test_public_feedback_route_is_throttled_and_fails_closed_with_shared_cache(self):
        with patch('service_domains.views.allow_public_event_request', return_value=False):
            limited = self.request(self.client, 'get', self.url)
        self.assertEqual(limited.status_code, 429)
        self.assertTrue(limited.has_header('Retry-After'))
        self.assertFalse(limited.cookies)
        with patch('service_domains.views.allow_public_event_request', return_value=None):
            unavailable = self.request(self.client, 'get', self.url)
        self.assertEqual(unavailable.status_code, 503)
        self.assertFalse(unavailable.cookies)

    def test_owner_summary_is_scoped_and_feedback_floor_configuration_is_validated(self):
        with self.assertRaises(PermissionDenied):
            training_series_feedback_summary(self.series,
                organization=self.other_org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            training_series_feedback_summary(self.series,
                organization=self.org.as_claim(), user=self.other_owner)
        for value in ('1', '51', 'bad'):
            form = TrainingFeedbackPrivacyForm({'minimum_responses': value})
            self.assertFalse(form.is_valid())
        self.assertTrue(TrainingFeedbackPrivacyForm({'minimum_responses': ''}).is_valid())

    def test_closed_session_feedback_submission_rejects_bad_score_and_model_identity(self):
        token = TrainingFeedback.issue_device_token(self.link)
        for value in (0, 6, 'bad'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                submit_training_feedback(self.link.feedback_public_id, token, value)
        linked = TrainingFeedback(series_session=self.link, device_digest='a' * 64,
            score=5, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'cannot be linked to an account'):
            linked.full_clean()
        self.assertEqual(TrainingFeedback.objects.count(), 0)

    def test_feedback_model_guards_organization_threshold_status_and_immutable_rows(self):
        token = TrainingFeedback.issue_device_token(self.link)
        digest = TrainingFeedback.digest_device_token(token, self.link)
        invalid_records = [
            (TrainingFeedback(series_session=self.link, device_digest='short', score=4,
                organization_id=self.org.id), 'digest is invalid'),
            (TrainingFeedback(series_session=self.link, device_digest=digest, score=4,
                organization_id=self.other_org.id), 'same organization'),
        ]
        no_floor = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='No floor')
        no_floor_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='No floor demo', status='closed')
        no_floor_link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=no_floor, assessment_session=no_floor_session, sequence=1)
        no_floor_feedback = TrainingFeedback(series_session=no_floor_link, device_digest='b' * 64,
            score=4, organization_id=self.org.id)
        invalid_records.append((no_floor_feedback, 'privacy threshold'))

        live_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Live demo', status='live')
        live_link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=live_session, sequence=2)
        live_feedback = TrainingFeedback(series_session=live_link, device_digest='c' * 64,
            score=4, organization_id=self.org.id)
        invalid_records.append((live_feedback, 'after the demo session closes'))

        for record, message in invalid_records:
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                record.full_clean()
        unattached = TrainingFeedback(device_digest='d' * 64, score=3,
            organization_id=self.org.id)
        unattached.clean()

        feedback = submit_training_feedback(self.link.feedback_public_id, token, 5)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            feedback.save()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            feedback.delete()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            TrainingFeedback.objects.filter(pk=feedback.pk).update(score=1)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            TrainingFeedback.objects.filter(pk=feedback.pk).delete()

    def test_feedback_operation_rejects_missing_active_unconfigured_and_racing_submissions(self):
        token = TrainingFeedback.issue_device_token(self.link)
        with self.assertRaisesMessage(ValidationError, 'link is unavailable'):
            submit_training_feedback(uuid.uuid4(), token, 4)

        active = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Active demo', status='live')
        active_link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=active, sequence=2)
        with self.assertRaisesMessage(ValidationError, 'after the demo session closes'):
            submit_training_feedback(active_link.feedback_public_id,
                TrainingFeedback.issue_device_token(active_link), 4)

        self.series.feedback_minimum_responses = None
        self.series.save(update_fields=('feedback_minimum_responses', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'has not configured'):
            submit_training_feedback(self.link.feedback_public_id, token, 4)
        self.series.feedback_minimum_responses = 3
        self.series.save(update_fields=('feedback_minimum_responses', 'updated_at'))
        with patch('service_domains.operations.TrainingFeedback.save', side_effect=IntegrityError):
            with self.assertRaisesMessage(ValidationError, 'already submitted feedback'):
                submit_training_feedback(self.link.feedback_public_id, token, 4)

    def test_privacy_form_shows_submission_link_only_after_closed_session(self):
        sign_in(self.client, self.owner, self.org)
        manage_url = reverse('training_series_manage', args=(self.series.pk,))
        response = self.request(self.client, 'get', manage_url)
        self.assertContains(response, 'Feedback link')
        self.assertContains(response, self.url)
        invalid = self.request(self.client, 'post', manage_url, {
            'action': 'feedback_privacy', 'minimum_responses': '1',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'greater than or equal to 2')
        changed = self.request(self.client, 'post', manage_url, {
            'action': 'feedback_privacy', 'minimum_responses': '',
        })
        self.assertEqual(changed.status_code, 302)
        self.series.refresh_from_db()
        self.assertIsNone(self.series.feedback_minimum_responses)
        self.assertEqual(self.request(self.client, 'get', self.url).status_code, 404)

    def test_invalid_cookie_form_and_operation_error_never_store_feedback(self):
        client = Client()
        self.request(client, 'get', self.url)
        cookie_name = f'livegrade_team_feedback_{self.link.feedback_public_id.hex}'
        old_token = client.cookies[cookie_name].value
        client.cookies[cookie_name] = signing.Signer().sign('tampered')
        refreshed = self.request(client, 'get', self.url)
        self.assertEqual(refreshed.status_code, 200)
        fresh_token = client.cookies[cookie_name].value
        self.assertNotEqual(old_token, fresh_token)

        invalid = self.request(client, 'post', self.url, {'score': '6'})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(TrainingFeedback.objects.count(), 0)

        without_cookie = Client()
        missing_token = self.request(without_cookie, 'post', self.url, {'score': '4'})
        self.assertEqual(missing_token.status_code, 400)
        self.assertIn(cookie_name, without_cookie.cookies)
        self.assertEqual(TrainingFeedback.objects.count(), 0)

        with patch('service_domains.views.submit_training_feedback',
                side_effect=ValidationError('The response could not be recorded.')):
            rejected = self.request(client, 'post', self.url, {'score': '4'})
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, 'could not be recorded', status_code=400)
        self.assertEqual(TrainingFeedback.objects.count(), 0)
