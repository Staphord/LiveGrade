"""PE-05: participants see and version the service privacy notice before scoring."""
from django.urls import reverse
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from accounts.testing import make_org, make_user
from assessments.models import (
    AssessmentSession, Evaluation, GroupMembership, ParticipationRecord,
    PresentationGroup, PresentationTurn, PrivacyNotice, RubricCategory,
)
from assessments.services import STUDENT_SESSION_KEY
from assessments.privacy_copy import (
    DEFAULT_PRIVACY_NOTICE_BODY, DEFAULT_PRIVACY_NOTICE_TITLE,
    DEFAULT_PRIVACY_NOTICE_VERSION,
)
from assessments.privacy import NOTICE_SHOWN_SESSION_KEY


class ParticipantPrivacyNoticeTests(TestCase):
    def setUp(self):
        self.org = make_org('Privacy Notice University')
        self.owner = make_user('privacy-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Privacy demo',
            status=AssessmentSession.Status.LIVE,
        )
        self.presenter = self.session.students.create(full_name='Presenter')
        self.evaluator = self.session.students.create(full_name='Evaluator')
        self.group = PresentationGroup.objects.create(assessment_session=self.session, name='Team')
        GroupMembership.objects.create(group=self.group, student=self.presenter)
        self.category = RubricCategory.objects.create(
            assessment_session=self.session, name='Evidence', weight=100,
        )
        self.turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group,
            status=PresentationTurn.Status.ACTIVE,
        )
        self.join_url = reverse('student_join', args=[self.session.uuid])
        self.notice_url = reverse('student_privacy_notice', args=[self.session.uuid])
        self.evaluate_url = reverse('student_evaluate', args=[self.session.uuid])

    def join_without_viewing_notice(self):
        self.client.get(self.join_url)
        response = self.client.post(self.join_url, {'identifier': 'Evaluator'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ParticipationRecord.objects.filter(
            assessment_session=self.session, student=self.evaluator,
            privacy_notice__isnull=True,
        ).exists())

    def test_notice_is_required_before_html_or_json_scoring_and_version_is_recorded(self):
        self.join_without_viewing_notice()
        response = self.client.get(self.evaluate_url)
        self.assertRedirects(response, self.notice_url, fetch_redirect_response=False)
        record = ParticipationRecord.objects.get(assessment_session=self.session, student=self.evaluator)
        self.assertIsNone(record.privacy_notice_id)

        notice_page = self.client.get(self.notice_url)
        self.assertEqual(notice_page.status_code, 200)
        self.assertContains(notice_page, 'How your information is used')
        self.assertContains(notice_page, 'roster and participation information')
        self.assertContains(notice_page, 'Notice version 3')
        self.assertContains(notice_page, 'Continue to scoring')
        record.refresh_from_db()
        first_notice = PrivacyNotice.objects.get(service='classroom', version='3')
        self.assertIsNone(record.privacy_notice_id)

        response = self.client.get(self.evaluate_url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 428)
        self.assertEqual(response.json()['status'], 'privacy_notice_required')
        self.assertEqual(self.client.post(self.notice_url, {'action': 'continue'}).status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.privacy_notice_id, first_notice.pk)
        already_seen = self.client.get(self.notice_url)
        self.assertRedirects(already_seen, self.evaluate_url, fetch_redirect_response=False)
        response = self.client.get(self.evaluate_url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['turn_id'], self.turn.pk)
        evaluate_page = self.client.get(self.evaluate_url)
        self.assertEqual(evaluate_page.status_code, 200)
        self.assertContains(evaluate_page, 'id="eval-form"', html=False)
        submitted = self.client.post(self.evaluate_url, {f'group_{self.category.pk}': '8'})
        self.assertRedirects(submitted, reverse('student_submitted', args=[self.session.uuid]))
        self.assertEqual(Evaluation.objects.filter(presentation_turn=self.turn).count(), 1)

    def test_team_peer_grading_has_its_own_active_privacy_notice(self):
        team_session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, service='team',
            name='Team demo privacy check', status=AssessmentSession.Status.LIVE,
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID,
        )
        team_session.students.create(full_name='Team learner', student_id='TEAM-01')
        join_url = reverse('student_join', args=[team_session.uuid])
        notice_url = reverse('student_privacy_notice', args=[team_session.uuid])

        self.client.get(join_url)
        joined = self.client.post(join_url, {'identifier': 'TEAM-01'})
        self.assertRedirects(joined,
            reverse('student_evaluate', args=[team_session.uuid]),
            fetch_redirect_response=False)

        notice_page = self.client.get(notice_url)
        self.assertEqual(notice_page.status_code, 200)
        self.assertContains(notice_page, 'How your Team demo information is used')
        self.assertContains(notice_page, 'cannot see which learner submitted a particular peer score')
        self.assertContains(notice_page, 'named competency assessments and evidence')

        accepted = self.client.post(notice_url, {'action': 'continue'})
        self.assertRedirects(accepted,
            reverse('student_evaluate', args=[team_session.uuid]),
            fetch_redirect_response=False)
        record = ParticipationRecord.objects.get(
            assessment_session=team_session, student__student_id='TEAM-01')
        self.assertEqual(record.privacy_notice.service, 'team')

    def test_json_submission_cannot_bypass_a_notice_that_has_not_been_shown(self):
        self.join_without_viewing_notice()
        response = self.client.post(
            self.evaluate_url, {f'group_{self.category.pk}': '8'},
            HTTP_ACCEPT='application/json', content_type='application/json',
        )
        self.assertEqual(response.status_code, 428)
        self.assertEqual(response.json(), {
            'status': 'privacy_notice_required', 'next_url': self.notice_url,
        })
        self.assertFalse(Evaluation.objects.exists())

    def test_each_new_active_version_is_shown_and_recorded(self):
        self.join_without_viewing_notice()
        self.client.get(self.notice_url)
        first = PrivacyNotice.objects.get(service='classroom', version='3')
        first.is_active = False
        first.save(update_fields=['is_active'])
        second = PrivacyNotice.objects.create(
            service='classroom', version='4', title='Updated privacy notice',
            body='Updated version content.', is_active=True,
        )

        response = self.client.get(self.evaluate_url)
        self.assertRedirects(response, self.notice_url, fetch_redirect_response=False)
        notice_page = self.client.get(self.notice_url)
        self.assertContains(notice_page, 'Updated privacy notice')
        self.assertContains(notice_page, 'Updated version content.')
        self.assertContains(notice_page, 'Notice version 4')
        self.client.post(self.notice_url, {'action': 'continue'})
        record = ParticipationRecord.objects.get(assessment_session=self.session, student=self.evaluator)
        self.assertEqual(record.privacy_notice_id, second.pk)

    def test_post_before_the_notice_was_served_does_not_mark_it_as_seen(self):
        self.join_without_viewing_notice()
        browser = self.client.session
        browser[NOTICE_SHOWN_SESSION_KEY] = []
        browser.save()
        response = self.client.post(self.notice_url, {'action': 'continue'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Review the notice on this page', status_code=400)
        self.assertIsNone(ParticipationRecord.objects.get(
            assessment_session=self.session, student=self.evaluator,
        ).privacy_notice_id)

    def test_notice_content_is_escaped_and_scoped_to_the_session_service(self):
        notice = PrivacyNotice.objects.get(service='classroom', version='3')
        notice.is_active = False
        notice.save(update_fields=['is_active'])
        notice = PrivacyNotice.objects.create(
            service='classroom', version='xss', title='Test notice',
            body='<script>alert(1)</script>', is_active=True,
        )
        self.join_without_viewing_notice()
        page = self.client.get(self.notice_url)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;', html=False)
        self.assertNotContains(page, '<script>alert(1)</script>', html=False)

        self.session.service = 'panel'
        self.session.save(update_fields=['service'])
        self.client.session[STUDENT_SESSION_KEY] = [self.evaluator.pk]
        self.client.session.save()
        unavailable = self.client.get(self.evaluate_url)
        self.assertEqual(unavailable.status_code, 503)
        self.assertContains(unavailable, "can't show the privacy notice", status_code=503)

    def test_notice_route_requires_a_join_and_fails_closed_without_an_active_notice(self):
        response = self.client.get(self.notice_url)
        self.assertRedirects(response, self.join_url, fetch_redirect_response=False)
        self.join_without_viewing_notice()
        PrivacyNotice.objects.filter(service='classroom').update(is_active=False)
        page = self.client.get(self.notice_url)
        self.assertEqual(page.status_code, 503)
        self.assertContains(page, "can't show the privacy notice", status_code=503)
        api = self.client.get(self.evaluate_url, HTTP_ACCEPT='application/json')
        self.assertEqual(api.status_code, 503)
        self.assertEqual(api.json(), {'status': 'privacy_notice_unavailable'})

    def test_published_content_is_immutable_and_referenced_versions_cannot_be_deleted(self):
        self.join_without_viewing_notice()
        self.client.get(self.notice_url)
        self.client.post(self.notice_url, {'action': 'continue'})
        notice = PrivacyNotice.objects.get(service='classroom', version='3')
        notice.title = 'Silently replaced'
        with self.assertRaises(ValidationError):
            notice.save()
        with self.assertRaises(ProtectedError):
            PrivacyNotice.objects.filter(service='classroom', version='3').delete()

    def test_notice_display_name_is_service_and_version(self):
        notice = PrivacyNotice.objects.get(service='classroom', version='3')
        self.assertEqual(str(notice), 'classroom privacy notice 3')
        self.assertEqual((notice.version, notice.title, notice.body), (
            DEFAULT_PRIVACY_NOTICE_VERSION, DEFAULT_PRIVACY_NOTICE_TITLE, DEFAULT_PRIVACY_NOTICE_BODY,
        ))

    def test_closed_sessions_do_not_require_a_current_notice_to_show_the_closing_page(self):
        self.join_without_viewing_notice()
        self.session.status = AssessmentSession.Status.CLOSED
        self.session.save(update_fields=['status'])
        page = self.client.get(self.evaluate_url)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, 'assessments/student/closed.html')
