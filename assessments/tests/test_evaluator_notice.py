"""FA-05: owner notice is editable and safely visible at join and before scoring."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.forms import AssessmentSessionForm
from assessments.models import AssessmentSession
from assessments.tests.participant_flow import join_as


class EvaluatorNoticeTests(TestCase):
    def setUp(self):
        self.org = make_org('University')
        self.owner = make_user('owner')
        sign_in(self.client, self.owner, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Class', status='live',
            instructions='Use evidence from the presentation.\nScore each criterion independently.')
        self.student = self.session.students.create(full_name='A student')
        self.group = self.session.groups.create(name='Presenters')
        self.category = self.session.rubric_categories.create(name='Evidence', weight=100)
        self.turn = self.session.presentation_turns.create(group=self.group, status='active')
        self.participant = Client()

    def edit(self, **extra):
        return self.client.post(reverse('assessment_session_edit', args=[self.session.pk]),
                                {'name': self.session.name, 'identify_by': 'full_name',
                                 'ungraded_penalty': '1', **extra})

    def test_notice_is_optional_and_bounded_on_form(self):
        data = {'name': 'Next', 'identify_by': 'full_name', 'ungraded_penalty': '1'}
        self.assertTrue(AssessmentSessionForm(data).is_valid())
        self.assertTrue(AssessmentSessionForm({**data, 'instructions': 'x' * 4000}).is_valid())
        form = AssessmentSessionForm({**data, 'instructions': 'x' * 4001})
        self.assertFalse(form.is_valid())
        self.assertIn('instructions', form.errors)

    def test_owner_can_create_with_notice(self):
        response = self.client.post(reverse('assessment_session_create'), {
            'name': 'Next class', 'identify_by': 'full_name', 'ungraded_penalty': '1',
            'instructions': 'Peer grades count towards your result.'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AssessmentSession.objects.get(name='Next class').instructions,
                         'Peer grades count towards your result.')

    def test_new_edit_and_setup_forms_render_notice_field(self):
        for name, args in (('assessment_session_create', []), ('assessment_session_edit', [self.session.pk]),
                           ('assessment_setup', [self.session.pk])):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=args))
                self.assertContains(response, 'name="instructions"')
                self.assertContains(response, 'Evaluator notice')
                rendered = response.content.decode()
                self.assertTrue('for="id_instructions"' in rendered or 'for="id_basics_instructions"' in rendered,
                                'Notice field must have an associated label')

    def test_live_edit_is_logged_without_copying_notice_text_to_history(self):
        response = self.edit(instructions='Keep this text out of the change log.')
        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.instructions, 'Keep this text out of the change log.')
        self.assertEqual(self.session.changes.get().summary, 'Changed the evaluator notice.')
        self.assertEqual(self.session.changes.get().by_user, self.owner)

    def test_notice_can_be_cleared(self):
        self.edit(instructions='')
        self.session.refresh_from_db()
        self.assertEqual(self.session.instructions, '')
        response = self.participant.get(reverse('student_join', args=[self.session.uuid]))
        self.assertNotContains(response, 'id="evaluator-notice-heading"')

    def test_closed_record_notice_cannot_be_changed(self):
        original = self.session.instructions
        self.session.status = 'closed'
        self.session.save()
        self.edit(instructions='Changed')
        self.session.refresh_from_db()
        self.assertEqual(self.session.instructions, original)

    def test_notice_is_visible_to_participant_at_join_and_before_scoring(self):
        join = reverse('student_join', args=[self.session.uuid])
        response = self.participant.get(join)
        self.assertContains(response, 'Use evidence from the presentation.<br>Score each criterion independently.', html=True)
        join_as(self.participant, self.session.uuid, 'A student')
        response = self.participant.get(reverse('student_evaluate', args=[self.session.uuid]))
        self.assertContains(response, 'Score each criterion independently.')
        self.assertLess(response.content.index(b'id="evaluator-notice-heading"'),
                        response.content.index(b'id="eval-form"'))

    def test_untrusted_notice_is_escaped_on_both_participant_pages(self):
        self.session.instructions = '<script>alert("unsafe")</script>\n<img src=x onerror=alert(1)>'
        self.session.save()
        join = reverse('student_join', args=[self.session.uuid])
        pages = [self.participant.get(join)]
        join_as(self.participant, self.session.uuid, 'A student')
        pages.append(self.participant.get(reverse('student_evaluate', args=[self.session.uuid])))
        for page in pages:
            self.assertContains(page, '&lt;script&gt;')
            self.assertNotContains(page, '<script>alert(')
            self.assertNotContains(page, '<img src=x')

    def test_notice_does_not_change_score_calculation_or_submission(self):
        join_as(self.participant, self.session.uuid, 'A student')
        response = self.participant.post(reverse('student_evaluate', args=[self.session.uuid]),
                                         {f'group_{self.category.pk}': '75', 'turn_id': self.turn.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.turn.evaluations.get().scores.get().value, 75)

    def test_other_owner_cannot_modify_notice(self):
        other = make_user('other')
        sign_in(self.client, other, self.org)
        response = self.edit(instructions='Forbidden')
        self.assertEqual(response.status_code, 404)
        self.session.refresh_from_db()
        self.assertNotEqual(self.session.instructions, 'Forbidden')


class EvaluatorNoticeMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, name):
        target = [('assessments', name)]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_existing_sessions_and_scores_survive_forward_and_reverse(self):
        before = self.migrate('0005_session_service')
        session = before.get_model('assessments', 'AssessmentSession').objects.create(
            organization_id=7, name='Existing class', service='classroom', status='closed')
        group = before.get_model('assessments', 'PresentationGroup').objects.create(assessment_session=session, name='Group')
        student = before.get_model('assessments', 'Student').objects.create(assessment_session=session, full_name='Student')
        category = before.get_model('assessments', 'RubricCategory').objects.create(assessment_session=session, name='Rubric', weight=100)
        turn = before.get_model('assessments', 'PresentationTurn').objects.create(assessment_session=session, group=group, status='closed')
        evaluation = before.get_model('assessments', 'Evaluation').objects.create(presentation_turn=turn, evaluator=student)
        score = before.get_model('assessments', 'EvaluationScore').objects.create(evaluation=evaluation, rubric_category=category, value=75, max_value=100)
        after = self.migrate('0006_evaluator_notice')
        updated = after.get_model('assessments', 'AssessmentSession').objects.get(pk=session.pk)
        self.assertEqual(updated.instructions, '')
        self.assertEqual(updated.uuid, session.uuid)
        self.assertEqual(updated.status, 'closed')
        self.assertEqual(after.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
        updated.instructions = 'New notice'
        updated.save()
        reversed_apps = self.migrate('0005_session_service')
        self.assertEqual(reversed_apps.get_model('assessments', 'AssessmentSession').objects.get(pk=session.pk).uuid, session.uuid)
        self.assertEqual(reversed_apps.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
