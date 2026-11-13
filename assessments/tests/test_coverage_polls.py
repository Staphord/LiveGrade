"""Poll rules at their edges: form input, publishing, approving a result, and hostile ballots."""
import json
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments import polls
from assessments.models import (
    AnonymousClassroomPollResponse, AssessmentSession, ClassroomPoll, ClassroomPollResponse)
from assessments.polls import PollForm


def question(prompt='Q', options=('A', 'B'), **extra):
    return {'prompt': prompt, 'allow_multiple': False, 'allow_abstain': True,
            'options': list(options), **extra}


def imported(prompt):
    return {'question': prompt, 'allow_multiple': False, 'options': ['A', 'B']}


class AnswerTypeNameTests(SimpleTestCase):
    def test_import_reads_the_new_answer_type_names(self):
        from assessments import poll_import
        from assessments.sheets import workbook_bytes
        rows = [['One answer', 'Multiple choice', 'A, B'], ['Several', 'Checkbox', 'A, B'],
                ['Old name', 'Single choice', 'A, B']]
        parsed = poll_import.parse_questions(workbook_bytes(['Question', 'Question Type', 'Answer Options'], rows))
        kinds = {q['question']: (q['type'], q['allow_multiple']) for q in parsed['questions']}
        self.assertEqual(kinds['One answer'], ('Multiple choice', False))
        self.assertEqual(kinds['Several'], ('Checkbox', True))
        self.assertEqual(kinds['Old name'], ('Multiple choice', False))
        self.assertEqual(dict(PollForm().fields['allow_multiple'].choices)['true'], 'Checkbox')


class FormInputTests(SimpleTestCase):
    def form(self, **overrides):
        data = {'question': 'Where?', 'allow_multiple': 'false', 'option_lines': 'Hall\nLibrary',
                'timezone_name': 'Africa/Dar_es_Salaam', 'allow_abstain': 'on'}
        data.update(overrides)
        return PollForm(data)

    def extra(self, value):
        form = PollForm()
        form.cleaned_data = {'extra_questions_json': value if isinstance(value, str) else json.dumps(value)}
        return form.clean_extra_questions_json()

    def test_answer_type_is_normalized_however_the_form_was_built(self):
        self.assertEqual(PollForm({'allow_multiple': 'on'}).data['allow_multiple'], 'true')
        self.assertEqual(PollForm(data={'question': 'Q'}).data['allow_multiple'], 'false')
        self.assertEqual(PollForm(data={'allow_multiple': False}).data['allow_multiple'], 'false')

    def test_extra_questions_are_validated_one_rule_at_a_time(self):
        good = question('Second?')
        self.assertEqual(self.extra(''), [])
        self.assertEqual(self.extra([good])[0]['prompt'], 'Second?')
        bad_inputs = [
            'not json', {'a': 1}, [good] * 100, ['text'],
            [question('')], [question('x' * 251)],
            [question(allow_multiple='yes')], [question(allow_multiple=False, allow_abstain='no')],
            [{'prompt': 'Q', 'allow_multiple': False, 'options': 'AB'}],
            [question(options=('only one',))], [question(options=('A', 'x' * 251))],
            [question(options=('A', 'a'))],
        ]
        for value in bad_inputs:
            with self.subTest(value=str(value)[:40]):
                with self.assertRaises(ValidationError):
                    self.extra(value)

    def test_deadline_date_and_time_are_checked_together(self):
        future = (timezone.now() + timedelta(days=3)).astimezone().strftime('%d/%m/%Y')
        cases = {
            'deadline_time': {'deadline_date': future},
            'deadline_date': {'deadline_time': '10:30'},
        }
        for field, data in cases.items():
            self.assertIn(field, self.form(**data).errors)
        self.assertIn('deadline_date', self.form(deadline_date='31-12-2099', deadline_time='10:30').errors)
        self.assertIn('deadline_time', self.form(deadline_date=future, deadline_time='ten').errors)
        self.assertNotIn('deadline_date', self.form(deadline_date='', deadline_time='', ).errors)
        valid = self.form(deadline_date='01/01/2099', deadline_time='10:30')
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertIsNotNone(valid.cleaned_data['deadline'])


class PollFlowCase(TestCase):
    def setUp(self):
        self.org = make_org('Poll edges')
        self.owner = make_user('poll-edges-owner')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Class')
        self.student = self.session.students.create(full_name='Ann', student_id='A1')

    def poll(self, questions=None, status=ClassroomPoll.Status.DRAFT, standalone=False, **fields):
        extra = ({'standalone': True, 'organization_id': self.org.pk, 'created_by': self.owner}
                 if standalone else {'assessment_session': self.session})
        poll = ClassroomPoll.objects.create(question='Q', status=status, **extra, **fields)
        polls._write_questions(poll, questions if questions is not None else [question()])
        return poll


