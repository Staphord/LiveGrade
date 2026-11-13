import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase
from assessments.models import ClassroomPoll, ClassroomPollQuestion, ClassroomPollOption
from assessments.polls import poll_results, save_anonymous_response


class PollProjectionTests(TestCase):
    def setUp(self):
        self.poll = ClassroomPoll.objects.create(question='Choose', standalone=True,
            organization_id=1, created_by=get_user_model().objects.create(username='poll-projection'), status='open')
        self.question = ClassroomPollQuestion.objects.create(poll=self.poll, prompt='Choose')
        self.options = [ClassroomPollOption.objects.create(poll=self.poll, question=self.question,
                        label=label) for label in ('One', 'Two')]

    def test_revising_ballot_replaces_counts_and_abstention_without_duplicate_response(self):
        token = uuid.uuid4()
        key = str(self.question.pk)
        def submit(options, abstained=False):
            save_anonymous_response(self.poll.pk, token, {key: {'options': options, 'abstained': abstained}})
        submit([self.options[0].pk])
        submit([self.options[1].pk])
        result = poll_results(self.poll)
        self.assertEqual(result['responded'], 1)
        self.assertEqual([option.vote_count for option in result['options']], [0, 1])
        submit([], True)
        result = poll_results(self.poll)
        self.assertEqual(result['abstained_count'], 1)
        self.assertEqual([option.vote_count for option in result['options']], [0, 0])
        with self.assertNumQueries(3):
            poll_results(self.poll)
