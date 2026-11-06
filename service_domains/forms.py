from django import forms
from django.forms import formset_factory
from pathlib import Path
from decimal import Decimal
from assessments.level_descriptors import (MAX_DESCRIPTOR_TEXT, MAX_LEVELS_PER_CRITERION,
    format_descriptors, parse_descriptors)

from .models import (CompetitionRoundEntry, PanelCandidate, PanelCriterion,
                     PanelDecision, PanelHearing, PanelScoringPanel, PanelVote,
                     PanelWrittenReview, SHOWCASE_BRAND_ACCENTS,
                     ShowcaseEvent, ShowcaseExhibit, ShowcaseJudgeCriterion)


class PanelHearingForm(forms.ModelForm):
    hearing_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].label = 'Assessment name'
        self.fields['hearing_date'].label = 'Date (optional)'
        self.fields['venue'].label = 'Room or meeting link (optional)'
        self.fields['workflow'].label = 'Decision process'
        self.fields['workflow'].help_text = ''

    class Meta:
        model = PanelHearing
        fields = ('name', 'hearing_date', 'venue', 'workflow')


class PanelCandidateForm(forms.ModelForm):
    scheduled_time = forms.TimeField(required=False, widget=forms.TimeInput(attrs={'type': 'time'}))

    class Meta:
        model = PanelCandidate
        fields = ('display_name', 'reference', 'order', 'scheduled_time')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['display_name'].label = 'Presenter, team, or entry name'
        self.fields['reference'].label = 'Reference (optional)'
        self.fields['order'].label = 'Presentation order'
        self.fields['scheduled_time'].label = 'Start time (optional)'


class PanelCriterionForm(forms.ModelForm):
    level_descriptors = forms.CharField(required=False, max_length=MAX_DESCRIPTOR_TEXT,
        label='Score level guidance',
        help_text='Optional. One continuous-score anchor per line: “point value | guidance”, from 0 to 100.',
        widget=forms.Textarea(attrs={'rows': 4,
            'placeholder': '0 | Not demonstrated\n50 | Partly demonstrated\n100 | Fully demonstrated'}))
    panels = forms.ModelMultipleChoiceField(queryset=PanelScoringPanel.objects.none(),
        required=False, widget=forms.CheckboxSelectMultiple,
        help_text=('Leave all panels unchecked to assign this criterion to every panel. If only '
            'some panels score it, their configured shares are normalized across those assigned panels.'))

    class Meta:
        model = PanelCriterion
        fields = ('name', 'description', 'weight', 'order')

    def __init__(self, *args, panels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['panels'].queryset = panels if panels is not None else PanelScoringPanel.objects.none()
        self.fields['weight'].required = False
        self.fields['weight'].help_text = 'Relative importance: use 40, 35 and 25 for a 40/35/25 rubric. Equal weights count equally.'
        if self.instance.pk:
            self.initial['level_descriptors'] = format_descriptors(self.instance.levels.all())

    def clean_weight(self):
        return self.cleaned_data.get('weight') or (self.instance.weight if self.instance.pk else Decimal('1'))

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and 'level_descriptors' not in self.data:
            descriptors = [{'score': row.score, 'description': row.description}
                for row in self.instance.levels.all()]
            errors = []
        else:
            descriptors, errors = parse_descriptors(cleaned.get('level_descriptors', ''), Decimal('100'))
        if len(descriptors) > MAX_LEVELS_PER_CRITERION:
            errors.append(f'A criterion can have at most {MAX_LEVELS_PER_CRITERION} score descriptors.')
        self._level_descriptors = descriptors
        for error in errors:
            self.add_error('level_descriptors', error)
        return cleaned


class PanelScoringPanelForm(forms.ModelForm):
    class Meta:
        model = PanelScoringPanel
        fields = ('name', 'admission_method', 'share', 'aggregation', 'minimum_examiners')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['aggregation'].choices = [
            choice for choice in self.fields['aggregation'].choices
            if choice[0] != PanelHearing.Aggregation.TRIMMED_MEAN]
        self.fields['aggregation'].help_text = (
            'Mean averages ratings. Median uses the middle rating and reduces the effect of unusually high or low scores.')
        self.fields['aggregation'].label = 'How to combine scores'
        self.fields['minimum_examiners'].label = 'Minimum examiner participation per entry'
        self.fields['minimum_examiners'].help_text = (
            'Minimum completed scores, votes, or written reviews needed for each entry, based on this process.')
        if self.instance.pk and self.instance.aggregation == PanelHearing.Aggregation.TRIMMED_MEAN:
            self.fields['aggregation'].choices += [
                (PanelHearing.Aggregation.TRIMMED_MEAN, 'Current saved rule (legacy)')]

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get('aggregation') == PanelHearing.Aggregation.TRIMMED_MEAN
                and self.instance.trim_fraction is None):
            self.add_error('trim_fraction', 'Trimmed mean requires an explicit trim fraction.')
        return cleaned


