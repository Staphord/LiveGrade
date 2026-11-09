"""Organizer forms for Competition draft setup."""

from decimal import Decimal
from pathlib import Path

from django import forms

from .models import Competition, CompetitionCriterion, CompetitionEntry, CompetitionTrack
from .form_mixins import StyledFormMixin, apply_form_field_styles


class CompetitionForm(StyledFormMixin, forms.ModelForm):
    audience_weight = forms.DecimalField(
        min_value=0, max_value=100, max_digits=5, decimal_places=2,
        required=False, initial=0,
        help_text='0% uses judge scores and keeps audience votes separate. 100% uses audience votes only. '
                  'A value between them combines both results using these percentages.',
    )

    class Meta:
        model = Competition
        fields = ('name', 'audience_weight', 'minimum_judges_per_entry',
                  'minimum_audience_ballots_per_track')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['minimum_judges_per_entry'].help_text = (
            'Require this many completed judge scorecards for each entry before results can advance.')
        self.fields['minimum_audience_ballots_per_track'].help_text = (
            'When audience votes count toward results, require this many complete ballots in each track.')
        self.fields['minimum_judges_per_entry'].required = False
        self.fields['minimum_judges_per_entry'].initial = 1
        self.fields['minimum_audience_ballots_per_track'].required = False
        self.fields['minimum_audience_ballots_per_track'].initial = 1

    def clean_audience_weight(self):
        return self.cleaned_data.get('audience_weight') or 0

    def clean(self):
        cleaned = super().clean()
        cleaned['minimum_judges_per_entry'] = cleaned.get('minimum_judges_per_entry') or 1
        cleaned['minimum_audience_ballots_per_track'] = cleaned.get('minimum_audience_ballots_per_track') or 1
        return cleaned


class CompetitionTrackForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CompetitionTrack
        fields = ('name', 'advancement_rule', 'advancement_value')


class CompetitionCriterionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CompetitionCriterion
        fields = ('track', 'name', 'description', 'weight', 'order')

    def __init__(self, *args, organization, user, competition, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['track'].queryset = (CompetitionTrack.objects.for_organization(organization)
            .owned_by(user).filter(competition=competition).order_by('pk'))
        self.fields['weight'].help_text = 'Percent of this track’s judge score; criteria must total 100%.'


class CompetitionEntryForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CompetitionEntry
        fields = ('track', 'name', 'reference', 'order')

    def __init__(self, *args, competition, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['track'].queryset = (CompetitionTrack.objects.for_organization(organization)
            .owned_by(user).filter(competition=competition))


class CompetitionFirstRoundForm(StyledFormMixin, forms.Form):
    name = forms.CharField(max_length=100, strip=True, label='Round name')


class CompetitionAudienceJoinForm(StyledFormMixin, forms.Form):
    code = forms.CharField(max_length=9, min_length=8, strip=True,
        label='Audience join code',
        help_text='Enter the eight-character code shown by the event organizer.',
        widget=forms.TextInput(attrs={'autocomplete': 'one-time-code',
            'autocapitalize': 'characters', 'spellcheck': 'false'}))

    def clean_code(self):
        code = self.cleaned_data['code'].replace('-', '').replace(' ', '').upper()
        alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        if len(code) != 8 or any(char not in alphabet for char in code):
            raise forms.ValidationError('Enter a valid eight-character audience code.')
        return code


class CompetitionJudgeInvitationForm(StyledFormMixin, forms.Form):
    track = forms.ModelChoiceField(queryset=CompetitionTrack.objects.none(), label='Track')
    email = forms.EmailField(max_length=254, label='Judge email address')
    expires_at = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}), label='Invitation expiry')

    def __init__(self, *args, organization, user, competition, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['track'].queryset = (CompetitionTrack.objects.for_organization(organization)
            .owned_by(user).filter(competition=competition).order_by('pk'))


class CompetitionJudgeScorecardForm(StyledFormMixin, forms.Form):
    def __init__(self, *args, entries, criteria=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = list(entries)
        self.criteria = list(criteria)
        self.single_criterion = len(self.criteria) <= 1
        for row in self.entries:
            row.score_fields = []
            self.fields[f'decision_{row.pk}'] = forms.ChoiceField(required=False,
                choices=(('score', 'Score this entry'), ('conflict', 'Conflict of interest'),
                         ('abstain', 'Unable to assess')), initial='score', label='Your decision')
            self.fields[f'reason_{row.pk}'] = forms.CharField(required=False, max_length=1000,
                label='Reason (required when not scoring)', widget=forms.Textarea(attrs={'rows': 2}))
            row.decision_field = self[f'decision_{row.pk}']
            row.reason_field = self[f'reason_{row.pk}']
            for criterion in self.criteria or [None]:
                field_name = (f'entry_{row.pk}' if self.single_criterion else
                    f'entry_{row.pk}_criterion_{criterion.pk}')
                label = (row.entry.name if criterion is None else
                    f'{row.entry.name} · {criterion.name}')
                self.fields[field_name] = forms.DecimalField(
                    min_value=Decimal('0'), max_value=Decimal('100'), max_digits=5,
                    decimal_places=2, label=label, required=False,
                    help_text=(f"Score from 0 to 100. Criterion weight: {criterion.weight}%."
                        if criterion else 'Score from 0 to 100.'))
                row.score_fields.append({'criterion': criterion, 'field': self[field_name]})
        apply_form_field_styles(self.fields)

    def clean(self):
        cleaned = super().clean()
        for row in self.entries:
            decision = cleaned.get(f'decision_{row.pk}') or 'score'
            if decision != 'score':
                if not cleaned.get(f'reason_{row.pk}'):
                    self.add_error(f'reason_{row.pk}', 'Give a reason for not scoring this entry.')
                if any(cleaned.get(item['field'].name) is not None for item in row.score_fields):
                    self.add_error(f'decision_{row.pk}', 'Clear the scores when declaring a conflict or abstaining.')
            else:
                for item in row.score_fields:
                    if cleaned.get(item['field'].name) is None:
                        self.add_error(item['field'].name, 'Enter a score or choose why you cannot assess this entry.')
        return cleaned

    def score_payload(self):
        if not self.is_valid():
            raise ValueError('A valid judge scorecard is required.')
        payload = {}
        for row in self.entries:
            decision = self.cleaned_data.get(f'decision_{row.pk}') or 'score'
            if decision != 'score':
                payload[str(row.pk)] = {'abstention': decision,
                                        'reason': self.cleaned_data[f'reason_{row.pk}']}
                continue
            if self.single_criterion:
                payload[str(row.pk)] = self.cleaned_data[f'entry_{row.pk}']
            else:
                payload[str(row.pk)] = {
                    str(criterion.pk): self.cleaned_data[
                        f'entry_{row.pk}_criterion_{criterion.pk}']
                    for criterion in self.criteria
                }
        return payload


class CompetitionAdvanceRoundForm(StyledFormMixin, forms.Form):
    name = forms.CharField(max_length=100, strip=True, label='Next round name')

    def __init__(self, *args, tie_requirements=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tie_requirements = tie_requirements or {}
        for track_id, requirement in self.tie_requirements.items():
            field_name = f'head_judge_decision_{track_id}'
            noun = 'entry' if requirement['slots'] == 1 else 'entries'
            self.fields[field_name] = forms.MultipleChoiceField(
                choices=[(str(row.entry_id), row.entry.name)
                         for row in requirement['entries']],
                required=True,
                label=(f'Select {requirement["slots"]} tied {noun} to advance for '
                       f'{requirement["track"]}'),
                help_text='Select exactly the tied entries that advance.',
                widget=forms.CheckboxSelectMultiple,
            )
            self.fields[f'head_judge_reason_{track_id}'] = forms.CharField(
                max_length=500, required=True,
                label=f'Reason for the {requirement["track"]} head-judge decision',
                widget=forms.Textarea(attrs={'rows': 2}),
            )
        apply_form_field_styles(self.fields)

    def clean(self):
        cleaned = super().clean()
        for track_id, requirement in self.tie_requirements.items():
            decision = cleaned.get(f'head_judge_decision_{track_id}')
            if decision is not None and len(decision) != requirement['slots']:
                self.add_error(f'head_judge_decision_{track_id}',
                    f'Select exactly {requirement["slots"]} tied entries.')
        return cleaned

    def head_judge_decisions(self):
        return {int(track_id): [int(value) for value in self.cleaned_data[
                    f'head_judge_decision_{track_id}']]
                for track_id in self.tie_requirements}

    def head_judge_reasons(self):
        return {int(track_id): self.cleaned_data[f'head_judge_reason_{track_id}']
                for track_id in self.tie_requirements}


def competition_head_judge_requirements(tracks, round_entries):
    """Find top-N cutoff ties that need a reasoned head-judge decision."""
    entries_by_track = {}
    for row in round_entries:
        entries_by_track.setdefault(row.track_id, []).append(row)
    requirements = {}
    for track in tracks:
        if track.advancement_rule != 'top_n':
            continue
        rows = entries_by_track.get(track.pk, [])
        if any(row.score is None for row in rows):
            continue
        try:
            count = int(track.advancement_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if count < 1 or count >= len(rows):
            continue
        ordered = sorted(rows, key=lambda row: (-Decimal(row.score), row.entry_id))
        cutoff = Decimal(ordered[count - 1].score)
        certain = [row for row in ordered if Decimal(row.score) > cutoff]
        tied = [row for row in ordered if Decimal(row.score) == cutoff]
        slots = count - len(certain)
        if len(tied) > slots:
            requirements[track.pk] = {
                'track': track.name, 'entries': tied, 'slots': slots,
            }
    return requirements


class CompetitionFinalTieForm(StyledFormMixin, forms.Form):
    """Collect every tied entry's head-judge order and the reasons."""

    def __init__(self, *args, tie_groups=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.tie_groups = list(tie_groups)
        for group in self.tie_groups:
            prefix = self.prefix_for(group)
            choices = [(str(row.entry_id), row.entry.name) for row in group['entries']]
            for position in range(1, len(choices) + 1):
                self.fields[f'{prefix}_rank_{position}'] = forms.ChoiceField(
                    choices=choices, label=f'Place {position}',
                    help_text='Choose each tied entry once in head-judge order.')
            self.fields[f'{prefix}_reason'] = forms.CharField(max_length=500,
                label='Reason for the head-judge order',
                widget=forms.Textarea(attrs={'rows': 2, 'autocomplete': 'off'}))
        apply_form_field_styles(self.fields)

    @staticmethod
    def prefix_for(group):
        cents = int(group['score'] * 100)
        return f"tie_{group['track_id']}_{cents}"

    def clean(self):
        cleaned = super().clean()
        for group in self.tie_groups:
            prefix = self.prefix_for(group)
            ordered = [cleaned.get(f'{prefix}_rank_{position}')
                for position in range(1, len(group['entries']) + 1)]
            if any(entry_id is None for entry_id in ordered):
                continue
            if len(ordered) != len(set(ordered)):
                self.add_error(None,
                    f"Order each {group['track'].name} entry tied at {group['score']} exactly once.")
            reason = cleaned.get(f'{prefix}_reason', '').strip()
            if not reason:
                self.add_error(f'{prefix}_reason', 'A reason is required for the head-judge decision.')
            else:
                cleaned[f'{prefix}_reason'] = reason
        return cleaned

    def decision_payloads(self):
        if not self.is_valid():
            raise ValueError('Read final tie decisions only from a valid form.')
        payloads = []
        for group in self.tie_groups:
            prefix = self.prefix_for(group)
            payloads.append({
                'track_id': group['track_id'],
                'score': group['score'],
                'ordered_entry_ids': [int(self.cleaned_data[f'{prefix}_rank_{position}'])
                    for position in range(1, len(group['entries']) + 1)],
                'reason': self.cleaned_data[f'{prefix}_reason'],
            })
        return payloads

    def display_groups(self):
        display = []
        for group in self.tie_groups:
            prefix = self.prefix_for(group)
            display.append({
                'track': group['track'], 'score': group['score'],
                'entries': group['entries'],
                'rank_fields': [self[f'{prefix}_rank_{position}']
                    for position in range(1, len(group['entries']) + 1)],
                'reason_field': self[f'{prefix}_reason'],
            })
        return display


class CompetitionEntryImportForm(StyledFormMixin, forms.Form):
    file = forms.FileField(label='Entry spreadsheet (.xlsx)')

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if Path(uploaded.name).suffix.lower() != '.xlsx':
            raise forms.ValidationError('Choose an Excel .xlsx file.')
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError('The spreadsheet must be no larger than 10 MB.')
        return uploaded


class CompetitionEntryImportApplyForm(StyledFormMixin, forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)
