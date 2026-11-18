"""Poll validation and transactional changes. Ballots never count as grading."""
import json
import uuid
from collections import Counter
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import (ClassroomPoll, ClassroomPollOption, ClassroomPollQuestion,
                     ClassroomPollResponse, ClassroomPollVoter, SessionChange,
                     AnonymousClassroomPollResponse, StandalonePollAudit)
from .realtime import broadcast_session_event

CHOICE_RESPONSE_TYPES = {'multiple_choice', 'checkboxes'}
TEXT_RESPONSE_LIMITS = {'short_answer': 250, 'paragraph': 4000}


def _positive_integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError
    if isinstance(value, str) and not value.isdigit():
        raise ValueError
    number = int(value)
    if number < 1:
        raise ValueError
    return number


class PollForm(forms.ModelForm):
    title = forms.CharField(max_length=250, label='Title',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'class': 'form-control',
                                      'placeholder': 'Enter a title for this poll'}),
        error_messages={'required': 'Enter a poll title.'})
    questions_json = forms.CharField(max_length=1_500_000, widget=forms.HiddenInput())
    deadline_local = forms.CharField(required=False, label='Response deadline', widget=forms.HiddenInput())
    deadline_date = forms.CharField(required=False, label='Date', max_length=10,
        widget=forms.TextInput(attrs={'placeholder': 'DD/MM/YYYY', 'inputmode': 'text', 'autocomplete': 'off'}))
    deadline_time = forms.CharField(required=False, label='Time', max_length=5,
        widget=forms.TextInput(attrs={'placeholder': 'HH:MM', 'inputmode': 'text', 'autocomplete': 'off'}))
    timezone_name = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = ClassroomPoll
        fields = ['title', 'personal_links_only', 'timezone_name']
        labels = {'personal_links_only': 'Require personal voting links'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.question_errors = {}
        if self.instance.pk:
            questions = self.instance.questions.prefetch_related('options').all()
            self.initial['questions_json'] = json.dumps([
                {'id': item.pk, 'prompt': item.prompt, 'response_type': item.response_type,
                 'required': item.required, 'allow_abstain': item.allow_abstain,
                 'options': [{'id': option.pk, 'label': option.label}
                             for option in item.options.all()]}
                for item in questions
            ])
            self.initial['timezone_name'] = self.instance.timezone_name
            if self.instance.deadline:
                local_deadline = self.instance.deadline.astimezone(ZoneInfo(self.instance.timezone_name))
                self.initial['deadline_local'] = local_deadline.strftime('%Y-%m-%dT%H:%M')
                self.initial['deadline_date'] = local_deadline.strftime('%d/%m/%Y')
                self.initial['deadline_time'] = local_deadline.strftime('%H:%M')
        else:
            self.initial['questions_json'] = json.dumps([{
                'id': None, 'prompt': '', 'response_type': 'multiple_choice',
                'required': True, 'allow_abstain': True,
                'options': [{'id': None, 'label': ''}, {'id': None, 'label': ''}],
            }])
            self.initial['timezone_name'] = timezone.get_current_timezone_name()
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                css_class = 'form-check-input'
            else:
                css_class = 'form-control'
            field.widget.attrs['class'] = css_class

    def clean_questions_json(self):
        try:
            entries = json.loads(self.cleaned_data['questions_json'])
        except (TypeError, ValueError):
            raise ValidationError('Check the questions and try again.')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValidationError('A poll must contain between 1 and 100 questions.')

        existing_questions = ({item.pk: item for item in self.instance.questions.prefetch_related('options').all()}
                              if self.instance.pk else {})
        seen_question_ids = set()
        cleaned = []
        choice_types = {'multiple_choice', 'checkboxes'}
        text_types = {'short_answer', 'paragraph'}
        valid_types = choice_types | text_types

        for index, entry in enumerate(entries, start=1):
            def invalid(message):
                self.question_errors[str(index)] = message
                raise ValidationError(message)

            if not isinstance(entry, dict):
                invalid(f'Question {index} is not valid.')
            raw_id = entry.get('id')
            if isinstance(raw_id, bool):
                invalid(f'Question {index} has an invalid identity.')
            try:
                question_id = _positive_integer(raw_id) if raw_id not in (None, '') else None
            except (TypeError, ValueError):
                invalid(f'Question {index} has an invalid identity.')
            if question_id is not None:
                if question_id not in existing_questions or question_id in seen_question_ids:
                    invalid(f'Question {index} is not part of this draft.')
                seen_question_ids.add(question_id)

            prompt = str(entry.get('prompt', '')).strip()
            response_type = entry.get('response_type')
            required = entry.get('required')
            allow_abstain = entry.get('allow_abstain')
            options = entry.get('options')
            if not prompt or len(prompt) > 250:
                invalid(f'Enter a question of 250 characters or fewer for question {index}.')
            if response_type not in valid_types:
                invalid(f'Choose an answer type for question {index}.')
            if not isinstance(required, bool):
                invalid(f'Choose whether question {index} is required.')
            if not isinstance(allow_abstain, bool):
                invalid(f'Choose whether question {index} allows abstention.')
            if not isinstance(options, list) or len(options) > 50:
                invalid(f'Question {index} can have no more than 50 answer options.')

            existing_option_ids = ({option.pk for option in existing_questions[question_id].options.all()}
                                    if question_id is not None else set())
            seen_option_ids = set()
            normalized_options = []
            for option in options:
                if not isinstance(option, dict):
                    invalid(f'Answer option in question {index} is not valid.')
                raw_option_id = option.get('id')
                if isinstance(raw_option_id, bool):
                    invalid(f'Answer option in question {index} has an invalid identity.')
                try:
                    option_id = _positive_integer(raw_option_id) if raw_option_id not in (None, '') else None
                except (TypeError, ValueError):
                    invalid(f'Answer option in question {index} has an invalid identity.')
                label = str(option.get('label', '')).strip()
                if option_id is not None:
                    if option_id not in existing_option_ids or option_id in seen_option_ids:
                        invalid(f'Answer option in question {index} is not part of this draft.')
                    seen_option_ids.add(option_id)
                if not label:
                    continue
                if len(label) > 250:
                    invalid(f'Keep each answer option for question {index} within 250 characters.')
                normalized_options.append({'id': option_id, 'label': label})

            if response_type in choice_types:
                if not 2 <= len(normalized_options) <= 50:
                    invalid(f'Question {index} needs between 2 and 50 answer options.')
                labels = [option['label'].casefold() for option in normalized_options]
                if len(set(labels)) != len(labels):
                    invalid(f'Answer options for question {index} must be different.')
            elif normalized_options or allow_abstain:
                invalid(f'Text question {index} cannot have answer options or abstention.')

            cleaned.append({
                'id': question_id,
                'prompt': prompt,
                'response_type': response_type,
                'required': required,
                'allow_abstain': allow_abstain,
                'options': normalized_options if response_type in choice_types else [],
            })
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
    if form.instance.pk and form.instance.status != ClassroomPoll.Status.DRAFT:
        raise ValidationError('Published poll questions cannot be edited. Create a new poll instead.')
    poll = form.save(commit=False)
    poll.assessment_session = session
    poll.standalone = False
    poll.organization_id = None
    poll.created_by = None
    poll.deadline = form.cleaned_data['deadline']
    poll.save()
    poll.options.filter(question__isnull=True).delete()
    _save_question_definitions(poll, form.cleaned_data['questions_json'])
    changed(poll, user, 'Saved draft.')
    return poll


def save_standalone_draft(form, organization, user):
    if form.instance.pk and form.instance.status != ClassroomPoll.Status.DRAFT:
        raise ValidationError('Published poll questions cannot be edited. Create a new poll instead.')
    poll = form.save(commit=False)
    poll.assessment_session = None
    poll.organization_id = organization['id']
    poll.created_by = user
    poll.standalone = True
    poll.personal_links_only = False
    poll.deadline = form.cleaned_data['deadline']
    poll.save()
    _save_question_definitions(poll, form.cleaned_data['questions_json'])
    changed(poll, user, 'Saved standalone poll draft.')
    return poll


def _response_type(item):
    response_type = item.get('response_type')
    if response_type:
        return response_type
    return ('checkboxes' if item.get('allow_multiple') else 'multiple_choice')


def stored_choice_ids(answer):
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict) and isinstance(answer.get('option_ids'), list):
        return answer['option_ids']
    return []


