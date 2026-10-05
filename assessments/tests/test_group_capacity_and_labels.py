"""Group size cap (PresentationGroup.max_size) and the roster form's
missing labels — both raised directly by the user after trying the UI."""


from accounts.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from .xlsx_helpers import xlsx_buffer
from accounts.testing import make_org, make_user, sign_in
from assessments.group_import import plan_import, parse_groups
from assessments.models import (
    AssessmentSession, GroupMembership, PresentationGroup, Student,
)


class ModelCapacityTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026')
        self.group = PresentationGroup.objects.create(
            assessment_session=self.session, name='Group 1', max_size=2)
        self.a = Student.objects.create(assessment_session=self.session, full_name='A')
        self.b = Student.objects.create(assessment_session=self.session, full_name='B')
        self.c = Student.objects.create(assessment_session=self.session, full_name='C')

    def test_group_reports_full_state(self):
        self.assertFalse(self.group.is_full())
        self.assertEqual(self.group.room_left(), 2)
        GroupMembership.objects.create(group=self.group, student=self.a)
        GroupMembership.objects.create(group=self.group, student=self.b)
        self.assertTrue(self.group.is_full())
        self.assertEqual(self.group.room_left(), 0)

    def test_third_member_rejected_when_max_size_two(self):
        GroupMembership.objects.create(group=self.group, student=self.a)
        GroupMembership.objects.create(group=self.group, student=self.b)
        overflow = GroupMembership(group=self.group, student=self.c)
        with self.assertRaises(ValidationError):
            overflow.full_clean()

    def test_unlimited_group_has_no_room_cap(self):
        unlimited = PresentationGroup.objects.create(assessment_session=self.session, name='Unlimited')
        self.assertIsNone(unlimited.room_left())
        self.assertFalse(unlimited.is_full())


class GroupViewsCapacityTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer)
        self.a = Student.objects.create(assessment_session=self.session, full_name='A')
        self.b = Student.objects.create(assessment_session=self.session, full_name='B')
        self.c = Student.objects.create(assessment_session=self.session, full_name='C')

    def test_create_group_with_size_and_exact_members(self):
        response = self.client.post(f'/assessments/{self.session.pk}/groups/', {
            'name': 'Group 1', 'max_size': '2',
            'members': [str(self.a.pk), str(self.b.pk)],
        })
        self.assertRedirects(response, f'/assessments/{self.session.pk}/groups/')
        group = PresentationGroup.objects.get(assessment_session=self.session, name='Group 1')
        self.assertEqual(group.max_size, 2)
        self.assertEqual(group.member_count(), 2)

    def test_create_group_rejects_more_members_than_size(self):
        response = self.client.post(f'/assessments/{self.session.pk}/groups/', {
            'name': 'Group 1', 'max_size': '2',
            'members': [str(self.a.pk), str(self.b.pk), str(self.c.pk)],
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 1').exists())

    def test_add_student_to_full_group_is_rejected(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', max_size=1)
        GroupMembership.objects.create(group=group, student=self.a)
        self.client.post(f'/assessments/{self.session.pk}/groups/{group.pk}/members/',
                          {'action': 'add', 'student_id': self.b.pk})
        self.assertEqual(group.member_count(), 1)

    def test_edit_group_updates_size(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        response = self.client.post(f'/assessments/{self.session.pk}/groups/{group.pk}/edit/',
                                     {'name': 'Group 1', 'max_size': '3'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/groups/')
        group.refresh_from_db()
        self.assertEqual(group.max_size, 3)

    def test_edit_cannot_shrink_size_below_current_members(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        GroupMembership.objects.create(group=group, student=self.a)
        GroupMembership.objects.create(group=group, student=self.b)
        response = self.client.post(f'/assessments/{self.session.pk}/groups/{group.pk}/edit/',
                                     {'name': 'Group 1', 'max_size': '1'})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/groups/')
        group.refresh_from_db()
        self.assertIsNone(group.max_size)  # rejected, unchanged

    def test_groups_page_shows_capacity_and_edit_controls(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', max_size=2)
        response = self.client.get(f'/assessments/{self.session.pk}/groups/')
        self.assertContains(response, '0/2')
        self.assertContains(response, 'group-edit-btn')


class ImportGroupsCapacityTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026')
        self.a = Student.objects.create(assessment_session=self.session, full_name='A', student_id='A1')
        self.b = Student.objects.create(assessment_session=self.session, full_name='B', student_id='B1')
        self.c = Student.objects.create(assessment_session=self.session, full_name='C', student_id='C1')

    def test_reimport_beyond_existing_groups_cap_is_flagged(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1', max_size=1)
        GroupMembership.objects.create(group=group, student=self.a)

        f = xlsx_buffer(['Group Name', 'Name', 'Student ID'], ['Group 1', 'A, B', 'A1, B1'])
        plan = plan_import(self.session, parse_groups(f))

        flagged = [g for g in plan['groups'] if not g['ready']]
        self.assertEqual(len(flagged), 1)
        self.assertIn('allows 1 member', flagged[0]['issues'][0])
        self.assertEqual(group.member_count(), 1)


class RosterFormLabelsTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)

    def test_roster_form_fields_are_labeled(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026',
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID, created_by=self.lecturer)
        response = self.client.get(f'/assessments/{session.pk}/roster/')
        self.assertContains(response, 'Full name')
        self.assertContains(response, 'Student ID')
        self.assertContains(response, 'used to join')
        self.assertContains(response, 'Email')
        self.assertContains(response, 'Programme')
