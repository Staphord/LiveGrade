"""Poll validation and transactional changes. Ballots never count as grading."""
import json
import uuid
from collections import Counter
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import (ClassroomPoll, ClassroomPollOption, ClassroomPollQuestion,
                     ClassroomPollResponse, ClassroomPollVoter, SessionChange,
                     AnonymousClassroomPollResponse, StandalonePollAudit)
from .realtime import broadcast_session_event


class PollForm(forms.ModelForm):
    question = forms.CharField(max_length=250, label='Question',
        widget=forms.Textarea(attrs={'rows': 1, 'data-autogrow': '', 'class': 'form-control'}))
    allow_multiple = forms.ChoiceField(
        choices=[('', 'Choose an answer type'), ('false', 'Multiple choice'), ('true', 'Checkbox')],
        required=True, widget=forms.Select(attrs={'hidden': True, 'data-hide-empty-option': True}), label='Answer type')
    option_lines = forms.CharField(label='Answer options', widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Add at least two options.')
    deadline_local = forms.CharField(required=False, label='Response deadline', widget=forms.HiddenInput())
    deadline_date = forms.CharField(required=False, label='Date', max_length=10,
        widget=forms.TextInput(attrs={'placeholder': 'DD/MM/YYYY', 'inputmode': 'text', 'autocomplete': 'off'}))
    deadline_time = forms.CharField(required=False, label='Time', max_length=5,
        widget=forms.TextInput(attrs={'placeholder': 'HH:MM', 'inputmode': 'text', 'autocomplete': 'off'}))
    timezone_name = forms.CharField(required=False, widget=forms.HiddenInput())
    extra_questions_json = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = ClassroomPoll
        fields = ['question', 'allow_multiple', 'allow_abstain', 'personal_links_only', 'timezone_name']
        labels = {'question': 'Question', 'allow_multiple': 'Answer type',
                  'allow_abstain': 'Allow “None of these options”',
                  'personal_links_only': 'Require personal voting links'}

    def __init__(self, *args, **kwargs):
        bound_data = kwargs.get('data', args[0] if args else None)
        if bound_data is not None:
            raw_type = bound_data.get('allow_multiple')
            normalized_type = ('false' if raw_type is None else
                               'true' if raw_type in ('on', '1', True) else
                               'false' if raw_type is False else raw_type)
            if raw_type is None or normalized_type != raw_type:
                bound_data = bound_data.copy()
                bound_data['allow_multiple'] = normalized_type
                if args:
                    args = (bound_data, *args[1:])
                else:
                    kwargs['data'] = bound_data
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            first = self.instance.questions.first()
            if first:
                self.initial['question'] = first.prompt
                self.initial['allow_multiple'] = 'true' if first.allow_multiple else 'false'
                self.initial['allow_abstain'] = first.allow_abstain
                self.initial['option_lines'] = '\n'.join(first.options.values_list('label', flat=True))
                self.initial['extra_questions_json'] = json.dumps([
                    {'prompt': item.prompt, 'allow_multiple': item.allow_multiple,
                     'allow_abstain': item.allow_abstain,
                     'options': list(item.options.values_list('label', flat=True))}
                    for item in self.instance.questions.all()[1:]
                ])
            else:
                self.initial['allow_multiple'] = 'true' if self.instance.allow_multiple else 'false'
                self.initial['option_lines'] = '\n'.join(self.instance.options.values_list('label', flat=True))
            self.initial['timezone_name'] = self.instance.timezone_name
            if self.instance.deadline:
                local_deadline = self.instance.deadline.astimezone(ZoneInfo(self.instance.timezone_name))
                self.initial['deadline_local'] = local_deadline.strftime('%Y-%m-%dT%H:%M')
                self.initial['deadline_date'] = local_deadline.strftime('%d/%m/%Y')
                self.initial['deadline_time'] = local_deadline.strftime('%H:%M')
        else:
            self.initial['allow_multiple'] = ''
            self.initial['timezone_name'] = timezone.get_current_timezone_name()
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                css_class = 'form-check-input'
            elif isinstance(field.widget, forms.Select):
                css_class = 'form-select poll-answer-type' if field_name == 'allow_multiple' else 'form-select'
            else:
                css_class = 'form-control'
            field.widget.attrs['class'] = css_class

    def clean_option_lines(self):
        options = [line.strip() for line in self.cleaned_data['option_lines'].splitlines() if line.strip()]
        if not 2 <= len(options) <= 50:
            raise ValidationError('Enter between 2 and 50 options.')
        if any(len(option) > 250 for option in options):
            raise ValidationError('Each option must be 250 characters or fewer.')
        if len({option.casefold() for option in options}) != len(options):
            raise ValidationError('Each option must be different.')
        return options

    def clean_allow_multiple(self):
        return self.cleaned_data['allow_multiple'] == 'true'

    def clean_extra_questions_json(self):
        raw = self.cleaned_data.get('extra_questions_json', '')
        if not raw:
            return []
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            raise ValidationError('Check the additional questions and try again.')
        if not isinstance(entries, list) or len(entries) > 99:
            raise ValidationError('A poll can contain up to 100 questions.')
        cleaned = []
        for index, entry in enumerate(entries, start=2):
            if not isinstance(entry, dict):
                raise ValidationError(f'Question {index} is not valid.')
            prompt = str(entry.get('prompt', '')).strip()
            options = entry.get('options')
            allow_multiple = entry.get('allow_multiple')
            allow_abstain = entry.get('allow_abstain', True)
            if not prompt or len(prompt) > 250:
                raise ValidationError(f'Enter a question of 250 characters or fewer for question {index}.')
            if not isinstance(allow_multiple, bool):
                raise ValidationError(f'Choose an answer type for question {index}.')
            if not isinstance(allow_abstain, bool):
                raise ValidationError(f'Choose whether to allow “None of these options” for question {index}.')
            if not isinstance(options, list):
                raise ValidationError(f'Enter answer options for question {index}.')
            options = [str(option).strip() for option in options if str(option).strip()]
            if not 2 <= len(options) <= 50:
                raise ValidationError(f'Question {index} needs between 2 and 50 answer options.')
            if any(len(option) > 250 for option in options):
                raise ValidationError(f'Keep each answer option for question {index} within 250 characters.')
            if len({option.casefold() for option in options}) != len(options):
                raise ValidationError(f'Answer options for question {index} must be different.')
            cleaned.append({'prompt': prompt, 'allow_multiple': allow_multiple,
                            'allow_abstain': allow_abstain, 'options': options})
        return cleaned

    def clean(self):
        cleaned = super().clean()
        date_part = (cleaned.get('deadline_date') or '').strip()
        time_part = (cleaned.get('deadline_time') or '').strip()
        if self.is_bound and ('deadline_date' in self.data or 'deadline_time' in self.data):
            if date_part and not time_part:
                self.add_error('deadline_time', 'Choose a time for this deadline.')
                raw = ''
            elif time_part and not date_part:
                self.add_error('deadline_date', 'Choose a date for this deadline.')
                raw = ''
            elif date_part and time_part:
                try:
                    parsed_date = datetime.strptime(date_part, '%d/%m/%Y').date()
                except ValueError:
                    self.add_error('deadline_date', 'Enter the date as DD/MM/YYYY.')
                    parsed_date = None
                try:
                    parsed_time = datetime.strptime(time_part, '%H:%M').time()
                except ValueError:
                    self.add_error('deadline_time', 'Enter the time as HH:MM (24-hour time).')
                    parsed_time = None
                raw = (f'{parsed_date.isoformat()}T{parsed_time.strftime("%H:%M")}'
                       if parsed_date and parsed_time else '')
            else:
                raw = ''
        else:
            raw = cleaned.get('deadline_local')
        zone_name = cleaned.get('timezone_name')
        zone_name = zone_name or timezone.get_current_timezone_name()
        cleaned['timezone_name'] = zone_name
        cleaned['deadline'] = None
        try:
            zone = ZoneInfo(zone_name)
        except (ValueError, ZoneInfoNotFoundError):
            self.add_error('timezone_name', 'Your device timezone could not be read. Reload the page and try again.')
            return cleaned
        if raw:
            try:
                naive = datetime.fromisoformat(raw)
                if naive.tzinfo is not None:
                    raise ValueError
                local = naive.replace(tzinfo=zone)
                # Reject nonexistent and ambiguous wall times instead of guessing.
                if (local.astimezone(datetime_timezone.utc).astimezone(zone).replace(tzinfo=None) != naive
                        or local.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset()):
                    raise ValueError
                if local <= timezone.now():
                    self.add_error('deadline_local', 'Choose a future deadline.')
                cleaned['deadline'] = local
            except (ValueError, ZoneInfoNotFoundError):
                self.add_error('deadline_local', 'Enter a valid, unambiguous date and time in your device timezone.')
        return cleaned


def changed(poll, user, summary):
    if poll.standalone:
        StandalonePollAudit.objects.create(poll=poll, by_user=user, summary=summary)
        return
    SessionChange.objects.create(session=poll.assessment_session, by_user=user,
        summary=f'Poll {poll.uuid}: {summary}')
    transaction.on_commit(lambda: broadcast_session_event(poll.assessment_session, 'poll.changed', {}))


def save_draft(form, session, user):
    poll = form.save(commit=False)
    poll.assessment_session = session
    poll.deadline = form.cleaned_data['deadline']
    poll.save()
    poll.questions.all().delete()
    poll.options.filter(question__isnull=True).delete()
    questions = [{
        'prompt': poll.question, 'allow_multiple': poll.allow_multiple,
        'allow_abstain': form.cleaned_data['allow_abstain'],
        'options': form.cleaned_data['option_lines'],
    }, *form.cleaned_data.get('extra_questions_json', [])]
    _write_questions(poll, questions)
    changed(poll, user, 'Saved draft.')
    return poll


def _write_questions(poll, questions, *, start=0):
    for index, item in enumerate(questions, start=start):
        question = ClassroomPollQuestion.objects.create(
            poll=poll, prompt=item['prompt'], allow_multiple=item['allow_multiple'],
            allow_abstain=item.get('allow_abstain', True), order=index)
        ClassroomPollOption.objects.bulk_create([
            ClassroomPollOption(poll=poll, question=question, label=label, order=option_index)
            for option_index, label in enumerate(item['options'])])


def create_imported_draft(session, items, user):
    """Create one draft poll containing all reviewed imported questions."""
    if not items:
        raise ValidationError('The file does not contain any questions.')
    first = items[0]
    poll = ClassroomPoll.objects.create(
        assessment_session=session, question=first['question'],
        allow_multiple=first['allow_multiple'])
    _write_questions(poll, [{
        'prompt': item['question'], 'allow_multiple': item['allow_multiple'],
        'options': item['options'],
    } for item in items])
    changed(poll, user, f'Imported a draft with {len(items)} question(s).')
    return poll


def create_standalone_draft(form, organization, user):
    poll = form.save(commit=False)
    poll.assessment_session = None
    poll.organization_id = organization['id']
    poll.created_by = user
    poll.standalone = True
    poll.personal_links_only = False
    poll.deadline = form.cleaned_data['deadline']
    poll.save()
    _write_questions(poll, [{
        'prompt': poll.question, 'allow_multiple': poll.allow_multiple,
        'allow_abstain': form.cleaned_data['allow_abstain'],
        'options': form.cleaned_data['option_lines'],
    }, *form.cleaned_data.get('extra_questions_json', [])])
    changed(poll, user, 'Saved standalone poll draft.')
    return poll


def create_imported_standalone_draft(items, organization, user):
    if not items:
        raise ValidationError('The file does not contain any questions.')
    first = items[0]
    poll = ClassroomPoll.objects.create(
        standalone=True, organization_id=organization['id'], created_by=user,
        question=first['question'], allow_multiple=first['allow_multiple'])
    _write_questions(poll, [{
        'prompt': item['question'], 'allow_multiple': item['allow_multiple'],
        'options': item['options'],
    } for item in items])
    changed(poll, user, f'Imported a standalone draft with {len(items)} question(s).')
    return poll


def append_imported_questions(poll, items, user):
    """Append reviewed questions to an existing draft poll."""
    if poll.status != ClassroomPoll.Status.DRAFT:
        raise ValidationError('Only a draft poll can be changed. Create a new poll instead.')
    def normalize(value):
        return ' '.join(str(value).split()).casefold()
    existing = {normalize(prompt) for prompt in poll.questions.values_list('prompt', flat=True)}
    existing.add(normalize(poll.question))
    duplicates = []
    for item in items:
        prompt = item['question']
        key = normalize(prompt)
        if key in existing:
            duplicates.append(prompt)
        existing.add(key)
    if duplicates:
        names = ', '.join(f'“{prompt}”' for prompt in dict.fromkeys(duplicates))
        raise ValidationError(f'{names} already in this poll. Remove duplicate questions from the import file.')
    current_count = poll.questions.count()
    if current_count + len(items) > 100:
        raise ValidationError('A poll can contain up to 100 questions.')
    _write_questions(poll, [{
        'prompt': item['question'], 'allow_multiple': item['allow_multiple'],
        'options': item['options'],
    } for item in items], start=current_count)
    changed(poll, user, f'Added {len(items)} question(s) from a file.')
    return poll


def transition(poll, action, user, *, option_id=None, note='', question_id=None):
    """Caller locks the poll: publication, ballots and closure share this lock."""
    if action == 'publish':
        if poll.status != ClassroomPoll.Status.DRAFT:
            raise ValidationError('Only a draft poll can be published.')
        if not poll.questions.exists() or any(question.options.count() < 2 for question in poll.questions.all()):
            raise ValidationError('Add at least two answer options to every question before publishing.')
        if not poll.standalone and not poll.assessment_session.students.exists():
            raise ValidationError('Add a student roster before publishing.')
        if poll.deadline and poll.deadline <= timezone.now():
            raise ValidationError('The response deadline has passed. Edit the draft first.')
        if not poll.standalone:
            students = poll.assessment_session.students.all()
            identify_by = poll.assessment_session.identify_by
            ClassroomPollVoter.objects.bulk_create([ClassroomPollVoter(
                poll=poll, student=student, name=student.full_name,
                identifier=student.student_id if identify_by == 'student_id' else student.full_name)
                for student in students])
        poll.status, poll.published_at = ClassroomPoll.Status.OPEN, timezone.now()
    elif action == 'close':
        if poll.status != ClassroomPoll.Status.OPEN:
            raise ValidationError('Only an open poll can be closed.')
        poll.status, poll.closed_at = ClassroomPoll.Status.CLOSED, timezone.now()
    elif action == 'confirm':
        if poll.status not in (ClassroomPoll.Status.OPEN, ClassroomPoll.Status.CLOSED):
            raise ValidationError('Close or finish collecting responses before confirming a decision.')
        if poll.accepting_responses:
            raise ValidationError('Close voting before approving a final result.')
        question = poll.questions.filter(pk=question_id).first() if question_id else poll.questions.first()
        if question is None:
            raise ValidationError('Choose a valid poll question.')
        if question.confirmed_option_id:
            raise ValidationError('This question’s result has already been approved.')
        counts = _rebuild_result_counts(poll)
        row = counts.get('questions', {}).get(str(question.pk), {})
        options = list(question.options.all())
        for option in options:
            option.vote_count = row.get('options', {}).get(str(option.pk), 0)
        leader, decision_state, _, tied_options = _decision_outcome(options, row.get('responded', 0))
        if decision_state == 'tie':
            if option_id is None:
                raise ValidationError('The top-voted options are tied. Choose one of the tied top options to break the tie.')
            leader = next((option for option in tied_options if str(option.pk) == str(option_id)), None)
            if leader is None:
                raise ValidationError('For a tie, choose only among the tied top options.')
        elif leader is None:
            if decision_state == 'no_votes':
                raise ValidationError('No option received votes for this question, so there is no result to approve.')
            raise ValidationError('No responses were recorded for this question, so there is no result to approve.')
        elif option_id is not None and str(option_id) != str(leader.pk):
            raise ValidationError('You can only approve the option with the most selections.')
        if len(note) > 2000:
            raise ValidationError('Keep the decision note within 2,000 characters.')
        question.confirmed_option, question.decision_note = leader, note
        question.save(update_fields=['confirmed_option', 'decision_note'])
        questions = list(poll.questions.all())
        poll.closed_at = poll.closed_at or timezone.now()
        if all(item.confirmed_option_id for item in questions):
            poll.confirmed_option = questions[0].confirmed_option
            poll.decision_note = questions[0].decision_note
            poll.status, poll.confirmed_at = ClassroomPoll.Status.CONFIRMED, timezone.now()
            poll.save(update_fields=['confirmed_option', 'decision_note', 'status', 'confirmed_at', 'closed_at'])
        else:
            poll.save(update_fields=['closed_at'])
    elif action == 'cancel':
        if poll.status not in (ClassroomPoll.Status.DRAFT, ClassroomPoll.Status.OPEN, ClassroomPoll.Status.CLOSED):
            raise ValidationError('This poll is already final.')
        poll.status, poll.closed_at = ClassroomPoll.Status.CANCELLED, timezone.now()
    else:
        raise ValidationError('Unknown poll action.')
    poll.save()
    changed(poll, user, f'{poll.get_status_display()}.')


@transaction.atomic
def save_response(poll_id, voter_id, selected, abstained=False, *, token, question_answers=None):
    poll = ClassroomPoll.objects.select_for_update().get(pk=poll_id)
    voter = poll.voters.filter(pk=voter_id, token=token, student__isnull=False).first()
    if voter is None or not poll.accepting_responses:
        raise ValidationError('This poll is closed or your invitation is no longer available.')
    questions = list(poll.questions.prefetch_related('options').all())
    if not questions:
        raise ValidationError('This poll has no questions. Ask the teacher to review it.')
    if question_answers is None:
        question_answers = {str(questions[0].pk): None if abstained else selected}
        if len(questions) > 1:
            raise ValidationError('Answer every question before submitting.')
    if set(question_answers) != {str(question.pk) for question in questions}:
        raise ValidationError('Answer every question before submitting.')

    answers, all_choices = {}, set()
    for question in questions:
        raw = question_answers[str(question.pk)]
        if isinstance(raw, dict):
            abstained_for_question = bool(raw.get('abstained'))
            raw_choices = raw.get('options', [])
            if abstained_for_question and raw_choices:
                raise ValidationError('Choose an answer or “None of these options” for each question, not both.')
            raw = None if abstained_for_question else raw_choices
        if raw is None:
            if not question.allow_abstain:
                raise ValidationError('Choose an answer for every question.')
            answers[str(question.pk)] = None
            continue
        try:
            choices = {int(value) for value in raw}
        except (TypeError, ValueError):
            raise ValidationError('Choose valid answer options.')
        if not choices:
            raise ValidationError('Choose an answer or select “None of these options”.')
        if not question.allow_multiple and len(choices) > 1:
            raise ValidationError('Choose only one answer for this question.')
        if question.options.filter(pk__in=choices).count() != len(choices):
            raise ValidationError('Choose answer options listed under each question.')
        answers[str(question.pk)] = sorted(choices)
        all_choices.update(choices)

    previous = ClassroomPollResponse.objects.filter(voter=voter).values('answers').first()
    response, _ = ClassroomPollResponse.objects.update_or_create(
        voter=voter, defaults={'answers': answers,
                               'abstained': all(value is None for value in answers.values())})
    response.options.set(all_choices)
    _update_counts(poll, previous['answers'] if previous else None, answers)
    transaction.on_commit(lambda: broadcast_session_event(poll.assessment_session, 'poll.changed', {}))
    return response


@transaction.atomic
def save_anonymous_response(poll_id, browser_token, question_answers):
    poll = ClassroomPoll.objects.select_for_update().get(pk=poll_id, standalone=True)
    if not poll.accepting_responses:
        raise ValidationError('This poll is closed.')
    try:
        browser_token = uuid.UUID(str(browser_token))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Refresh the poll page and try again.')
    questions = list(poll.questions.prefetch_related('options').all())
    if set(question_answers) != {str(question.pk) for question in questions}:
        raise ValidationError('Answer every question before submitting.')
    answers = {}
    for question in questions:
        raw = question_answers[str(question.pk)]
        abstained = bool(raw.get('abstained'))
        choices_raw = raw.get('options', [])
        if abstained and choices_raw:
            raise ValidationError('Choose an answer or “None of these options” for each question, not both.')
        if abstained:
            if not question.allow_abstain:
                raise ValidationError('Choose an answer for every question.')
            answers[str(question.pk)] = None
            continue
        try:
            choices = {int(value) for value in choices_raw}
        except (TypeError, ValueError):
            raise ValidationError('Choose valid answer options.')
        if not choices or (not question.allow_multiple and len(choices) > 1):
            raise ValidationError('Choose valid answer options for every question.')
        if question.options.filter(pk__in=choices).count() != len(choices):
            raise ValidationError('Choose answer options listed under each question.')
        answers[str(question.pk)] = sorted(choices)
    previous = AnonymousClassroomPollResponse.objects.filter(poll=poll, token=browser_token).values('answers').first()
    response, _ = AnonymousClassroomPollResponse.objects.update_or_create(
        poll=poll, token=browser_token, defaults={'answers': answers})
    _update_counts(poll, previous['answers'] if previous else None, answers)
    return response


def _update_counts(poll, previous, answers):
    """Update a bounded projection inside the ballot's existing poll transaction."""
    counts = poll.result_counts or {'responded': 0, 'questions': {}}
    if previous is None:
        counts['responded'] += 1
    for source, direction in ((previous or {}, -1), (answers, 1)):
        for key, choices in source.items():
            row = counts['questions'].setdefault(key, {'responded': 0, 'abstained': 0, 'options': {}})
            row['responded'] += direction
            if choices is None:
                row['abstained'] += direction
            else:
                for option in choices:
                    option = str(option)
                    row['options'][option] = row['options'].get(option, 0) + direction
    poll.result_counts = counts
    poll.save(update_fields=['result_counts'])


def _rebuild_result_counts(poll):
    """Repair the cached projection from persisted ballots for a finished poll."""
    responses = (AnonymousClassroomPollResponse.objects.filter(poll_id=poll.pk)
                 if poll.standalone else
                 ClassroomPollResponse.objects.filter(voter__poll_id=poll.pk))
    counts = {'responded': 0, 'questions': {}}
    for answers in responses.values_list('answers', flat=True).iterator(chunk_size=500):
        # The list counts response records, so preserve that total even if an
        # older or malformed ballot has no usable per-question answer map.
        counts['responded'] += 1
        if not isinstance(answers, dict):
            continue
        for key, choices in answers.items():
            row = counts['questions'].setdefault(key, {'responded': 0, 'abstained': 0, 'options': {}})
            row['responded'] += 1
            if choices is None:
                row['abstained'] += 1
            else:
                for option in choices:
                    option = str(option)
                    row['options'][option] = row['options'].get(option, 0) + 1
    ClassroomPoll.objects.filter(pk=poll.pk).update(result_counts=counts)
    poll.result_counts = counts
    return counts


def _decision_outcome(options, responded):
    """Return the unique leader, outcome state, count, and tied top options."""
    if not responded:
        return None, 'no_responses', 0, []
    highest_count = max((option.vote_count for option in options), default=0)
    if highest_count == 0:
        return None, 'no_votes', 0, []
    leaders = [option for option in options if option.vote_count == highest_count]
    if len(leaders) != 1:
        return None, 'tie', highest_count, leaders
    return leaders[0], 'unique_leader', highest_count, leaders


def poll_results(poll):
    # Reading results costs O(questions + options), independent of ballot history.
    counts = ClassroomPoll.objects.values_list('result_counts', flat=True).get(pk=poll.pk)
    if not isinstance(counts, dict):
        counts = {'responded': 0, 'questions': {}}
    if not poll.accepting_responses:
        responses = (AnonymousClassroomPollResponse.objects.filter(poll_id=poll.pk)
                     if poll.standalone else
                     ClassroomPollResponse.objects.filter(voter__poll_id=poll.pk))
        if counts.get('responded', 0) != responses.count():
            # Closed polls cannot receive another ballot, so a one-time repair
            # can safely restore the projection without racing active updates.
            counts = _rebuild_result_counts(poll)
    responded = counts.get('responded', 0)
    total = None if poll.standalone else poll.voters.count()
    question_results = []
    for question in poll.questions.all():
        row = counts.get('questions', {}).get(str(question.pk), {})
        question_responded = row.get('responded', 0)
        options = list(question.options.all())
        for option in options:
            option.vote_count = row.get('options', {}).get(str(option.pk), 0)
            option.percent = round(100 * option.vote_count / question_responded) if question_responded else 0
        leading_option, decision_state, leading_vote_count, tied_options = _decision_outcome(options, question_responded)
        question_results.append({'question': question, 'options': options,
                                 'responded': question_responded, 'abstained_count': row.get('abstained', 0),
                                 'confirmed_option': question.confirmed_option,
                                 'decision_note': question.decision_note,
                                 'leading_option': leading_option,
                                 'decision_state': decision_state,
                                 'leading_vote_count': leading_vote_count,
                                 'tied_options': tied_options})
    first = question_results[0] if question_results else {'options': [], 'abstained_count': 0}
    return {'questions': question_results, 'options': first['options'],
            'invited': total, 'responded': responded,
            'unanswered': None if poll.standalone else max(0, total - responded),
            'abstained_count': first['abstained_count']}
