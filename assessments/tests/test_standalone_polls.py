"""End-to-end unit and edge-case verification for standalone anonymous polls."""
import io
import openpyxl

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AnonymousClassroomPollResponse, ClassroomPoll
from assessments.views.standalone_poll_views import TOKEN_KEY, IMPORT_KEY


class StandalonePollsEndToEndTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = make_org('Tech Org')
        self.user = make_user('poll-admin')
        self.other_user = make_user('other-admin')
        self.client = Client()
        sign_in(self.client, self.user, self.org)
        self.public_client = Client()

    def _create_poll(self, status='draft', question='Favorite language?'):
        poll = ClassroomPoll.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            standalone=True,
            status=status,
            question=question,
            description='Select your preferred language',
            allow_multiple=False,
            allow_abstain=True,
            timezone_name='UTC'
        )
        q = poll.questions.create(prompt=question, allow_multiple=False, order=1)
        q.options.create(poll=poll, label='Python', order=1)
        q.options.create(poll=poll, label='Go', order=2)
        q.options.create(poll=poll, label='Rust', order=3)
        return poll

    def test_standalone_poll_list_and_create(self):
        url = reverse('standalone_poll_list')
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)

        # POST create draft with errors
        bad_post = {'action': 'save', 'question': '', 'option_lines': 'OnlyOne'}
        res = self.client.post(url, bad_post)
        self.assertEqual(res.status_code, 200)

        # POST create draft valid
        post_data = {
            'action': 'save',
            'question': 'Which database?',
            'description': 'Production database poll',
            'option_lines': 'PostgreSQL\nSQLite\nMySQL',
            'timezone_name': 'UTC',
            'allow_abstain': 'on',
        }
        res = self.client.post(url, post_data)
        self.assertEqual(res.status_code, 302)
        poll = ClassroomPoll.objects.get(question='Which database?')
        self.assertTrue(poll.standalone)
        self.assertEqual(poll.questions.count(), 1)
        self.assertEqual(poll.questions.first().options.count(), 3)

    def test_standalone_poll_edit_and_transition(self):
        poll = self._create_poll()

        # Update draft
        post_data = {
            'action': 'save',
            'question': 'Updated question?',
            'description': 'Updated description',
            'option_lines': 'Option A\nOption B',
            'timezone_name': 'UTC',
        }
        res = self.client.post(reverse('standalone_poll_list'), {**post_data, 'poll': str(poll.uuid)})
        self.assertEqual(res.status_code, 302)
        poll.refresh_from_db()
        self.assertEqual(poll.question, 'Updated question?')

        # Publish draft -> open
        res = self.client.post(reverse('standalone_poll_list'), {'action': 'publish', 'poll': str(poll.uuid)})
        self.assertEqual(res.status_code, 302)
        poll.refresh_from_db()
        self.assertEqual(poll.status, 'open')
        self.assertTrue(poll.accepting_responses)

        # Attempting to edit a published poll must fail
        res = self.client.post(reverse('standalone_poll_list'), {**post_data, 'poll': str(poll.uuid)})
        self.assertEqual(res.status_code, 200)

        # State endpoint for open poll
        res = self.client.get(reverse('standalone_poll_state', args=[poll.uuid]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['status'], 'open')

    def test_standalone_poll_question_import_flow(self):
        url = reverse('standalone_poll_list')
        # Generate valid XLSX matching expected headers
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Question', 'Question Type', 'Answer Option'])
        ws.append(['What is your role?', 'Single choice', 'Frontend'])
        ws.append(['What is your role?', 'Single choice', 'Backend'])
        ws.append(['What is your role?', 'Single choice', 'DevOps'])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'questions.xlsx'

        # Invalid upload action without file
        res = self.client.post(url, {'action': 'upload-questions'})
        self.assertEqual(res.status_code, 200)

        res = self.client.post(url, {'action': 'upload-questions', 'file': buf})
        self.assertEqual(res.status_code, 302)

        # Cancel question import
        res = self.client.post(url, {'action': 'cancel-question-import'})
        self.assertEqual(res.status_code, 302)
        self.assertNotIn(IMPORT_KEY, self.client.session)

        # Upload again and apply
        buf.seek(0)
        self.client.post(url, {'action': 'upload-questions', 'file': buf})

        # Apply import to create standalone draft
        res = self.client.post(url, {'action': 'apply-question-import'})
        self.assertEqual(res.status_code, 302)
        imported_poll = ClassroomPoll.objects.filter(standalone=True, question='What is your role?').first()
        self.assertIsNotNone(imported_poll)
        self.assertEqual(imported_poll.questions.first().options.count(), 3)

        # Test append to existing draft
        draft = self._create_poll()
        buf.seek(0)
        self.client.post(url, {'action': 'upload-questions', 'file': buf, 'poll': str(draft.uuid)})
        res = self.client.post(url, {'action': 'apply-question-import', 'poll': str(draft.uuid)})
        self.assertEqual(res.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.questions.count(), 2)

    def test_standalone_poll_sample_download(self):
        sample_url = reverse('standalone_poll_sample')
        res = self.client.get(sample_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_standalone_poll_voting_and_revision(self):
        poll = self._create_poll(status='open')
        q = poll.questions.first()
        opt1 = q.options.first()
        opt2 = q.options.all()[1]

        vote_url = reverse('standalone_poll_public', args=[poll.uuid])
        res = self.public_client.get(vote_url)
        self.assertEqual(res.status_code, 200)
        self.assertIn(TOKEN_KEY, self.public_client.session)

        # Invalid action triggers 404
        res = self.public_client.post(vote_url, {'action': 'invalid'})
        self.assertEqual(res.status_code, 404)

        # Vote opt1
        vote_data = {
            'action': 'vote',
            f'question-{q.pk}': [str(opt1.pk)],
        }
        res = self.public_client.post(vote_url, vote_data)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(AnonymousClassroomPollResponse.objects.filter(poll=poll).count(), 1)
        resp = AnonymousClassroomPollResponse.objects.get(poll=poll)
        self.assertEqual(resp.answers.get(str(q.pk)), [opt1.pk])

        # Revise vote to opt2
        vote_data[f'question-{q.pk}'] = [str(opt2.pk)]
        res = self.public_client.post(vote_url, vote_data)
        self.assertEqual(res.status_code, 302)
        resp.refresh_from_db()
        self.assertEqual(resp.answers.get(str(q.pk)), [opt2.pk])
        self.assertEqual(AnonymousClassroomPollResponse.objects.filter(poll=poll).count(), 1)

        # Vote abstain
        res = self.public_client.post(vote_url, {'action': 'vote', f'abstain-{q.pk}': 'yes'})
        self.assertEqual(res.status_code, 302)
        resp.refresh_from_db()
        self.assertIsNone(resp.answers.get(str(q.pk)))

        # Poll state query param on vote view
        res = self.public_client.get(vote_url + '?state=1')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['status'], 'open')

    def test_standalone_poll_live_state_and_results(self):
        poll = self._create_poll(status='open')
        state_url = reverse('standalone_poll_live_state', args=[poll.uuid])
        res = self.public_client.get(state_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'open')
        self.assertTrue(data['accepting'])

        live_results_url = reverse('standalone_poll_live_results', args=[poll.uuid])
        res = self.public_client.get(live_results_url)
        self.assertEqual(res.status_code, 200)

        qr_url = reverse('standalone_poll_qr', args=[poll.uuid])
        res = self.public_client.get(qr_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')

    def test_standalone_poll_error_cases(self):
        # Invalid UUID lookup
        res = self.client.get(reverse('standalone_poll_list') + '?poll=not-a-uuid')
        self.assertEqual(res.status_code, 404)

        # Access draft poll state (must be 404)
        draft = self._create_poll(status='draft')
        res = self.client.get(reverse('standalone_poll_state', args=[draft.uuid]))
        self.assertEqual(res.status_code, 404)

        # Non-poll service scope access must return 404
        from django.contrib.sessions.middleware import SessionMiddleware
        from assessments.views.standalone_poll_views import standalone_poll_list
        from accounts.access import ORGS_KEY
        from django.test import RequestFactory
        from django.http import Http404

        rf = RequestFactory()
        req = rf.get(reverse('standalone_poll_list'))
        SessionMiddleware(lambda r: None).process_request(req)
        req.session[ORGS_KEY] = [{'id': self.org.pk, 'name': self.org.name}]
        req.session.save()
        req.user = self.user
        req.organization = {'id': self.org.pk, 'name': self.org.name}
        req.service = 'classroom'

        with self.assertRaises(Http404):
            standalone_poll_list(req)

        # Transition without choosing a poll
        res = self.client.post(reverse('standalone_poll_list'), {'action': 'publish'})
        self.assertEqual(res.status_code, 200)

        # Stash with errors or stale draft
        url = reverse('standalone_poll_list')
        session = self.client.session
        session[IMPORT_KEY] = {'poll_owner': self.user.pk, 'errors': [(1, 'Fatal error')]}
        session.save()
        res = self.client.post(url, {'action': 'apply-question-import'})
        self.assertEqual(res.status_code, 200)

        # Stash with mismatched target poll
        session[IMPORT_KEY] = {'poll_owner': self.user.pk, 'target_poll': 99999, 'errors': []}
        session.save()
        res = self.client.post(url, {'action': 'apply-question-import', 'poll': str(draft.uuid)})
        self.assertEqual(res.status_code, 200)

        # Stash owned by someone else must be reset to None
        session[IMPORT_KEY] = {'poll_owner': 99999, 'errors': []}
        session.save()
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)

        # Fatal error during question parsing upload
        bad_xlsx = io.BytesIO(b"not a real xlsx")
        bad_xlsx.name = "bad.xlsx"
        res = self.client.post(url, {'action': 'upload-questions', 'file': bad_xlsx})
        self.assertEqual(res.status_code, 200)

        # Voting with an actual answer displays user_answer_display
        poll = self._create_poll(status='open')
        q = poll.questions.first()
        opt = q.options.first()
        vote_url = reverse('standalone_poll_public', args=[poll.uuid])
        self.public_client.post(vote_url, {'action': 'vote', f'question-{q.pk}': [str(opt.pk)]})
        # Reload vote page to inspect answer display rendering
        res = self.public_client.get(vote_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, opt.label)

        # Public poll accessed with wrong service context
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        with patch('assessments.views.standalone_poll_views.save_anonymous_response', side_effect=ValidationError('Invalid ballot submission')):
            res = self.public_client.post(vote_url, {'action': 'vote'})
            self.assertEqual(res.status_code, 200)
            self.assertContains(res, 'Invalid ballot submission')

        from assessments.views.standalone_poll_views import standalone_poll_public
        req = rf.get(vote_url)
        req.service = 'classroom'
        with self.assertRaises(Http404):
            standalone_poll_public(req, poll.uuid)
