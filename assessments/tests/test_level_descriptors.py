"""FA-02: owner-defined rubric point anchors survive each setup path and guide scoring."""
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import reverse

from assessments import rubric_import
from assessments.level_descriptors import parse_descriptors
from assessments.models import Evaluation, EvaluationScore, LevelDescriptor, PresentationTurn
from assessments.tests.test_edges_lecturer_views import LecturerTestCase
from assessments.tests.participant_flow import join_as
from assessments.tests.xlsx_helpers import xlsx_buffer


class DescriptorParserTests(SimpleTestCase):
    def test_owner_defined_point_values_are_sorted_and_pipe_text_is_preserved(self):
        levels, errors = parse_descriptors('30 | Complete | clear\n0 | Not shown', Decimal('30'))
        self.assertEqual(errors, [])
        self.assertEqual(levels, [
            {'score': Decimal('0.00'), 'description': 'Not shown'},
            {'score': Decimal('30.00'), 'description': 'Complete | clear'},
        ])

    def test_invalid_duplicate_out_of_range_and_overprecise_points_are_reported(self):
        levels, errors = parse_descriptors(
            'not a point | Invalid\n10 | First\n10.00 | Duplicate\n31 | Too high\n1.001 | Precise',
            Decimal('30'))
        self.assertEqual(len(levels), 1)
        self.assertEqual(len(errors), 4)
        self.assertTrue(any('repeats point value 10' in error for error in errors))
        self.assertTrue(any('between 0 and 30' in error for error in errors))
        self.assertTrue(any('at most 2 decimal places' in error for error in errors))

    def test_level_count_and_text_size_are_bounded_for_participant_pages(self):
        too_many = '\n'.join(f'{value} | Level {value}' for value in range(13))
        self.assertTrue(any('at most 12' in error for error in parse_descriptors(too_many, Decimal('100'))[1]))
        self.assertTrue(any('longer than 4000' in error for error in parse_descriptors('x' * 4001, Decimal('100'))[1]))


