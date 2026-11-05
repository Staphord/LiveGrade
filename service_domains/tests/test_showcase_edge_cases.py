"""Regression checks for Showcase model and operation boundary conditions."""
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from service_domains.models import (
    ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit, ShowcaseJudgeAssignment,
    ShowcaseJudgeCriterion, ShowcaseJudgeEvaluation,
    ShowcaseJudgeExhibitAssignment, ShowcaseJudgeInvitation,
)
from service_domains.operations import (
    issue_showcase_judge_invitation, replace_showcase_invited_judge_assignments,
    replace_showcase_judge_assignments, replace_showcase_judge_criteria,
    revoke_showcase_judge_invitation, showcase_judge_progress,
    submit_showcase_judge_evaluation,
)
from service_domains.throttling import (
    allow_showcase_judge_request, allow_unknown_bearer_request,
)


class ShowcaseModelBoundaryTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase model edge organization')
        self.owner = make_user('showcase-model-edge-owner')
        self.event = ShowcaseEvent.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Model edges')
        self.exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            name='Exhibit')
        self.invitation = ShowcaseJudgeInvitation.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            email='judge@example.org', token_digest='a' * 64,
            expires_at=timezone.now() + timedelta(hours=1))

    def test_manual_assignment_rejects_closed_events(self):
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status', 'updated_at'))
        assignment = ShowcaseJudgeAssignment(event=self.event, exhibit=self.exhibit,
            judge_label='Judge', organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'locked after the event closes'):
            assignment.clean()

    def test_invitation_rejects_missing_email_expiry_and_malformed_digest(self):
        blank = ShowcaseJudgeInvitation(event=self.event, email=' ', token_digest='a' * 64,
            expires_at=timezone.now() + timedelta(hours=1), organization_id=self.org.id,
            created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'judge email'):
            blank.clean()
        expired = ShowcaseJudgeInvitation(event=self.event, email='judge@example.org',
            token_digest='a' * 64, expires_at=timezone.now() - timedelta(seconds=1),
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'expiry must be in the future'):
            expired.clean()
        malformed = ShowcaseJudgeInvitation(event=self.event, email='judge@example.org',
            token_digest='short', expires_at=timezone.now() + timedelta(hours=1),
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'token digest is invalid'):
            malformed.clean()

    def test_invited_assignment_enforces_event_relations_and_lifecycle(self):
        other_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event')
        foreign_invitation = ShowcaseJudgeInvitation.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=other_event,
            email='other@example.org', token_digest='b' * 64,
            expires_at=timezone.now() + timedelta(hours=1))
        wrong_invitation = ShowcaseJudgeExhibitAssignment(event=self.event,
            invitation=foreign_invitation, exhibit=self.exhibit,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'must belong to this event'):
            wrong_invitation.clean()
        other_exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=other_event, name='Foreign exhibit')
        wrong_exhibit = ShowcaseJudgeExhibitAssignment(event=self.event,
            invitation=self.invitation, exhibit=other_exhibit,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'assigned exhibit must belong'):
            wrong_exhibit.clean()

        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status', 'updated_at'))
        locked = ShowcaseJudgeExhibitAssignment(event=self.event,
            invitation=self.invitation, exhibit=self.exhibit,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'locked after results are published'):
            locked.clean()

    def test_criteria_enforce_draft_and_scale_specific_checklists(self):
        for items in ([], [''], ['x' * 241], ['valid', 3], ['x'] * 21):
            criterion = ShowcaseJudgeCriterion(event=self.event, name='Checklist',
                weight=100, scale=ShowcaseJudgeCriterion.Scale.CHECKLIST,
                checklist_items=items, organization_id=self.org.id,
                created_by=self.owner)
            with self.subTest(items=items), self.assertRaisesMessage(
                    ValidationError, 'checklist needs 1 to 20'):
                criterion.clean()
        with_items_on_points = ShowcaseJudgeCriterion(event=self.event, name='Points',
            weight=100, scale=ShowcaseJudgeCriterion.Scale.POINTS,
            checklist_items=['unexpected'], organization_id=self.org.id,
            created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'only for checklist'):
            with_items_on_points.clean()
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        locked = ShowcaseJudgeCriterion(event=self.event, name='Locked', weight=100,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'locked after the event opens'):
            locked.clean()

    def test_evaluation_clean_rejects_org_mapping_shape_and_score_mismatch(self):
        assignment = ShowcaseJudgeExhibitAssignment.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            invitation=self.invitation, exhibit=self.exhibit)
        criterion = ShowcaseJudgeCriterion.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            name='Quality', weight=100)
        mismatched_org = ShowcaseJudgeEvaluation(assignment=assignment,
            raw_scores={str(criterion.pk): '80'}, score=80,
            organization_id=self.org.id + 1, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'same organization'):
            mismatched_org.clean()
        for scores in ([], {}, {str(criterion.pk): '80', 'extra': 1}):
            invalid = ShowcaseJudgeEvaluation(assignment=assignment,
                raw_scores=scores, score=80, organization_id=self.org.id,
                created_by=self.owner)
            with self.subTest(scores=scores), self.assertRaises(ValidationError):
                invalid.clean()
        wrong_score = ShowcaseJudgeEvaluation(assignment=assignment,
            raw_scores={str(criterion.pk): '80'}, score=79,
            organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'normalized score'):
            wrong_score.clean()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            ShowcaseJudgeEvaluation.objects.filter(pk=999999).delete()
        incomplete = ShowcaseJudgeEvaluation(raw_scores={}, score=0,
            organization_id=self.org.id, created_by=self.owner)
        incomplete.clean()


class ShowcaseOperationBoundaryTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase operation edge organization')
        self.owner = make_user('showcase-operation-edge-owner')
        self.other = make_user('showcase-operation-edge-other')
        self.event = ShowcaseEvent.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Operation edges')
        self.exhibits = [ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            name=f'Exhibit {index}') for index in range(2)]

    def issue(self, email='judge@example.org'):
        return issue_showcase_judge_invitation(self.event, email,
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner)

    def test_owner_manual_assignments_normalize_labels_and_replace_atomically(self):
        assignments = replace_showcase_judge_assignments(self.event, {
            str(self.exhibits[0].pk): ['  Judge   One ', 'judge one', 'Judge Two'],
        }, organization=self.org.as_claim(), user=self.owner, minimum_judges=2)
        self.assertEqual([row.judge_label for row in assignments], ['Judge One', 'Judge Two'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.minimum_judges_per_exhibit, 2)
        with self.assertRaises(ValidationError):
            replace_showcase_judge_assignments(self.event,
                {self.exhibits[1].pk: ['New Judge']}, organization=self.org.as_claim(),
                user=self.owner, minimum_judges=0)
        self.assertEqual(ShowcaseJudgeAssignment.objects.filter(
            event=self.event, active=True).count(), 2)
        # Omitting the optional threshold preserves the configured event value.
        replace_showcase_judge_assignments(self.event, {},
            organization=self.org.as_claim(), user=self.owner)

    def test_manual_assignment_rejects_invalid_shapes_and_inactive_events(self):
        cases = (
            ([], 'grouped by exhibit'),
            ({'bad': ['Judge']}, 'unknown exhibit'),
            ({True: ['Judge']}, 'unknown exhibit'),
            ({self.exhibits[0].pk: 'Judge'}, 'must be a list'),
            ({self.exhibits[0].pk: [1]}, 'must be text'),
            ({self.exhibits[0].pk: [' ']}, '1 to 160 characters'),
        )
        for assignments, message in cases:
            with self.subTest(assignments=assignments), self.assertRaisesMessage(
                    ValidationError, message):
                replace_showcase_judge_assignments(self.event, assignments,
                    organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'locked after the event closes'):
            replace_showcase_judge_assignments(self.event, {},
                organization=self.org.as_claim(), user=self.owner)

    def test_invitation_duplicate_and_revocation_missing_id_are_rejected(self):
        self.issue()
        with self.assertRaisesMessage(ValidationError, 'already exists'):
            self.issue('JUDGE@example.org')
        with self.assertRaisesMessage(ValidationError, 'valid judge email'):
            issue_showcase_judge_invitation(self.event, None,
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
                user=self.owner)
        with self.assertRaises(PermissionDenied):
            revoke_showcase_judge_invitation(self.event, 999999,
                organization=self.org.as_claim(), user=self.owner)

    def test_invited_assignments_validate_ids_types_ownership_and_caps(self):
        invitation, _ = self.issue()
        malformed_cases = (
            ([], 'map invitation IDs'),
            ({True: []}, 'unknown judge invitation'),
            ({'bad': []}, 'unknown judge invitation'),
            ({999999: []}, 'permission'),
            ({invitation.pk: 'not-a-list'}, 'list of exhibits'),
            ({invitation.pk: [True]}, 'unknown exhibit'),
            ({invitation.pk: ['bad']}, 'unknown exhibit'),
        )
        for assignments, message in malformed_cases:
            with self.subTest(assignments=assignments):
                error = PermissionDenied if message == 'permission' else ValidationError
                with self.assertRaises(error):
                    replace_showcase_invited_judge_assignments(self.event, assignments,
                        organization=self.org.as_claim(), user=self.owner)
        with patch('service_domains.operations.MAX_SHOWCASE_JUDGE_ASSIGNMENTS', 1):
            with self.assertRaisesMessage(ValidationError, 'cannot have more than 1'):
                replace_showcase_invited_judge_assignments(self.event,
                    {invitation.pk: [item.pk for item in self.exhibits]},
                    organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status', 'updated_at'))
        with self.assertRaises(PermissionDenied):
            replace_showcase_invited_judge_assignments(self.event, {},
                organization=self.org.as_claim(), user=self.owner)

    def test_criteria_replacement_validates_shapes_names_and_numeric_weights(self):
        invalid = (
            ('not a list', 'must be a list'),
            ([{'name': '', 'weight': 100}], 'names must be unique'),
            ([{'name': 'Quality', 'weight': 50},
              {'name': ' quality ', 'weight': 50}], 'names must be unique'),
            ([{'name': 'Quality', 'weight': 'not-a-number'}], 'valid percentages'),
            ([{'name': 'Quality', 'weight': 'NaN'}], 'total exactly 100'),
        )
        for criteria, message in invalid:
            with self.subTest(criteria=criteria), self.assertRaisesMessage(
                    ValidationError, message):
                replace_showcase_judge_criteria(self.event, criteria,
                    organization=self.org.as_claim(), user=self.owner)

    def test_evaluation_integrity_race_returns_duplicate_message(self):
        invitation, token = self.issue()
        criterion = replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100'}],
            organization=self.org.as_claim(), user=self.owner)[0]
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibits[0].pk]},
            organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        with patch.object(ShowcaseJudgeEvaluation, 'save', side_effect=IntegrityError):
            with self.assertRaisesMessage(ValidationError, 'already submitted'):
                submit_showcase_judge_evaluation(token, self.exhibits[0].public_id,
                    {str(criterion.pk): '80'})

    def test_evaluation_rechecks_event_state_after_token_resolution(self):
        invitation, token = self.issue()
        replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100'}],
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibits[0].pk]},
            organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.save(update_fields=('status', 'updated_at'))
        with patch('service_domains.operations.resolve_showcase_judge_invitation',
                   return_value=invitation):
            with self.assertRaises(PermissionDenied):
                submit_showcase_judge_evaluation(token, self.exhibits[0].public_id, {})

    def test_progress_fails_closed_for_non_owner(self):
        with self.assertRaises(PermissionDenied):
            showcase_judge_progress(self.event, organization=self.org.as_claim(),
                user=self.other)

    def test_public_judge_throttles_handle_cache_expiry_and_outage(self):
        invitation, _ = self.issue()
        request = RequestFactory().get('/', REMOTE_ADDR='203.0.113.12')

        with patch('service_domains.throttling.cache.add', return_value=True):
            self.assertTrue(allow_showcase_judge_request(invitation))
        with patch('service_domains.throttling.cache.add', return_value=False), \
                patch('service_domains.throttling.cache.incr', return_value=2):
            self.assertTrue(allow_showcase_judge_request(invitation))
        with patch('service_domains.throttling.cache.add', side_effect=[False, True]), \
                patch('service_domains.throttling.cache.incr', side_effect=ValueError):
            self.assertTrue(allow_showcase_judge_request(invitation))
        with patch('service_domains.throttling.cache.add', side_effect=RuntimeError):
            self.assertIsNone(allow_showcase_judge_request(invitation))

        with patch('service_domains.throttling.cache.add', return_value=True):
            self.assertTrue(allow_unknown_bearer_request(request, service='showcase'))
        with patch('service_domains.throttling.cache.add', side_effect=[False, True]), \
                patch('service_domains.throttling.cache.incr', side_effect=ValueError):
            self.assertTrue(allow_unknown_bearer_request(request, service='showcase'))
        invalid_address = RequestFactory().get('/', REMOTE_ADDR='not-an-address')
        self.assertIsNone(allow_unknown_bearer_request(invalid_address, service='showcase'))
        with patch('service_domains.throttling.cache.add', side_effect=RuntimeError):
            self.assertIsNone(allow_unknown_bearer_request(request, service='showcase'))