def stored_abstained(answer):
    return answer is None or (isinstance(answer, dict) and answer.get('abstained') is True)


def _save_question_definitions(poll, definitions):
    existing_questions = {question.pk: question for question in
                          poll.questions.prefetch_related('options').all()}
    retained_question_ids = set()
    new_questions = []
    updated_questions = []
    rows = []

    for index, item in enumerate(definitions):
        question_id = item['id']
        if question_id is not None:
            question = existing_questions[question_id]
            retained_question_ids.add(question_id)
        else:
            question = ClassroomPollQuestion(poll=poll)
            new_questions.append(question)
        question.prompt = item['prompt']
        question.response_type = _response_type(item)
        question.required = item.get('required', True)
        question.allow_abstain = item.get('allow_abstain', True) if question.response_type in {
            'multiple_choice', 'checkboxes'} else False
        question.order = index
        rows.append((item, question))
        if question.pk:
            updated_questions.append(question)

    if new_questions:
        ClassroomPollQuestion.objects.bulk_create(new_questions, batch_size=500)
    if updated_questions:
        ClassroomPollQuestion.objects.bulk_update(updated_questions,
            ['prompt', 'response_type', 'required', 'allow_abstain', 'order'], batch_size=500)

    new_options = []
    updated_options = []
    removed_option_ids = []
    for item, question in rows:
        existing_options = {option.pk: option for option in
                            existing_questions.get(question.pk, question).options.all()}
        retained_option_ids = set()
        for index, option_data in enumerate(item.get('options', [])):
            option_id = option_data['id']
            if option_id is not None:
                option = existing_options[option_id]
                retained_option_ids.add(option_id)
                option.label = option_data['label']
                option.order = index
                updated_options.append(option)
            else:
                new_options.append(ClassroomPollOption(
                    poll=poll, question=question,
                    label=option_data['label'], order=index))
        removed_option_ids.extend(set(existing_options) - retained_option_ids)

    if updated_options:
        ClassroomPollOption.objects.bulk_update(updated_options, ['label', 'order'], batch_size=500)
    if new_options:
        ClassroomPollOption.objects.bulk_create(new_options, batch_size=500)
    if removed_option_ids:
        ClassroomPollOption.objects.filter(pk__in=removed_option_ids).delete()
    removed_question_ids = set(existing_questions) - retained_question_ids
    if removed_question_ids:
        ClassroomPollQuestion.objects.filter(poll=poll, pk__in=removed_question_ids).delete()

    first = rows[0][1]
    poll.allow_multiple = first.response_type == 'checkboxes'
    poll.allow_abstain = first.allow_abstain
    poll.save(update_fields=['allow_multiple', 'allow_abstain'])


