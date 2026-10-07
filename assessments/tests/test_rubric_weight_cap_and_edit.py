"""A rubric's combined weights must never be allowed to exceed 100% — not
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
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer,)
        self.existing = RubricCategory.objects.create(
            assessment_session=self.session, name='Existing', scope=RubricCategory.Scope.GROUP,
            weight=Decimal('90'))

    def test_new_category_pushing_the_total_over_100_is_rejected(self):
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Extra', 'description': '', 'scope': 'group',
            'weight': '20',
        })
        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.assertContains(response, 'over 100%')
        self.assertEqual(self.session.rubric_categories.filter(scope='group').count(), 1)

    def test_new_category_exactly_filling_remaining_room_is_allowed(self):
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Extra', 'description': '', 'scope': 'group',
            'weight': '10',
        })
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')
        self.assertEqual(self.session.rubric_categories.filter(scope='group').count(), 2)

    def test_the_100_is_one_pool_shared_by_group_and_individual_categories(self):
        # 90% already used in group scope leaves only 10% for individual categories too.
        too_much = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Communication', 'description': '', 'scope': 'individual', 'weight': '11'})
        self.assertEqual(too_much.status_code, 200)
        self.assertContains(too_much, 'over 100%')
        self.assertContains(too_much, 'At most 10% is available')
        self.assertFalse(self.session.rubric_categories.filter(scope='individual').exists())
        fits = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Communication', 'description': '', 'scope': 'individual', 'weight': '10'})
        self.assertRedirects(fits, f'/assessments/{self.session.pk}/rubric/')
        self.assertEqual(self.session.rubric_weight_total(), Decimal('100'))

    def test_a_weight_of_zero_or_a_negative_one_is_rejected(self):
        for bad in ('0', '-5'):
            response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
                'name': 'Extra', 'description': '', 'scope': 'group', 'weight': bad})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'more than 0 and at most 100')

    def test_edit_pushing_the_total_over_100_is_rejected_on_the_weight_field(self):
        other = RubricCategory.objects.create(
            assessment_session=self.session, name='Other', scope=RubricCategory.Scope.GROUP,
            weight=Decimal('5'))
        edit_url = f'/assessments/{self.session.pk}/rubric/{other.pk}/edit/'
        response = self.client.post(
            edit_url, {'name': 'Other renamed', 'description': 'typed', 'scope': 'group', 'weight': '20'})
        # Shown again in place - not redirected with a toast, and nothing typed is lost.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['editing_category'].pk, other.pk)
        self.assertEqual(list(response.context['rubric_form'].errors), ['weight'])
        self.assertContains(response, 'Weights would total 110%, over 100%. At most 10% is available.')
        html = response.content.decode()
        weight_error = html.index('Weights would total 110%')
        self.assertLess(html.rindex('id="id_rubric_weight"', 0, weight_error), weight_error)
        self.assertNotIn('alert alert-danger py-2 t-sm">Weights would total', html)
        self.assertContains(response, 'value="Other renamed"')
        self.assertContains(response, 'value="typed"')
        self.assertContains(response, 'value="20"')
        self.assertContains(response, f'action="{edit_url}"')
        self.assertContains(response, 'Update category')
        self.assertContains(response, 'Edit rubric category')
        self.assertContains(response, 'rubric-form-mode-banner is-active')
        self.assertFalse(any('Weights would total' in str(m) for m in response.context['messages']))
        other.refresh_from_db()
        self.assertEqual((other.weight, other.name), (Decimal('5'), 'Other'))  # unchanged

    def test_every_field_error_of_a_failed_edit_shows_under_its_own_field(self):
        url = f'/assessments/{self.session.pk}/rubric/{self.existing.pk}/edit/'
        response = self.client.post(url, {'name': '', 'description': '', 'scope': 'group', 'weight': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context['rubric_form'].errors), {'name', 'weight'})
        self.assertContains(response, 'This field is required.', count=2)

    def test_after_a_failed_edit_the_form_still_saves_when_fixed(self):
        url = f'/assessments/{self.session.pk}/rubric/{self.existing.pk}/edit/'
        self.client.post(url, {'name': 'Existing', 'description': '', 'scope': 'group', 'weight': '101'})
        fixed = self.client.post(url, {'name': 'Existing', 'description': '', 'scope': 'group', 'weight': '95'})
        self.assertRedirects(fixed, f'/assessments/{self.session.pk}/rubric/')
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.weight, Decimal('95'))

    def test_the_add_form_shows_the_over_100_error_under_the_weight_box_too(self):
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Extra', 'description': '', 'scope': 'group', 'weight': '20'})
        self.assertNotContains(response, 'alert alert-danger py-2 t-sm">Weights would total')
        self.assertContains(response, '<div class="field-error">Weights would total 110%')
        self.assertContains(response, 'Add category')
        self.assertNotContains(response, 'rubric-form-mode-banner is-active')

    def test_edit_can_keep_its_own_weight_without_double_counting(self):
        # Re-saving the existing 90%-weight category unchanged must not be
        # treated as 90% (existing) + 90% (itself) = 180%.
        response = self.client.post(
            f'/assessments/{self.session.pk}/rubric/{self.existing.pk}/edit/',
            {'name': 'Existing', 'description': '', 'scope': 'group', 'weight': '90'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')

    def test_edit_updates_fields(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/rubric/{self.existing.pk}/edit/',
            {'name': 'Renamed', 'description': 'New desc', 'scope': 'group',
             'weight': '90'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.name, 'Renamed')
        self.assertEqual(self.existing.description, 'New desc')

    def test_rubric_page_renders_edit_controls(self):
        response = self.client.get(f'/assessments/{self.session.pk}/rubric/')
        self.assertContains(response, 'rubric-edit-btn')
        self.assertContains(response, f'data-id="{self.existing.pk}"')