class RubricLevelDescriptorTests(LecturerTestCase):
    def edit(self, category, descriptors, weight='60'):
        return self.client.post(reverse('assessment_rubric_edit', args=[self.session.pk, category.pk]), {
            'name': category.name, 'description': category.description, 'scope': category.scope,
            'weight': weight, 'level_descriptors': descriptors,
        })

    def test_owner_can_add_edit_and_remove_point_descriptors(self):
        response = self.edit(self.quality, '0 | No evidence\n30 | Partial\n60 | Strong')
        self.assertRedirects(response, reverse('assessment_rubric', args=[self.session.pk]))
        self.assertEqual(list(self.quality.level_descriptors.values_list('score', 'description')),
                         [(Decimal('0.00'), 'No evidence'), (Decimal('30.00'), 'Partial'),
                          (Decimal('60.00'), 'Strong')])

        response = self.edit(self.quality, '0 | Not shown\n45 | Well supported')
        self.assertRedirects(response, reverse('assessment_rubric', args=[self.session.pk]))
        self.assertEqual(list(self.quality.level_descriptors.values_list('score', 'description')),
                         [(Decimal('0.00'), 'Not shown'), (Decimal('45.00'), 'Well supported')])

        response = self.edit(self.quality, '')
        self.assertRedirects(response, reverse('assessment_rubric', args=[self.session.pk]))
        self.assertFalse(self.quality.level_descriptors.exists())

    def test_form_rejects_bad_levels_and_a_weight_below_an_existing_anchor(self):
        for text in ('60 | Valid\n60 | Duplicate', '61 | Above weight', '2.001 | Too precise', '30 Missing separator'):
            response = self.edit(self.quality, text)
            self.assertEqual(response.status_code, 200)
            self.assertIn('level_descriptors', response.context['rubric_form'].errors)
            self.assertFalse(self.quality.level_descriptors.exists())

        self.edit(self.quality, '55 | High anchor')
        response = self.edit(self.quality, '55 | High anchor', weight='50')
        self.assertEqual(response.status_code, 200)
        self.assertIn('level_descriptors', response.context['rubric_form'].errors)
        self.assertEqual(self.quality.level_descriptors.get().score, Decimal('55.00'))

    def test_spreadsheet_import_parses_previews_and_saves_owner_defined_descriptors(self):
        headers = ['Category', 'Description', 'Scope', 'Weight %', 'Level descriptors']
        file = xlsx_buffer(headers,
            ['Technical', 'Evidence quality', 'Group', 60, '0 | Unsupported\n30 | Partly supported\n60 | Fully supported'],
            ['Teamwork', '', 'Individual', 40, '0 | No contribution\n40 | Clear contribution'])
        parsed = rubric_import.parse_rubric(file)
        self.assertIsNone(parsed['fatal'])
        verdict = rubric_import.check_import(parsed['rows'], self.session, 'replace')
        self.assertTrue(verdict['ok'], verdict['errors'])
        self.assertEqual(len(verdict['rows'][0]['level_descriptors']), 3)
        rubric_import.apply_import(self.session, parsed['rows'], 'replace')
        technical = self.session.rubric_categories.get(name='Technical')
        self.assertEqual(list(technical.level_descriptors.values_list('score', 'description')),
                         [(Decimal('0.00'), 'Unsupported'), (Decimal('30.00'), 'Partly supported'),
                          (Decimal('60.00'), 'Fully supported')])

    def test_spreadsheet_rejects_levels_above_criterion_weight_before_writing(self):
        rows = [{'row': 2, 'name': 'Technical', 'description': '', 'scope': 'Group', 'weight': '60',
                 'level_descriptors': '61 | Too high'}]
        verdict = rubric_import.check_import(rows, self.session, 'replace')
        self.assertFalse(verdict['ok'])
        self.assertTrue(any('between 0 and 60 points' in error for _, error in verdict['errors']))
        self.assertEqual(self.session.rubric_categories.count(), 2)

    def test_table_editor_updates_descriptors_and_freezes_them_after_scores_exist(self):
        url = reverse('assessment_rubric_table', args=[self.session.pk])

        def table(descriptors):
            data = {'rows-total': '2'}
            for index, (category, weight, level_text) in enumerate((
                    (self.quality, '60', descriptors), (self.teamwork, '40', ''))):
                data.update({f'rows-{index}-id': str(category.pk), f'rows-{index}-name': category.name,
                    f'rows-{index}-description': category.description, f'rows-{index}-scope': category.scope,
                    f'rows-{index}-weight': weight, f'rows-{index}-level_descriptors': level_text})
            return data

        saved = self.client.post(url, table('0 | Not shown\n60 | Strong'))
        self.assertRedirects(saved, reverse('assessment_rubric', args=[self.session.pk]))
        self.assertEqual(self.quality.level_descriptors.count(), 2)
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=self.quality, value=30)
        refused = self.client.post(url, table('0 | Changed\n60 | Strong'))
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, 'Score level guidance cannot change after this criterion has been scored.')
        self.assertEqual(self.quality.level_descriptors.get(score=0).description, 'Not shown')

    def test_participant_sees_escaped_descriptors_next_to_the_rubric_scale(self):
        LevelDescriptor.objects.create(rubric_category=self.quality, score=30,
                                       description='<script>alert(1)</script>Partly supported')
        self.teamwork.level_descriptors.create(score=20, description="Clear individual contribution")
        self.set_status('live')
        self.active_turn(self.red)
        participant = self.client_class()
        join_as(participant, self.session.uuid, self.cat.student_id)
        response = participant.get(reverse('student_evaluate', args=[self.session.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '30 points')
        self.assertContains(response, 'Clear individual contribution', count=1)
        self.assertContains(response, f'aria-describedby="individual-levels-{self.teamwork.pk}"')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;Partly supported')
        self.assertNotContains(response, '<script>alert(1)</script>Partly supported')


class LevelDescriptorMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, name):
        target = [('assessments', name)]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_forward_migration_preserves_existing_session_and_result_rows(self):
        old = self.migrate('0009_safe_submission_retry')
        session = old.get_model('assessments', 'AssessmentSession').objects.create(
            organization_id=300, name='Existing session')
        student = old.get_model('assessments', 'Student').objects.create(
            assessment_session=session, full_name='Existing student')
        group = old.get_model('assessments', 'PresentationGroup').objects.create(
            assessment_session=session, name='Existing group')
        category = old.get_model('assessments', 'RubricCategory').objects.create(
            assessment_session=session, name='Existing criterion', weight=100)
        turn = old.get_model('assessments', 'PresentationTurn').objects.create(
            assessment_session=session, group=group, status='closed')
        evaluation = old.get_model('assessments', 'Evaluation').objects.create(
            presentation_turn=turn, evaluator=student)
        score = old.get_model('assessments', 'EvaluationScore').objects.create(
            evaluation=evaluation, rubric_category=category, value=75, max_value=100)

        new = self.migrate('0010_leveldescriptor_templateleveldescriptor')
        self.assertEqual(new.get_model('assessments', 'AssessmentSession').objects.get(pk=session.pk).uuid,
                         session.uuid)
        self.assertEqual(new.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
        self.assertFalse(new.get_model('assessments', 'LevelDescriptor').objects.exists())

    def test_reverse_migration_refuses_to_discard_authored_descriptors(self):
        apps = self.migrate('0010_leveldescriptor_templateleveldescriptor')
        session = apps.get_model('assessments', 'AssessmentSession').objects.create(
            organization_id=300, name='Descriptor session')
        category = apps.get_model('assessments', 'RubricCategory').objects.create(
            assessment_session=session, name='Criterion', weight=100)
        descriptor = apps.get_model('assessments', 'LevelDescriptor').objects.create(
            rubric_category=category, score=50, description='Supported by strong evidence')
        template = apps.get_model('assessments', 'SessionTemplate').objects.create(
            organization_id=300, name='Reusable rubric')
        template_category = apps.get_model('assessments', 'TemplateRubricCategory').objects.create(
            template=template, name='Template criterion', weight=100)
        template_descriptor = apps.get_model('assessments', 'TemplateLevelDescriptor').objects.create(
            rubric_category=template_category, score=50, description='Template guidance')
        with self.assertRaisesRegex(RuntimeError, 'Rubric level descriptors exist'):
            self.migrate('0009_safe_submission_retry')
        descriptor_model = apps.get_model('assessments', 'LevelDescriptor')
        self.assertEqual(descriptor_model.objects.get(pk=descriptor.pk).description,
                         'Supported by strong evidence')
        template_descriptor_model = apps.get_model('assessments', 'TemplateLevelDescriptor')
        self.assertEqual(template_descriptor_model.objects.get(pk=template_descriptor.pk).description,
                         'Template guidance')