class PanelInvitationForm(forms.Form):
    email = forms.EmailField(max_length=254, label='Examiner email address')
    expires_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}))
    panel = forms.ModelChoiceField(queryset=PanelScoringPanel.objects.none(), required=False,
        help_text='If left blank, the first configured panel is used.')

    def __init__(self, *args, panels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['panel'].queryset = panels if panels is not None else PanelScoringPanel.objects.none()


class PanelDecisionForm(forms.ModelForm):
    class Meta:
        model = PanelDecision
        fields = ('outcome', 'rationale', 'conditions', 'dissent')
        widgets = {
            'rationale': forms.Textarea(attrs={'rows': 3}),
            'conditions': forms.Textarea(attrs={'rows': 2}),
            'dissent': forms.Textarea(attrs={'rows': 2}),
        }


class PanelVoteForm(forms.Form):
    choice = forms.ChoiceField(choices=PanelVote.Choice.choices, widget=forms.RadioSelect)


class PanelWrittenReviewForm(forms.ModelForm):
    class Meta:
        model = PanelWrittenReview
        fields = ('report', 'revision_reason')
        labels = {'report': 'Independent written review'}
        help_texts = {
            'report': ('Summarize evidence, strengths and concerns. The report is private to the chair; '
                'avoid names if your review process requires unattributed reports.'),
            'revision_reason': 'Required when correcting a previously submitted report.',
        }
        widgets = {'report': forms.Textarea(attrs={'rows': 6, 'maxlength': 6000}),
                   'revision_reason': forms.TextInput(attrs={'maxlength': 500})}


class PanelCandidateImportForm(forms.Form):
    file = forms.FileField(label='Candidate spreadsheet (.xlsx)',
        widget=forms.FileInput(attrs={'id': 'id_panel_candidate_file'}))

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError('The spreadsheet must be 10 MB or smaller.')
        if not uploaded.name.lower().endswith('.xlsx'):
            raise forms.ValidationError('Upload an .xlsx spreadsheet.')
        return uploaded


class PanelCandidateImportApplyForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)


class PanelRubricImportForm(forms.Form):
    file = forms.FileField(label='Rubric spreadsheet (.xlsx)',
        widget=forms.FileInput(attrs={'id': 'id_panel_rubric_file'}))

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError('The spreadsheet must be 10 MB or smaller.')
        if not uploaded.name.lower().endswith('.xlsx'):
            raise forms.ValidationError('Upload an .xlsx spreadsheet.')
        return uploaded


class PanelRubricImportApplyForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)


class PanelInvitationImportForm(forms.Form):
    file = forms.FileField(label='Examiner spreadsheet (.xlsx)',
        widget=forms.FileInput(attrs={'id': 'id_panel_invitation_file'}))
    expires_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}))
    panel = forms.ModelChoiceField(queryset=PanelScoringPanel.objects.none(), required=False,
        help_text='If left blank, the first configured panel is used.')

    def __init__(self, *args, panels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['panel'].queryset = panels if panels is not None else PanelScoringPanel.objects.none()

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError('The spreadsheet must be 10 MB or smaller.')
        if not uploaded.name.lower().endswith('.xlsx'):
            raise forms.ValidationError('Upload an .xlsx spreadsheet.')
        return uploaded


class PanelInvitationImportApplyForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)


class PanelResultAdjustmentForm(forms.Form):
    adjusted_score = forms.DecimalField(min_value=0, max_value=100, max_digits=9,
        decimal_places=6, label='Adjusted result')
    reason = forms.CharField(max_length=1000, required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'autocomplete': 'off'}),
        label='Reason for adjustment')

    def clean_reason(self):
        reason = self.cleaned_data['reason'].strip()
        if not reason:
            raise forms.ValidationError('A reason is required for every result adjustment.')
        return reason