def _write_questions(poll, questions, *, start=0):
    question_rows = [ClassroomPollQuestion(
        poll=poll,
        prompt=item.get('prompt', item.get('question')),
        response_type=_response_type(item),
        required=item.get('required', True),
        allow_abstain=item.get('allow_abstain', True),
        order=index,
    ) for index, item in enumerate(questions, start=start)]
    ClassroomPollQuestion.objects.bulk_create(question_rows, batch_size=500)
    option_rows = []
    for item, question in zip(questions, question_rows):
        for index, option in enumerate(item.get('options', [])):
            label = option.get('label', '') if isinstance(option, dict) else option
            if label:
                option_rows.append(ClassroomPollOption(
                    poll=poll, question=question, label=label, order=index))
    if option_rows:
        ClassroomPollOption.objects.bulk_create(option_rows, batch_size=500)


def create_imported_draft(session, items, user):
    """Create one draft poll containing all reviewed imported questions."""
    if not items:
        raise ValidationError('The file does not contain any questions.')
    first = items[0]
    poll = ClassroomPoll.objects.create(
        assessment_session=session, title='Imported poll',
        allow_multiple=first['allow_multiple'])
    _write_questions(poll, [{
        'prompt': item['question'], 'allow_multiple': item['allow_multiple'],
        'options': item['options'],
    } for item in items])
    changed(poll, user, f'Imported a draft with {len(items)} question(s).')
    return poll


