from decimal import Decimal
import os

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from .level_descriptors import MAX_DESCRIPTOR_TEXT, format_descriptors, parse_descriptors
from .models import (
    TOTAL_WEIGHT,
    AssessmentSession,
    LevelDescriptor,
    PanelExaminer,
    PanelVote,
    PresentationGroup,
    RubricCategory,
    Student,
)


def _fmt(decimal_value):
    """A Decimal weight/percent without the trailing '.00' its field's
    fixed decimal_places otherwise always renders (e.g. Decimal('100.00')
    -> '100', not '100.00') - matches the `floatformat:'-2'` filter these
    same numbers get in the templates."""
    text = format(decimal_value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class AssessmentSessionForm(forms.ModelForm):
    """A session's basics, assessment modes (Peer, Panel, Both), delivery format,
    and panel/moderation rules. Backward-compatible when legacy callers omit
    the new mode/panel fields."""

    enable_peer = forms.BooleanField(required=False, initial=True, label='Peer')
    enable_panel = forms.BooleanField(required=False, initial=False, label='Panel')
    delivery_mode = forms.ChoiceField(
        required=False,
        choices=AssessmentSession.DeliveryMode.choices,
        initial=AssessmentSession.DeliveryMode.LIVE_STAGE,
        label='Delivery',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    entry_mode = forms.ChoiceField(
        required=False,
        choices=AssessmentSession.EntryMode.choices,
        initial=AssessmentSession.EntryMode.GROUP,
        label='Entries',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    timezone_name = forms.CharField(
        required=False,
        initial='Africa/Dar_es_Salaam',
        label='Session timezone',
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    peer_weight = forms.DecimalField(
        required=False,
        initial=Decimal('100.00'),
        min_value=Decimal('0'),
        max_value=Decimal('100'),
        label='Peer (%)',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
    )
    panel_weight = forms.DecimalField(
        required=False,
        initial=Decimal('0.00'),
        min_value=Decimal('0'),
        max_value=Decimal('100'),
        label='Panel (%)',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
    )
    minimum_peer_evaluators = forms.IntegerField(
        required=False,
        initial=1,
        min_value=1,
        max_value=500,
        label='Min peer reviews',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'max': '500'}),
    )
    minimum_panel_evaluators = forms.IntegerField(
        required=False,
        initial=1,
        min_value=1,
        max_value=50,
        label='Min panel reviews',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'max': '50'}),
    )
    moderation_threshold = forms.DecimalField(
        required=False,
        initial=Decimal('15.00'),
        min_value=Decimal('0'),
        max_value=Decimal('100'),
        label='Discrepancy threshold',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
    )
    panel_aggregation = forms.ChoiceField(
        required=False,
        choices=AssessmentSession.PanelAggregation.choices,
        initial=AssessmentSession.PanelAggregation.MEAN,
        label='Score aggregation',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = AssessmentSession
        fields = [
            'name', 'identify_by', 'ungraded_penalty', 'instructions',
            'enable_peer', 'enable_panel', 'delivery_mode', 'entry_mode', 'timezone_name',
            'peer_weight', 'panel_weight', 'minimum_peer_evaluators', 'minimum_panel_evaluators',
            'moderation_threshold', 'avoid_reciprocal_pairs', 'panel_aggregation',
            'panel_trim_fraction', 'panel_enable_rubric', 'panel_enable_vote',
            'panel_enable_written_review', 'panel_enable_private_notes', 'require_chair_approval',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Sprint Demo - Q1 2026'}),
            'identify_by': forms.Select(attrs={'class': 'form-select'}),
            'instructions': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'ungraded_penalty': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
            'panel_trim_fraction': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.001', 'min': '0', 'max': '0.499'}),
        }
        labels = {
            'ungraded_penalty': 'Missed-review penalty',
            'instructions': 'Instructions (optional)',
            'identify_by': 'Participant join field',
            'panel_enable_rubric': 'Include rubric scoring',
            'panel_enable_vote': 'Panel vote (Pass / Revise / Fail)',
            'panel_enable_written_review': 'Written panel reviews',
            'panel_enable_private_notes': 'Private panel notes',
            'require_chair_approval': 'Require chair sign-off',
        }

    def clean(self):
        cleaned = super().clean()
        # Preserve backward compatibility when legacy callers omit mode fields entirely.
        mode_keys_present = 'enable_peer' in self.data or 'enable_panel' in self.data
        if not mode_keys_present and not self.instance.pk:
            cleaned['enable_peer'] = True
            cleaned['enable_panel'] = False
        enable_peer = bool(cleaned.get('enable_peer'))
        enable_panel = bool(cleaned.get('enable_panel'))
        if not enable_peer and not enable_panel:
            raise forms.ValidationError('Select at least one assessment mode: Peer assessment or Panel grading.')

        cleaned['delivery_mode'] = cleaned.get('delivery_mode') or AssessmentSession.DeliveryMode.LIVE_STAGE
        cleaned['entry_mode'] = cleaned.get('entry_mode') or AssessmentSession.EntryMode.GROUP
        cleaned['timezone_name'] = (cleaned.get('timezone_name') or 'Africa/Dar_es_Salaam').strip()
        cleaned['minimum_peer_evaluators'] = cleaned.get('minimum_peer_evaluators') or 1
        cleaned['minimum_panel_evaluators'] = cleaned.get('minimum_panel_evaluators') or 1
        cleaned['moderation_threshold'] = (
            cleaned.get('moderation_threshold')
            if cleaned.get('moderation_threshold') is not None
            else Decimal('15.00')
        )
        cleaned['panel_aggregation'] = cleaned.get('panel_aggregation') or AssessmentSession.PanelAggregation.MEAN
        if 'panel_enable_rubric' not in self.data:
            cleaned['panel_enable_rubric'] = True
        if 'panel_enable_private_notes' not in self.data:
            cleaned['panel_enable_private_notes'] = True

        if enable_peer and not enable_panel:
            cleaned['peer_weight'] = Decimal('100.00')
            cleaned['panel_weight'] = Decimal('0.00')
        elif enable_panel and not enable_peer:
            cleaned['panel_weight'] = Decimal('100.00')
            cleaned['peer_weight'] = Decimal('0.00')
        else:
            pw = cleaned.get('peer_weight')
            pan_w = cleaned.get('panel_weight')
            if pw is None or pan_w is None:
                cleaned['peer_weight'] = Decimal('30.00')
                cleaned['panel_weight'] = Decimal('70.00')
            elif pw + pan_w != Decimal('100.00'):
                self.add_error('peer_weight', 'Panel and Peer weights must total exactly 100%.')

        if cleaned.get('panel_aggregation') == AssessmentSession.PanelAggregation.TRIMMED_MEAN:
            if cleaned.get('panel_trim_fraction') is None:
                self.add_error('panel_trim_fraction', 'Trimmed mean requires an explicit trim fraction.')
        return cleaned


class PanelExaminerForm(forms.ModelForm):
    """Invite a panel examiner to an AssessmentSession."""

    class Meta:
        model = PanelExaminer
        fields = ['display_name', 'email', 'is_chair']
        widgets = {
            'display_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'is_chair': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'display_name': 'Examiner full name / title',
            'email': 'Examiner email address',
            'is_chair': 'Designate as panel chair',
        }


class AsyncSubmissionForm(forms.Form):
    """Form for submitting entry work in asynchronous delivery mode."""

    title = forms.CharField(
        max_length=200,
        label='Submission title',
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    content = forms.CharField(
        required=False,
        max_length=20000,
        label='Submission content',
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
    )
    external_url = forms.URLField(
        required=False,
        max_length=500,
        label='External link (optional)',
        widget=forms.URLInput(attrs={'class': 'form-control'}),
    )

    def clean(self):
        cleaned = super().clean()
        content = (cleaned.get('content') or '').strip()
        external_url = (cleaned.get('external_url') or '').strip()
        if not content and not external_url:
            raise forms.ValidationError('Provide either submission content or an external link.')
        return cleaned


class PanelVoteForm(forms.ModelForm):
    """Composable panel vote and written review form."""

    class Meta:
        model = PanelVote
        fields = ['choice', 'written_review', 'private_note']
        widgets = {
            'choice': forms.Select(attrs={'class': 'form-select'}),
            'written_review': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'private_note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {
            'choice': 'Panel recommendation',
            'written_review': 'Written review',
            'private_note': 'Private panel note (never shown to participants)',
        }


class SaveSessionTemplateForm(forms.Form):
    name = forms.CharField(max_length=150, label='Template name',
                           widget=forms.TextInput(attrs={
                               'class': 'form-control',
                               'aria-describedby': 'id_name_inline_message',
                           }))
    description = forms.CharField(max_length=500, required=False, label='Description',
        widget=forms.Textarea(attrs={
            'class': 'form-control', 'rows': 3,
            'aria-describedby': 'id_description_inline_message',
        }))


class TemplateChoiceForm(forms.Form):
    template = forms.ChoiceField(required=False, label='Start from a template',
                                widget=forms.Select(attrs={'class': 'form-select'}))

    def __init__(self, *args, saved_templates=(), starters=(), **kwargs):
        super().__init__(*args, **kwargs)
        choices = [('', 'Blank session')]
        if starters:
            choices.append(('Assessment starters', [('starter:' + item.key, item.name) for item in starters]))
        if saved_templates:
            choices.append(('Your saved templates', [('saved:' + str(item.pk), item.name) for item in saved_templates]))
        self.fields['template'].choices = choices


class DuplicateSessionForm(forms.Form):
    name = forms.CharField(max_length=150, label='New session name',
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'aria-describedby': 'id_name_inline_message',
        }))
    include_roster = forms.BooleanField(required=False, label='Copy the student roster',
        help_text='Copies names, IDs, emails and programmes.',
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input',
            'aria-describedby': 'id_include_roster_helptext id_include_roster_inline_message',
        }))
    include_groups = forms.BooleanField(required=False, label='Copy the presentation groups',
        help_text='Group members copy only with the roster.',
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input',
            'aria-describedby': 'id_include_groups_helptext id_include_groups_inline_message',
        }))


