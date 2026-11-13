from django.test import TestCase

from accounts.testing import make_org, make_user
from service_domains.forms import (
    ShowcaseJudgeAssignmentsForm, ShowcaseJudgeCriterionForm,
    ShowcaseJudgeScorecardForm,
)
from service_domains.models import (
    ShowcaseEvent, ShowcaseExhibit, ShowcaseJudgeAssignment,
    ShowcaseJudgeCriterion,
)


class ShowcaseJudgeFormTests(TestCase):
    def setUp(self):
        self.organization = make_org('Showcase forms organization')
        self.owner = make_user('showcase-forms-owner')
        self.event = ShowcaseEvent.objects.create(
            organization_id=self.organization.id, created_by=self.owner,
            name='Community showcase', minimum_judges_per_exhibit=2)
        self.exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.organization.id, created_by=self.owner,
            event=self.event, name='Solar prototype')

    def test_owner_assignments_form_loads_current_state_and_returns_clean_labels(self):
        ShowcaseJudgeAssignment.objects.create(
            organization_id=self.organization.id, created_by=self.owner,
            event=self.event, exhibit=self.exhibit, judge_label='Judge One')
        unbound = ShowcaseJudgeAssignmentsForm(event=self.event)
        self.assertEqual(unbound.initial['minimum_judges'], 2)
        self.assertEqual(unbound.fields[f'exhibit_{self.exhibit.pk}'].initial, 'Judge One')

        bound = ShowcaseJudgeAssignmentsForm({
            'minimum_judges': '2', f'exhibit_{self.exhibit.pk}': ' Judge One \n Judge Two  \n',
        }, event=self.event)
        self.assertTrue(bound.is_valid())
        self.assertEqual(bound.assignment_map(), {self.exhibit.pk: ['Judge One', 'Judge Two']})

    def test_assignment_map_refuses_invalid_form(self):
        form = ShowcaseJudgeAssignmentsForm({'minimum_judges': 'bad'}, event=self.event)
        with self.assertRaisesMessage(ValueError, 'valid form'):
            form.assignment_map()

    def test_criterion_form_normalizes_checklist_and_only_keeps_items_for_checklist_scale(self):
        checklist = ShowcaseJudgeCriterionForm(data={
            'name': 'Evidence', 'description': '', 'weight': '100',
            'scale': ShowcaseJudgeCriterion.Scale.CHECKLIST, 'max_points': '100',
            'checklist_items_text': '  Working demo  \n\nClear explanation\n',
        })
        self.assertTrue(checklist.is_valid(), checklist.errors)
        self.assertEqual(checklist.cleaned_data['checklist_items_text'],
                         ['Working demo', 'Clear explanation'])
        self.assertEqual(checklist.criterion_data()['checklist_items'],
                         ['Working demo', 'Clear explanation'])

        points = ShowcaseJudgeCriterionForm(data={
            'name': 'Points', 'description': '', 'weight': '100',
            'scale': ShowcaseJudgeCriterion.Scale.POINTS, 'max_points': '100',
            'checklist_items_text': 'ignored on this scale',
        })
        self.assertTrue(points.is_valid(), points.errors)
        self.assertEqual(points.criterion_data()['checklist_items'], [])

    def test_criterion_form_rejects_too_many_or_overlong_checklist_items(self):
        for text in ('\n'.join(f'Item {index}' for index in range(21)), 'x' * 241):
            form = ShowcaseJudgeCriterionForm(data={
                'name': 'Checklist', 'description': '', 'weight': '100',
                'scale': ShowcaseJudgeCriterion.Scale.CHECKLIST, 'max_points': '100',
                'checklist_items_text': text,
            })
            self.assertFalse(form.is_valid())
            self.assertIn('checklist_items_text', form.errors)

    def test_scorecard_builds_fields_and_payload_for_each_scale(self):
        criteria = [
            ShowcaseJudgeCriterion(pk=1, name='Points', scale='points', max_points=100,
                                   description='Points description'),
            ShowcaseJudgeCriterion(pk=2, name='Stars', scale='stars', max_points=5),
            ShowcaseJudgeCriterion(pk=3, name='Outcome', scale='pass_fail', max_points=1),
            ShowcaseJudgeCriterion(pk=4, name='Checklist', scale='checklist', max_points=1,
                                   checklist_items=['Works', 'Explained']),
            ShowcaseJudgeCriterion(pk=5, name='Final points', scale='points', max_points=10),
        ]
        form = ShowcaseJudgeScorecardForm(data={
            'criterion_1': '82.5', 'criterion_2': '4', 'criterion_3': 'pass',
            'criterion_4': ['0'], 'criterion_5': '9',
        }, criteria=criteria)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.score_payload(), {
            '1': form.cleaned_data['criterion_1'], '2': '4', '3': 'pass', '4': ['0'],
            '5': form.cleaned_data['criterion_5'],
        })

    def test_scorecard_payload_requires_valid_form(self):
        criterion = ShowcaseJudgeCriterion(pk=5, name='Evidence', scale='points',
            max_points=100)
        form = ShowcaseJudgeScorecardForm(data={}, criteria=[criterion])
        with self.assertRaisesMessage(ValueError, 'valid scorecard'):
            form.score_payload()

    def test_scorecard_rejects_unknown_criterion_scale(self):
        criterion = ShowcaseJudgeCriterion(pk=6, name='Unknown', scale='unknown',
            max_points=10)
        with self.assertRaisesMessage(ValueError, 'Unsupported Showcase judge scale'):
            ShowcaseJudgeScorecardForm(criteria=[criterion])
