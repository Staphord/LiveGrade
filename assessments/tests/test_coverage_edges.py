"""Exercise edge branches found by the planned Classroom coverage audit."""
from decimal import Decimal, localcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase

from assessments.forms import RubricCategoryForm, TemplateChoiceForm
from assessments.level_descriptors import parse_descriptors
from assessments.models import (
    LevelDescriptor, PresentationTurn, SessionTemplate, TemplateLevelDescriptor,
    TemplateRubricCategory,
)
from assessments.services import submit_evaluation, submission_digest
from assessments.session_templates import CLASSROOM_STARTERS, validate_categories
from assessments.tests.test_edges_lecturer_views import LecturerTestCase


class RubricUtilityCoverageTests(SimpleTestCase):
    def test_blank_descriptor_lines_are_ignored_and_missing_or_long_guidance_is_reported(self):
        levels, errors = parse_descriptors('\n  \n5 |   \n10 | ' + ('x' * 501), Decimal('10'))
        self.assertEqual(levels, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any('needs guidance' in error for error in errors))
        self.assertTrue(any('longer than 500' in error for error in errors))

    def test_point_rounding_context_failure_is_reported(self):
        with localcontext() as context:
            context.prec = 1
            levels, errors = parse_descriptors('1.23 | Supported', Decimal('10'))
        self.assertEqual(levels, [])
        self.assertTrue(any('at most 2 decimal places' in error for error in errors))


class RubricFormCoverageTests(LecturerTestCase):
    def test_template_choice_groups_include_starters_and_saved_templates(self):
        saved = SimpleNamespace(pk=27, name='My reusable rubric')
        form = TemplateChoiceForm(starters=CLASSROOM_STARTERS, saved_templates=[saved])
        self.assertIn(('Classroom starters', [
            ('starter:' + item.key, item.name) for item in CLASSROOM_STARTERS]),
            form.fields['template'].choices)
        self.assertIn(('Your saved templates', [('saved:27', 'My reusable rubric')]),
                      form.fields['template'].choices)
        self.assertEqual(TemplateChoiceForm().fields['template'].choices, [('', 'Blank session')])

    def test_form_checks_descriptor_parser_contract_weight_cap_and_commit_false(self):
        form = RubricCategoryForm(data={
            'name': 'New criterion', 'description': '', 'scope': 'group', 'weight': '60',
            'level_descriptors': '50 | Supported'})
        self.assertTrue(form.is_valid(), form.errors)
        unsaved = form.save(commit=False)
        self.assertIsNone(unsaved.pk)
        self.assertEqual(unsaved.assessment_session_id, None)

        with patch('assessments.forms.parse_descriptors', return_value=(
                [{'score': Decimal('61'), 'description': 'Outside weight'}], [])):
            invalid = RubricCategoryForm(data={
                'name': 'Invalid criterion', 'description': '', 'scope': 'group', 'weight': '60',
                'level_descriptors': ''}, assessment_session=self.session)
            self.assertFalse(invalid.is_valid())
            self.assertIn('cannot exceed', str(invalid.errors['level_descriptors']))

        too_heavy = RubricCategoryForm(data={
            'name': 'Too heavy', 'description': '', 'scope': 'group', 'weight': '70',
            'level_descriptors': ''}, assessment_session=self.session)
        self.assertFalse(too_heavy.is_valid())
        self.assertIn('over 100%', str(too_heavy.errors['weight']))