def create_imported_standalone_draft(items, organization, user):
    if not items:
        raise ValidationError('The file does not contain any questions.')
    first = items[0]
    poll = ClassroomPoll.objects.create(
        standalone=True, organization_id=organization['id'], created_by=user,
        title='Imported poll', allow_multiple=first['allow_multiple'])
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
        questions = list(poll.questions.prefetch_related('options').all())
        if not questions:
            raise ValidationError('Add at least one question before publishing.')
        for question in questions:
            if question.response_type in CHOICE_RESPONSE_TYPES and question.options.count() < 2:
                raise ValidationError('Add at least two answer options to every choice question before publishing.')
            if question.response_type not in CHOICE_RESPONSE_TYPES | set(TEXT_RESPONSE_LIMITS):
                raise ValidationError('Choose a supported answer type for every question before publishing.')
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
        if question.response_type not in CHOICE_RESPONSE_TYPES:
            raise ValidationError('Only choice questions can have an approved result.')
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
        decision_questions = [item for item in questions if item.response_type in CHOICE_RESPONSE_TYPES]
        poll.closed_at = poll.closed_at or timezone.now()
        if decision_questions and all(item.confirmed_option_id for item in decision_questions):
            poll.confirmed_option = decision_questions[0].confirmed_option
            poll.decision_note = decision_questions[0].decision_note
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
    answers, all_choices = _validated_answers(questions, question_answers)

    previous = ClassroomPollResponse.objects.filter(voter=voter).values('answers').first()
    response, _ = ClassroomPollResponse.objects.update_or_create(
        voter=voter, defaults={'answers': answers,
                               'abstained': bool(answers) and all(
                                   stored_abstained(value) for value in answers.values())})
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
    answers, _ = _validated_answers(questions, question_answers)
    previous = AnonymousClassroomPollResponse.objects.filter(poll=poll, token=browser_token).values('answers').first()
    response, _ = AnonymousClassroomPollResponse.objects.update_or_create(
        poll=poll, token=browser_token, defaults={'answers': answers})
    _update_counts(poll, previous['answers'] if previous else None, answers)
    return response


def _validated_answers(questions, question_answers):
    if not isinstance(question_answers, dict):
        raise ValidationError('Submit answers in the expected format.')
    question_ids = {str(question.pk) for question in questions}
    if set(question_answers) - question_ids:
        raise ValidationError('Choose answers only for questions in this poll.')

    answers = {}
    all_choices = set()
    for question in questions:
        key = str(question.pk)

        def invalid(message):
            raise ValidationError({key: message})

        if key not in question_answers:
            if question.required:
                invalid('Answer this required question before submitting.')
            continue
        raw = question_answers[key]
        if question.response_type in TEXT_RESPONSE_LIMITS:
            if not isinstance(raw, dict) or raw.get('type') != question.response_type:
                invalid('Submit a valid text answer for this question.')
            answer = raw.get('text')
            if not isinstance(answer, str):
                invalid('Enter a text answer.')
            if len(answer) > TEXT_RESPONSE_LIMITS[question.response_type]:
                invalid(f'Keep this answer within {TEXT_RESPONSE_LIMITS[question.response_type]} characters.')
            if not answer.strip():
                if question.required:
                    invalid('Answer this required question before submitting.')
                continue
            answers[key] = {'type': question.response_type, 'text': answer}
            continue

        if question.response_type not in CHOICE_RESPONSE_TYPES:
            invalid('This question uses an unsupported answer type.')
        if raw is None:
            abstained = True
            choices_raw = []
        elif isinstance(raw, dict):
            submitted_type = raw.get('type')
            if submitted_type is not None and submitted_type != question.response_type:
                raise ValidationError('The answer type does not match this question.')
            abstained = raw.get('abstained', False)
            choices_raw = raw.get('option_ids', raw.get('options', []))
            if not isinstance(abstained, bool):
                invalid('Choose a valid answer or abstention.')
        else:
            abstained = False
            choices_raw = raw
        if not isinstance(choices_raw, (list, tuple)):
            invalid('Choose valid answer options.')
        if abstained and choices_raw:
            invalid('Choose an answer or “None of these options”, not both.')
        if abstained:
            if not question.allow_abstain:
                invalid('Choose an answer for this question.')
            answers[key] = {'type': question.response_type, 'option_ids': [], 'abstained': True}
            continue
        try:
            choice_values = [_positive_integer(value) for value in choices_raw]
        except (TypeError, ValueError):
            invalid('Choose valid answer options.')
        if len(choice_values) != len(choices_raw) or len(set(choice_values)) != len(choice_values):
            invalid('Choose each answer option only once.')
        if not choice_values:
            if question.required:
                invalid('Choose an answer or select “None of these options”.')
            continue
        if question.response_type == 'multiple_choice' and len(choice_values) > 1:
            invalid('Choose only one answer for this question.')
        allowed_options = {option.pk for option in question.options.all()}
        if not set(choice_values) <= allowed_options:
            invalid('Choose answers listed under this question.')
        choices = sorted(choice_values)
        answers[key] = {'type': question.response_type, 'option_ids': choices, 'abstained': False}
        all_choices.update(choices)
    return answers, all_choices