class FormInitialTests(PollFlowCase):
    def test_a_poll_without_questions_still_fills_the_form(self):
        poll = self.poll(questions=[])
        poll.options.create(label='Left', order=0)
        form = PollForm(instance=poll)
        self.assertEqual(form.initial['option_lines'], 'Left')
        self.assertEqual(form.initial['allow_multiple'], 'false')


class ImportTests(PollFlowCase):
    def test_nothing_to_import_is_refused(self):
        with self.assertRaises(ValidationError):
            polls.create_imported_draft(self.session, [], self.owner)
        with self.assertRaises(ValidationError):
            polls.create_imported_standalone_draft([], self.org.as_claim(), self.owner)

    def test_appending_is_limited_to_drafts_and_to_one_hundred_questions(self):
        with self.assertRaises(ValidationError):
            polls.append_imported_questions(self.poll(status=ClassroomPoll.Status.OPEN), [imported('New')], self.owner)
        draft = self.poll()
        with self.assertRaises(ValidationError):
            polls.append_imported_questions(draft, [imported(f'extra {n}') for n in range(100)], self.owner)


class TransitionTests(PollFlowCase):
    def test_publishing_needs_two_options_for_every_question(self):
        with self.assertRaises(ValidationError):
            polls.transition(self.poll(questions=[]), 'publish', self.owner)

    def confirm(self, poll, **kwargs):
        polls.transition(poll, 'confirm', self.owner, **kwargs)

    def ballots(self, poll, *choices_per_ballot):
        standalone = poll.standalone
        for choices in choices_per_ballot:
            answers = {str(q.pk): choice for q, choice in zip(poll.questions.all(), choices)}
            if standalone:
                AnonymousClassroomPollResponse.objects.create(poll=poll, answers=answers)

    def closed_standalone(self, questions=None):
        return self.poll(questions, status=ClassroomPoll.Status.CLOSED, standalone=True)

    def test_confirming_needs_a_valid_unconfirmed_question_and_a_clear_leader(self):
        empty = self.poll(questions=[], status=ClassroomPoll.Status.CLOSED, standalone=True)
        with self.assertRaisesMessage(ValidationError, 'valid poll question'):
            self.confirm(empty)
        poll = self.closed_standalone()
        with self.assertRaisesMessage(ValidationError, 'No responses were recorded'):
            self.confirm(poll)
        first, second = list(poll.questions.first().options.all())
        self.ballots(poll, ([first.pk],), ([second.pk],))
        with self.assertRaisesMessage(ValidationError, 'tied'):
            self.confirm(poll)
        with self.assertRaisesMessage(ValidationError, 'only among the tied'):
            self.confirm(poll, option_id=999999)
        self.confirm(poll, option_id=first.pk, note='Chair decided')
        poll.refresh_from_db()
        self.assertEqual(poll.status, ClassroomPoll.Status.CONFIRMED)
        poll.status = ClassroomPoll.Status.CLOSED
        with self.assertRaisesMessage(ValidationError, 'already been approved'):
            self.confirm(poll)

    def test_a_leader_must_have_votes_and_the_note_must_be_short(self):
        poll = self.closed_standalone()
        first, second = list(poll.questions.first().options.all())
        self.ballots(poll, (None,))
        with self.assertRaisesMessage(ValidationError, 'No option received votes'):
            self.confirm(poll)
        AnonymousClassroomPollResponse.objects.all().delete()
        self.ballots(poll, ([first.pk],), ([first.pk],), ([second.pk],))
        with self.assertRaisesMessage(ValidationError, 'most selections'):
            self.confirm(poll, option_id=second.pk)
        with self.assertRaisesMessage(ValidationError, '2,000'):
            self.confirm(poll, note='x' * 2001)

    def test_confirming_one_question_of_several_keeps_the_poll_closed(self):
        poll = self.closed_standalone([question('One'), question('Two')])
        first_question, second_question = list(poll.questions.all())
        a1 = first_question.options.first()
        b1 = second_question.options.first()
        self.ballots(poll, ([a1.pk], [b1.pk]))
        self.confirm(poll, question_id=first_question.pk)
        poll.refresh_from_db()
        self.assertEqual(poll.status, ClassroomPoll.Status.CLOSED)


