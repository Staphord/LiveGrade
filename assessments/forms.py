import os

from django import forms
from django.core.exceptions import ValidationError

from .models import AssessmentSession, PresentationGroup, RubricCategory, Student


def _fmt(decimal_value):
    """A Decimal weight/percent without the trailing '.00' its field's
    fixed decimal_places otherwise always renders (e.g. Decimal('100.00')
    -> '100', not '100.00') - matches the `floatformat:'-2'` filter these
    same numbers get in the templates."""
    text = format(decimal_value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class AssessmentSessionForm(forms.ModelForm):
    class Meta:
        model = AssessmentSession
        fields = ['name', 'identify_by', 'group_weight_percent', 'individual_weight_percent']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Sprint Demo — Q1 2026'}),
            'identify_by': forms.Select(attrs={'class': 'form-select'}),
            'group_weight_percent': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
            'individual_weight_percent': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
        }

    def clean(self):
        cleaned = super().clean()
        total = (cleaned.get('group_weight_percent') or 0) + (cleaned.get('individual_weight_percent') or 0)
        if total != 100:
            raise forms.ValidationError('Group weight and individual weight must add up to 100%.')
        return cleaned


class RubricCategoryForm(forms.ModelForm):
    """Takes the session explicitly (not inferred from `instance`) so the
    same form works for both create and edit, and checks the new weight
    against the *other* categories already in that scope — a category can
    never be saved if it would push its scope's total over 100%, rather
    than being allowed to save and just showing a red "200% of 100%" after
    the fact."""

    class Meta:
        model = RubricCategory
        # 'order' deliberately left off: it's an internal sort key with a
        # model default, not something the add-category form exposes. A
        # ModelForm field the template never renders is a required field the
        # browser can never satisfy — every submission would silently fail
        # validation. (This exact bug shipped once already; see
        # PresentationGroupForm below and the regression tests for both.)
        fields = ['name', 'description', 'scope', 'max_points', 'weight']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional guidance shown to students'}),
            'scope': forms.Select(attrs={'class': 'form-select'}),
            'max_points': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'min': '0.5'}),
            'weight': forms.NumberInput(attrs={'class': 'form-control', 'step': '1', 'min': '0', 'max': '100'}),
        }

    def __init__(self, *args, assessment_session=None, **kwargs):
        self.assessment_session = assessment_session
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get('scope')
        weight = cleaned.get('weight')
        if self.assessment_session is None or scope is None or weight is None:
            return cleaned
        target = (self.assessment_session.group_weight_percent
                  if scope == RubricCategory.Scope.GROUP
                  else self.assessment_session.individual_weight_percent)
        others = self.assessment_session.rubric_categories.filter(scope=scope)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        existing_total = sum((c.weight for c in others), start=type(weight)(0))
        new_total = existing_total + weight
        if new_total > target:
            room_left = target - existing_total
            scope_label = RubricCategory.Scope(scope).label
            raise forms.ValidationError(
                f'{scope_label} weights would total {_fmt(new_total)}%, over {_fmt(target)}% - '
                f'this session\'s {scope_label.lower()} share. At most {_fmt(room_left)}% is available.')
        return cleaned


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


class RosterImportForm(forms.Form):
    file = forms.FileField(
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='Columns: registration number, name, email, programme/class.')


class GroupImportForm(forms.Form):
    file = forms.FileField(
        validators=[validate_workbook],
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
        help_text='Columns: Group, and Student (ID or name) — one row per student. '
                  'Every student must already be on the roster.')


class PresentationGroupForm(forms.ModelForm):
    class Meta:
        model = PresentationGroup
        fields = ['name', 'max_size']  # see the comment on RubricCategoryForm re: 'order'
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Group 1'}),
            'max_size': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 2', 'min': '1'}),
        }
        labels = {'max_size': 'Group size'}

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
                    f'This group already has {current} member(s) — the size '
                    f'limit can\'t be set below that.')
        return max_size


class JoinForm(forms.Form):
    identifier = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg', 'autofocus': True,
            'autocomplete': 'off', 'inputmode': 'text',
        }))