class PanelCommentRedactionForm(forms.Form):
    reason = forms.CharField(max_length=1000, required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'autocomplete': 'off'}),
        label='Reason for removing this comment')

    def clean_reason(self):
        reason = self.cleaned_data['reason'].strip()
        if not reason:
            raise forms.ValidationError('A reason is required for every comment redaction.')
        return reason


class PanelCommentReleaseForm(forms.Form):
    reason = forms.CharField(max_length=1000, required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'autocomplete': 'off'}),
        label='Reason for sharing this anonymous comment')

    def clean_reason(self):
        reason = self.cleaned_data['reason'].strip()
        if not reason:
            raise forms.ValidationError('A reason is required before releasing a comment.')
        return reason


class ShowcaseRatingForm(forms.Form):
    stars = forms.ChoiceField(
        choices=((str(value), f'{value} star' if value == 1 else f'{value} stars')
                 for value in range(1, 6)),
        widget=forms.RadioSelect,
        label='Your rating',
    )
class ShowcaseEventForm(forms.ModelForm):
    branding_tagline = forms.CharField(required=False, max_length=160,
        label='Event tagline', help_text='Optional short line shown on public event pages.')
    branding_accent = forms.ChoiceField(required=False,
        choices=tuple((name, name.title()) for name in SHOWCASE_BRAND_ACCENTS),
        label='Event accent color')
    minimum_judges_per_exhibit = forms.IntegerField(min_value=0, max_value=50, initial=1,
        label='Required judge scorecards per exhibit',
        help_text='Use 0 for an audience-only event. Otherwise each exhibit needs this many judge scorecards before results can be published.')

    class Meta:
        model = ShowcaseEvent
        fields = ('name', 'starts_at', 'ends_at', 'includes_minors', 'minimum_judges_per_exhibit')
        widgets = {
            'starts_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'ends_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        branding = self.instance.branding if isinstance(self.instance.branding, dict) else {}
        self.initial.setdefault('branding_tagline', branding.get('tagline', ''))
        self.initial.setdefault('branding_accent', branding.get('accent', 'blue'))
        # Honour the model default for older forms and callers which omit it.
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault('minimum_judges_per_exhibit', 1)
        elif self.is_bound and 'minimum_judges_per_exhibit' not in self.data:
            self.fields['minimum_judges_per_exhibit'].required = False

    def clean_minimum_judges_per_exhibit(self):
        value = self.cleaned_data.get('minimum_judges_per_exhibit')
        if value is None:
            return self.instance.minimum_judges_per_exhibit or 1
        return value

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('starts_at')
        end = cleaned.get('ends_at')
        if start and end and end <= start:
            self.add_error('ends_at', 'The event end must be after the start.')
        if 'branding_tagline' not in self.data:
            cleaned['branding_tagline'] = self.instance.brand_tagline
        if not cleaned.get('branding_accent'):
            branding = self.instance.branding if isinstance(self.instance.branding, dict) else {}
            cleaned['branding_accent'] = branding.get('accent', 'blue')
        return cleaned

    def save(self, commit=True):
        event = super().save(commit=False)
        event.branding = {
            'tagline': self.cleaned_data.get('branding_tagline', '').strip(),
            'accent': self.cleaned_data.get('branding_accent') or 'blue',
        }
        if commit:
            event.save()
            self.save_m2m()
        return event


class ShowcaseEventTransitionForm(forms.Form):
    target_status = forms.ChoiceField(label='Next event state')
    reason = forms.CharField(max_length=500, label='Reason',
        widget=forms.Textarea(attrs={'rows': 2, 'autocomplete': 'off'}))

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target_status'].choices = choices


class ShowcaseMinorConsentForm(forms.Form):
    confirmation = forms.BooleanField(
        required=True,
        label='I confirm that the required consent for participating minors has been collected.',
    )
    reason = forms.CharField(
        max_length=500,
        label='Confirmation details',
        help_text='Record the consent collection process. Do not enter a minor’s name or contact details.',
        widget=forms.Textarea(attrs={'rows': 2, 'autocomplete': 'off'}),
    )


class ShowcaseJudgeAssignmentsForm(forms.Form):
    minimum_judges = forms.IntegerField(min_value=0, max_value=50, initial=1,
        label='Minimum assigned judges per exhibit',
        help_text='Use 0 for an audience-only event.')

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        assignments = {}
        for assignment in event.judge_assignments.filter(active=True).order_by('pk'):
            assignments.setdefault(assignment.exhibit_id, []).append(assignment.judge_label)
        for exhibit in event.exhibits.all():
            self.fields[self.field_name(exhibit.pk)] = forms.CharField(
                required=False,
                label=f'Assigned judges for {exhibit.name}',
                initial='\n'.join(assignments.get(exhibit.pk, [])),
                widget=forms.Textarea(attrs={'rows': 3, 'autocomplete': 'off'}),
                help_text='Enter one judge name or label per line. Do not enter email addresses.',
            )
        if not self.is_bound:
            self.initial['minimum_judges'] = event.minimum_judges_per_exhibit

    @staticmethod
    def field_name(exhibit_id):
        return f'exhibit_{exhibit_id}'

    def assignment_map(self):
        if not self.is_valid():
            raise ValueError('Read assignments only from a valid form.')
        return {
            exhibit.pk: [label.strip() for label in self.cleaned_data[self.field_name(exhibit.pk)].splitlines()
                         if label.strip()]
            for exhibit in self.event.exhibits.all()
        }


class ShowcaseJudgeInvitationForm(forms.Form):
    email = forms.EmailField(max_length=254, label='Judge email address')
    expires_at = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}), label='Link expiry')
    exhibits = forms.ModelMultipleChoiceField(
        queryset=ShowcaseExhibit.objects.none(), required=True,
        widget=forms.CheckboxSelectMultiple, label='Assigned exhibits')

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['exhibits'].queryset = event.exhibits.order_by('category__order', 'order', 'pk')


