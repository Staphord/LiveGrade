"""Forms for the Co-Assessment service.

Enforces input validation, design system styling, and descriptive error messages.
"""

from decimal import Decimal
from django import forms
from .form_mixins import StyledFormMixin
from .models import (
    CoAssessmentSession,
    CoAssessmentCriterion,
    CoAssessmentGroup,
    CoAssessmentParticipant,
    CoAssessmentExaminer,
    CoAssessmentModeration,
)


def _one_line_area(words, placeholder=''):
    """A textarea that starts on one line and grows with the text up to `words` words."""
    return forms.Textarea(attrs={
        'rows': 1, 'data-autogrow': '', 'data-word-limit': str(words), 'placeholder': placeholder})


class CoAssessmentPolicyForm(StyledFormMixin, forms.ModelForm):
    """Step 1 Form: Configures event metadata, weights, and stage timers."""

    class Meta:
        model = CoAssessmentSession
        fields = (
            'name',
            'description',
            'panel_weight',
            'peer_weight',
            'moderation_threshold',
            'minimum_panel_evaluators',
            'minimum_peer_evaluators',
            'allow_anonymous_peer_comments',
        )
        widgets = {'description': _one_line_area(60, 'Optional: venue, room or notes for presenters')}
        labels = {
            'name': 'Event title',
            'description': 'Description or venue instructions',
            'panel_weight': 'Panel weight (%)',
            'peer_weight': 'Peer weight (%)',
            'moderation_threshold': 'Discrepancy threshold (points)',
            'minimum_panel_evaluators': 'Minimum panel examiners per group',
            'minimum_peer_evaluators': 'Minimum peer ratings per group',
            'allow_anonymous_peer_comments': 'Allow constructive peer comments',
        }
        help_texts = {
            'panel_weight': 'Examiner contribution to the final blended mark.',
            'peer_weight': 'Student peer contribution to the final blended mark.',
            'moderation_threshold': 'Trigger mandatory chair review if panel and peer marks diverge by more than this amount.',
        }

    def clean(self):
        cleaned = super().clean()
        panel_w = cleaned.get('panel_weight')
        peer_w = cleaned.get('peer_weight')
        if panel_w is not None and peer_w is not None:
            if panel_w + peer_w != Decimal('100.00'):
                self.add_error('peer_weight', 'Panel and Peer weights must total exactly 100%.')
        return cleaned


class CoAssessmentCriterionForm(StyledFormMixin, forms.ModelForm):
    """Form to add or edit a shared rubric criterion."""

    scope = forms.ChoiceField(
        choices=CoAssessmentCriterion.Scope.choices,
        initial=CoAssessmentCriterion.Scope.GROUP,
        required=False,
        label='Scoring scope',
        help_text='Group rates the whole team. Individual rates each student.',
    )

    class Meta:
        model = CoAssessmentCriterion
        fields = ('name', 'scope', 'weight', 'description')
        widgets = {'description': _one_line_area(40, 'Optional: what examiners and peers should look for')}
        labels = {
            'name': 'Criterion name',
            'weight': 'Criterion weight (%)',
            'description': 'Scoring guidance',
        }
        help_texts = {
            'weight': 'Share of the 100% total.',
        }

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session

    def clean_scope(self):
        return self.cleaned_data.get('scope') or CoAssessmentCriterion.Scope.GROUP

    def clean_name(self):
        name = ' '.join((self.cleaned_data.get('name') or '').split())
        if self.session is not None and self.session.criteria.filter(
                name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('A criterion with this name is already in the rubric.')
        return name


class CoAssessmentGroupForm(StyledFormMixin, forms.ModelForm):
    """Form to add or edit a scheduled presentation group."""

    class Meta:
        model = CoAssessmentGroup
        fields = ('name', 'topic')
        labels = {
            'name': 'Team / group name',
            'topic': 'Presentation topic / project title',
        }


class CoAssessmentParticipantForm(StyledFormMixin, forms.ModelForm):
    """Form to manually add a student participant to a group."""

    class Meta:
        model = CoAssessmentParticipant
        fields = ('full_name', 'student_id', 'email', 'group')
        labels = {
            'full_name': 'Full name',
            'student_id': 'Student ID',
            'email': 'Email address',
            'group': 'Assigned team (optional)',
        }


class CoAssessmentMemberForm(StyledFormMixin, forms.ModelForm):
    """Form to add a student to an existing presenting group."""

    class Meta:
        model = CoAssessmentParticipant
        fields = ('full_name', 'student_id', 'email')
        labels = {
            'full_name': 'Student name',
            'student_id': 'Student ID (optional)',
            'email': 'Email address (optional)',
        }

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session

    def clean_full_name(self):
        name = (self.cleaned_data.get('full_name') or '').strip()
        if not name:
            raise forms.ValidationError('Enter the student name.')
        return name

    def clean_student_id(self):
        student_id = (self.cleaned_data.get('student_id') or '').strip()
        if student_id and self.session is not None and self.session.participants.filter(student_id=student_id).exists():
            raise forms.ValidationError('A student with this ID is already on the roster.')
        return student_id


class CoAssessmentExaminerForm(StyledFormMixin, forms.ModelForm):
    """Form to invite a panel examiner."""

    class Meta:
        model = CoAssessmentExaminer
        fields = ('display_name', 'email', 'is_chair')
        labels = {
            'display_name': 'Examiner full name / title',
            'email': 'Examiner email address',
            'is_chair': 'Designate as panel chair',
        }
        help_texts = {
            'is_chair': 'Chairs can record formal moderation decisions and sign off the final results.',
        }


class CoAssessmentModerationForm(StyledFormMixin, forms.ModelForm):
    """Form for recording a chair moderation decision on flagged scores."""

    class Meta:
        model = CoAssessmentModeration
        fields = ('action', 'final_score', 'justification')
        labels = {
            'action': 'Moderation resolution',
            'final_score': 'Final official score (0-100)',
            'justification': 'Audited rationale for decision',
        }
        help_texts = {
            'justification': 'Required for academic records and audit trail.',
        }

    def clean_justification(self):
        text = self.cleaned_data.get('justification', '').strip()
        if not text:
            raise forms.ValidationError('A documented justification is required for chair moderation.')
        return text
