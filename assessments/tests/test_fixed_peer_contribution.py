"""Owner decision 2026-10-26 supersedes the proposed adjustable peer split."""
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.forms import AssessmentSessionForm
from assessments.models import AssessmentSession, EvaluationScore, GroupMembership, SessionTemplate
from assessments.scoring import final_score_percent, session_results
from assessments.tests.participant_flow import join_as


class FixedPeerContributionTests(TestCase):
    def setUp(self):
        self.org = make_org('University')
        self.owner = make_user('owner')
        sign_in(self.client, self.owner, self.org)
        self.session = AssessmentSession.objects.create(name='Class', organization_id=self.org.id,
            created_by=self.owner, status='live', ungraded_penalty=0)
        self.student = self.session.students.create(full_name='Presenter')
        self.evaluator = self.session.students.create(full_name='Evaluator')
        self.group = self.session.groups.create(name='Group')
        GroupMembership.objects.create(group=self.group, student=self.student)
        category = self.session.rubric_categories.create(name='Criterion', weight=100)
        turn = self.session.presentation_turns.create(group=self.group, status='active')
        evaluation = turn.evaluations.create(evaluator=self.evaluator)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=category, value=75, max_value=100)

    def test_entire_assessment_component_comes_from_peer_scores(self):
        self.assertEqual(final_score_percent(self.student, self.group, self.session), Decimal('75.0'))
        result = session_results(self.session)['student_rows'][0]
        self.assertEqual(result['final_percent'], Decimal('75.0'))

    def test_peer_split_and_manual_marks_are_not_model_or_form_configuration(self):
        for field in ('peer_weight_percent', 'manual_mark', 'manual_weight_percent'):
            self.assertNotIn(field, AssessmentSessionForm.base_fields)
            self.assertNotIn(field, {item.name for item in AssessmentSession._meta.fields})
            self.assertNotIn(field, {item.name for item in SessionTemplate._meta.fields})

    def test_posted_weight_and_manual_marks_cannot_change_results(self):
        for weight in ('0', '50', '100'):
            with self.subTest(weight=weight):
                response = self.client.post(reverse('assessment_session_edit', args=[self.session.pk]), {
                    'name': 'Class', 'identify_by': 'full_name', 'ungraded_penalty': '0',
                    'peer_weight_percent': weight, 'manual_mark': '0', 'manual_weight_percent': '100'})
                self.assertEqual(response.status_code, 302)
                self.session.refresh_from_db()
                self.assertEqual(final_score_percent(self.student, self.group, self.session), Decimal('75.0'))

    def test_owner_and_participant_screens_disclose_fixed_peer_contribution(self):
        for route, args in (('assessment_session_create', []), ('assessment_session_edit', [self.session.pk]),
                            ('assessment_results', [self.session.pk])):
            self.assertContains(self.client.get(reverse(route, args=args)), 'Peer grading supplies 100% of the assessment score.')
        participant = Client()
        self.assertContains(participant.get(reverse('student_join', args=[self.session.uuid])),
                            'Peer grading supplies 100% of the assessment score.')
        join_as(participant, self.session.uuid, 'Evaluator')
        self.assertContains(participant.get(reverse('student_evaluate', args=[self.session.uuid])),
                            'Peer grading supplies 100% of the assessment score.')
