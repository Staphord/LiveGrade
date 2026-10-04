from decimal import Decimal

from accounts.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.models import (
    AssessmentSession, Evaluation, GroupMembership, PresentationGroup,
    PresentationTurn, RubricCategory, Student,
)


class StudentFlowTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer,
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID,
            status=AssessmentSession.Status.LIVE)
        self.group_cat = RubricCategory.objects.create(
            assessment_session=self.session, name='Implementation',
            scope=RubricCategory.Scope.GROUP, max_points=Decimal('10'), weight=Decimal('100'))
        self.group1 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 1')
        self.student_a = Student.objects.create(assessment_session=self.session,
            full_name='Student A', student_id='A1')
        self.student_c = Student.objects.create(assessment_session=self.session,
            full_name='Student C', student_id='C1')
        GroupMembership.objects.create(group=self.group1, student=self.student_a)
        self.turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.group1,
            status=PresentationTurn.Status.ACTIVE)

    def test_join_with_correct_id_succeeds(self):
        response = self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        self.assertRedirects(response, f'/assess/{self.session.uuid}/evaluate/')

    def test_join_with_wrong_id_shows_error(self):
        response = self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'ZZZ'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn&#x27;t find you")

    def test_evaluate_page_shows_ineligible_for_own_group_member(self):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'a1'})
        response = self.client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(response, "can't grade it")

    def test_full_evaluate_and_submit(self):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        response = self.client.post(f'/assess/{self.session.uuid}/evaluate/', {
            f'group_{self.group_cat.pk}': '8',
        })
        self.assertRedirects(response, f'/assess/{self.session.uuid}/submitted/')

    def test_cannot_submit_twice(self):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        self.client.post(f'/assess/{self.session.uuid}/evaluate/', {f'group_{self.group_cat.pk}': '8'})
        response = self.client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(response, "already graded")

    def test_checked_in_student_name_shown_on_evaluate_page(self):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        response = self.client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(response, 'Checked in as')
        self.assertContains(response, self.student_c.full_name)

    def test_checked_in_student_name_shown_on_submitted_page(self):
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        self.client.post(f'/assess/{self.session.uuid}/evaluate/', {f'group_{self.group_cat.pk}': '8'})
        response = self.client.get(f'/assess/{self.session.uuid}/submitted/')
        self.assertContains(response, self.student_c.full_name)

    def test_join_page_does_not_show_a_student_badge_before_checking_in(self):
        response = self.client.get(f'/assess/{self.session.uuid}/join/')
        # Check for the rendered badge's own content, not the CSS class name
        # — `.student-pill` itself appears in every page's shared stylesheet
        # regardless of whether the badge is actually rendered.
        self.assertNotContains(response, 'Checked in as')

    def test_submission_after_turn_expired_is_not_counted(self):
        self.turn.voting_ends_at = timezone.now() - timezone.timedelta(seconds=5)
        self.turn.save(update_fields=['voting_ends_at'])
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        response = self.client.post(f'/assess/{self.session.uuid}/evaluate/', {
            f'group_{self.group_cat.pk}': '8', 'turn_id': self.turn.pk,
        })
        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected to submitted
        self.assertContains(response, "wasn&#x27;t counted")
        self.assertEqual(Evaluation.objects.count(), 0)

    def test_submission_for_a_turn_that_is_no_longer_active_is_not_counted(self):
        self.turn.status = PresentationTurn.Status.CLOSED
        self.turn.save(update_fields=['status'])
        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        response = self.client.post(f'/assess/{self.session.uuid}/evaluate/', {
            f'group_{self.group_cat.pk}': '8', 'turn_id': self.turn.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Evaluation.objects.count(), 0)

    def test_lineup_and_next_group_in_evaluate_page(self):
        group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)
        group3 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 3', order=3)
        self.session.designated_next_group = group3
        self.session.save(update_fields=['designated_next_group'])

        self.client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'c1'})
        response = self.client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Presentation Order')
        self.assertContains(response, 'Group 3')
        self.assertEqual(response.context['next_group'], group3)

    def test_join_screen_state_json(self):
        group2 = PresentationGroup.objects.create(assessment_session=self.session, name='Group 2', order=2)
        self.session.designated_next_group = group2
        self.session.save(update_fields=['designated_next_group'])

        response = self.client.get(f'/assess/{self.session.uuid}/join-screen/state/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['public_group_name'], 'Group 1')
        self.assertEqual(data['public_next_group_name'], 'Group 2')
        self.assertEqual(data['public_total_groups'], 2)
