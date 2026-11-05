import os

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from .level_descriptors import MAX_DESCRIPTOR_TEXT, format_descriptors, parse_descriptors
from .models import TOTAL_WEIGHT, AssessmentSession, LevelDescriptor, PresentationGroup, RubricCategory, Student


def _fmt(decimal_value):
    """A Decimal weight/percent without the trailing '.00' its field's
    fixed decimal_places otherwise always renders (e.g. Decimal('100.00')
    -> '100', not '100.00') - matches the `floatformat:'-2'` filter these
    same numbers get in the templates."""
    text = format(decimal_value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class AssessmentSessionForm(forms.ModelForm):
    """A session's basics: name, how students join, and the penalty for every other
    group a student does not grade. The same form creates a session and edits it,
    draft or live."""

    class Meta:
        model = AssessmentSession
        fields = ['name', 'identify_by', 'ungraded_penalty', 'instructions']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Sprint Demo - Q1 2026'}),
            'identify_by': forms.Select(attrs={'class': 'form-select'}),
            'instructions': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'ungraded_penalty': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
        }
        labels = {'ungraded_penalty': 'Penalty for each group not graded', 'instructions': 'Evaluator notice'}


class SaveSessionTemplateForm(forms.Form):
    name = forms.CharField(max_length=150, label='Template name',
                           widget=forms.TextInput(attrs={'class': 'form-control'}))
    description = forms.CharField(max_length=500, required=False, label='Description',
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}))


class TemplateChoiceForm(forms.Form):
    template = forms.ChoiceField(required=False, label='Start from a template',
                                widget=forms.Select(attrs={'class': 'form-select'}))

    def __init__(self, *args, saved_templates=(), starters=(), **kwargs):
        super().__init__(*args, **kwargs)
        choices = [('', 'Blank session')]
        if starters:
            choices.append(('Classroom starters', [('starter:' + item.key, item.name) for item in starters]))
        if saved_templates:
            choices.append(('Your saved templates', [('saved:' + str(item.pk), item.name) for item in saved_templates]))
        self.fields['template'].choices = choices


class DuplicateSessionForm(forms.Form):
    name = forms.CharField(max_length=150, label='New session name',
                           widget=forms.TextInput(attrs={'class': 'form-control'}))
    include_roster = forms.BooleanField(required=False, label='Copy the student roster',
        help_text='Includes names, identifiers, email addresses and programmes.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    include_groups = forms.BooleanField(required=False, label='Copy the presentation groups',
        help_text='Group membership is copied only when the student roster is also selected.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))


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
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='Use one row per answer option. Include Question, Question Type and Answer Option columns.')


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
            'autocomplete': 'off', 'inputmode': 'text', 'aria-describedby': 'identifier-help',
        }))
