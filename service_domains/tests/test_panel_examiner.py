from collections import Counter
from datetime import timedelta
from decimal import Decimal
import re
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import Http404
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    PanelAbstention, PanelAuditEvent, PanelCandidate, PanelCriterion, PanelCriterionLevel,
    PanelDecision, PanelHearing,
    PanelInvitationAttempt,
    PanelInvitation, PanelScore, PanelScoringPanel,
    PanelWrittenReview, PanelVote, PanelLiveDisplayLink,
)
from service_domains.operations import (abstain_panel_score, issue_panel_invitation,
                                        open_panel_hearing, bind_panel_invitation_device,
                                        issue_panel_device_cookie, activate_panel_candidate)
from service_domains.views import (_masked_invitation_email, panel_examiner,
                                   panel_hearing_list, panel_hearing_manage)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   DEFAULT_FROM_EMAIL='noreply@example.test')
class PanelExaminerViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel examiner org')
        self.owner = make_user('panel-examiner-owner')
        self.hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Research defence', status=PanelHearing.Status.OPEN)
        self.candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, display_name='Candidate A')
        self.criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=self.hearing, name='Evidence', description='Use the evidence.')
        activate_panel_candidate(self.hearing, self.candidate,
            organization=self.org.as_claim(), user=self.owner)
        self.invitation, self.token = issue_panel_invitation(self.hearing,
            'examiner@example.test', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        self.url = reverse('panel_examiner', kwargs={'token': self.token})
        self.device_cookie_name = f'livegrade_panel_examiner_{self.invitation.pk}'
        self.device_cookie = issue_panel_device_cookie(self.invitation)
        self.client.cookies[self.device_cookie_name] = self.device_cookie
        bind_panel_invitation_device(self.invitation, self.device_cookie)

    def request(self, method, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            return getattr(self.client, method)(*args, **kwargs)

    def test_invited_examiner_can_open_phone_page_and_submit_private_score(self):
        response = self.request('get', self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.device_cookie_name, self.client.cookies)
        self.invitation.refresh_from_db()
        self.assertEqual(len(self.invitation.device_digest), 64)
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.DEVICE_BOUND).count(), 1)
        self.assertContains(response, 'Research defence')
        self.assertContains(response, 'Candidate A')
        self.assertContains(response, 'Use the evidence.')
        self.assertContains(response, 'I cannot assess this candidate fairly')
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow, noarchive')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertFalse(response.wsgi_request.user.is_authenticated)

        posted = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '86.5', 'comment': 'Clear evidence.',
        })
        self.assertEqual(posted.status_code, 302)
        score = PanelScore.objects.get(invitation=self.invitation)
        self.assertEqual(str(score.score), '86.50')
        self.assertEqual(score.comment, 'Clear evidence.')
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.SCORE_SUBMITTED).count(), 1)
        shown = self.request('get', self.url)
        self.assertContains(shown, '86.50')

    def test_examiner_page_shows_owner_defined_score_anchors_as_guidance(self):
        PanelCriterionLevel.objects.create(organization_id=self.org.id,
            created_by=self.owner, criterion=self.criterion, score=Decimal('0'),
            description='Not demonstrated')
        PanelCriterionLevel.objects.create(organization_id=self.org.id,
            created_by=self.owner, criterion=self.criterion, score=Decimal('100'),
            description='Fully demonstrated')
        response = self.request('get', self.url)
        self.assertContains(response, '0 points:')
        self.assertContains(response, 'Not demonstrated')
        self.assertContains(response, '100 points:')
        self.assertContains(response, 'Fully demonstrated')
        self.assertContains(response, 'step="0.01"')

    def test_conflict_submission_recuses_examiner_and_score_route_refuses(self):
        conflict = self.request('post', self.url, {
            'action': 'conflict', 'candidate_id': self.candidate.pk,
            'reason': 'I supervised this candidate.',
        })
        self.assertEqual(conflict.status_code, 302)
        self.assertEqual(self.invitation.conflicts.get().reason, 'I supervised this candidate.')
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.CONFLICT_DECLARED).count(), 1)
        refused = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '90',
        })
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, 'cannot be accepted', status_code=400)
        self.assertFalse(PanelScore.objects.exists())
        page = self.request('get', self.url)
        self.assertContains(page, 'You reported a conflict for this candidate')
        self.assertNotContains(page, 'name="score"')

    def test_invitation_is_bound_to_first_browser_and_denies_a_second_browser(self):
        invitation, token = issue_panel_invitation(self.hearing, 'device@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        url = reverse('panel_examiner', kwargs={'token': token})
        first_client = Client()
        cookie_name = f'livegrade_panel_examiner_{invitation.pk}'
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            first = first_client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, 'Accept invitation')
        self.assertEqual(invitation.device_digest, '')
        self.assertIn(cookie_name, first.cookies)
        self.assertTrue(first.cookies[cookie_name]['httponly'])
        self.assertEqual(first.cookies[cookie_name]['samesite'], 'Strict')
        # Repeated GETs and pre-bind POSTs do not claim the invitation.
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            prepared_again = first_client.get(url)
            unbound_score = first_client.post(url, {
                'action': 'score', 'candidate_id': self.candidate.pk,
                'criterion_id': self.criterion.pk, 'score': '89',
            })
        self.assertEqual(prepared_again.status_code, 200)
        self.assertNotIn(cookie_name, prepared_again.cookies)
        self.assertEqual(unbound_score.status_code, 403)
        self.assertEqual(invitation.device_digest, '')
        second_client = Client()
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            second_pending = second_client.get(url)
        self.assertEqual(second_pending.status_code, 200)
        self.assertIn(cookie_name, second_pending.cookies)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            bound = first_client.post(url, {'action': 'bind'})
        self.assertEqual(bound.status_code, 302)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            repeated_bind = first_client.post(url, {'action': 'bind'})
        self.assertEqual(repeated_bind.status_code, 302)
        invitation.refresh_from_db()
        self.assertEqual(len(invitation.device_digest), 64)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            second = second_client.get(url)
            second_bind = second_client.post(url, {'action': 'bind'})
            second_post = second_client.post(url, {
                'action': 'score', 'candidate_id': self.candidate.pk,
                'criterion_id': self.criterion.pk, 'score': '90',
            })
        self.assertEqual(second.status_code, 403)
        self.assertEqual(second_bind.status_code, 403)
        self.assertEqual(second_post.status_code, 403)
        self.assertNotContains(second, 'examiner@example.test', status_code=403)
        self.assertEqual(PanelInvitationAttempt.objects.filter(
            invitation=invitation, reason=PanelInvitationAttempt.Reason.DEVICE_MISMATCH).count(), 3)
        self.assertFalse(PanelScore.objects.exists())
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            first_client.get(url)
            first_post = first_client.post(url, {
                'action': 'score', 'candidate_id': self.candidate.pk,
                'criterion_id': self.criterion.pk, 'score': '86.5',
            })
        self.assertEqual(first_post.status_code, 302)
        self.assertEqual(PanelScore.objects.get().score, 86.5)

    def test_invalid_pending_browser_cookie_is_replaced_without_claiming_invitation(self):
        invitation, token = issue_panel_invitation(self.hearing, 'invalid-cookie@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        url = reverse('panel_examiner', kwargs={'token': token})
        client = Client()
        cookie_name = f'livegrade_panel_examiner_{invitation.pk}'
        self.assertEqual(bind_panel_invitation_device(invitation, 'invalid'),
                         (None, False, False))
        client.cookies[cookie_name] = 'forged-cookie'
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(cookie_name, response.cookies)
        self.assertNotEqual(response.cookies[cookie_name].value, 'forged-cookie')
        invitation.refresh_from_db()
        self.assertEqual(invitation.device_digest, '')
        self.assertEqual(_masked_invitation_email('malformed-address'),
                         'the invited email address')

    def test_examiner_link_requests_are_throttled_and_cache_failure_fails_closed(self):
        with override_settings(PANEL_EXAMINER_RATE_LIMIT=1, PANEL_EXAMINER_RATE_WINDOW=30):
            first = self.request('get', self.url)
            second = self.request('get', self.url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second['Retry-After'], '30')

        with patch('service_domains.views.allow_panel_examiner_request', return_value=None):
            unavailable = self.request('get', self.url)
        self.assertEqual(unavailable.status_code, 503)

    def test_duplicate_scores_invalid_actions_and_unknown_references_are_rejected(self):
        payload = {'action': 'score', 'candidate_id': self.candidate.pk,
                   'criterion_id': self.criterion.pk, 'score': '80'}
        accepted = self.request('post', self.url, payload)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(PanelAuditEvent.objects.filter(
            hearing=self.hearing, action=PanelAuditEvent.Action.SCORE_SUBMITTED).count(), 1)
        duplicate = self.request('post', self.url, payload)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(PanelScore.objects.count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(
            hearing=self.hearing, action=PanelAuditEvent.Action.SCORE_SUBMITTED).count(), 1)
        invalid = self.request('post', self.url, {
            'action': 'other', 'candidate_id': self.candidate.pk,
        })
        self.assertEqual(invalid.status_code, 400)
        unknown = self.request('post', self.url, {
            'action': 'score', 'candidate_id': '999999',
            'criterion_id': self.criterion.pk, 'score': '50',
        })
        self.assertEqual(unknown.status_code, 404)
        unknown_criterion = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': '999999', 'score': '50',
        })
        self.assertEqual(unknown_criterion.status_code, 404)
        invalid_score = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '101',
        })
        self.assertEqual(invalid_score.status_code, 400)
        self.assertEqual(PanelScore.objects.count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(
            hearing=self.hearing, action=PanelAuditEvent.Action.SCORE_SUBMITTED).count(), 1)

    def test_integrity_race_is_recorded_as_a_duplicate_attempt(self):
        with patch('service_domains.views.submit_panel_score', side_effect=IntegrityError):
            response = self.request('post', self.url, {
                'action': 'score', 'candidate_id': self.candidate.pk,
                'criterion_id': self.criterion.pk, 'score': '80',
            })
        self.assertEqual(response.status_code, 400)
        attempt = PanelInvitationAttempt.objects.get(invitation=self.invitation)
        self.assertEqual((attempt.outcome, attempt.reason), ('rejected', 'duplicate'))
        self.assertFalse(PanelAuditEvent.objects.filter(
            hearing=self.hearing, action=PanelAuditEvent.Action.SCORE_SUBMITTED).exists())

    def test_accepted_and_rejected_attempts_are_durable_minimal_and_append_only(self):
        self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '80', 'comment': 'Private text.',
        })
        accepted = PanelInvitationAttempt.objects.get(invitation=self.invitation)
        self.assertEqual((accepted.action, accepted.outcome, accepted.reason),
            ('score', 'accepted', 'submitted'))
        rejected = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '101', 'comment': 'Do not log me.',
        })
        self.assertEqual(rejected.status_code, 400)
        failed = PanelInvitationAttempt.objects.filter(invitation=self.invitation).latest('pk')
        self.assertEqual((failed.action, failed.outcome, failed.reason),
            ('score', 'rejected', 'invalid_input'))
        serialized = str(list(PanelInvitationAttempt.objects.values()))
        self.assertNotIn(self.token, serialized)
        self.assertNotIn('Private text.', serialized)
        self.assertNotIn('Do not log me.', serialized)
        field_names = {field.name for field in PanelInvitationAttempt._meta.fields}
        self.assertFalse({'ip_address', 'user_agent', 'token'} & field_names)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            failed.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            failed.delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelInvitationAttempt.objects.filter(pk=failed.pk).update(reason='changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            PanelInvitationAttempt.objects.filter(pk=failed.pk).delete()

    def test_rate_limited_and_unavailable_posts_are_logged(self):
        payload = {'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '80'}
        with override_settings(PANEL_EXAMINER_RATE_LIMIT=1, PANEL_EXAMINER_RATE_WINDOW=30):
            self.assertEqual(self.request('get', self.url).status_code, 200)
            limited = self.request('post', self.url, payload)
        self.assertEqual(limited.status_code, 429)
        attempt = PanelInvitationAttempt.objects.get(invitation=self.invitation)
        self.assertEqual((attempt.outcome, attempt.reason), ('rate_limited', 'rate_limit'))

        with patch('service_domains.views.allow_panel_examiner_request', return_value=None):
            unavailable = self.request('post', self.url, payload)
        self.assertEqual(unavailable.status_code, 503)
        attempt = PanelInvitationAttempt.objects.filter(invitation=self.invitation).latest('pk')
        self.assertEqual((attempt.outcome, attempt.reason),
            ('unavailable', 'throttle_unavailable'))

    def test_examiner_can_abstain_once_and_cannot_later_score_that_criterion(self):
        page = self.request('get', self.url)
        self.assertContains(page, 'Skip this criterion')
        response = self.request('post', self.url, {
            'action': 'abstain', 'abstention_reason': 'Unable to assess this criterion.', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk,
        })
        self.assertEqual(response.status_code, 302)
        abstention = PanelAbstention.objects.get(invitation=self.invitation,
            candidate=self.candidate, criterion=self.criterion)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.SCORE_ABSTAINED).count(), 1)
        shown = self.request('get', self.url)
        self.assertContains(shown, 'You chose not to score this criterion')
        self.assertNotContains(shown, 'name="score"')
        refused_score = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '80',
        })
        self.assertEqual(refused_score.status_code, 400)
        self.assertFalse(PanelScore.objects.exists())
        duplicate = self.request('post', self.url, {
            'action': 'abstain', 'abstention_reason': 'Unable to assess this criterion.', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk,
        })
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(PanelAbstention.objects.count(), 1)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            abstention.delete()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            PanelAbstention.objects.filter(pk=abstention.pk).update(criterion=self.criterion)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            PanelAbstention.objects.filter(pk=abstention.pk).delete()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            abstention.save()

        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Separate hearing')
        foreign_candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, display_name='Foreign')
        foreign_criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, name='Foreign rule')
        wrong_candidate = PanelAbstention(invitation=self.invitation,
            candidate=foreign_candidate, criterion=self.criterion,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'candidate must belong'):
            wrong_candidate.full_clean()
        wrong_criterion = PanelAbstention(invitation=self.invitation,
            candidate=self.candidate, criterion=foreign_criterion,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'criterion must belong'):
            wrong_criterion.full_clean()

    def test_abstention_rejected_after_score_and_when_examiner_is_recused(self):
        scored = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '80',
        })
        self.assertEqual(scored.status_code, 302)
        refused = self.request('post', self.url, {
            'action': 'abstain', 'abstention_reason': 'Unable to assess this criterion.', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk,
        })
        self.assertEqual(refused.status_code, 400)
        self.assertFalse(PanelAbstention.objects.exists())

        self.invitation.scores.all().delete()
        self.invitation.conflicts.create(organization_id=self.org.id,
            created_by=self.owner, candidate=self.candidate, reason='Declared conflict.')
        recused = self.request('post', self.url, {
            'action': 'abstain', 'abstention_reason': 'Unable to assess this criterion.', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk,
        })
        self.assertEqual(recused.status_code, 400)
        self.assertFalse(PanelAbstention.objects.exists())

    def test_abstention_operation_rejects_foreign_references_and_closed_hearing(self):
        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Unrelated hearing', status=PanelHearing.Status.OPEN)
        foreign_candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, display_name='Foreign candidate')
        foreign_criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, name='Foreign criterion')
        with self.assertRaises(PermissionDenied):
            abstain_panel_score(self.token, foreign_candidate, self.criterion)
        with self.assertRaises(PermissionDenied):
            abstain_panel_score(self.token, self.candidate, foreign_criterion)
        self.hearing.status = PanelHearing.Status.REVIEW
        self.hearing.save(update_fields=('status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'not accepting abstentions'):
            abstain_panel_score(self.token, self.candidate, self.criterion)

    def test_expired_revoked_and_closed_hearing_links_are_not_usable(self):
        self.invitation.expires_at = timezone.now() - timedelta(seconds=1)
        self.invitation.save(update_fields=('expires_at', 'updated_at'))
        self.assertEqual(self.request('get', self.url).status_code, 404)
        expired_post = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '70',
        })
        self.assertEqual(expired_post.status_code, 404)
        self.assertEqual(PanelInvitationAttempt.objects.get().reason, 'invitation_unusable')
        self.invitation.expires_at = timezone.now() + timedelta(hours=1)
        self.invitation.revoked_at = timezone.now()
        self.invitation.save(update_fields=('expires_at', 'revoked_at', 'updated_at'))
        self.assertEqual(self.request('get', self.url).status_code, 404)
        revoked_post = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '70',
        })
        self.assertEqual(revoked_post.status_code, 404)
        self.assertEqual(PanelInvitationAttempt.objects.filter(reason='invitation_unusable').count(), 2)
        self.invitation.revoked_at = None
        self.invitation.save(update_fields=('revoked_at', 'updated_at'))
        self.hearing.status = PanelHearing.Status.REVIEW
        self.hearing.save(update_fields=('status', 'updated_at'))
        self.assertEqual(self.request('get', self.url).status_code, 404)
        closed_post = self.request('post', self.url, {
            'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '75',
        })
        self.assertEqual(closed_post.status_code, 404)
        attempt = PanelInvitationAttempt.objects.filter(invitation=self.invitation).latest('pk')
        self.assertEqual((attempt.outcome, attempt.reason), ('rejected', 'hearing_closed'))

    def test_unusable_link_attempts_are_limited_and_unknown_tokens_are_not_persisted(self):
        self.invitation.expires_at = timezone.now() - timedelta(seconds=1)
        self.invitation.save(update_fields=('expires_at', 'updated_at'))
        unknown_url = reverse('panel_examiner', kwargs={'token': 'not-a-known-token'})
        unknown = self.request('post', unknown_url, {'action': 'score'})
        self.assertEqual(unknown.status_code, 404)
        self.assertFalse(PanelInvitationAttempt.objects.exists())
        with patch('service_domains.views.allow_unknown_bearer_request', return_value=False):
            limited_unknown = self.request('get', unknown_url)
        self.assertEqual(limited_unknown.status_code, 429)
        self.assertIn('Retry-After', limited_unknown)
        with patch('service_domains.views.allow_unknown_bearer_request', return_value=None):
            unavailable_unknown = self.request('get', unknown_url)
        self.assertEqual(unavailable_unknown.status_code, 503)
        self.assertFalse(PanelInvitationAttempt.objects.exists())
        long_unknown_url = reverse('panel_examiner', kwargs={'token': 'x' * 257})
        with patch('service_domains.views.allow_unknown_bearer_request', return_value=True):
            self.assertEqual(self.request('get', long_unknown_url).status_code, 404)

        payload = {'action': 'score', 'candidate_id': self.candidate.pk,
            'criterion_id': self.criterion.pk, 'score': '70'}
        with patch('service_domains.views.allow_panel_examiner_request', return_value=None):
            unavailable = self.request('post', self.url, payload)
        self.assertEqual(unavailable.status_code, 404)
        self.assertEqual(PanelInvitationAttempt.objects.get().outcome, 'unavailable')

        with patch('service_domains.views.allow_panel_examiner_request', return_value=False):
            limited = self.request('post', self.url, payload)
        self.assertEqual(limited.status_code, 404)
        self.assertEqual(PanelInvitationAttempt.objects.count(), 2)
        self.assertEqual(PanelInvitationAttempt.objects.latest('pk').outcome, 'rate_limited')

    def test_conflict_requires_reason_and_cannot_target_foreign_candidate(self):
        missing = self.request('post', self.url, {
            'action': 'conflict', 'candidate_id': self.candidate.pk, 'reason': '  ',
        })
        self.assertEqual(missing.status_code, 400)
        self.assertFalse(self.invitation.conflicts.exists())
        other_hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other hearing', status=PanelHearing.Status.OPEN)
        foreign = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=other_hearing, display_name='Foreign candidate')
        refused = self.request('post', self.url, {
            'action': 'conflict', 'candidate_id': foreign.pk, 'reason': 'Reason.',
        })
        self.assertEqual(refused.status_code, 404)
        self.assertFalse(self.invitation.conflicts.exists())

    def test_anonymous_state_changes_require_csrf(self):
        invitation, token = issue_panel_invitation(self.hearing, 'csrf@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        url = reverse('panel_examiner', kwargs={'token': token})
        csrf_client = Client(enforce_csrf_checks=True)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            page = csrf_client.get(url)
            self.assertEqual(page.status_code, 200)
            response = csrf_client.post(url, {
                'action': 'score', 'candidate_id': self.candidate.pk,
                'criterion_id': self.criterion.pk, 'score': '75',
            })
        self.assertEqual(response.status_code, 403)
        invitation.refresh_from_db()
        self.assertEqual(invitation.device_digest, '')
        self.assertFalse(PanelScore.objects.exists())

    def test_examiner_view_requires_active_panel_service_scope(self):
        request = RequestFactory().get(self.url)
        with self.assertRaises(Http404):
            panel_examiner(request, self.token)


class PanelOwnerWorkspaceTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel owner org')
        self.owner = make_user('panel-owner-workspace')
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def test_scored_hearing_open_button_tracks_examiner_acceptance(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Readiness check', workflow=PanelHearing.Workflow.SCORED)
        PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate A')
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Evidence')
        panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Default panel', share=Decimal('100.00'))
        panel.criteria.add(criterion)
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))

        needs_acceptance = self.request('get', manage)
        self.assertEqual(needs_acceptance.status_code, 200)
        self.assertFalse(needs_acceptance.context['setup_ready'])
        self.assertContains(needs_acceptance, 'disabled aria-describedby="open-readiness-help"')
        self.assertRegex(needs_acceptance.content.decode(),
            r'<div class="panel-field-label-row">\s*<label[^>]*>Decision process:</label>\s*'
            r'<button[^>]*aria-label="More about decision process"')

        invitation, _token = issue_panel_invitation(hearing, 'examiner@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        PanelInvitation.objects.filter(pk=invitation.pk).update(confirmed_at=timezone.now())
        ready = self.request('get', manage)
        self.assertEqual(ready.status_code, 200)
        self.assertTrue(ready.context['setup_ready'])
        self.assertNotContains(ready, 'aria-describedby="open-readiness-help"')

    def test_setup_forms_have_unique_accessible_control_ids(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Accessible setup', workflow=PanelHearing.Workflow.SCORED)
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Evidence')
        first_panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Panel A', share=Decimal('50.00'))
        second_panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Panel B', share=Decimal('50.00'))
        first_panel.criteria.add(criterion)
        second_panel.criteria.add(criterion)

        response = self.request('get', reverse('panel_hearing_manage', args=(hearing.pk,)))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        ids = re.findall(r'\bid="([^"]+)"', html)
        duplicates = [element_id for element_id, count in Counter(ids).items() if count > 1]
        self.assertEqual(duplicates, [])
        self.assertEqual(html.count('id="candidate_display_name"'), 1)
        self.assertEqual(html.count(f'id="panel_edit_{first_panel.pk}_name"'), 1)

    def test_multi_candidate_results_review_forms_have_unique_ids_and_bind_correctly(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Multi-candidate outcome review',
            status=PanelHearing.Status.REVIEW, workflow=PanelHearing.Workflow.OUTCOME)
        candidates = [PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name=f'Candidate {index}', order=index)
            for index in (1, 2)]
        url = reverse('panel_results_review', args=(hearing.pk,))

        response = self.request('get', url)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        ids = re.findall(r'\bid="([^"]+)"', html)
        duplicates = [element_id for element_id, count in Counter(ids).items() if count > 1]
        self.assertEqual(duplicates, [])
        self.assertEqual(html.count(f'id="id_decision_{candidates[0].pk}-outcome"'), 1)
        self.assertEqual(html.count(f'id="id_decision_{candidates[1].pk}-outcome"'), 1)

        recorded = self.request('post', url, {
            'action': 'decision', 'candidate_id': str(candidates[1].pk),
            f'decision_{candidates[1].pk}-outcome': PanelDecision.Outcome.ACCEPT,
            f'decision_{candidates[1].pk}-rationale': 'Evidence meets the agreed criteria.',
            f'decision_{candidates[1].pk}-conditions': '',
            f'decision_{candidates[1].pk}-dissent': '',
        })
        self.assertEqual(recorded.status_code, 302)
        self.assertEqual(PanelDecision.objects.filter(hearing=hearing).count(), 1)
        self.assertEqual(PanelDecision.objects.get(hearing=hearing).candidate, candidates[1])

    def test_chair_outcome_workflow_uses_non_examiner_opening_copy(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Chair outcome', workflow=PanelHearing.Workflow.OUTCOME)
        PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate A')
        response = self.request('get', reverse('panel_hearing_manage', args=(hearing.pk,)))

        self.assertTrue(response.context['setup_ready'])
        self.assertFalse(response.context['requires_examiners'])
        self.assertEqual([step['label'] for step in response.context['setup_checklist']],
            ['Choose a workflow', 'Add at least one candidate'])
        self.assertContains(response, 'Next: review setup')
        self.assertNotContains(response, 'Examiner groups')
        self.assertNotContains(response, 'Invite examiners')
        self.assertEqual(self.request('get', reverse('panel_invitation_import',
            args=(hearing.pk,))).status_code, 404)
        self.assertEqual(self.request('get', reverse('panel_invitation_import_sample',
            args=(hearing.pk,))).status_code, 404)
        self.assertContains(response, 'Check the details before opening.')
        self.assertContains(response, '>Open assessment</button>')
        self.assertNotContains(response, 'Open assessment for examiners')

        denied_invitation = self.request('post', reverse('panel_hearing_manage',
            args=(hearing.pk,)), {'action': 'invite', 'email': 'examiner@example.test',
                'expires_at': (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')})
        self.assertEqual(denied_invitation.status_code, 400)
        self.assertContains(denied_invitation,
            'This chair-recorded process does not use examiner invitations.', status_code=400)
        self.assertFalse(PanelInvitation.objects.filter(hearing=hearing).exists())

        opened = self.request('post', reverse('panel_hearing_manage', args=(hearing.pk,)),
            {'action': 'open'}, follow=True)
        self.assertContains(opened, 'Assessment opened for the chair to record outcomes.')
        self.assertContains(opened, 'When the discussion is complete, record an outcome for each candidate')
        self.assertContains(opened, 'Finish and record outcomes')
        self.assertNotContains(opened, 'Invitation management')
        closed = self.request('post', reverse('panel_hearing_manage', args=(hearing.pk,)),
            {'action': 'review'}, follow=True)
        self.assertContains(closed, 'Assessment closed. The chair can now record outcomes.')
        self.assertContains(closed, 'Save decision')

    def test_owner_can_create_configure_open_and_invite_to_hearing(self):
        listing = reverse('panel_hearing_list')
        guide = self.request('get', listing)
        self.assertContains(guide, 'You haven’t created an assessment yet')
        self.assertContains(guide, 'Create an assessment')
        self.assertContains(guide, reverse('panel_hearing_create'))
        created = self.request('post', reverse('panel_hearing_create'), {
            'name': 'Engineering defence', 'aggregation': 'mean',
            'minimum_examiners': '1', 'workflow': PanelHearing.Workflow.SCORED,
        })
        hearing = PanelHearing.objects.get(name='Engineering defence')
        self.assertEqual(created.status_code, 302)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.HEARING_CREATED).count(), 1)
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        initial_setup = self.request('get', manage)
        self.assertContains(initial_setup, 'Name and schedule')
        self.assertContains(initial_setup, 'Add at least one candidate')
        self.assertContains(initial_setup, 'Set panel shares to exactly 100%')

        refused_open = self.request('post', manage, {'action': 'open'})
        hearing.refresh_from_db()
        self.assertEqual(refused_open.status_code, 400)
        self.assertEqual(hearing.status, PanelHearing.Status.DRAFT)
        self.assertContains(refused_open,
            'Add at least one candidate and the required scoring criteria before opening.',
            status_code=400)

        candidate = self.request('post', manage, {
            'action': 'candidate', 'display_name': 'Candidate A', 'reference': 'A-1', 'order': '0',
        })
        criterion = self.request('post', manage, {
            'action': 'criterion', 'name': 'Evidence', 'description': 'Ground the result.', 'order': '0',
        })
        self.assertEqual(candidate.status_code, 302)
        self.assertEqual(criterion.status_code, 302)
        completed_setup = self.request('get', manage)
        self.assertContains(completed_setup, 'Add at least one candidate')
        self.assertFalse(completed_setup.context['setup_ready'])
        self.assertFalse(completed_setup.context['setup_checklist'][-1]['complete'])
        self.assertContains(completed_setup, 'disabled aria-describedby="open-readiness-help"')
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.CANDIDATE_ADDED).count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.CRITERION_ADDED).count(), 1)

        refused_without_examiner = self.request('post', manage, {'action': 'open'})
        self.assertEqual(refused_without_examiner.status_code, 400)
        self.assertContains(refused_without_examiner,
            '0 accepted examiner links; 1 are required', status_code=400)
        hearing.refresh_from_db()
        self.assertEqual(hearing.status, PanelHearing.Status.DRAFT)

        expiry = (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        with patch('service_domains.views.send_panel_invitation_email.delay'), \
             self.captureOnCommitCallbacks(execute=True):
            invitation_response = self.request('post', manage, {
                'action': 'invite', 'email': 'examiner@example.test', 'expires_at': expiry,
            })
        self.assertEqual(invitation_response.status_code, 302)
        invitation = PanelInvitation.objects.get(hearing=hearing)
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.PENDING)
        self.assertEqual(invitation.token_digest, None)
        from service_domains.tasks import send_panel_invitation_email
        with patch('service_domains.tasks.secrets.token_urlsafe', return_value='known-test-token'):
            send_panel_invitation_email(invitation.pk)
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['examiner@example.test'])
        self.assertIn('/panel/examiner/', mail.outbox[0].body)
        self.assertIn('/panel/examiner/known-test-token', mail.outbox[0].body)
        self.assertEqual(invitation.token_digest, PanelInvitation.digest_token('known-test-token'))
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.INVITATION_ISSUED).count(), 1)

        examiner_client = Client()
        examiner_url = reverse('panel_examiner', kwargs={'token': 'known-test-token'})
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            bind_page = examiner_client.get(examiner_url)
            self.assertEqual(bind_page.status_code, 200)
            bound = examiner_client.post(examiner_url, {'action': 'bind'})
        self.assertEqual(bound.status_code, 302)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.confirmed_at)

        ready_to_open = self.request('get', manage)
        self.assertTrue(ready_to_open.context['setup_ready'])
        self.assertTrue(all(step['complete'] for step in ready_to_open.context['setup_checklist']))
        self.assertNotContains(ready_to_open, 'aria-describedby="open-readiness-help"')
        opened = self.request('post', manage, {'action': 'open'})
        self.assertEqual(opened.status_code, 302)
        hearing.refresh_from_db()
        self.assertEqual(hearing.status, PanelHearing.Status.OPEN)
        late_candidate = self.request('post', manage, {
            'action': 'candidate', 'display_name': 'Late candidate', 'reference': 'L-1', 'order': '2',
        })
        self.assertEqual(late_candidate.status_code, 400)
        self.assertFalse(hearing.candidates.filter(display_name='Late candidate').exists())
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.HEARING_OPENED).count(), 1)

    def test_owner_can_configure_shares_and_assign_criteria_to_selected_panels(self):
        self.request('post', reverse('panel_hearing_create'), {
            'name': 'Split panel hearing', 'aggregation': 'mean', 'minimum_examiners': '1',
            'workflow': PanelHearing.Workflow.SCORED,
        })
        hearing = PanelHearing.objects.get(name='Split panel hearing')
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        default = hearing.panels.get(name='Default panel')
        added = self.request('post', manage, {
            'action': 'panel', 'name': 'External panel', 'admission_method': 'invited',
            'share': '50.00', 'aggregation': 'median', 'minimum_examiners': '1',
            'trim_fraction': '',
        })
        self.assertEqual(added.status_code, 302)
        external = hearing.panels.get(name='External panel')
        invalid_update = self.request('post', manage, {
            'action': 'update_panel', 'panel_id': default.pk, 'name': 'Default panel',
            'admission_method': 'invited', 'share': '150.00', 'aggregation': 'mean',
            'minimum_examiners': '1', 'trim_fraction': '',
        })
        self.assertEqual(invalid_update.status_code, 400)
        missing_panel = self.request('post', manage, {
            'action': 'update_panel', 'panel_id': '999999', 'name': 'Missing panel',
            'admission_method': 'invited', 'share': '50.00', 'aggregation': 'mean',
            'minimum_examiners': '1', 'trim_fraction': '',
        })
        self.assertEqual(missing_panel.status_code, 404)
        updated = self.request('post', manage, {
            'action': 'update_panel', 'panel_id': default.pk, 'name': 'Default panel',
            'admission_method': 'invited', 'share': '50.00', 'aggregation': 'mean',
            'minimum_examiners': '1', 'trim_fraction': '',
        })
        self.assertEqual(updated.status_code, 302)
        criterion = self.request('post', manage, {
            'action': 'criterion', 'name': 'External evidence', 'description': '',
            'order': '0', 'panels': [str(external.pk)],
        })
        self.assertEqual(criterion.status_code, 302)
        item = hearing.criteria.get(name='External evidence')
        self.assertEqual(list(item.panels.values_list('pk', flat=True)), [external.pk])
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.PANEL_ADDED).count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.PANEL_UPDATED).count(), 1)

    def test_opening_legacy_hearing_creates_its_default_panel_and_checks_share_total(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Legacy panel setup',
            workflow=PanelHearing.Workflow.OUTCOME)
        PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate')
        PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Criterion')
        opened = open_panel_hearing(hearing, organization=self.org.as_claim(), user=self.owner)
        panel = PanelScoringPanel.objects.get(hearing=hearing)
        self.assertEqual(opened.status, PanelHearing.Status.OPEN)
        self.assertEqual(panel.share, Decimal('100.00'))
        self.assertTrue(panel.criteria.filter(hearing=hearing).exists())

    def test_opening_hearing_rejects_panel_shares_that_do_not_total_one_hundred(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Invalid shares')
        PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate')
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Criterion')
        panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Underweight panel',
            share=Decimal('90.00'))
        criterion.panels.add(panel)
        with self.assertRaisesMessage(ValidationError, 'total exactly 100%'):
            open_panel_hearing(hearing, organization=self.org.as_claim(), user=self.owner)
        hearing.refresh_from_db()
        self.assertEqual(hearing.status, PanelHearing.Status.DRAFT)

    def test_opening_hearing_rejects_a_panel_without_any_assigned_criterion(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Unassigned panel criterion')
        PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate')
        PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Criterion')
        PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Empty panel', share=Decimal('100.00'))
        with self.assertRaisesMessage(ValidationError, 'Assign at least one criterion'):
            open_panel_hearing(hearing, organization=self.org.as_claim(), user=self.owner)

    def test_review_page_keeps_hearing_open_when_a_candidate_has_no_scores(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='No score review', status=PanelHearing.Status.OPEN)
        PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate')
        PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Criterion')
        response = self.request('post', reverse('panel_hearing_manage', args=(hearing.pk,)),
            {'action': 'review'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Results cannot be compiled because no eligible examiner scored',
            status_code=400)
        hearing.refresh_from_db()
        self.assertEqual(hearing.status, PanelHearing.Status.OPEN)
    def test_invitation_email_failure_is_recorded_without_exposing_bearer_link(self):
        sign_in(self.client, self.owner, self.org)
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Fallback defence')
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        expiry = (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        with patch('service_domains.views.send_panel_invitation_email.delay'), \
             self.captureOnCommitCallbacks(execute=True):
            response = self.request('post', manage, {
                'action': 'invite', 'email': 'fallback@example.test', 'expires_at': expiry,
            })
        self.assertEqual(response.status_code, 302)
        invitation = PanelInvitation.objects.get(hearing=hearing)
        from service_domains.tasks import send_panel_invitation_email
        with patch('service_domains.tasks.send_mail', side_effect=OSError('mail unavailable')):
            self.assertEqual(send_panel_invitation_email(invitation.pk), 'failed')
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.FAILED)
        self.assertEqual(invitation.delivery_error, 'mail_backend_error')
        manage_page = self.request('get', manage)
        self.assertContains(manage_page, 'Resend email')
        self.assertNotContains(manage_page, '/panel/examiner/')

    def test_management_is_owner_and_organization_scoped(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Private hearing')
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        foreign_org = make_org('Other panel org')
        sign_in(self.client, self.owner, foreign_org)
        self.assertEqual(self.request('get', manage).status_code, 404)
        other = make_user('other-panel-owner')
        sign_in(self.client, other, self.org)
        self.assertEqual(self.request('get', manage).status_code, 404)

    def test_owner_forms_reject_invalid_actions_and_duplicate_hearing_children(self):
        listing = reverse('panel_hearing_create')
        invalid_hearing = self.request('post', listing, {
            'name': '', 'aggregation': 'mean', 'minimum_examiners': '2',
        })
        self.assertEqual(invalid_hearing.status_code, 200)
        self.assertEqual(PanelHearing.objects.count(), 0)

        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Draft')
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        invalid_action = self.request('post', manage, {'action': 'unexpected'})
        self.assertEqual(invalid_action.status_code, 400)
        invalid_candidate = self.request('post', manage, {
            'action': 'candidate', 'display_name': '', 'reference': '', 'order': '0',
        })
        self.assertEqual(invalid_candidate.status_code, 400)
        candidate_created = self.request('post', manage, {
            'action': 'candidate', 'display_name': 'Candidate A', 'reference': 'A-1', 'order': '0',
        })
        self.assertEqual(candidate_created.status_code, 302)
        candidate = hearing.candidates.get(reference='A-1')
        duplicate_candidate = self.request('post', manage, {
            'action': 'candidate', 'display_name': 'Duplicate', 'reference': 'A-1', 'order': '1',
        })
        self.assertEqual(duplicate_candidate.status_code, 400)
        self.assertEqual(hearing.candidates.count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.CANDIDATE_ADDED).count(), 1)
        invalid_criterion = self.request('post', manage, {
            'action': 'criterion', 'name': '', 'description': '', 'order': '0',
        })
        self.assertEqual(invalid_criterion.status_code, 400)
        criterion_created = self.request('post', manage, {
            'action': 'criterion', 'name': 'Evidence', 'description': '', 'order': '0',
            'level_descriptors': '0 | Not demonstrated\n100 | Fully demonstrated',
        })
        self.assertEqual(criterion_created.status_code, 302)
        criterion = hearing.criteria.get(name='Evidence')
        self.assertEqual(list(criterion.levels.values_list('score', 'description')),
            [(Decimal('0.00'), 'Not demonstrated'), (Decimal('100.00'), 'Fully demonstrated')])
        duplicate_criterion = self.request('post', manage, {
            'action': 'criterion', 'name': 'Evidence', 'description': '', 'order': '1',
        })
        self.assertEqual(duplicate_criterion.status_code, 400)
        self.assertEqual(hearing.criteria.count(), 1)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=hearing,
            action=PanelAuditEvent.Action.CRITERION_ADDED).count(), 1)
        self.assertTrue(candidate.pk)
        self.assertTrue(criterion.pk)

    def test_invite_and_revoke_errors_do_not_create_duplicate_audit_records(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Open hearing', status=PanelHearing.Status.OPEN)
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        invalid = self.request('post', manage, {
            'action': 'invite', 'email': 'bad-email', 'expires_at': 'not-a-date',
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(hearing.invitations.exists())

        candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate')
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Evidence')
        del candidate, criterion
        review = self.request('post', manage, {'action': 'open'})
        self.assertEqual(review.status_code, 400)
        self.assertContains(review, 'Only a draft hearing can be opened', status_code=400)

        expiry = (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        issued = self.request('post', manage, {
            'action': 'invite', 'email': 'examiner@example.test', 'expires_at': expiry,
        })
        self.assertEqual(issued.status_code, 302)
        invitation = hearing.invitations.get()
        invalid_revoke = self.request('post', manage, {
            'action': 'revoke', 'invitation_id': invitation.pk, 'reason': '',
        })
        self.assertEqual(invalid_revoke.status_code, 400)
        self.assertIsNone(hearing.invitations.get().revoked_at)
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.INVITATION_REVOKED).count(), 0)
        revoked = self.request('post', manage, {
            'action': 'revoke', 'invitation_id': invitation.pk, 'reason': 'Wrong address.',
        })
        self.assertEqual(revoked.status_code, 302)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.revoked_at)
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.INVITATION_REVOKED).count(), 1)

    def test_panel_owner_routes_require_panel_service_scope(self):
        request = RequestFactory().get(reverse('panel_hearing_list'))
        request.service = 'showcase'
        with self.assertRaises(Http404):
            panel_hearing_list(request)
        request = RequestFactory().get(reverse('panel_hearing_manage', args=(1,)))
        request.service = None
        with self.assertRaises(Http404):
            panel_hearing_manage(request, hearing_id=1)

    def test_hearing_removed_before_child_write_fails_closed(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Racing draft')
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        original_scope = PanelHearing.objects.for_organization
        calls = {'count': 0}

        def remove_on_locked_read(organization):
            calls['count'] += 1
            scoped = original_scope(organization)
            return scoped.none() if calls['count'] == 2 else scoped

        with patch('service_domains.views.PanelHearing.objects.for_organization',
                   side_effect=remove_on_locked_read):
            response = self.request('post', manage, {
                'action': 'candidate', 'display_name': 'No orphan', 'reference': '', 'order': '0',
            })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(hearing.candidates.exists())

    def test_panel_creation_validation_and_invitation_state_errors_are_rendered(self):
        listing = reverse('panel_hearing_create')
        original = PanelHearing.full_clean
        calls = {'count': 0}

        def fail_after_form_validation(instance, *args, **kwargs):
            calls['count'] += 1
            if calls['count'] == 2:
                raise ValidationError('Panel hearing validation failed.')
            return original(instance, *args, **kwargs)

        with patch.object(PanelHearing, 'full_clean', new=fail_after_form_validation):
            refused = self.request('post', listing, {
                'name': 'Invalid after form', 'aggregation': 'mean', 'minimum_examiners': '2',
                'workflow': PanelHearing.Workflow.SCORED,
            })
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, 'Panel hearing validation failed.')
        self.assertFalse(PanelHearing.objects.filter(name='Invalid after form').exists())

        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Closed hearing', status=PanelHearing.Status.SIGNED_OFF,
            signed_off_at=timezone.now())
        manage = reverse('panel_hearing_manage', args=(hearing.pk,))
        expiry = (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        invitation_refused = self.request('post', manage, {
            'action': 'invite', 'email': 'examiner@example.test', 'expires_at': expiry,
        })
        self.assertEqual(invitation_refused.status_code, 400)
        self.assertContains(invitation_refused, 'not accepting examiner invitations', status_code=400)
        missing_revoke = self.request('post', manage, {
            'action': 'revoke', 'invitation_id': '999999', 'reason': 'Reason.',
        })
        self.assertEqual(missing_revoke.status_code, 404)

    def test_open_operation_fails_closed_for_foreign_owner(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Private draft')
        with self.assertRaises(PermissionDenied):
            open_panel_hearing(hearing, organization=self.org.as_claim(), user=make_user('not-owner'))

    def test_panel_criterion_form_rejects_duplicate_out_of_range_and_too_many_anchors(self):
        from service_domains.forms import PanelCriterionForm
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Descriptor validation')
        panel = PanelScoringPanel.objects.create(hearing=hearing,
            organization_id=self.org.id, created_by=self.owner, name='Panel')
        for text in (
            '10 | First\n10.00 | Duplicate',
            '-1 | Negative',
            '101 | Too high',
            '\n'.join(f'{index} | Level {index}' for index in range(13)),
        ):
            form = PanelCriterionForm(data={'name': f'Criterion {len(text)}',
                'description': '', 'order': '0', 'level_descriptors': text,
                'panels': [str(panel.pk)]}, panels=PanelScoringPanel.objects.filter(pk=panel.pk))
            self.assertFalse(form.is_valid(), text)
            self.assertIn('level_descriptors', form.errors)

    def test_panel_criterion_edit_form_shows_preserves_and_clears_saved_anchors(self):
        from service_domains.forms import PanelCriterionForm
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Descriptor editing')
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Evidence')
        PanelCriterionLevel.objects.create(organization_id=self.org.id,
            created_by=self.owner, criterion=criterion, score=50,
            description='Partly demonstrated')
        panel = PanelScoringPanel.objects.create(hearing=hearing,
            organization_id=self.org.id, created_by=self.owner, name='Panel')
        form = PanelCriterionForm(instance=criterion, panels=PanelScoringPanel.objects.filter(pk=panel.pk))
        self.assertEqual(form.initial['level_descriptors'], '50 | Partly demonstrated')
        unchanged = PanelCriterionForm(data={'name': 'Evidence', 'description': '',
            'order': '0', 'panels': [str(panel.pk)]}, instance=criterion,
            panels=PanelScoringPanel.objects.filter(pk=panel.pk))
        self.assertTrue(unchanged.is_valid(), unchanged.errors)
        self.assertEqual(unchanged._level_descriptors, [
            {'score': Decimal('50.00'), 'description': 'Partly demonstrated'}])

    def test_panel_criterion_level_validates_required_text_owner_and_organization(self):
        from service_domains.models import PanelCriterionLevel
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Descriptor model validation')
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Evidence')
        level = PanelCriterionLevel(criterion=criterion, score=50, description=' ',
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'Enter guidance'):
            level.clean()
        valid_level = PanelCriterionLevel(criterion=criterion, score=60,
            description='Clearly demonstrated', organization_id=self.org.id,
            created_by=self.owner)
        valid_level.clean()

    def test_panel_hearing_list_dashboard_populated(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Populated dashboard hearing', status=PanelHearing.Status.OPEN)
        res = self.request('get', reverse('panel_hearing_list'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, hearing.name)

    def test_panel_hearing_manage_live_activity_json(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Activity hearing', status=PanelHearing.Status.OPEN)
        manage_url = reverse('panel_hearing_manage', args=(hearing.pk,))
        res = self.request('get', manage_url + '?activity=1')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/json')

    def test_panel_hearing_manage_update_details_and_candidate_lifecycle(self):
        sign_in(self.client, self.owner, self.org)
        draft = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Draft hearing', status=PanelHearing.Status.DRAFT)
        draft_cand = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=draft, display_name='Draft Cand')
        draft_url = reverse('panel_hearing_manage', args=(draft.pk,))

        res = self.request('post', draft_url, {
            'action': 'update_hearing',
            'name': 'Updated Draft Name',
            'aggregation': 'mean',
            'minimum_examiners': '1',
            'workflow': PanelHearing.Workflow.SCORED,
        })
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.name, 'Updated Draft Name')

        draft.status = PanelHearing.Status.OPEN
        draft.save()

        res = self.request('post', draft_url, {
            'action': 'activate_candidate',
            'candidate_id': str(draft_cand.pk),
        })
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.active_candidate_id, draft_cand.pk)

        res = self.request('post', draft_url, {'action': 'end_candidate'})
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertIsNone(draft.active_candidate_id)

    def test_panel_hearing_manage_display_link_issue_and_revoke(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Display hearing', status=PanelHearing.Status.OPEN)
        manage_url = reverse('panel_hearing_manage', args=(hearing.pk,))

        res = self.request('post', manage_url, {'action': 'issue_display_link'})
        self.assertEqual(res.status_code, 200)
        link = PanelLiveDisplayLink.objects.filter(hearing=hearing, revoked_at__isnull=True).first()
        self.assertIsNotNone(link)

        res = self.request('post', manage_url, {
            'action': 'revoke_display_link',
            'display_link_id': str(link.pk),
        })
        self.assertEqual(res.status_code, 302)
        link.refresh_from_db()
        self.assertIsNotNone(link.revoked_at)

    def test_panel_examiner_polling_decline_vote_and_review(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Examiner actions', status=PanelHearing.Status.OPEN,
            workflow=PanelHearing.Workflow.REVIEW_VOTE)
        candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Candidate')
        panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Review panel')
        invitation, token = issue_panel_invitation(hearing, 'reviewer@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=panel)
        invitation.confirmed_at = timezone.now()
        invitation.save(update_fields=('confirmed_at',))
        examiner_url = reverse('panel_examiner', kwargs={'token': token})

        cookie_name = f'livegrade_panel_examiner_{invitation.pk}'
        cookie_val = issue_panel_device_cookie(invitation)
        self.client.cookies[cookie_name] = cookie_val
        bind_panel_invitation_device(invitation, cookie_val)

        res = self.request('get', examiner_url + '?state=1')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/json')

        other_inv, other_token = issue_panel_invitation(hearing,
            'declining@example.test', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        decline_url = reverse('panel_examiner', kwargs={'token': other_token})
        res = self.request('post', decline_url, {'action': 'decline'})
        self.assertEqual(res.status_code, 200)
        other_inv.refresh_from_db()
        self.assertIsNotNone(other_inv.declined_at)

        activate_panel_candidate(hearing, candidate, organization=self.org.as_claim(), user=self.owner)

        res = self.request('post', examiner_url, {
            'action': 'vote',
            'candidate_id': str(candidate.pk),
            'choice': PanelVote.Choice.SUPPORT,
        })
        self.assertEqual(res.status_code, 302)
        self.assertTrue(PanelVote.objects.filter(hearing=hearing, candidate=candidate).exists())

        res = self.request('post', examiner_url, {
            'action': 'review',
            'candidate_id': str(candidate.pk),
            'report': 'Thorough presentation and good methodology.',
        })
        self.assertEqual(res.status_code, 302)
        self.assertTrue(PanelWrittenReview.objects.filter(hearing=hearing, candidate=candidate).exists())

    def test_panel_hearing_export_with_all_sheets(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Export hearing', status=PanelHearing.Status.OPEN,
            workflow=PanelHearing.Workflow.REVIEW_VOTE)
        candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Export cand')
        criterion = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Export crit')
        panel = PanelScoringPanel.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, name='Export panel')
        invitation, token = issue_panel_invitation(hearing, 'export@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=panel)

        PanelDecision.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, candidate=candidate, outcome=PanelDecision.Outcome.ACCEPT,
            rationale='Good defence', conditions='None', dissent='None', decided_by=self.owner, revision=1)
        PanelVote.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, candidate=candidate, invitation=invitation,
            choice=PanelVote.Choice.SUPPORT)
        PanelWrittenReview.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, candidate=candidate, invitation=invitation,
            report='Detailed review', revision=1)
        PanelAbstention.objects.create(organization_id=self.org.id, created_by=self.owner,
            candidate=candidate, criterion=criterion, invitation=invitation,
            reason='Conflict of interest')
        hearing.status = PanelHearing.Status.REVIEW
        hearing.save()

        export_url = reverse('panel_results_export', args=(hearing.pk,))
        res = self.request('get', export_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_panel_embedded_imports_and_cancel(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from assessments.sheets import workbook_bytes

        draft = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Import Draft', status=PanelHearing.Status.DRAFT)
        c_url = reverse('panel_candidate_import', args=(draft.pk,))

        res = self.request('post', c_url, {'action': 'cancel'})
        self.assertEqual(res.status_code, 302)
        self.assertIn('step=candidates', res['Location'])

        wb_bytes = workbook_bytes(['Candidate', 'Reference'],
            [['Alice Smith', 'Quantum ML']]).getvalue()
        wb_file = SimpleUploadedFile('candidates.xlsx', wb_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        res = self.request('post', f'{c_url}?embedded=1', {'file': wb_file})
        self.assertEqual(res.status_code, 302)
        self.assertIn('step=candidates', res['Location'])

        manage_res = self.request('get', res['Location'])
        self.assertEqual(manage_res.status_code, 200)
        self.assertContains(manage_res, 'panel-candidate-preview')
        self.assertContains(manage_res, 'Alice Smith')

        res_err = self.request('post', f'{c_url}?embedded=1', {})
        self.assertEqual(res_err.status_code, 302)
        self.assertIn('step=candidates', res_err['Location'])

    def test_panel_setup_inline_previews_and_unsaved_guard(self):
        draft = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Inline Preview Draft', status=PanelHearing.Status.DRAFT)
        manage_url = reverse('panel_hearing_manage', args=(draft.pk,))

        page = self.request('get', manage_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="panel-unsaved-dialog"')
        self.assertContains(page, 'data-panel-details-form')
        self.assertContains(page, '(required)</span>')
        self.assertContains(page, 'data-panel-import-form')

        # Test candidate import inline preview
        from assessments.sheets import workbook_bytes
        from django.core.files.uploadedfile import SimpleUploadedFile
        c_url = reverse('panel_candidate_import', args=(draft.pk,))
        wb_bytes = workbook_bytes(['Candidate', 'Reference'], [['Bob Preview', 'B-101']]).getvalue()
        wb_file = SimpleUploadedFile('candidates.xlsx', wb_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        import_post = self.request('post', f'{c_url}?embedded=1', {'file': wb_file})
        self.assertEqual(import_post.status_code, 302)

        preview_page = self.request('get', import_post['Location'])
        self.assertEqual(preview_page.status_code, 200)
        self.assertContains(preview_page, 'id="candidate-inline-preview"')
        self.assertContains(preview_page, 'Bob Preview')
        self.assertContains(preview_page, 'Import 1 candidate')
        self.assertContains(preview_page, 'Cancel preview')

    def test_panel_step_one_save_advance_and_inline_feedback(self):
        draft = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Original Hearing', status=PanelHearing.Status.DRAFT)
        manage_url = reverse('panel_hearing_manage', args=(draft.pk,))

        res_save = self.request('post', manage_url, {
            'action': 'update_hearing',
            'name': 'Saved Hearing Name',
            'aggregation': 'mean',
            'workflow': PanelHearing.Workflow.SCORED,
        })
        self.assertEqual(res_save.status_code, 302)
        self.assertIn('step=details', res_save['Location'])
        self.assertIn('saved=1', res_save['Location'])

        get_saved = self.request('get', res_save['Location'])
        self.assertEqual(get_saved.status_code, 200)
        self.assertContains(get_saved, 'Hearing details saved successfully.')

        res_advance = self.request('post', manage_url, {
            'action': 'update_hearing',
            'name': 'Advanced Hearing Name',
            'aggregation': 'mean',
            'workflow': PanelHearing.Workflow.SCORED,
            'advance': '1',
        })
        self.assertEqual(res_advance.status_code, 302)
        self.assertIn('step=candidates', res_advance['Location'])

    def test_panel_candidate_and_criterion_removal_in_draft(self):
        draft = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Removal Draft', status=PanelHearing.Status.DRAFT)
        cand = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=draft, display_name='Delete Candidate')
        crit = PanelCriterion.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=draft, name='Delete Criterion', weight=10)
        manage_url = reverse('panel_hearing_manage', args=(draft.pk,))

        res_cand = self.request('post', manage_url, {
            'action': 'remove_candidate',
            'candidate_id': str(cand.pk),
        })
        self.assertEqual(res_cand.status_code, 302)
        self.assertIn('step=candidates', res_cand['Location'])
        self.assertFalse(PanelCandidate.objects.filter(pk=cand.pk).exists())

        # Test candidate removal when referenced by PanelAuditEvent
        cand_with_audit = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=draft, display_name='Audited Candidate')
        audit_event = PanelAuditEvent.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=draft,
            candidate=cand_with_audit,
            action=PanelAuditEvent.Action.CANDIDATE_ADDED,
            reason='Added candidate',
        )
        res_audited = self.request('post', manage_url, {
            'action': 'remove_candidate',
            'candidate_id': str(cand_with_audit.pk),
        })
        self.assertEqual(res_audited.status_code, 302)
        self.assertFalse(PanelCandidate.objects.filter(pk=cand_with_audit.pk).exists())
        audit_event.refresh_from_db()
        self.assertIsNone(audit_event.candidate)

        res_crit = self.request('post', manage_url, {
            'action': 'remove_criterion',
            'criterion_id': str(crit.pk),
        })
        self.assertEqual(res_crit.status_code, 302)
        self.assertIn('step=scoring', res_crit['Location'])
        self.assertFalse(PanelCriterion.objects.filter(pk=crit.pk).exists())

    def test_panel_record_decision_error_scenarios(self):
        hearing = PanelHearing.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Decision hearing', status=PanelHearing.Status.REVIEW)
        candidate = PanelCandidate.objects.create(organization_id=self.org.id,
            created_by=self.owner, hearing=hearing, display_name='Dec cand')
        review_url = reverse('panel_results_review', args=(hearing.pk,))

        res = self.request('post', review_url, {
            'action': 'decision',
            'candidate_id': '999999',
        })
        self.assertEqual(res.status_code, 404)

        res = self.request('post', review_url, {
            'action': 'decision',
            'candidate_id': str(candidate.pk),
            f'decision_{candidate.pk}-outcome': '',
        })
        self.assertEqual(res.status_code, 400)
        self.assertContains(res, 'outcome', status_code=400)

    def test_panel_mandatory_fields_inline_validation_no_toast(self):
        draft = PanelHearing.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Validation Draft',
            status=PanelHearing.Status.DRAFT,
        )
        manage_url = reverse('panel_hearing_manage', args=(draft.pk,))

        res_get = self.request('get', manage_url)
        self.assertEqual(res_get.status_code, 200)
        self.assertContains(res_get, 'id="panel-unsaved-dialog"')
        self.assertContains(res_get, '<form method="post" novalidate data-panel-form')

        # Submitting candidate with empty mandatory display_name yields inline error without toast
        res_post = self.request('post', manage_url, {
            'action': 'candidate',
            'display_name': '',
        })
        self.assertEqual(res_post.status_code, 400)
        self.assertContains(res_post, 'class="field-error"', status_code=400)
        self.assertContains(res_post, 'role="alert"', status_code=400)
        self.assertContains(res_post, 'This field is required.', status_code=400)
        self.assertNotContains(res_post, 'flash-region"><div class="alert', status_code=400)
        self.assertNotContains(res_post, 'Please correct the highlighted fields and try again.', status_code=400)

    def test_panel_step_one_save_no_top_toast(self):
        draft = PanelHearing.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Save Hearing No Toast',
            status=PanelHearing.Status.DRAFT,
        )
        manage_url = reverse('panel_hearing_manage', args=(draft.pk,))

        res_save = self.request('post', manage_url, {
            'action': 'update_hearing',
            'name': 'Updated Name',
            'aggregation': 'mean',
            'workflow': PanelHearing.Workflow.SCORED,
        })
        self.assertEqual(res_save.status_code, 302)
        self.assertIn('step=details', res_save['Location'])
        self.assertIn('saved=1', res_save['Location'])

        res_follow = self.request('get', res_save['Location'])
        self.assertEqual(res_follow.status_code, 200)
        self.assertContains(res_follow, 'Hearing details saved successfully.')
        self.assertNotContains(res_follow, 'flash-region"><div class="alert')

    def test_import_warning_does_not_render_flash_messages(self):
        draft = PanelHearing.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Import Warning Isolation Draft',
            status=PanelHearing.Status.DRAFT,
        )
        manage_url = reverse('panel_hearing_manage', args=(draft.pk,))
        res_advance = self.request('post', manage_url, {
            'action': 'update_hearing',
            'name': 'Draft with Flash Message',
            'aggregation': 'mean',
            'workflow': PanelHearing.Workflow.SCORED,
            'advance': '1',
        })
        self.assertEqual(res_advance.status_code, 302)
        res_get = self.request('get', res_advance['Location'])
        self.assertEqual(res_get.status_code, 200)
        content = res_get.content.decode()
        self.assertRegex(content, r'<div id="panel-candidate-import-warning"[^>]*hidden')
        match = re.search(r'<div id="panel-candidate-import-warning"[^>]*>(.*?)</div>', content, re.DOTALL)
        self.assertTrue(match)
        self.assertNotIn('Assessment details saved.', match.group(1))
