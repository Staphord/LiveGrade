"""Regression coverage for a real bug: RubricCategoryForm and
PresentationGroupForm both included an 'order' field that no template ever
rendered a widget for. Since a ModelForm field with no rendered input is a
required field a real browser can never submit, every "Add category" /
"Create group" click silently failed validation — and because none of the
three add-forms (roster, rubric, groups) rendered form.errors anywhere, the
page just reloaded with the typed values still in the boxes and no
indication anything had gone wrong. Exactly what was reported: click Add,
page reloads, nothing added, no message.

These tests POST exactly what the real template sends (no 'order' key,
since the field is gone from the form) and also check that a genuine
validation failure is now visible in the rendered page.
"""

from accounts.models import User
from django.test import TestCase

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, PresentationGroup, RubricCategory, Student,
)


class AddFormsMatchRealTemplatePayloadTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer)

    def test_add_rubric_category_with_no_order_field_in_payload(self):
        # Exactly the fields rubric.html renders — no 'order' input exists.
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': 'Problem Identification & Real-World Relevance',
            'description': '', 'scope': 'group', 'weight': '10',
        })
        self.assertRedirects(response, f'/assessments/{self.session.pk}/rubric/')
        self.assertTrue(
            RubricCategory.objects.filter(
                assessment_session=self.session,
                name='Problem Identification & Real-World Relevance').exists())

    def test_add_group_with_no_order_field_in_payload(self):
        # Exactly the fields groups.html renders — no 'order' input exists.
        response = self.client.post(f'/assessments/{self.session.pk}/groups/', {'name': 'Group 1', 'max_size': '4'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/groups/')
        self.assertTrue(
            PresentationGroup.objects.filter(assessment_session=self.session, name='Group 1').exists())

    def test_rubric_form_error_is_visible_on_the_page(self):
        response = self.client.post(f'/assessments/{self.session.pk}/rubric/', {
            'name': '', 'description': '', 'scope': 'group', 'weight': '10',
        })
        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.assertContains(response, 'field-error')
        self.assertEqual(RubricCategory.objects.filter(assessment_session=self.session).count(), 0)

    def test_group_form_error_is_visible_on_the_page(self):
        response = self.client.post(f'/assessments/{self.session.pk}/groups/', {'name': ''})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'field-error')
        self.assertEqual(PresentationGroup.objects.filter(assessment_session=self.session).count(), 0)

    def test_roster_form_error_is_visible_on_the_page(self):
        response = self.client.post(f'/assessments/{self.session.pk}/roster/', {
            'full_name': '', 'student_id': '', 'email': '', 'programme': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'field-error')
        self.assertEqual(Student.objects.filter(assessment_session=self.session).count(), 0)
