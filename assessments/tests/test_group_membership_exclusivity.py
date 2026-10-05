"""A student belongs to exactly one group per session — enforced at three
layers: the model (defense in depth for any write path), the manual
add/create views, and the bulk Excel import.
"""

from io import BytesIO

import openpyxl
from accounts.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from .xlsx_helpers import xlsx_buffer
from accounts.testing import make_org, make_user, sign_in
from assessments.group_import import apply_import, parse_groups, plan_import
from assessments.models import (
    AssessmentSession, GroupMembership, PresentationGroup, Student,
)


class ModelLevelExclusivityTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026')
        self.student = Student.objects.create(assessment_session=self.session, full_name='Alice Wang')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2')

    def test_second_group_membership_rejected_by_clean(self):
        GroupMembership.objects.create(group=self.group1, student=self.student)
        conflict = GroupMembership(group=self.group2, student=self.student)
        with self.assertRaises(ValidationError):
            conflict.full_clean()

    def test_clean_does_not_flag_the_students_own_group(self):
        # clean() must only object to a *different* group — the exclusion
        # query itself is what's under test here, not Django's separate
        # unique_together/pk validation (which is its own, unrelated check
        # and out of scope for a hand-built instance carrying another row's pk).
        GroupMembership.objects.create(group=self.group1, student=self.student)
        same_group_attempt = GroupMembership(group=self.group1, student=self.student)
        same_group_attempt.clean()  # no ValidationError for "already in this group"


class ManualGroupViewsExclusivityTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)

        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer)
        self.student = Student.objects.create(assessment_session=self.session, full_name='Alice Wang')
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2')

    def test_adding_to_second_group_is_rejected(self):
        GroupMembership.objects.create(group=self.group1, student=self.student)
        self.client.post(
            f'/assessments/{self.session.pk}/groups/{self.group2.pk}/members/',
            {'action': 'add', 'student_id': self.student.pk})
        self.assertFalse(GroupMembership.objects.filter(group=self.group2, student=self.student).exists())
        self.assertTrue(GroupMembership.objects.filter(group=self.group1, student=self.student).exists())

    def test_ungrouped_students_dropdown_excludes_assigned_students(self):
        # The student's own "remove from Group 1" control legitimately
        # carries their pk (value="N") — what must NOT appear is an
        # <option> offering them for a group they're not in.
        GroupMembership.objects.create(group=self.group1, student=self.student)
        response = self.client.get(f'/assessments/{self.session.pk}/groups/')
        self.assertNotContains(response, f'<option value="{self.student.pk}">')

    def test_create_group_with_selected_members(self):
        other = Student.objects.create(assessment_session=self.session, full_name='Bob Otieno')
        response = self.client.post(f'/assessments/{self.session.pk}/groups/', {
            'name': 'Group 3', 'max_size': '3', 'members': [str(other.pk)],
        })
        self.assertEqual(response.status_code, 302)
        group = PresentationGroup.objects.get(assessment_session=self.session, name='Group 3')
        self.assertEqual(set(group.member_ids()), {other.pk})

    def test_create_group_rejects_already_assigned_member(self):
        GroupMembership.objects.create(group=self.group1, student=self.student)
        response = self.client.post(f'/assessments/{self.session.pk}/groups/', {
            'name': 'Group 4', 'members': [str(self.student.pk)],
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with an error, not redirected
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 4').exists())


class ImportGroupsExclusivityTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026')
        self.a = Student.objects.create(assessment_session=self.session, full_name='Alice Wang', student_id='A1')
        self.b = Student.objects.create(assessment_session=self.session, full_name='Bob Otieno', student_id='B1')

    def parsed(self, *rows):
        return parse_groups(xlsx_buffer(['Group Name', 'Name', 'Student ID'], *rows))

    def test_student_already_in_existing_group_is_flagged(self):
        existing_group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        GroupMembership.objects.create(group=existing_group, student=self.a)

        parsed = self.parsed(['Group 2', 'Alice Wang, Bob Otieno', 'A1, B1'])
        plan = plan_import(self.session, parsed)
        apply_import(self.session, parsed)

        flagged = [g for g in plan['groups'] if not g['ready']]
        self.assertEqual([g['name'] for g in flagged], ['Group 2'])
        self.assertIn('already in "Group 1"', flagged[0]['issues'][0])
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 2').exists())

    def test_reimporting_the_same_group_is_not_a_conflict(self):
        apply_import(self.session, self.parsed(['Group 1', 'Alice Wang', 'A1']))
        parsed = self.parsed(['Group 1', 'Alice Wang, Bob Otieno', 'A1, B1'])
        self.assertTrue(plan_import(self.session, parsed)['groups'][0]['ready'])
        apply_import(self.session, parsed)
        group = PresentationGroup.objects.get(assessment_session=self.session, name='Group 1')
        self.assertEqual(set(group.member_ids()), {self.a.pk, self.b.pk})

    def test_student_listed_under_two_groups_in_same_file_flags_both(self):
        parsed = self.parsed(['Group 1', 'Alice Wang', 'A1'], ['Group 2', 'Alice Wang, Bob Otieno', 'A1, B1'])
        plan = plan_import(self.session, parsed)
        self.assertEqual({g['name'] for g in plan['groups'] if not g['ready']}, {'Group 1', 'Group 2'})
        apply_import(self.session, parsed)
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session).exists())