class ShowcaseJudgeCriterionForm(forms.Form):
    name = forms.CharField(max_length=120, label='Criterion')
    description = forms.CharField(max_length=1000, required=False,
        widget=forms.Textarea(attrs={'rows': 2}), label='What to assess')
    weight = forms.DecimalField(min_value=0, max_value=100, max_digits=5,
        decimal_places=2, label='Weight (%)')
    scale = forms.ChoiceField(choices=ShowcaseJudgeCriterion.Scale.choices,
        label='Scoring scale')
    max_points = forms.DecimalField(min_value=Decimal('0.01'), max_value=1000,
        max_digits=6, decimal_places=2, initial=100,
        label='Maximum points (points scale only)')
    checklist_items_text = forms.CharField(required=False, max_length=5000,
        widget=forms.Textarea(attrs={'rows': 3}),
        label='Checklist items (one per line)')
    DELETE = forms.BooleanField(required=False, label='Remove this criterion')

    def clean_checklist_items_text(self):
        value = self.cleaned_data.get('checklist_items_text', '')
        items = [item.strip() for item in value.splitlines() if item.strip()]
        if len(items) > 20 or any(len(item) > 240 for item in items):
            raise forms.ValidationError('Use at most 20 checklist items, each no longer than 240 characters.')
        return items

    def criterion_data(self):
        return {
            'name': self.cleaned_data['name'],
            'description': self.cleaned_data['description'],
            'weight': self.cleaned_data['weight'],
            'scale': self.cleaned_data['scale'],
            'max_points': self.cleaned_data['max_points'],
            'checklist_items': (self.cleaned_data['checklist_items_text']
                if self.cleaned_data['scale'] == ShowcaseJudgeCriterion.Scale.CHECKLIST else []),
        }


class BaseShowcaseJudgeCriterionFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(form.errors for form in self.forms):
            return
        active = [form for form in self.forms
                  if form.cleaned_data and not form.cleaned_data.get('DELETE')]
        if not active:
            raise forms.ValidationError('Add at least one judge criterion.')
        weights = [form.cleaned_data['weight'] for form in active]
        if sum(weights) != 100:
            raise forms.ValidationError('Judge criterion weights must total exactly 100%.')
        names = [form.cleaned_data['name'].strip().casefold() for form in active]
        if len(names) != len(set(names)):
            raise forms.ValidationError('Judge criterion names must be unique.')


ShowcaseJudgeCriterionFormSet = formset_factory(ShowcaseJudgeCriterionForm,
    formset=BaseShowcaseJudgeCriterionFormSet, extra=1, can_delete=True, max_num=50,
    validate_max=True)


