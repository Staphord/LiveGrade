import json
import uuid

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AnonymousClassroomPollResponse, AssessmentSession,
                                ClassroomPoll, ClassroomPollResponse, ClassroomPollVoter)
from assessments.polls import (PollForm, _positive_integer, _rebuild_result_counts,
                               _update_counts, _validated_answers,
                               create_imported_standalone_draft, poll_results, save_draft,
                               save_response, save_standalone_draft, stored_choice_ids,
                               text_response_page, transition)


def question(prompt, *, required=True, response_type='multiple_choice', options=None,
             question_id=None, abstain=True):
    return {
        'id': question_id,
        'prompt': prompt,
        'response_type': response_type,
        'required': required,
        'allow_abstain': abstain,
        'options': options if options is not None else [
            {'id': None, 'label': 'Option A'}, {'id': None, 'label': 'Option B'},
        ],
    }


class PollDefinitionPlanTests(TestCase):
    def setUp(self):
        self.org = make_org('Poll builder')
        self.user = make_user('poll-builder-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.user, name='Poll builder class')
        self.session.students.create(full_name='Student One', student_id='S001')

    def data(self, questions=None, **overrides):
        values = {
            'title': 'Classroom preference',
            'questions_json': json.dumps(questions if questions is not None else [question('First prompt')]),
            'timezone_name': 'UTC',
            'personal_links_only': '',
            'allow_abstain': 'on',
        }
        values.update(overrides)
        return values

    def save(self, *, instance=None, questions=None, **overrides):
        form = PollForm(self.data(questions, **overrides), instance=instance)
        self.assertTrue(form.is_valid(), form.errors)
        return save_draft(form, self.session, self.user)

    def test_title_and_first_question_are_independent_and_question_ids_survive_edits(self):
        poll = self.save()
        first = poll.questions.get()
        option_ids = list(first.options.values_list('pk', flat=True))

        updated = question(
            'Reworded prompt', question_id=first.pk,
            options=[
                {'id': option_ids[1], 'label': 'Second option'},
                {'id': option_ids[0], 'label': 'First option'},
            ])
        poll = self.save(instance=poll, questions=[updated], title='New poll title')
        first.refresh_from_db()

        self.assertEqual(poll.title, 'New poll title')
        self.assertEqual(first.prompt, 'Reworded prompt')
        self.assertEqual(list(first.options.values_list('pk', flat=True)), option_ids[::-1])
        self.assertEqual(list(first.options.values_list('label', flat=True)), ['Second option', 'First option'])

    def test_definition_rejects_blank_title_duplicate_options_and_foreign_question_ids(self):
        form = PollForm(self.data(title='   '))
        self.assertFalse(form.is_valid())
        self.assertIn('title', form.errors)
        self.assertEqual(form.errors['title'], ['Enter a poll title.'])

        duplicate = question('Duplicate choices', options=[
            {'id': None, 'label': 'Same'}, {'id': None, 'label': ' same '},
        ])
        form = PollForm(self.data([duplicate]))
        self.assertFalse(form.is_valid())
        self.assertIn('questions_json', form.errors)

        poll = self.save()
        foreign = question('Foreign question', question_id=999999)
        form = PollForm(self.data([foreign]), instance=poll)
        self.assertFalse(form.is_valid())
        self.assertIn('questions_json', form.errors)

    def test_question_and_option_identities_are_strict_and_blank_options_are_skipped(self):
        for invalid_id in (True, 0, 1.5, 'not-a-number'):
            with self.subTest(question_id=invalid_id):
                form = PollForm(self.data([question('Invalid identity', question_id=invalid_id)]))
                self.assertFalse(form.is_valid())
                self.assertIn('questions_json', form.errors)

        for invalid_id in (True, 'not-a-number'):
            with self.subTest(option_id=invalid_id):
                definition = question('Invalid option identity', options=[
                    {'id': invalid_id, 'label': 'A'}, {'id': None, 'label': 'B'},
                ])
                form = PollForm(self.data([definition]))
                self.assertFalse(form.is_valid())
                self.assertIn('questions_json', form.errors)

        poll = self.save()
        existing = poll.questions.get()
        foreign_option = question('Foreign option', question_id=existing.pk, options=[
            {'id': 999999, 'label': 'A'}, {'id': None, 'label': 'B'},
        ])
        form = PollForm(self.data([foreign_option]), instance=poll)
        self.assertFalse(form.is_valid())
        self.assertIn('questions_json', form.errors)

        definition = question('Blank option is ignored', options=[
            {'id': None, 'label': 'A'}, {'id': None, 'label': '  '}, {'id': None, 'label': 'B'},
        ])
        form = PollForm(self.data([definition]))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([item['label'] for item in form.cleaned_data['questions_json'][0]['options']], ['A', 'B'])
        for invalid_value in (True, 0, 1.5, 'bad'):
            with self.subTest(positive_integer=invalid_value), self.assertRaises(ValueError):
                _positive_integer(invalid_value)

    def test_optional_blank_response_is_counted_as_a_submission_not_an_answer_or_abstention(self):
        poll = self.save(questions=[question('Optional prompt', required=False)])
        transition(poll, 'publish', self.user)
        voter = ClassroomPollVoter.objects.get(poll=poll)
        token = voter.token
        response = save_response(poll.pk, voter.pk, [], token=token,
                                 question_answers={str(poll.questions.get().pk): {'options': []}})

        self.assertEqual(response.answers, {})
        self.assertFalse(response.abstained)
        self.assertEqual(ClassroomPollResponse.objects.filter(voter=voter).count(), 1)
        results = poll_results(poll)
        self.assertEqual(results['responded'], 1)
        self.assertEqual(results['questions'][0]['responded'], 0)
        self.assertEqual(results['questions'][0]['unanswered'], 1)
        self.assertEqual(results['questions'][0]['abstained_count'], 0)

    def test_required_blank_and_unknown_questions_are_rejected_without_saving(self):
        poll = self.save()
        transition(poll, 'publish', self.user)
        voter = ClassroomPollVoter.objects.get(poll=poll)
        qid = poll.questions.get().pk

        with self.assertRaises(ValidationError):
            save_response(poll.pk, voter.pk, [], token=voter.token,
                          question_answers={str(qid): {'options': []}})
        with self.assertRaises(ValidationError):
            save_response(poll.pk, voter.pk, [], token=voter.token,
                          question_answers={str(qid + 1): {'options': []}})
        self.assertFalse(ClassroomPollResponse.objects.filter(voter=voter).exists())

    def test_removing_a_question_and_one_of_its_options_removes_only_those_rows(self):
        poll = self.save(questions=[question('Keep this one'), question('Remove this one')])
        keep, remove = list(poll.questions.all())
        old_options = list(keep.options.values_list('pk', flat=True))
        removed_question_id = remove.pk
        updated = question('Updated prompt', question_id=keep.pk, options=[
            {'id': old_options[0], 'label': 'Kept option'},
            {'id': None, 'label': 'New option'},
        ])

        self.save(instance=poll, questions=[updated])

        self.assertFalse(poll.questions.filter(pk=removed_question_id).exists())
        self.assertFalse(poll.options.filter(pk=old_options[1]).exists())
        self.assertEqual(list(poll.questions.get().options.values_list('label', flat=True)),
                         ['Kept option', 'New option'])

    def test_published_draft_helpers_refuse_edits_and_import_skips_blank_options(self):
        classroom_poll = self.save()
        transition(classroom_poll, 'publish', self.user)
        with self.assertRaisesMessage(ValidationError, 'Published poll questions cannot be edited.'):
            save_draft(PollForm(instance=classroom_poll), self.session, self.user)

        standalone_poll = ClassroomPoll.objects.create(
            title='Published standalone', organization_id=self.org.pk,
            created_by=self.user, standalone=True, status='open')
        with self.assertRaisesMessage(ValidationError, 'Published poll questions cannot be edited.'):
            save_standalone_draft(PollForm(instance=standalone_poll), {'id': self.org.pk}, self.user)

        imported = create_imported_standalone_draft([{
            'question': 'Imported prompt', 'allow_multiple': False,
            'options': ['', 'First', 'Second'],
        }], {'id': self.org.pk}, self.user)
        self.assertEqual(list(imported.questions.get().options.values_list('label', flat=True)),
                         ['First', 'Second'])

    def test_legacy_choice_maps_are_counted_when_updating_and_rebuilding_projections(self):
        poll = self.save()
        question_row = poll.questions.get()
        option = question_row.options.first()

        _update_counts(poll, None, {str(question_row.pk): [option.pk]})

        self.assertEqual(poll.result_counts['questions'][str(question_row.pk)]['options'],
                         {str(option.pk): 1})

        transition(poll, 'publish', self.user)
        voter = ClassroomPollVoter.objects.get(poll=poll)
        ClassroomPollResponse.objects.create(
            voter=voter, answers={str(question_row.pk): [option.pk]})
        result = _rebuild_result_counts(poll)

        self.assertEqual(result['responded'], 1)
        self.assertEqual(result['questions'][str(question_row.pk)]['options'], {str(option.pk): 1})

    def test_unsupported_persisted_question_type_is_rejected(self):
        poll = self.save()
        unsupported = poll.questions.get()
        unsupported.response_type = 'unsupported'

        with self.assertRaisesMessage(ValidationError, 'This question uses an unsupported answer type.'):
            _validated_answers([unsupported], {str(unsupported.pk): []})


class PollFormResponseIdentityTests(TestCase):
    def test_optional_answer_map_does_not_require_a_question_key(self):
        org = make_org('Optional answer organization')
        user = make_user('optional-answer-owner')
        session = AssessmentSession.objects.create(
            organization_id=org.pk, created_by=user, name='Optional answer class')
        session.students.create(full_name='Learner', student_id='L001')
        poll = ClassroomPoll.objects.create(
            title='Optional answer poll', assessment_session=session)
        q = poll.questions.create(prompt='Optional question', required=False)
        q.options.create(poll=poll, label='A')
        q.options.create(poll=poll, label='B')
        transition(poll, 'publish', user)
        voter = ClassroomPollVoter.objects.get(poll=poll)

        response = save_response(poll.pk, voter.pk, [], token=voter.token, question_answers={})

        self.assertEqual(response.answers, {})


class PollWrittenResponsePrivacyTests(TestCase):
    def setUp(self):
        self.org = make_org('Written response organization')
        self.owner = make_user('written-response-owner')
        self.other_user = make_user('written-response-other-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Written response class')
        self.student = self.session.students.create(full_name='Learner Example', student_id='L001')
        self.owner_client = Client()
        sign_in(self.owner_client, self.owner, self.org)
        self.public_client = Client()

    def _classroom_poll(self, *, response_type='short_answer', required=True):
        poll = ClassroomPoll.objects.create(title='Class feedback', assessment_session=self.session)
        question_row = poll.questions.create(
            prompt='What should we improve?', response_type=response_type,
            required=required, allow_abstain=False)
        transition(poll, 'publish', self.owner)
        return poll, question_row

    def _standalone_poll(self, *, response_type='short_answer', required=True):
        poll = ClassroomPoll.objects.create(
            title='Anonymous feedback', organization_id=self.org.pk, created_by=self.owner,
            standalone=True, status='open')
        question_row = poll.questions.create(
            prompt='What should we improve?', response_type=response_type,
            required=required, allow_abstain=False)
        return poll, question_row

    def test_form_accepts_text_types_only_without_choice_options_or_abstention(self):
        for response_type in ('short_answer', 'paragraph'):
            definition = question('Written prompt', response_type=response_type,
                                  options=[], abstain=False)
            form = PollForm({
                'title': 'Written poll',
                'questions_json': json.dumps([definition]),
                'timezone_name': 'UTC',
            })
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data['questions_json'][0]['response_type'], response_type)

        invalid = question('Written prompt', response_type='short_answer', options=['unused'])
        form = PollForm({
            'title': 'Written poll', 'questions_json': json.dumps([invalid]), 'timezone_name': 'UTC',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('questions_json', form.errors)

    def test_classroom_text_answers_are_stored_and_aggregated_without_entering_public_counts(self):
        poll = ClassroomPoll.objects.create(title='Class feedback', assessment_session=self.session)
        short = poll.questions.create(prompt='One suggestion?', response_type='short_answer',
                                      required=True, allow_abstain=False)
        paragraph = poll.questions.create(prompt='Anything else?', response_type='paragraph',
                                          required=False, allow_abstain=False, order=1)
        transition(poll, 'publish', self.owner)
        voter = ClassroomPollVoter.objects.get(poll=poll, student=self.student)
        secret = 'A private classroom suggestion.'

        response = save_response(poll.pk, voter.pk, [], token=voter.token, question_answers={
            str(short.pk): {'type': 'short_answer', 'text': secret},
            str(paragraph.pk): {'type': 'paragraph', 'text': '   '},
        })

        self.assertEqual(response.answers, {str(short.pk): {'type': 'short_answer', 'text': secret}})
        results = poll_results(poll)
        self.assertEqual([item['responded'] for item in results['questions']], [1, 0])
        self.assertEqual([item['unanswered'] for item in results['questions']], [0, 1])
        poll.refresh_from_db()
        self.assertNotIn(secret, json.dumps(poll.result_counts))
        self.assertEqual(poll.result_counts['responded'], 1)

        with self.assertRaises(ValidationError):
            save_response(poll.pk, voter.pk, [], token=voter.token, question_answers={
                str(short.pk): {'type': 'paragraph', 'text': 'Wrong type'},
            })
        with self.assertRaises(ValidationError):
            save_response(poll.pk, voter.pk, [], token=voter.token, question_answers={
                str(short.pk): {'type': 'short_answer', 'text': 'x' * 251},
            })
        with self.assertRaisesMessage(ValidationError, 'Enter a text answer.'):
            save_response(poll.pk, voter.pk, [], token=voter.token, question_answers={
                str(short.pk): {'type': 'short_answer', 'text': None},
            })
        response.refresh_from_db()
        self.assertEqual(response.answers[str(short.pk)]['text'], secret)

    def test_classroom_written_answers_are_only_rendered_in_owner_review(self):
        poll, question_row = self._classroom_poll()
        public_url = reverse('classroom_poll', args=[poll.uuid])
        start = self.public_client.get(public_url)
        self.assertTrue(start.context['has_text_questions'])
        self.assertContains(start, 'your teacher can review written answers.')
        self.assertContains(start, 'Written answers may include details that identify you.')
        self.public_client.post(public_url, {'action': 'identify', 'identifier': self.student.full_name})
        invalid = self.public_client.post(public_url, {
            'action': 'vote', f'question-{question_row.pk}': '',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.context['questions'][0].validation_error,
                         'Answer this required question before submitting.')
        self.assertEqual(invalid.context['questions'][0].user_text, '')
        secret = 'Please add more reading time.'
        submitted = self.public_client.post(public_url, {
            'action': 'vote', f'question-{question_row.pk}': secret,
        })
        self.assertEqual(submitted.status_code, 302)

        public_results = self.public_client.get(reverse('classroom_poll_results', args=[poll.uuid]))
        self.assertNotContains(public_results, secret)
        public_state = self.public_client.get(public_url + '?public_state=1').json()['html']
        self.assertNotIn(secret, public_state)
        self.assertContains(public_results, 'Written answers are private to the poll owner.')

        owner_results = self.owner_client.get(reverse('assessment_polls', args=[self.session.pk]), {
            'poll': str(poll.uuid), 'text_question': str(question_row.pk),
        })
        self.assertEqual(owner_results.status_code, 200)
        page = owner_results.content.decode()
        review = page.split('id="poll-text-response-review"', 1)[1].split('</section>', 1)[0]
        self.assertIn(secret, review)
        self.assertNotIn(self.student.full_name, review)

        self.owner_client.post(reverse('assessment_polls', args=[self.session.pk]), {
            'action': 'close', 'poll': str(poll.uuid),
        })
        closed_response = self.public_client.get(public_url)
        self.assertContains(closed_response, 'Your responses')
        self.assertContains(closed_response, secret)

    def test_anonymous_written_answers_are_owner_only_and_do_not_enter_public_results(self):
        poll, question_row = self._standalone_poll()
        public_url = reverse('standalone_poll_public', args=[poll.uuid])
        self.public_client.get(public_url)
        invalid = self.public_client.post(public_url, {
            'action': 'vote', f'question-{question_row.pk}': '',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.context['questions'][0].validation_error,
                         'Answer this required question before submitting.')
        secret = 'I would like quieter rooms.'
        submitted = self.public_client.post(public_url, {
            'action': 'vote', f'question-{question_row.pk}': secret,
        })
        self.assertEqual(submitted.status_code, 302)
        self.assertEqual(AnonymousClassroomPollResponse.objects.get(poll=poll).answers,
                         {str(question_row.pk): {'type': 'short_answer', 'text': secret}})
        poll.refresh_from_db()
        self.assertNotIn(secret, json.dumps(poll.result_counts))

        other_browser = Client()
        response_page = other_browser.get(public_url)
        self.assertNotContains(response_page, secret)
        for url in (
            reverse('standalone_poll_live_results', args=[poll.uuid]),
            reverse('standalone_poll_live_state', args=[poll.uuid]),
            public_url + '?state=1',
        ):
            response = other_browser.get(url)
            self.assertNotContains(response, secret)

        owner_review = self.owner_client.get(reverse('standalone_poll_list'), {
            'poll': str(poll.uuid), 'text_question': str(question_row.pk),
        })
        self.assertContains(owner_review, secret)
        owner_review_html = owner_review.content.decode()
        review = owner_review_html.split('id="poll-text-response-review"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn(self.other_user.username, review)

        other_owner = Client()
        sign_in(other_owner, self.other_user, self.org)
        denied = other_owner.get(reverse('standalone_poll_list'), {
            'poll': str(poll.uuid), 'text_question': str(question_row.pk),
        })
        self.assertEqual(denied.status_code, 404)

        self.owner_client.post(reverse('standalone_poll_list'), {
            'action': 'close', 'poll': str(poll.uuid),
        })
        closed_response = self.public_client.get(public_url)
        self.assertContains(closed_response, 'Your anonymous response')
        self.assertContains(closed_response, secret)

    def test_text_response_review_paginates_deidentified_answers_and_ignores_other_types(self):
        poll, question_row = self._standalone_poll(response_type='paragraph', required=False)
        text_answer_rows = [AnonymousClassroomPollResponse(
            poll=poll, token=uuid.uuid4(), answers={str(question_row.pk): {
                'type': 'paragraph', 'text': f'Written answer {index}',
            }}) for index in range(26)]
        text_answer_rows.append(AnonymousClassroomPollResponse(
            poll=poll, token=uuid.uuid4(), answers={str(question_row.pk): {
                'type': 'short_answer', 'text': 'Wrong answer type',
            }}))
        AnonymousClassroomPollResponse.objects.bulk_create(text_answer_rows)

        first_page = text_response_page(poll, question_row.pk, 1)
        second_page = text_response_page(poll, question_row.pk, 2)
        self.assertEqual(first_page['page'].paginator.num_pages, 2)
        self.assertEqual(len(first_page['answers']), 25)
        self.assertEqual(second_page['answers'], ['Written answer 25'])
        self.assertIsNone(text_response_page(poll, 'invalid', 1))
        self.assertIsNone(text_response_page(poll, 999999, 1))

    def test_choice_ballot_shapes_and_unsupported_definitions_are_rejected(self):
        poll = ClassroomPoll.objects.create(title='Choice poll', assessment_session=self.session)
        question_row = poll.questions.create(
            prompt='Choose one', response_type='multiple_choice', allow_abstain=True)
        first = question_row.options.create(poll=poll, label='First')
        question_row.options.create(poll=poll, label='Second')
        transition(poll, 'publish', self.owner)
        voter = ClassroomPollVoter.objects.get(poll=poll, student=self.student)
        key = str(question_row.pk)
        invalid_ballots = [
            ([], 'Submit answers in the expected format.'),
            ({key: {'type': 'checkboxes', 'option_ids': [first.pk]}},
             'The answer type does not match this question.'),
            ({key: {'abstained': 'yes'}}, 'Choose a valid answer or abstention.'),
            ({key: {'option_ids': str(first.pk)}}, 'Choose valid answer options.'),
            ({key: [first.pk, first.pk]}, 'Choose each answer option only once.'),
            ({key: [-1]}, 'Choose valid answer options.'),
        ]
        for answers, message in invalid_ballots:
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                save_response(poll.pk, voter.pk, [], token=voter.token, question_answers=answers)

        unsupported_poll = ClassroomPoll.objects.create(
            title='Unsupported poll', assessment_session=self.session)
        unsupported_poll.questions.create(prompt='Unsupported', response_type='unsupported')
        with self.assertRaisesMessage(ValidationError, 'Choose a supported answer type'):
            transition(unsupported_poll, 'publish', self.owner)

        one_choice_poll = ClassroomPoll.objects.create(
            title='Incomplete choice poll', assessment_session=self.session)
        incomplete = one_choice_poll.questions.create(prompt='Incomplete choice')
        incomplete.options.create(poll=one_choice_poll, label='Only choice')
        with self.assertRaisesMessage(ValidationError, 'Add at least two answer options'):
            transition(one_choice_poll, 'publish', self.owner)

        text_poll = ClassroomPoll.objects.create(
            title='Closed text poll', assessment_session=self.session, status='closed')
        text_question = text_poll.questions.create(
            prompt='Written decision', response_type='short_answer', allow_abstain=False)
        with self.assertRaisesMessage(ValidationError, 'Only choice questions'):
            transition(text_poll, 'confirm', self.owner, question_id=text_question.pk)

    def test_legacy_and_malformed_stored_answers_are_read_without_exposing_choices(self):
        poll = ClassroomPoll.objects.create(title='Legacy answers', assessment_session=self.session)
        question_row = poll.questions.create(prompt='Choose one')
        option = question_row.options.create(poll=poll, label='Option')

        self.assertEqual(stored_choice_ids([option.pk]), [option.pk])
        self.assertEqual(stored_choice_ids({'option_ids': [option.pk]}), [option.pk])
        self.assertEqual(stored_choice_ids({'option_ids': 'invalid'}), [])
        self.assertEqual(stored_choice_ids('invalid'), [])

        _update_counts(poll, {str(question_row.pk): 'malformed'}, {})
        poll.refresh_from_db()
        self.assertEqual(poll.result_counts['questions'][str(question_row.pk)]['responded'], 0)
