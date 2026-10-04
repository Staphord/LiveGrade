"""Regression test for a real bug: the roster/participation/results pager
footers called page_obj.previous_page_number / next_page_number
unconditionally, which raises django.core.paginator.EmptyPage (uncaught by
Django's template variable resolution) whenever that link doesn't apply —
i.e. on page 1 of any list with more than one page. A single-student setup
never exercises a second page, so this slipped past every earlier test.
"""

from decimal import Decimal

from accounts.models import User
from django.test import TestCase

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, GroupMembership, PresentationGroup, RubricCategory,
    Student,
)


class MultiPageRenderTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)

        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer)
        # 30 students forces a second page at the default page size of 25 —
        # the exact shape that broke on import.
        for i in range(30):
            Student.objects.create(
                assessment_session=self.session, full_name=f'Student {i:02d}',
                student_id=f'S{i:02d}')
        self.group = PresentationGroup.objects.create(
            assessment_session=self.session, name='Group 1')
        RubricCategory.objects.create(
            assessment_session=self.session, name='Implementation',
            scope=RubricCategory.Scope.GROUP, max_points=Decimal('10'), weight=Decimal('100'))

    def test_roster_page_one_renders_with_more_than_one_page(self):
        response = self.client.get(f'/assessments/{self.session.pk}/roster/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Student 00')

    def test_participation_page_one_renders_with_more_than_one_page(self):
        response = self.client.get(f'/assessments/{self.session.pk}/participation/')
        self.assertEqual(response.status_code, 200)

    def test_results_page_one_renders_with_more_than_one_page(self):
        for student in self.session.students.all():
            GroupMembership.objects.get_or_create(group=self.group, student=student)
        response = self.client.get(f'/assessments/{self.session.pk}/results/')
        self.assertEqual(response.status_code, 200)

    def test_roster_last_page_next_link_does_not_crash(self):
        response = self.client.get(f'/assessments/{self.session.pk}/roster/?page=2')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Student 29')