class ShowcaseJudgeScorecardForm(forms.Form):
    """One invited judge's scores for every event criterion on one exhibit."""

    def __init__(self, *args, criteria, **kwargs):
        super().__init__(*args, **kwargs)
        self.criteria = list(criteria)
        for criterion in self.criteria:
            key = f'criterion_{criterion.pk}'
            if criterion.scale == ShowcaseJudgeCriterion.Scale.POINTS:
                self.fields[key] = forms.DecimalField(min_value=0,
                    max_value=criterion.max_points, max_digits=8, decimal_places=2,
                    label=f'{criterion.name} (0–{criterion.max_points:g})',
                    help_text=criterion.description)
            elif criterion.scale == ShowcaseJudgeCriterion.Scale.STARS:
                self.fields[key] = forms.ChoiceField(choices=[('', 'Choose stars'),
                    ('1', '1 star'), ('2', '2 stars'), ('3', '3 stars'),
                    ('4', '4 stars'), ('5', '5 stars')], label=criterion.name,
                    help_text=criterion.description)
            elif criterion.scale == ShowcaseJudgeCriterion.Scale.PASS_FAIL:
                self.fields[key] = forms.ChoiceField(choices=[('', 'Choose an outcome'),
                    ('pass', 'Pass'), ('fail', 'Fail')], label=criterion.name,
                    help_text=criterion.description)
            elif criterion.scale == ShowcaseJudgeCriterion.Scale.CHECKLIST:
                self.fields[key] = forms.MultipleChoiceField(
                    choices=[(str(index), item) for index, item in enumerate(criterion.checklist_items)],
                    required=False, widget=forms.CheckboxSelectMultiple,
                    label=criterion.name, help_text=criterion.description)
            else:
                raise ValueError(f'Unsupported Showcase judge scale: {criterion.scale}')

    def score_payload(self):
        if not self.is_valid():
            raise ValueError('Read score payload only from a valid scorecard.')
        return {str(item.pk): self.cleaned_data[f'criterion_{item.pk}'] for item in self.criteria}


class ShowcaseJudgeConflictForm(forms.Form):
    reason = forms.CharField(max_length=500, label='Why you have a conflict',
        help_text='Briefly explain the conflict. Do not include sensitive personal details.',
        widget=forms.Textarea(attrs={'rows': 2, 'autocomplete': 'off'}))


class ShowcaseImportForm(forms.Form):
    file = forms.FileField(label='Exhibit spreadsheet (.xlsx)')

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if Path(uploaded.name).suffix.lower() != '.xlsx':
            raise forms.ValidationError('Choose an Excel .xlsx file.')
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError('The spreadsheet must be no larger than 10 MB.')
        return uploaded


class ShowcaseImportApplyForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)


class CompetitionAudienceBallotForm(forms.Form):
    """Explicit audience ranking; weighted events require a complete ranking."""

    def __init__(self, *args, round_obj, track, require_complete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_complete = require_complete
        self.entries = list(CompetitionRoundEntry.objects.filter(
            round=round_obj, track=track).select_related('entry').order_by('entry__order', 'entry_id'))
        maximum = max(1, len(self.entries))
        for row in self.entries:
            self.fields[f'rank_{row.entry_id}'] = forms.IntegerField(
                required=require_complete, min_value=1, max_value=maximum,
                label=f'Rank: {row.entry.name}',
                help_text=('1 is your first choice; rank every entry.' if require_complete else
                    '1 is your first choice; leave blank to omit this entry.'))

    def clean(self):
        cleaned = super().clean()
        ranks = []
        seen = set()
        for row in self.entries:
            value = cleaned.get(f'rank_{row.entry_id}')
            if value is None:
                continue
            if value in seen:
                self.add_error(f'rank_{row.entry_id}', 'Each rank can be used once.')
            seen.add(value)
            ranks.append((value, row.entry_id))
        if not ranks:
            raise forms.ValidationError('Rank at least one entry.')
        if self.require_complete and len(ranks) != len(self.entries):
            raise forms.ValidationError('This event combines audience and judge scores; rank every entry.')
        cleaned['ranking'] = [entry_id for _, entry_id in sorted(ranks)]
        return cleaned