class RubricCategoryForm(forms.ModelForm):
    """Takes the session explicitly (not inferred from `instance`) so the
    same form works for both create and edit, and checks the new weight
    against the *other* categories already in the session - group and
    individual together - so a category can never be saved if it would push
    the rubric's total over 100%, rather than being allowed to save and just
    showing a red "200% of 100%" after the fact."""

    level_descriptors = forms.CharField(required=False, max_length=MAX_DESCRIPTOR_TEXT,
        label='Score level guidance',
        help_text='Optional. Add one point anchor per line as “point value | guidance”, from 0 to this category’s weight.',
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 5,
            'placeholder': '0 | Not demonstrated\n15 | Partly demonstrated\n30 | Fully demonstrated'}))

    class Meta:
        model = RubricCategory
        # 'order' deliberately left off: it's an internal sort key with a
        # model default, not something the add-category form exposes. A
        # ModelForm field the template never renders is a required field the
        # browser can never satisfy — every submission would silently fail
        # validation. (This exact bug shipped once already; see
        # PresentationGroupForm below and the regression tests for both.)
        fields = ['name', 'description', 'scope', 'weight']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional guidance shown to students'}),
            'scope': forms.Select(attrs={'class': 'form-select'}),
            'weight': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'min': '0.01', 'max': '100', 'placeholder': 'e.g. 30'}),
        }

    def __init__(self, *args, assessment_session=None, **kwargs):
        self.assessment_session = assessment_session
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['level_descriptors'] = format_descriptors(
                self.instance.level_descriptors.all())
        if not self.instance.pk:
            # The model's 0.00 default is not a value the lecturer chose: leave the box
            # empty so the placeholder shows and they can just start typing.
            self.initial['weight'] = None

    def clean_weight(self):
        weight = self.cleaned_data['weight']
        if weight <= 0 or weight > TOTAL_WEIGHT:
            raise forms.ValidationError(f'Weight must be more than 0 and at most {_fmt(TOTAL_WEIGHT)}.')
        return weight

    def clean(self):
        cleaned = super().clean()
        weight = cleaned.get('weight')
        if self.instance.pk and 'level_descriptors' not in self.data:
            descriptors = [
                {'score': level.score, 'description': level.description}
                for level in self.instance.level_descriptors.all()]
            errors = []
        else:
            descriptors, errors = parse_descriptors(
                cleaned.get('level_descriptors', ''), weight if weight is not None else TOTAL_WEIGHT)
        self._level_descriptors = descriptors
        for error in errors:
            self.add_error('level_descriptors', error)
        if weight is not None and any(level['score'] > weight for level in descriptors):
            self.add_error('level_descriptors',
                           'A descriptor point value cannot exceed the criterion weight.')
        if self.assessment_session is None or weight is None:
            return cleaned
        others = self.assessment_session.rubric_categories.all()
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        existing_total = sum((c.weight for c in others), start=type(weight)(0))
        new_total = existing_total + weight
        if new_total > TOTAL_WEIGHT:
            room_left = TOTAL_WEIGHT - existing_total
            # On the weight field itself, so the page can show it right under the box
            # that needs fixing.
            self.add_error('weight',
                           f'Weights would total {_fmt(new_total)}%, over {_fmt(TOTAL_WEIGHT)}%. '
                           f'At most {_fmt(room_left)}% is available.')
        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        category = super().save(commit=False)
        if not commit:
            return category
        if not category.assessment_session_id and self.assessment_session is not None:
            category.assessment_session = self.assessment_session
        category.save()
        LevelDescriptor.objects.filter(rubric_category=category).delete()
        LevelDescriptor.objects.bulk_create([
            LevelDescriptor(rubric_category=category, **row)
            for row in getattr(self, '_level_descriptors', [])])
        return category


class StudentForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = ['full_name', 'student_id', 'email', 'programme']
        widgets = {
            'full_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Jane Doe'}),
            'student_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. SCT221-100/2022'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'jane@students.example.ac.ke'}),
            'programme': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. BSc. Computer Science'}),
        }


#: A roster or a list of groups is a few hundred rows; this is far beyond that
#: and still small enough that opening it cannot tie up a worker.
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def validate_workbook(upload):
    """Refuse anything that is not a modest .xlsx before it is opened."""
    if os.path.splitext(upload.name)[1].lower() != '.xlsx':
        raise ValidationError('Please choose an Excel (.xlsx) file.')
    if upload.size > MAX_IMPORT_BYTES:
        raise ValidationError(
            f'That file is larger than {MAX_IMPORT_BYTES // 1024 // 1024} MB.')


class GroupImportForm(forms.Form):
    file = forms.FileField(
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='One row per group: Group Name, then Name (members separated by commas). '
                  'Student ID, Topic and Presentation Location are optional.')


class RubricImportForm(forms.Form):
    MODES = [('replace', 'Replace the current rubric'), ('add', 'Add to the current rubric')]

    file = forms.FileField(
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='Columns: Category, Description, Scope, Weight %. Weights must add up to 100.')
    mode = forms.ChoiceField(choices=MODES, initial='replace', widget=forms.RadioSelect)