def _update_counts(poll, previous, answers):
    """Update a bounded projection inside the ballot's existing poll transaction."""
    counts = poll.result_counts or {'responded': 0, 'questions': {}}
    if previous is None:
        counts['responded'] += 1
    for source, direction in ((previous or {}, -1), (answers, 1)):
        for key, choices in source.items():
            row = counts['questions'].setdefault(key, {'responded': 0, 'abstained': 0, 'options': {}})
            row['responded'] += direction
            if stored_abstained(choices):
                row['abstained'] += direction
            elif isinstance(choices, dict):
                for option in choices.get('option_ids', []):
                    option = str(option)
                    row['options'][option] = row['options'].get(option, 0) + direction
            elif isinstance(choices, list):
                for option in choices:
                    option = str(option)
                    row['options'][option] = row['options'].get(option, 0) + direction
            else:
                row['responded'] -= direction
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
            if stored_abstained(choices):
                row['abstained'] += 1
            elif isinstance(choices, dict):
                for option in choices.get('option_ids', []):
                    option = str(option)
                    row['options'][option] = row['options'].get(option, 0) + 1
            elif isinstance(choices, list):
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
    for question in poll.questions.select_related('confirmed_option').prefetch_related('options').all():
        row = counts.get('questions', {}).get(str(question.pk), {})
        question_responded = row.get('responded', 0)
        options = list(question.options.all())
        for option in options:
            option.vote_count = row.get('options', {}).get(str(option.pk), 0)
            option.percent = round(100 * option.vote_count / question_responded) if question_responded else 0
        if question.response_type in CHOICE_RESPONSE_TYPES:
            leading_option, decision_state, leading_vote_count, tied_options = _decision_outcome(
                options, question_responded)
        else:
            leading_option, decision_state, leading_vote_count, tied_options = None, 'not_applicable', 0, []
        question_results.append({'question': question, 'options': options,
                                 'responded': question_responded, 'abstained_count': row.get('abstained', 0),
                                 'unanswered': max(0, responded - question_responded),
                                 'confirmed_option': question.confirmed_option,
                                 'decision_note': question.decision_note,
                                 'leading_option': leading_option,
                                 'decision_state': decision_state,
                                 'leading_vote_count': leading_vote_count,
                                 'tied_options': tied_options})
    first = question_results[0] if question_results else {'options': [], 'abstained_count': 0}
    decision_question_count = sum(
        item['question'].response_type in CHOICE_RESPONSE_TYPES for item in question_results)
    return {'questions': question_results, 'options': first['options'],
            'decision_question_count': decision_question_count,
            'has_decision_questions': decision_question_count > 0,
            'invited': total, 'responded': responded,
            'unanswered': None if poll.standalone else max(0, total - responded),
            'abstained_count': first['abstained_count']}


def text_response_page(poll, question_id, page_number):
    try:
        question_id = int(question_id)
    except (TypeError, ValueError):
        return None
    question = poll.questions.filter(pk=question_id, response_type__in=TEXT_RESPONSE_LIMITS).first()
    if question is None:
        return None
    key = str(question.pk)
    if poll.standalone:
        response_answers = AnonymousClassroomPollResponse.objects.filter(
            poll=poll, answers__has_key=key).values_list('answers', flat=True).order_by('pk')
    else:
        response_answers = ClassroomPollResponse.objects.filter(
            voter__poll=poll, answers__has_key=key).values_list('answers', flat=True).order_by('pk')
    page = Paginator(response_answers, 25).get_page(page_number)
    answers = []
    for answer_map in page.object_list:
        answer = answer_map.get(key) if isinstance(answer_map, dict) else None
        if (isinstance(answer, dict) and answer.get('type') == question.response_type
                and isinstance(answer.get('text'), str)):
            answers.append(answer['text'])
    return {'question': question, 'page': page, 'answers': answers}
