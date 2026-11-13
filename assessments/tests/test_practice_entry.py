"""FA-04: practice is usable, labelled, and excluded from every counted outcome."""
from decimal import Decimal
from io import BytesIO

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from accounts.testing import make_org, make_user, sign_in
from assessments.forms import PresentationGroupForm
from assessments.models import (
    AssessmentSession, Evaluation, EvaluationScore, GroupMembership, PresentationGroup,
    PresentationTurn,
)
from assessments.tests.participant_flow import join_as
from assessments.scoring import (
    final_score_percent, fraction_averages, group_score_percent, group_score_percents, group_vote_counts,
    individual_score_percent, session_results, turn_vote_counts, ungraded_group_count,
)
from assessments.services import submit_evaluation
from assessments.views.lecturer_views import _live_context, _participation_rows
from assessments.views.student_views import _public_live_state


class PracticeEntryTests(TestCase):
    def setUp(self):
        self.org = make_org('University')
        self.owner = make_user('owner')
        sign_in(self.client, self.owner, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.id, created_by=self.owner,
            name='Class', status='live', ungraded_penalty=5)
        self.ann = self.session.students.create(full_name='Ann')
        self.bob = self.session.students.create(full_name='Bob')
        self.eva = self.session.students.create(full_name='Eva')
        self.a = self.session.groups.create(name='Assessed A', max_size=3)
        self.b = self.session.groups.create(name='Assessed B', max_size=3)
        GroupMembership.objects.create(group=self.a, student=self.ann)
        GroupMembership.objects.create(group=self.b, student=self.bob)
        self.group_cat = self.session.rubric_categories.create(name='Group', scope='group', weight=80)
        self.individual_cat = self.session.rubric_categories.create(name='Individual', scope='individual', weight=20)
        self.turn_a = self.session.presentation_turns.create(group=self.a, status='closed')
        self.turn_b = self.session.presentation_turns.create(group=self.b, status='closed')
        self.score(self.turn_a, self.eva, self.group_cat, 80)
        self.score(self.turn_a, self.eva, self.individual_cat, 10, self.ann)
        self.score(self.turn_b, self.eva, self.group_cat, 40)
        self.practice = self.session.groups.create(name='PRACTICE-SAMPLE', is_practice=True, max_size=3, order=2)
        self.practice_turn = self.session.presentation_turns.create(group=self.practice, status='active')

    def score(self, turn, evaluator, category, value, target=None):
        evaluation, _ = Evaluation.objects.get_or_create(presentation_turn=turn, evaluator=evaluator, target_student=target)
        return EvaluationScore.objects.create(evaluation=evaluation, rubric_category=category,
                                              value=value, max_value=category.weight)

    def practice_votes(self):
        self.score(self.practice_turn, self.ann, self.group_cat, 80)
        self.score(self.practice_turn, self.ann, self.individual_cat, 20, self.ann)
        self.score(self.practice_turn, self.bob, self.group_cat, 80)

    def url(self, name):
        return reverse(name, args=[self.session.pk])

    def test_existing_and_new_groups_are_assessed_by_default(self):
        self.assertFalse(self.a.is_practice)
        self.assertFalse(self.session.groups.create(name='Default').is_practice)

    def test_practice_scores_never_change_bulk_or_single_student_results(self):
        before = session_results(self.session)
        self.practice_votes()
        self.assertEqual(session_results(self.session), before)
        self.assertEqual(group_score_percent(self.practice, self.session), None)
        self.assertEqual(group_score_percents(self.session, [self.practice]), {self.practice.pk: None})
        self.assertEqual(individual_score_percent(self.ann, self.session), Decimal('10.0'))
        self.assertEqual(final_score_percent(self.ann, self.a, self.session), Decimal('85.0'))
        self.assertIsNone(final_score_percent(self.ann, self.practice, self.session))
        self.assertEqual({row['group'].pk for row in before['group_rows']}, {self.a.pk, self.b.pk})
        self.assertEqual({row['student'].pk for row in before['student_rows']}, {self.ann.pk, self.bob.pk})

    def test_shared_score_aggregation_excludes_practice_even_with_a_raw_queryset(self):
        self.practice_votes()
        averages = fraction_averages(EvaluationScore.objects.filter(rubric_category=self.group_cat), 'rubric_category_id')
        self.assertEqual(averages, {(self.group_cat.pk,): Decimal('0.75')})

    def test_skipped_practice_and_practice_votes_do_not_change_penalties(self):
        self.assertEqual(ungraded_group_count(self.ann, self.a, self.session), 1)
        self.practice_votes()
        self.assertEqual(ungraded_group_count(self.ann, self.a, self.session), 1)
        self.assertEqual(ungraded_group_count(self.eva, None, self.session), 0)
        row = next(row for row in session_results(self.session)['student_rows'] if row['student'] == self.ann)
        self.assertEqual(row['penalty'], 5)
        self.assertEqual(row['ungraded'], 1)

    def test_practice_votes_are_not_counted_as_participation(self):
        before = _participation_rows(self.session)
        self.practice_votes()
        after = _participation_rows(self.session)
        self.assertEqual(before, after)
        self.assertNotIn(self.practice, after['groups'])
        self.assertIsNone(after['active_group_id'])
        self.assertIsNone(after['active_group_name'])
        ann = next(row for row in after['rows'] if row['student'] == self.ann)
        self.assertEqual(ann['graded_count'], 0)
        self.assertEqual(ann['eligible_count'], 1)
        self.assertEqual(ann['status'], 'none')
        self.assertNotIn(self.practice.pk, group_vote_counts(self.session))
        self.assertNotIn(self.practice_turn.pk, turn_vote_counts([self.practice_turn.pk]))

    def test_practice_membership_does_not_leak_into_participation_report(self):
        GroupMembership.objects.create(group=self.practice, student=self.eva)
        data = _participation_rows(self.session)
        eva = next(row for row in data['rows'] if row['student'] == self.eva)
        self.assertIsNone(eva['own_group'])
        self.assertEqual(eva['eligible_count'], 2)
        self.assertNotIn(self.practice, data['groups'])

    def test_live_rankings_and_closed_timeline_exclude_practice(self):
        self.practice_votes()
        live = _live_context(self.session)
        self.assertEqual({row['group'].pk for row in live['group_rankings']}, {self.a.pk, self.b.pk})
        self.assertEqual(live['voted_count'], 1)  # Only Eva submitted assessed scores.
        self.practice_turn.status = 'closed'
        self.practice_turn.closed_at = timezone.now()
        self.practice_turn.save()
        self.session.status = 'closed'
        self.session.save()
        closed = _live_context(self.session)
        self.assertNotIn(self.practice, [row['group'] for row in closed['presentation_timeline']])

    def test_results_and_participation_exports_contain_no_practice_group(self):
        self.practice_votes()
        GroupMembership.objects.create(group=self.practice, student=self.eva)
        response = self.client.get(self.url('assessment_results_export'))
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), data_only=True)
        for sheet in workbook:
            for row in sheet.values:
                self.assertNotIn('PRACTICE-SAMPLE', row)
        results = list(workbook['Results'].values)
        ann = next(row for row in results[1:] if row[0] == 'Ann')
        self.assertEqual(ann[-1], 85)
        self.assertEqual(len(list(workbook['Group scores'].values)), 3)

    def test_practice_can_be_scored_through_normal_service(self):
        submit_evaluation(self.practice_turn, self.ann, {self.group_cat.pk: Decimal('60')}, {})
        self.assertEqual(self.practice_turn.evaluations.get().scores.get().value, 60)
        self.assertIsNone(group_score_percent(self.practice, self.session))
        with self.assertRaises(ValidationError):
            submit_evaluation(self.practice_turn, self.ann, {self.group_cat.pk: Decimal('60')}, {})

    def test_practice_uses_existing_self_score_and_pause_rules(self):
        GroupMembership.objects.create(group=self.practice, student=self.ann)
        with self.assertRaises(ValidationError):
            submit_evaluation(self.practice_turn, self.ann, {self.group_cat.pk: Decimal('60')}, {})
        self.session.voting_paused = True
        self.session.save()
        self.practice_turn.assessment_session = self.session
        with self.assertRaises(ValidationError):
            submit_evaluation(self.practice_turn, self.bob, {self.group_cat.pk: Decimal('60')}, {})

    def test_practice_label_shown_to_owner_participant_and_public_room(self):
        self.assertContains(self.client.get(self.url('assessment_groups')), 'Practice')
        self.assertContains(self.client.get(self.url('assessment_session_detail')), 'PRACTICE-SAMPLE · Practice')
        participant = Client()
        join_as(participant, self.session.uuid, 'Ann')
        response = participant.get(reverse('student_evaluate', args=[self.session.uuid]))
        self.assertContains(response, 'Practice - does not count.')
        participant.post(reverse('student_evaluate', args=[self.session.uuid]),
                         {f'group_{self.group_cat.pk}': '60', 'turn_id': self.practice_turn.pk})
        response = participant.get(reverse('student_submitted', args=[self.session.uuid]))
        self.assertContains(response, 'Practice submission received.')
        state = _public_live_state(self.session)
        self.assertTrue(state['public_is_practice'])
        self.assertTrue(state['public_stage'].startswith('Practice - '))
        self.assertEqual(state['public_submitted_count'], 0)
        self.assertContains(participant.get(reverse('assessment_join_screen', args=[self.session.uuid])), 'Practice')

    def test_owner_can_create_and_edit_practice_before_it_presents(self):
        response = self.client.post(self.url('assessment_groups'), {'name': 'Warmup', 'max_size': '3', 'is_practice': 'on'})
        self.assertEqual(response.status_code, 302)
        group = self.session.groups.get(name='Warmup')
        self.assertTrue(group.is_practice)
        edit = reverse('assessment_group_edit', args=[self.session.pk, group.pk])
        self.client.post(edit, {'name': 'Warmup', 'max_size': '3'})
        group.refresh_from_db()
        self.assertFalse(group.is_practice)

    def test_presented_group_practice_status_is_immutable_but_metadata_is_editable(self):
        edit = reverse('assessment_group_edit', args=[self.session.pk, self.practice.pk])
        response = self.client.post(edit, {'name': 'Same exercise', 'max_size': '3'}, follow=True)
        self.assertContains(response, 'Practice status cannot change after this group has presented.')
        self.practice.refresh_from_db()
        self.assertTrue(self.practice.is_practice)
        self.assertEqual(self.practice.name, 'PRACTICE-SAMPLE')
        self.client.post(edit, {'name': 'Same exercise', 'max_size': '3', 'is_practice': 'on'})
        self.practice.refresh_from_db()
        self.assertEqual(self.practice.name, 'Same exercise')
        self.assertTrue(self.practice.is_practice)
        form = PresentationGroupForm({'name': self.a.name, 'max_size': 3, 'is_practice': 'on'}, instance=self.a)
        self.assertFalse(form.is_valid())
        self.assertIn('is_practice', form.errors)

    def test_copy_preserves_practice_configuration_but_not_practice_scores(self):
        self.practice_votes()
        response = self.client.post(self.url('assessment_session_duplicate'),
                                   {'name': 'Copy', 'include_groups': 'on'})
        self.assertEqual(response.status_code, 302)
        copy = AssessmentSession.objects.get(name='Copy')
        self.assertTrue(copy.groups.get(name='PRACTICE-SAMPLE').is_practice)
        self.assertFalse(copy.groups.get(name=self.a.name).is_practice)
        self.assertFalse(copy.presentation_turns.exists())

    def test_other_owner_and_other_service_cannot_set_practice_flag(self):
        edit = reverse('assessment_group_edit', args=[self.session.pk, self.a.pk])
        for service, owner in [('classroom', make_user('colleague')), ('panel', self.owner)]:
            self.session.service = service
            self.session.save()
            sign_in(self.client, owner, self.org)
            self.assertEqual(self.client.post(edit, {'name': 'Stolen', 'max_size': 3, 'is_practice': 'on'}).status_code, 404)
        self.a.refresh_from_db()
        self.assertFalse(self.a.is_practice)


class PracticeMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, name):
        targets = [('assessments', name)]
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_existing_groups_remain_assessed_and_reverse_keeps_original_data(self):
        old = self.migrate('0006_evaluator_notice')
        session = old.get_model('assessments', 'AssessmentSession').objects.create(organization_id=10, name='Existing')
        group = old.get_model('assessments', 'PresentationGroup').objects.create(assessment_session=session, name='Assessed')
        student = old.get_model('assessments', 'Student').objects.create(assessment_session=session, full_name='Existing student')
        category = old.get_model('assessments', 'RubricCategory').objects.create(assessment_session=session, name='Existing criterion', weight=100)
        turn = old.get_model('assessments', 'PresentationTurn').objects.create(assessment_session=session, group=group, status='closed')
        evaluation = old.get_model('assessments', 'Evaluation').objects.create(presentation_turn=turn, evaluator=student)
        score = old.get_model('assessments', 'EvaluationScore').objects.create(evaluation=evaluation, rubric_category=category, value=75, max_value=100)
        new = self.migrate('0007_practice_entry')
        self.assertEqual(new.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
        self.assertFalse(new.get_model('assessments', 'PresentationGroup').objects.get(pk=group.pk).is_practice)
        reversed_apps = self.migrate('0006_evaluator_notice')
        kept = reversed_apps.get_model('assessments', 'PresentationGroup').objects.get(pk=group.pk)
        self.assertEqual(kept.name, 'Assessed')
        self.assertEqual(kept.assessment_session_id, session.pk)
        self.assertEqual(reversed_apps.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)

    def test_reverse_refuses_to_reclassify_existing_practice_as_assessed_work(self):
        apps = self.migrate('0007_practice_entry')
        session = apps.get_model('assessments', 'AssessmentSession').objects.create(organization_id=10, name='Practice class')
        group = apps.get_model('assessments', 'PresentationGroup').objects.create(
            assessment_session=session, name='Keep excluded', is_practice=True)
        with self.assertRaisesMessage(IrreversibleError, 'Practice groups exist'):
            self.migrate('0006_evaluator_notice')
        self.assertTrue(apps.get_model('assessments', 'PresentationGroup').objects.get(pk=group.pk).is_practice)