class PollQuestionImportForm(forms.Form):
    file = forms.FileField(
        required=False,
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='Use one row per question with Question, Question Type and Answer Options columns.')

    def clean_file(self):
        uploaded_file = self.cleaned_data.get('file')
        if uploaded_file is None:
            raise ValidationError('Please select a file.')
        return uploaded_file


class PresentationGroupForm(forms.ModelForm):
    class Meta:
        model = PresentationGroup
        fields = ['name', 'max_size', 'topic', 'location', 'is_practice']  # see the comment on RubricCategoryForm re: 'order'
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Group 1'}),
            'max_size': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 2', 'min': '1'}),
            'topic': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Smart campus map'}),
            'is_practice': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'location': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Room 1340'}),
        }
        labels = {'max_size': 'Group size', 'location': 'Presentation location', 'is_practice': 'Practice presentation'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model field stays nullable (existing groups created before
        # this requirement keep working), but the form requires it going
        # forward — a known size is what makes the capacity bar/chip on
        # each group card meaningful.
        self.fields['max_size'].required = True

    def clean_max_size(self):
        max_size = self.cleaned_data.get('max_size')
        if max_size is not None and self.instance.pk:
            current = self.instance.member_count()
            if max_size < current:
                raise forms.ValidationError(
                    f'This group already has {current} member(s) - the size '
                    f'limit can\'t be set below that.')
        return max_size


class JoinForm(forms.Form):
    identifier = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg', 'autofocus': True,
            'autocomplete': 'off', 'inputmode': 'text',
        }))
