"""A rubric scope's weights must never be allowed to exceed 100% — not
"allowed to save and shown in red afterward" (see screenshot: adding a
10th category pushed the group scope to 200% and it just saved). Also
covers the rubric category edit action added alongside the fix.
"""

from decimal import Decimal

from accounts.models import User
from django.test import TestCase

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, RubricCategory


class RubricWeightCapTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        # Weights below are exercising the per-scope cap mechanism itself,
        # not this session's real group/individual split - group=100,
        # individual=100 (never full_clean()'d here, so the model's usual
        # sum-to-100 invariant isn't in play) keeps every existing number
        # and assertion in this file meaning exactly what it always has.
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer,
            group_weight_percent=Decimal('100'), individual_weight_percent=Decimal('100'))
        self.existing = RubricCategory.objects.create(
            assessment_session=self.session, name='Existing', scope=RubricCategory.Scope.GROUP,
            max_points=Decimal('10'), weight=Decimal('90'))

    def test_new_category_pushing_scope_over_100_is_rejected(self):
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Extra', 'description': '', 'scope': 'group',
            'max_points': '10', 'weight': '20',
        })
        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.assertContains(response, 'over 100%')
        self.assertEqual(self.session.rubric_categories.filter(scope='group').count(), 1)

    def test_new_category_exactly_filling_remaining_room_is_allowed(self):
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Extra', 'description': '', 'scope': 'group',
            'max_points': '10', 'weight': '10',
        })
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')
        self.assertEqual(self.session.rubric_categories.filter(scope='group').count(), 2)

    def test_individual_scope_is_independent_of_group_scope(self):
        # 90% already used in group scope must not block individual scope.
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Communication', 'description': '', 'scope': 'individual',
            'max_points': '10', 'weight': '100',
        })
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')

    def test_edit_pushing_scope_over_100_is_rejected(self):
        other = RubricCategory.objects.create(
            assessment_session=self.session, name='Other', scope=RubricCategory.Scope.GROUP,
            max_points=Decimal('10'), weight=Decimal('5'))
        response = self.client.post(
            f'/assessments/{self.session.pk}/rubric/{other.pk}/edit/',
            {'name': 'Other', 'description': '', 'scope': 'group', 'max_points': '10', 'weight': '20'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')
        other.refresh_from_db()
        self.assertEqual(other.weight, Decimal('5'))  # unchanged

    def test_edit_can_keep_its_own_weight_without_double_counting(self):
        # Re-saving the existing 90%-weight category unchanged must not be
        # treated as 90% (existing) + 90% (itself) = 180%.
        response = self.client.post(
            f'/assessments/{self.session.pk}/rubric/{self.existing.pk}/edit/',
            {'name': 'Existing', 'description': '', 'scope': 'group', 'max_points': '10', 'weight': '90'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')

    def test_edit_updates_fields(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/rubric/{self.existing.pk}/edit/',
            {'name': 'Renamed', 'description': 'New desc', 'scope': 'group',
             'max_points': '15', 'weight': '90'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.name, 'Renamed')
        self.assertEqual(self.existing.description, 'New desc')
        self.assertEqual(self.existing.max_points, Decimal('15'))

    def test_rubric_page_renders_edit_controls(self):
        response = self.client.get(f'/assessments/{self.session.pk}/rubric/')
        self.assertContains(response, 'rubric-edit-btn')
        self.assertContains(response, f'data-id="{self.existing.pk}"')