class RubricModelCoverageTests(LecturerTestCase):
    def test_descriptor_models_validate_bounds_guidance_and_render_labels(self):
        descriptor = LevelDescriptor(rubric_category=self.quality, score=Decimal('61'), description='  ')
        with self.assertRaises(ValidationError) as caught:
            descriptor.clean()
        self.assertIn('score', caught.exception.message_dict)
        descriptor.score = Decimal('30')
        with self.assertRaises(ValidationError) as caught:
            descriptor.clean()
        self.assertIn('description', caught.exception.message_dict)
        descriptor.description = 'Supported'
        descriptor.clean()
        self.assertIn('Quality', str(descriptor))

        template = SessionTemplate.objects.create(name='Reusable template', organization_id=self.org.pk,
                                                  created_by=self.lecturer)
        self.assertEqual(str(template), 'Reusable template')
        category = TemplateRubricCategory.objects.create(
            template=template, name='Template criterion', weight=60)
        self.assertEqual(str(category), 'Template criterion')
        template_level = TemplateLevelDescriptor(rubric_category=category, score=61, description=' ')
        with self.assertRaises(ValidationError) as caught:
            template_level.clean()
        self.assertIn('score', caught.exception.message_dict)
        template_level.score = 30
        with self.assertRaises(ValidationError) as caught:
            template_level.clean()
        self.assertIn('description', caught.exception.message_dict)
        template_level.description = 'Supported'
        template_level.clean()

    def test_closed_turn_is_never_open_for_voting(self):
        turn = PresentationTurn(status=PresentationTurn.Status.CLOSED)
        self.assertFalse(turn.is_voting_open)


class TemplateValidationCoverageTests(LecturerTestCase):
    def row(self):
        return {'name': 'Evidence', 'description': '', 'scope': 'group', 'weight': Decimal('100'),
                'order': 0, 'level_descriptors': []}

    def test_template_descriptor_count_text_range_duplicate_and_length_limits(self):
        row = self.row()
        row['level_descriptors'] = [{'score': Decimal(i), 'description': 'ok'} for i in range(13)]
        with self.assertRaisesMessage(ValidationError, 'at most 12'):
            validate_categories([row])

        row = self.row()
        row['level_descriptors'] = [{'score': Decimal(i), 'description': 'x' * 500} for i in range(9)]
        with self.assertRaisesMessage(ValidationError, 'longer than 4000'):
            validate_categories([row])

        row = self.row()
        row['level_descriptors'] = [{'score': Decimal('1'), 'description': 'one'},
                                    {'score': Decimal('1'), 'description': 'two'}]
        with self.assertRaisesMessage(ValidationError, 'repeat a descriptor'):
            validate_categories([row])

        row = self.row()
        row['level_descriptors'] = [{'score': Decimal('1'), 'description': 'x' * 501}]
        with self.assertRaisesMessage(ValidationError, '1 to 500 characters'):
            validate_categories([row])


class SubmissionServiceCoverageTests(LecturerTestCase):
    def test_malformed_nested_submission_data_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'Invalid score data'):
            submission_digest({}, None)

    def test_unique_key_race_returns_the_matching_receipt(self):
        from assessments.models import Evaluation

        turn = self.active_turn(self.red)
        existing = Evaluation(presentation_turn=turn, evaluator=self.cat)
        with patch('assessments.services.Evaluation.objects.create', side_effect=IntegrityError), \
                patch('assessments.services._retry', side_effect=[None, existing]), \
                patch('assessments.services.submission_digest', return_value='digest'):
            result = submit_evaluation(turn, self.cat, {}, {}, key='f6dc4d2d-df32-4d51-9a66-49cf433debea')
        self.assertIs(result, existing)

    def test_duplicate_unique_key_race_reports_an_already_submitted_evaluation(self):
        from assessments.models import Evaluation

        turn = self.active_turn(self.red)
        with patch('assessments.services.Evaluation.objects.create', side_effect=IntegrityError) as create, \
                patch('assessments.services._retry', return_value=None), \
                patch('assessments.services.already_submitted', side_effect=[False, True]) as already_submitted, \
                patch('assessments.services.submission_digest', return_value='digest'):
            with self.assertRaisesMessage(ValidationError, 'already submitted'):
                submit_evaluation(turn, self.cat, {}, {}, key='f6dc4d2d-df32-4d51-9a66-49cf433debea')
        create.assert_called_once()
        self.assertEqual(already_submitted.call_count, 2)

    def test_unrelated_integrity_error_is_not_hidden_as_a_duplicate(self):
        turn = self.active_turn(self.red)
        with patch('assessments.services.Evaluation.objects.create', side_effect=IntegrityError), \
                patch('assessments.services._retry', return_value=None), \
                patch('assessments.services.already_submitted', return_value=False), \
                patch('assessments.services.submission_digest', return_value='digest'):
            with self.assertRaises(IntegrityError):
                submit_evaluation(turn, self.cat, {}, {}, key='f6dc4d2d-df32-4d51-9a66-49cf433debea')
