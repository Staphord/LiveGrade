from decimal import Decimal

from accounts.models import User
from django.test import TestCase

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, PresentationGroup, PresentationTurn, RubricCategory,
    Student,
)


class LecturerFlowTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)

    def test_create_session(self):
        response = self.client.post('/assessments/new/', {
            'name': 'FYP 2026', 'identify_by': 'full_name',
            'group_weight_percent': '60', 'individual_weight_percent': '40',
        })
        self.assertEqual(AssessmentSession.objects.count(), 1)
        session = AssessmentSession.objects.first()
        self.assertRedirects(response, f'/assessments/{session.pk}/groups/')

    def test_session_list_renders(self):
        response = self.client.get('/assessments/')
        self.assertEqual(response.status_code, 200)

    def test_session_create_page_renders(self):
        response = self.client.get('/assessments/new/')
        self.assertEqual(response.status_code, 200)

    def test_session_list_empty_state_links_to_create_page(self):
        response = self.client.get('/assessments/')
        self.assertContains(response, 'href="/assessments/new/"')

    def test_full_setup_flow_renders_and_goes_live(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer)

        # Roster
        response = self.client.post(f'/assessments/{session.pk}/roster/', {
            'full_name': 'Student A', 'student_id': 'A1', 'email': '', 'programme': '',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(session.students.count(), 1)

        response = self.client.get(f'/assessments/{session.pk}/roster/')
        self.assertEqual(response.status_code, 200)

        # Rubric
        response = self.client.post(f'/assessments/{session.pk}/rubric/', {
            'name': 'Technical implementation', 'description': '', 'scope': 'group',
            'max_points': '10', 'weight': '60',
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get(f'/assessments/{session.pk}/rubric/')
        self.assertEqual(response.status_code, 200)

        # Groups
        response = self.client.post(f'/assessments/{session.pk}/groups/', {'name': 'Group 1', 'max_size': '5'})
        self.assertEqual(response.status_code, 302)
        group = PresentationGroup.objects.get(assessment_session=session)
        response = self.client.get(f'/assessments/{session.pk}/groups/')
        self.assertEqual(response.status_code, 200)

        student = Student.objects.get(assessment_session=session)
        response = self.client.post(
            f'/assessments/{session.pk}/groups/{group.pk}/members/',
            {'action': 'add', 'student_id': student.pk})
        self.assertEqual(response.status_code, 302)

        # Overview + go live
        response = self.client.get(f'/assessments/{session.pk}/')
        self.assertEqual(response.status_code, 200)

        session.refresh_from_db()
        self.assertTrue(session.can_go_live())
        response = self.client.post(f'/assessments/{session.pk}/go-live/')
        session.refresh_from_db()
        self.assertEqual(session.status, AssessmentSession.Status.LIVE)

        # Activate the group
        response = self.client.post(f'/assessments/{session.pk}/activate/{group.pk}/')
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(session.active_turn())

        # QR endpoint
        response = self.client.get(f'/assessments/{session.pk}/qr.png')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

        # Participation + results pages render
        self.assertEqual(self.client.get(f'/assessments/{session.pk}/participation/').status_code, 200)
        self.assertEqual(self.client.get(f'/assessments/{session.pk}/results/').status_code, 200)
        self.assertEqual(self.client.get(f'/assessments/{session.pk}/results/export/').status_code, 200)

    def test_cannot_go_live_without_rubric_or_groups(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Empty', created_by=self.lecturer)
        response = self.client.post(f'/assessments/{session.pk}/go-live/')
        session.refresh_from_db()
        self.assertEqual(session.status, AssessmentSession.Status.DRAFT)

    def test_developer_cannot_access(self):
        developer = make_user('dev')
        self.client.logout()
        sign_in(self.client, developer)  # DevPerf lists no organization they may run it for
        response = self.client.get('/assessments/')
        self.assertEqual(response.status_code, 403)


class RosterEditViewTests(TestCase):
    """The roster's edit button mirrors the Groups tab's reused-form
    pattern — this covers the POST endpoint it submits to."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session', created_by=self.lecturer)
        self.student = Student.objects.create(
            assessment_session=self.session, full_name='Jane Doe', student_id='SCT001')

    def test_updates_the_student(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/roster/{self.student.pk}/edit/',
            {'full_name': 'Jane Updated', 'student_id': 'SCT001', 'email': '', 'programme': 'BSc. CS'})

        self.assertRedirects(response, f'/assessments/{self.session.pk}/roster/')
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, 'Jane Updated')
        self.assertEqual(self.student.programme, 'BSc. CS')

    def test_rejects_a_blank_identifying_field(self):
        self.session.identify_by = AssessmentSession.IdentifyBy.STUDENT_ID
        self.session.save(update_fields=['identify_by'])

        self.client.post(
            f'/assessments/{self.session.pk}/roster/{self.student.pk}/edit/',
            {'full_name': 'Jane Doe', 'student_id': '', 'email': '', 'programme': ''})

        self.student.refresh_from_db()
        self.assertEqual(self.student.student_id, 'SCT001')  # unchanged — the blank ID was rejected


class RosterSearchViewTests(TestCase):
    """The search box now filters via a silent fetch instead of a full
    page reload — this covers the JSON endpoint it calls."""

    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Session', created_by=self.lecturer)
        Student.objects.create(assessment_session=self.session, full_name='Alice Wang', student_id='SCT001')
        Student.objects.create(assessment_session=self.session, full_name='Boniface Omondi', student_id='SCT002')

    def test_filters_by_query_and_returns_fragments(self):
        response = self.client.get(f'/assessments/{self.session.pk}/roster/search/', {'q': 'alice'})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('Alice Wang', data['table_html'])
        self.assertNotIn('Boniface Omondi', data['table_html'])
        self.assertIn('1', data['count_html'])

    def test_blank_query_returns_everyone(self):
        data = self.client.get(f'/assessments/{self.session.pk}/roster/search/').json()

        self.assertIn('Alice Wang', data['table_html'])
        self.assertIn('Boniface Omondi', data['table_html'])


class LecturerNextGroupTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Live Session', status=AssessmentSession.Status.LIVE, created_by=self.lecturer)
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='G1', order=1)
        self.group2 = PresentationGroup.objects.create(assessment_session=self.session, name='G2', order=2)
        self.group3 = PresentationGroup.objects.create(assessment_session=self.session, name='G3', order=3)

    def test_set_designated_next_group(self):
        response = self.client.post(
            f'/assessments/{self.session.pk}/set-next-group/',
            {'group_id': self.group3.pk},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.designated_next_group, self.group3)
        self.assertEqual(self.session.get_effective_next_group(), self.group3)

    def test_clear_designated_next_group(self):
        self.session.designated_next_group = self.group3
        self.session.save(update_fields=['designated_next_group'])
        response = self.client.post(
            f'/assessments/{self.session.pk}/set-next-group/',
            {'group_id': ''},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.session.refresh_from_db()
        self.assertIsNone(self.session.designated_next_group)
        self.assertEqual(self.session.get_effective_next_group(), self.group1)