class ClassroomBallotTests(PollFlowCase):
    def open_poll(self, questions=None):
        poll = self.poll(questions, standalone=False)
        polls.transition(poll, 'publish', self.owner)
        voter = poll.voters.get()
        return poll, voter

    def vote(self, poll, voter, selected=None, abstained=False, answers=None):
        return polls.save_response(poll.pk, voter.pk, selected or [], abstained, token=voter.token,
                                   question_answers=answers)

    def test_ballots_for_polls_without_questions_or_with_the_wrong_questions_are_refused(self):
        poll, voter = self.open_poll()
        poll.questions.all().delete()
        with self.assertRaisesMessage(ValidationError, 'no questions'):
            self.vote(poll, voter)
        poll, voter = self.open_poll([question('One'), question('Two')])
        with self.assertRaisesMessage(ValidationError, 'Answer every question'):
            self.vote(poll, voter, [1])
        with self.assertRaisesMessage(ValidationError, 'Answer every question'):
            self.vote(poll, voter, answers={'999999': [1]})


    def test_a_single_question_ballot_may_be_given_as_a_plain_selection(self):
        poll, voter = self.open_poll()
        option = poll.questions.get().options.first()
        self.assertFalse(self.vote(poll, voter, [option.pk]).abstained)
        self.assertTrue(self.vote(poll, voter, abstained=True).abstained)
        self.assertEqual(ClassroomPollResponse.objects.filter(voter=voter).count(), 1)


class AnonymousBallotTests(PollFlowCase):
    def standalone(self, questions=None, **fields):
        poll = self.poll(questions, standalone=True, **fields)
        polls.transition(poll, 'publish', self.owner)
        return poll

    def answer(self, poll, token=None, **per_question):
        answers = per_question.get('answers')
        return polls.save_anonymous_response(poll.pk, token or uuid.uuid4(), answers)

    def test_hostile_ballots_are_refused(self):
        poll = self.standalone([question('One', allow_abstain=False)])
        q = poll.questions.get()
        a, b = list(q.options.all())
        foreign = self.poll([question('Other')], standalone=True).questions.get().options.first()
        key = str(q.pk)
        cases = [
            ('not-a-uuid', {key: {'options': [a.pk]}}, 'Refresh'),
            (uuid.uuid4(), {}, 'Answer every question'),
            (uuid.uuid4(), {key: {'abstained': True, 'options': [a.pk]}}, 'not both'),
            (uuid.uuid4(), {key: {'abstained': True}}, 'Choose an answer for every question'),
            (uuid.uuid4(), {key: {'options': ['x']}}, 'valid answer options'),
            (uuid.uuid4(), {key: {'options': []}}, 'valid answer options for every'),
            (uuid.uuid4(), {key: {'options': [a.pk, b.pk]}}, 'valid answer options for every'),
            (uuid.uuid4(), {key: {'options': [foreign.pk]}}, 'listed under each question'),
        ]
        for token, answers, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    polls.save_anonymous_response(poll.pk, token, answers)

    def test_closed_polls_take_no_ballots(self):
        poll = self.standalone()
        polls.transition(poll, 'close', self.owner)
        with self.assertRaisesMessage(ValidationError, 'closed'):
            polls.save_anonymous_response(poll.pk, uuid.uuid4(), {})


class ResultProjectionTests(PollFlowCase):
    def test_rebuilding_counts_tolerates_malformed_and_abstained_ballots(self):
        poll = self.poll(standalone=True, status=ClassroomPoll.Status.CLOSED)
        q = poll.questions.get()
        option = q.options.first()
        AnonymousClassroomPollResponse.objects.create(poll=poll, answers=['junk'])
        AnonymousClassroomPollResponse.objects.create(poll=poll, answers={str(q.pk): None})
        AnonymousClassroomPollResponse.objects.create(poll=poll, answers={str(q.pk): [option.pk]})
        counts = polls._rebuild_result_counts(poll)
        self.assertEqual(counts['responded'], 3)
        self.assertEqual(counts['questions'][str(q.pk)]['abstained'], 1)

    def test_results_repair_an_unreadable_projection_on_closed_polls(self):
        poll = self.poll(standalone=True, status=ClassroomPoll.Status.CLOSED)
        ClassroomPoll.objects.filter(pk=poll.pk).update(result_counts='garbage')
        result = polls.poll_results(ClassroomPoll.objects.get(pk=poll.pk))
        self.assertEqual(result['responded'], 0)

    def test_open_polls_with_an_unreadable_projection_show_no_responses(self):
        poll = self.poll(standalone=True, status=ClassroomPoll.Status.OPEN)
        ClassroomPoll.objects.filter(pk=poll.pk).update(result_counts=[1])
        self.assertEqual(polls.poll_results(ClassroomPoll.objects.get(pk=poll.pk))['responded'], 0)
