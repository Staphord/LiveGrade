from datetime import timedelta
import hashlib
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.utils import timezone
from django.urls import reverse
from unittest.mock import patch

from accounts.testing import make_org, make_user
from service_domains.models import (ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit,
    ShowcaseJudgeCriterion, ShowcaseJudgeEvaluation, ShowcaseJudgeExhibitAssignment,
    ShowcaseJudgeInvitation, ShowcaseJudgeConflict)
from service_domains.operations import (issue_showcase_judge_invitation,
    declare_showcase_judge_conflict,
    replace_showcase_invited_judge_assignments, resolve_showcase_judge_invitation,
    revoke_showcase_judge_invitation, replace_showcase_judge_criteria,
    submit_showcase_judge_evaluation, showcase_judge_progress,
    showcase_judge_coverage)


class ShowcaseInvitedJudgeFoundationTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase invited judges')
        self.owner = make_user('showcase-invited-judge-owner')
        self.other = make_user('showcase-invited-judge-other')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Science day')
        self.category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Science')
        self.exhibits = [ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            category=self.category, name=f'Exhibit {index}') for index in (1, 2)]

    def issue(self, email='judge@example.org', *, user=None, event=None, expires=None):
        return issue_showcase_judge_invitation(event or self.event, email,
            expires or timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=user or self.owner)

    def test_invitation_persists_only_digest_and_resolves_only_during_open_event(self):
        invitation, token = self.issue()
        self.assertEqual(invitation.token_digest,
            hashlib.sha256(token.encode('utf-8')).hexdigest())
        self.assertNotEqual(invitation.token_digest, token)
        self.assertEqual(resolve_showcase_judge_invitation(token), invitation)

        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibits[0].pk]},
            organization=self.org.as_claim(), user=self.owner)
        assignment = ShowcaseJudgeExhibitAssignment.objects.get()
        self.assertEqual(assignment.invitation, invitation)
        self.assertEqual(assignment.exhibit, self.exhibits[0])

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        self.assertEqual(resolve_showcase_judge_invitation(token), invitation)

    def test_revoked_expired_and_wrong_event_tokens_do_not_resolve(self):
        revoked, revoked_token = self.issue('revoked@example.org')
        expired, expired_token = self.issue('expired@example.org')
        expired.expires_at = timezone.now() - timedelta(seconds=1)
        expired.save(update_fields=('expires_at', 'updated_at'))
        other_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event')
        _, scoped_token = self.issue('scoped@example.org', event=other_event)

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        revoke_showcase_judge_invitation(self.event, revoked.pk,
            organization=self.org.as_claim(), user=self.owner)
        for token in (revoked_token, expired_token, 'x' * 257):
            with self.subTest(token=token[:12]):
                self.assertIsNone(resolve_showcase_judge_invitation(token))
        self.assertEqual(resolve_showcase_judge_invitation(scoped_token).event, other_event)

    def test_revoke_deactivates_assignments_and_checks_owner_scope(self):
        invitation, token = self.issue()
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibits[0].pk]},
            organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            revoke_showcase_judge_invitation(self.event, invitation.pk,
                organization=self.org.as_claim(), user=self.other)
        revoked = revoke_showcase_judge_invitation(self.event, invitation.pk,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIsNotNone(revoked.revoked_at)
        self.assertFalse(ShowcaseJudgeExhibitAssignment.objects.get().active)
        with self.assertRaisesMessage(ValidationError, 'already revoked'):
            revoke_showcase_judge_invitation(self.event, invitation.pk,
                organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        self.assertIsNone(resolve_showcase_judge_invitation(token))

    def test_invitation_requires_owner_scope_valid_email_and_future_expiry(self):
        with self.assertRaises(PermissionDenied):
            self.issue(user=self.other)
        # A second organization's claim must not issue a token for this event.
        from accounts.testing import make_org
        other_org = make_org('Other Showcase organization')
        with self.assertRaises(PermissionDenied):
            issue_showcase_judge_invitation(self.event, 'judge@example.org',
                timezone.now() + timedelta(hours=1), organization=other_org.as_claim(),
                user=self.owner)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(PermissionDenied):
            self.issue('closed@example.org')
        self.event.status = ShowcaseEvent.Status.DRAFT
        self.event.save(update_fields=('status', 'updated_at'))
        for email, expires in (
            ('not-an-email', timezone.now() + timedelta(hours=1)),
            ('judge@example.org', timezone.now() - timedelta(seconds=1)),
        ):
            with self.subTest(email=email), self.assertRaises(ValidationError):
                self.issue(email, expires=expires)

    def test_assignment_replacement_is_atomic_scoped_and_keeps_other_judges(self):
        first, _ = self.issue('first@example.org')
        second, _ = self.issue('second@example.org')
        created = replace_showcase_invited_judge_assignments(self.event, {
            first.pk: [self.exhibits[0].pk, self.exhibits[1].pk],
            second.pk: [self.exhibits[0].pk],
        }, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(created), 3)
        self.assertEqual(ShowcaseJudgeExhibitAssignment.objects.filter(active=True).count(), 3)

        other_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event')
        other_category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=other_event, name='Other')
        foreign_exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=other_event, category=other_category, name='Foreign')
        with self.assertRaises(PermissionDenied):
            replace_showcase_invited_judge_assignments(self.event,
                {first.pk: [foreign_exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(ShowcaseJudgeExhibitAssignment.objects.filter(active=True).count(), 3)

        replaced = replace_showcase_invited_judge_assignments(self.event,
            {first.pk: [self.exhibits[1].pk]}, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(replaced), 1)
        self.assertEqual(ShowcaseJudgeExhibitAssignment.objects.filter(active=True).count(), 1)
        self.assertEqual(ShowcaseJudgeExhibitAssignment.objects.filter(active=False).count(), 2)

    def test_criteria_scales_normalize_to_weighted_score_and_evaluation_is_one_time(self):
        invitation, token = self.issue()
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibits[0].pk]},
            organization=self.org.as_claim(), user=self.owner)
        criteria = replace_showcase_judge_criteria(self.event, [
            {'name': 'Accuracy', 'weight': '40', 'scale': 'points', 'max_points': '10'},
            {'name': 'Presentation', 'weight': '20', 'scale': 'stars'},
            {'name': 'Safe', 'weight': '20', 'scale': 'pass_fail'},
            {'name': 'Evidence', 'weight': '20', 'scale': 'checklist',
             'checklist_items': ['Evidence shown', 'Claim supported', 'Limits explained']},
        ], organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(criteria), 4)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        payload = {
            str(criteria[0].pk): '8',
            str(criteria[1].pk): '4',
            str(criteria[2].pk): 'pass',
            str(criteria[3].pk): [0, 2],
        }
        evaluation = submit_showcase_judge_evaluation(token,
            self.exhibits[0].public_id, payload)
        self.assertEqual(evaluation.score, Decimal('81.33'))
        self.assertEqual(evaluation.raw_scores, payload)
        with self.assertRaisesMessage(ValidationError, 'already submitted'):
            submit_showcase_judge_evaluation(token, self.exhibits[0].public_id, payload)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            evaluation.save()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            ShowcaseJudgeEvaluation.objects.filter(pk=evaluation.pk).update(score=100)
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            evaluation.delete()
        progress = showcase_judge_progress(self.event,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual((progress['assigned_count'], progress['submitted_count'],
            progress['pending_count']), (1, 1, 0))

    def test_judge_score_submission_rejects_unassigned_foreign_and_closed_access(self):
        invitation, token = self.issue()
        criterion = replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100', 'scale': 'points'}
        ], organization=self.org.as_claim(), user=self.owner)[0]
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        payload = {str(criterion.pk): '90'}
        with self.assertRaises(PermissionDenied):
            submit_showcase_judge_evaluation(token, self.exhibits[0].public_id, payload)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibits[0].pk]},
            organization=self.org.as_claim(), user=self.owner)
        foreign_public_id = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=ShowcaseEvent.objects.create(
                organization_id=self.org.id, created_by=self.owner, name='Other'),
            name='Other exhibit').public_id
        with self.assertRaises(PermissionDenied):
            submit_showcase_judge_evaluation(token, foreign_public_id, payload)
        self.event.status = ShowcaseEvent.Status.CLOSED
        self.event.save(update_fields=('status', 'updated_at'))
        closed_result = submit_showcase_judge_evaluation(
            token, self.exhibits[0].public_id, payload)
        self.assertEqual(closed_result.score, Decimal('90.00'))

    def test_criterion_replacement_requires_complete_valid_weights_and_draft_event(self):
        valid = [{'name': 'Quality', 'weight': '100', 'scale': 'points'}]
        for bad, message in (
            ([], 'between 1 and 50'),
            ([{'name': 'Quality', 'weight': '99', 'scale': 'points'}], 'total exactly 100'),
            ([{'name': 'Quality', 'weight': '100', 'scale': 'unknown'}], 'scale'),
            ([{'name': 'Quality', 'weight': '100', 'scale': 'checklist'}], 'checklist needs'),
            ([{'name': 'Quality', 'weight': '100', 'scale': 'points', 'extra': 'x'}], 'invalid fields'),
        ):
            with self.subTest(criteria=bad), self.assertRaisesMessage(ValidationError, message):
                replace_showcase_judge_criteria(self.event, bad,
                    organization=self.org.as_claim(), user=self.owner)
        replace_showcase_judge_criteria(self.event, valid,
            organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(PermissionDenied):
            replace_showcase_judge_criteria(self.event, valid,
                organization=self.org.as_claim(), user=self.owner)


class ShowcaseJudgeWorkspaceViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase judge pages')
        self.owner = make_user('showcase-judge-page-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Open day')
        category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Robotics')
        self.exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, category=category,
            name='Sorting robot', description='Sorts recyclable items.')
        self.other_exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, category=category,
            name='Solar rover')
        self.invitation, self.token = issue_showcase_judge_invitation(
            self.event, 'private-judge@example.org', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {self.invitation.pk: [self.exhibit.pk]},
            organization=self.org.as_claim(), user=self.owner)
        self.criteria = replace_showcase_judge_criteria(self.event, [
            {'name': 'Engineering', 'description': 'Does the prototype work?',
             'weight': '100', 'scale': 'points', 'max_points': '10'},
        ], organization=self.org.as_claim(), user=self.owner)
        self.url = reverse('showcase_judge_workspace', kwargs={'token': self.token})

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def test_judge_link_waits_before_open_then_shows_only_assigned_exhibits(self):
        waiting = self.request('get', self.url)
        self.assertEqual(waiting.status_code, 200)
        self.assertContains(waiting, 'Scoring will be available when the organizer opens')
        self.assertContains(waiting, 'Sorting robot')
        self.assertNotContains(waiting, 'Solar rover')
        self.assertNotContains(waiting, 'private-judge@example.org')
        self.assertIn('no-store', waiting['Cache-Control'])
        self.assertEqual(waiting['Referrer-Policy'], 'same-origin')

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        opened = self.request('get', self.url)
        self.assertContains(opened, 'Engineering')
        self.assertContains(opened, 'Submit this exhibit scorecard')

    def test_judge_submits_each_assigned_exhibit_once_and_receives_a_receipt(self):
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        prefix = f'exhibit_{ShowcaseJudgeExhibitAssignment.objects.get().pk}'
        payload = {'exhibit_id': str(self.exhibit.public_id), f'{prefix}-criterion_{self.criteria[0].pk}': '8'}
        response = self.request('post', self.url, payload)
        self.assertEqual(response.status_code, 302)
        receipt = self.request('get', self.url)
        self.assertContains(receipt, 'Scorecard submitted')
        self.assertContains(receipt, '80.00 out of 100')
        duplicate = self.request('post', self.url, payload)
        self.assertEqual(duplicate.status_code, 400)
        self.assertContains(duplicate, 'already submitted', status_code=400)
        self.assertEqual(ShowcaseJudgeEvaluation.objects.count(), 1)

    def test_conflict_declaration_is_recorded_and_blocks_scoring(self):
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        payload = {'action': 'declare_conflict',
            'exhibit_id': str(self.exhibit.public_id),
            'reason': 'I supervised this project recently.'}
        response = self.request('post', self.url, payload)
        self.assertEqual(response.status_code, 302)
        conflict = ShowcaseJudgeConflict.objects.get()
        self.assertEqual(conflict.assignment.exhibit, self.exhibit)
        self.assertEqual(conflict.reason, 'I supervised this project recently.')

        workspace = self.request('get', self.url)
        self.assertContains(workspace, 'declared a conflict')
        self.assertNotContains(workspace, 'Submit this exhibit scorecard')
        self.assertEqual(showcase_judge_progress(self.event,
            organization=self.org.as_claim(), user=self.owner)['conflict_count'], 1)
        coverage = showcase_judge_coverage(self.event, minimum_judges=1)
        solar = next(row for row in coverage['exhibits'] if row['exhibit'] == self.exhibit.name)
        self.assertFalse(solar['covered'])
        self.assertEqual(solar['gap'], 1)
        with self.assertRaises(PermissionDenied):
            submit_showcase_judge_evaluation(self.token, self.exhibit.public_id,
                {str(self.criteria[0].pk): '8'})
        with self.assertRaisesMessage(ValidationError, 'already been declared'):
            declare_showcase_judge_conflict(self.token, self.exhibit.public_id,
                'I know the exhibitor.')

    def test_unassigned_exhibit_and_expired_or_revoked_link_are_denied(self):
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        self.assertEqual(self.request('post', self.url, {
            'exhibit_id': str(self.other_exhibit.public_id),
        }).status_code, 404)
        self.invitation.revoked_at = timezone.now()
        self.invitation.save(update_fields=('revoked_at', 'updated_at'))
        self.assertEqual(self.request('get', self.url).status_code, 404)

    def test_unknown_and_known_invitation_throttles_fail_closed(self):
        with patch('service_domains.views.allow_unknown_bearer_request', return_value=None):
            unavailable = self.request('get', reverse('showcase_judge_workspace',
                kwargs={'token': 'unknown-token'}))
        self.assertEqual(unavailable.status_code, 503)
        with patch('service_domains.views.allow_unknown_bearer_request', return_value=False):
            limited = self.request('get', reverse('showcase_judge_workspace',
                kwargs={'token': 'unknown-token'}))
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited)
        with patch('service_domains.views.allow_showcase_judge_request', return_value=None):
            unavailable = self.request('get', self.url)
        self.assertEqual(unavailable.status_code, 503)
        with patch('service_domains.views.allow_showcase_judge_request', return_value=False):
            limited = self.request('get', self.url)
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited)

    def test_judge_post_reports_waiting_missing_criteria_and_invalid_score(self):
        assignment = ShowcaseJudgeExhibitAssignment.objects.get()
        payload = {'exhibit_id': str(self.exhibit.public_id)}
        waiting = self.request('post', self.url, payload)
        self.assertEqual(waiting.status_code, 400)
        self.assertContains(waiting, 'available when the Showcase event opens', status_code=400)

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        self.criteria[0].delete()
        missing_criteria = self.request('post', self.url, payload)
        self.assertEqual(missing_criteria.status_code, 400)
        self.assertContains(missing_criteria, 'not configured judge criteria', status_code=400)

        self.event.status = ShowcaseEvent.Status.DRAFT
        self.event.save(update_fields=('status', 'updated_at'))
        self.criteria = replace_showcase_judge_criteria(self.event, [
            {'name': 'Engineering', 'weight': 100, 'max_points': 10}],
            organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        invalid_score = self.request('post', self.url, {
            'exhibit_id': str(self.exhibit.public_id),
            f'exhibit_{assignment.pk}-criterion_{self.criteria[0].pk}': 'not-a-score',
        })
        self.assertEqual(invalid_score.status_code, 400)

    def test_judge_post_handles_submission_conflict_and_authorization_race(self):
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        assignment = ShowcaseJudgeExhibitAssignment.objects.get()
        payload = {'exhibit_id': str(self.exhibit.public_id),
            f'exhibit_{assignment.pk}-criterion_{self.criteria[0].pk}': '8'}
        with patch('service_domains.views.submit_showcase_judge_evaluation',
                   side_effect=ValidationError('scorecard changed during submission')):
            conflict = self.request('post', self.url, payload)
        self.assertEqual(conflict.status_code, 400)
        self.assertContains(conflict, 'scorecard changed during submission', status_code=400)
        with patch('service_domains.views.submit_showcase_judge_evaluation',
                   side_effect=PermissionDenied):
            denied = self.request('post', self.url, payload)
        self.assertEqual(denied.status_code, 404)
