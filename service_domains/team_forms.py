from django import forms

from assessments.models import AssessmentSession, SessionTemplate
from assessments.forms import validate_workbook
from assessments.session_templates import StarterTemplate, starters_for

from .form_mixins import StyledFormMixin
from .models import TrainingCompetency, TrainingParticipant, TrainingSeries


class TrainingSeriesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainingSeries
        fields = ('name', 'description', 'cadence_note', 'feedback_minimum_responses')
        help_texts = {
            'cadence_note': 'Optional planning reminder; this does not send calendar invitations.',
            'feedback_minimum_responses': (
                'Optional. Choose the minimum anonymous responses required before a presenter can see feedback.'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].label = 'Purpose or intended skill goal'
        self.fields['description'].help_text = (
            'What performance need should this series help address, and what should learners be able to do?')


class TrainingSeriesDetailsForm(StyledFormMixin, forms.ModelForm):
    PURPOSE_WORD_LIMIT = 100

    class Meta:
        model = TrainingSeries
        fields = ('name', 'description', 'cadence_note')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].label = 'Purpose or intended skill goal'
        self.fields['description'].help_text = (
            'What performance need should this series help address, and what should learners be able to do? '
            'Maximum 100 words.')
        self.fields['description'].widget.attrs.update({
            'rows': 1,
            'data-purpose-autogrow': '',
            'data-word-limit': str(self.PURPOSE_WORD_LIMIT),
        })

    def clean_description(self):
        description = self.cleaned_data.get('description', '')
        if len(description.split()) > self.PURPOSE_WORD_LIMIT:
            raise forms.ValidationError('Use 100 words or fewer.')
        return description

class TrainingParticipantForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainingParticipant
        fields = ('display_name', 'reference', 'group_name')
        help_texts = {
            'reference': 'Optional unless the selected session template identifies learners by ID.',
        }


class TrainingRosterImportForm(StyledFormMixin, forms.Form):
    file = forms.FileField(
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='One learner per row. Required columns: Name and Team. Student ID is optional.')


class TrainingCompetencyForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainingCompetency
        fields = ('name', 'description', 'passing_score')


class CreateTrainingSessionForm(StyledFormMixin, forms.Form):
    name = forms.CharField(max_length=150, label='Demo session name')
    scheduled_for = forms.DateTimeField(required=False, label='Planned date and time',
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        help_text='Optional. Shown in this series so the next demo is easy to track.')
    template = forms.ChoiceField(label='Starting rubric')
    identify_by = forms.ChoiceField(label='How learners join', required=False,
        choices=(('', 'Use the rubric setting (usually full name)'),
                 (AssessmentSession.IdentifyBy.FULL_NAME, 'Full name'),
                 (AssessmentSession.IdentifyBy.STUDENT_ID, 'Student ID')),
        help_text='Use Student ID when names repeat or learners need an unambiguous private join.')

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        templates = (
            SessionTemplate.objects.for_organization(organization).for_service('team')
            .owned_by(user).order_by('name', 'pk'))
        self._template_objects = {}
        starter_choices = []
        for starter in starters_for('team'):
            value = f'starter:{starter.key}'
            self._template_objects[value] = starter
            starter_choices.append((value, starter.name))
        saved_choices = []
        for template in templates:
            value = f'saved:{template.pk}'
            self._template_objects[value] = template
            saved_choices.append((value, template.name))
        self.fields['template'].choices = [('', 'Choose a starting point')] + [
            ('Built-in Team rubrics', starter_choices),
            ('Your saved Team rubrics', saved_choices),
        ]

    def clean_template(self):
        choice = self.cleaned_data['template']
        template = self._template_objects.get(choice)
        if template is None:
            raise forms.ValidationError('Choose a Team and Training rubric template.')
        if isinstance(template, SessionTemplate) and template.service != 'team':
            raise forms.ValidationError('Choose a Team and Training rubric template.')
        return template


class TrainingCompetencyResultForm(StyledFormMixin, forms.Form):
    score = forms.DecimalField(min_value=0, max_value=100, max_digits=5, decimal_places=2,
        label='Competency score')
    evidence = forms.CharField(max_length=1000, label='Evidence for this decision',
        widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Record specific, work-related evidence. Avoid sensitive personal information.')
    correction_reason = forms.CharField(required=False, max_length=500,
        label='Reason for correcting the previous decision',
        widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Required when correcting an earlier decision.')


class TrainingFeedbackForm(StyledFormMixin, forms.Form):
    score = forms.ChoiceField(
        choices=tuple((str(value), f'{value} out of 5') for value in range(1, 6)),
        widget=forms.RadioSelect, label='How useful was this demo?')


class TrainingFeedbackPrivacyForm(StyledFormMixin, forms.Form):
    minimum_responses = forms.IntegerField(required=False, min_value=2, max_value=50,
        label='Minimum anonymous responses before showing feedback',
        help_text='Leave blank to disable feedback collection for this series.')
