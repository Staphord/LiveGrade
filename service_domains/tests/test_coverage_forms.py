"""Validation rules of forms that the page-level tests never reach."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from accounts.testing import make_org, make_user
from service_domains.coassessment_forms import (
    CoAssessmentMemberForm, CoAssessmentModerationForm, CoAssessmentPolicyForm)
from service_domains.competition_forms import (
    CompetitionCriterionForm, CompetitionEntryForm, CompetitionJudgeInvitationForm,
    CompetitionJudgeScorecardForm)
from service_domains.forms import (
    CompetitionAudienceBallotForm, PanelRubricImportForm, PanelScoringPanelForm)
from service_domains.models import (
    PanelHearing, PanelScoringPanel)
from assessments.models import SessionTemplate
from service_domains.team_forms import CreateTrainingSessionForm, TrainingSeriesForm


def cleaned(form, **values):
    form.cleaned_data = values
    return form


class CoAssessmentFormTests(SimpleTestCase):
    def test_policy_weights_are_only_checked_when_both_are_given(self):
        form = cleaned(CoAssessmentPolicyForm(), panel_weight=None, peer_weight=Decimal('30'))
        self.assertEqual(form.clean()['peer_weight'], Decimal('30'))
        self.assertFalse(form.errors)

    def test_member_name_must_not_be_blank(self):
        form = cleaned(CoAssessmentMemberForm(), full_name='   ')
        with self.assertRaises(forms.ValidationError):
            form.clean_full_name()
        self.assertEqual(cleaned(CoAssessmentMemberForm(), full_name=' Ada ').clean_full_name(), 'Ada')

    def test_moderation_needs_a_justification(self):
        with self.assertRaises(forms.ValidationError):
            cleaned(CoAssessmentModerationForm(), justification='  ').clean_justification()
        self.assertEqual(cleaned(CoAssessmentModerationForm(), justification=' ok ').clean_justification(), 'ok')


class CompetitionOrderDefaultTests(TestCase):
    def build(self, form_class, model_name, **extra):
        org = make_org(f'Order org {model_name}')
        user = make_user(f'order-user-{model_name}')
        competition = SimpleNamespace(organization_id=org.pk)
        with patch('service_domains.competition_forms.CompetitionTrack'):
            form = form_class(organization=org, user=user, competition=competition, **extra)
        return form

    def test_next_order_is_filled_in_for_criteria_and_entries(self):
        for form_class, model_name in ((CompetitionCriterionForm, 'CompetitionCriterion'),
                                       (CompetitionEntryForm, 'CompetitionEntry')):
            form = self.build(form_class, model_name)
            track = SimpleNamespace(organization_id=None)
            with patch(f'service_domains.competition_forms.{model_name}') as model:
                model.objects.filter.return_value.count.return_value = 2
                result = cleaned(form, track=track, order=None).clean()
            self.assertEqual(result['order'], 3)
            self.assertEqual(form.instance.order, 3)

    def test_an_explicit_order_is_kept(self):
        form = self.build(CompetitionEntryForm, 'CompetitionEntry')
        self.assertEqual(cleaned(form, track=None, order=5).clean()['order'], 5)

    def test_invitation_expiry_keeps_a_given_initial_value(self):
        org, user = make_org('Invite org'), make_user('invite-user')
        with patch('service_domains.competition_forms.CompetitionTrack'):
            form = CompetitionJudgeInvitationForm(
                organization=org, user=user, competition=SimpleNamespace(),
                initial={'expires_at': '2030-01-01T10:00'})
        self.assertEqual(form.initial['expires_at'], '2030-01-01T10:00')


class JudgeScorecardTests(SimpleTestCase):
    def entries(self):
        return [SimpleNamespace(pk=1, entry=SimpleNamespace(name='Alpha')),
                SimpleNamespace(pk=2, entry=SimpleNamespace(name='Beta'))]

    def criteria(self):
        return [SimpleNamespace(pk=7, name='Clarity', weight=60), SimpleNamespace(pk=8, name='Depth', weight=40)]

    def test_not_scoring_needs_a_reason_and_no_scores(self):
        form = CompetitionJudgeScorecardForm(
            {'decision_1': 'conflict', 'entry_1': '50', 'decision_2': 'abstain', 'reason_2': 'ill'},
            entries=self.entries())
        self.assertFalse(form.is_valid())
        self.assertIn('reason_1', form.errors)
        self.assertIn('decision_1', form.errors)
        self.assertNotIn('decision_2', form.errors)

    def test_scoring_needs_every_score(self):
        form = CompetitionJudgeScorecardForm({'entry_1': '80'}, entries=self.entries())
        self.assertFalse(form.is_valid())
        self.assertIn('entry_2', form.errors)

    def test_payload_shapes(self):
        form = CompetitionJudgeScorecardForm(
            {'entry_1': '80', 'decision_2': 'conflict', 'reason_2': 'friend'}, entries=self.entries())
        self.assertEqual(form.score_payload(), {'1': Decimal('80'), '2': {'abstention': 'conflict', 'reason': 'friend'}})
        weighted = CompetitionJudgeScorecardForm(
            {'entry_1_criterion_7': '70', 'entry_1_criterion_8': '60',
             'entry_2_criterion_7': '1', 'entry_2_criterion_8': '2'},
            entries=self.entries(), criteria=self.criteria())
        self.assertEqual(weighted.score_payload()['1'], {'7': Decimal('70'), '8': Decimal('60')})

    def test_payload_needs_a_valid_form(self):
        form = CompetitionJudgeScorecardForm({}, entries=self.entries())
        with self.assertRaises(ValueError):
            form.score_payload()


class AudienceBallotTests(SimpleTestCase):
    def ballot(self, require_complete, data):
        rows = [SimpleNamespace(entry_id=1, entry=SimpleNamespace(name='A')),
                SimpleNamespace(entry_id=2, entry=SimpleNamespace(name='B'))]
        with patch('service_domains.forms.CompetitionRoundEntry') as model:
            model.objects.filter.return_value.select_related.return_value.order_by.return_value = rows
            return CompetitionAudienceBallotForm(
                data, round_obj=1, track=1, require_complete=require_complete)

    def test_weighted_events_need_every_entry_ranked(self):
        form = cleaned(self.ballot(True, None), rank_1=1, rank_2=None)
        with self.assertRaises(forms.ValidationError):
            form.clean()

    def test_rank_order_is_returned(self):
        form = cleaned(self.ballot(False, None), rank_1=2, rank_2=1)
        self.assertEqual(form.clean()['ranking'], [2, 1])


class PanelFormTests(TestCase):
    def test_legacy_trimmed_rule_stays_selectable_and_needs_a_fraction(self):
        legacy = PanelScoringPanel(pk=1, aggregation=PanelHearing.Aggregation.TRIMMED_MEAN)
        form = PanelScoringPanelForm(instance=legacy)
        self.assertIn(PanelHearing.Aggregation.TRIMMED_MEAN, [c[0] for c in form.fields['aggregation'].choices])
        form.cleaned_data = {'aggregation': PanelHearing.Aggregation.TRIMMED_MEAN}
        form.clean()
        self.assertIn('aggregation', form.errors)

    def test_rubric_upload_must_be_a_small_xlsx(self):
        big = SimpleUploadedFile('r.xlsx', b'x')
        big.size = 11 * 1024 * 1024
        with self.assertRaises(forms.ValidationError):
            cleaned(PanelRubricImportForm(), file=big).clean_file()
        with self.assertRaises(forms.ValidationError):
            cleaned(PanelRubricImportForm(), file=SimpleUploadedFile('r.csv', b'x')).clean_file()
        good = SimpleUploadedFile('r.XLSX', b'x')
        self.assertIs(cleaned(PanelRubricImportForm(), file=good).clean_file(), good)


class TeamFormTests(TestCase):
    def test_series_form_relabels_the_description(self):
        form = TrainingSeriesForm()
        self.assertEqual(form.fields['description'].label, 'Purpose or intended skill goal')

    def test_unknown_or_foreign_rubric_templates_are_refused(self):
        form = CreateTrainingSessionForm.__new__(CreateTrainingSessionForm)
        foreign = SessionTemplate(name='x', service='classroom')
        form._template_objects = {'known': SessionTemplate(name='y', service='team'), 'foreign': foreign}
        for choice in ('missing', 'foreign'):
            form.cleaned_data = {'template': choice}
            with self.assertRaises(forms.ValidationError):
                form.clean_template()
        form.cleaned_data = {'template': 'known'}
        self.assertEqual(form.clean_template().name, 'y')
