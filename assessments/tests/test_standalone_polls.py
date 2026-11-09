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
        self.assertContains(res, 'data-confirm-title="Delete polls"')
        self.assertContains(res, 'data-poll-select-all')
        self.assertContains(res, 'data-poll-delete-selected hidden')
        self.assertContains(res, 'remove.hidden = selected.length === 0')
        self.assertContains(res, 'form.addEventListener(\'submit\', async function (event)')
        self.assertContains(res, "headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}")
        self.assertContains(res, "form.getAttribute('action') || window.location.href")
        self.assertContains(res, "warningText.dataset.inlineMessageText = ''")
        self.assertContains(res, 'data-unsaved-ignore')
        self.assertContains(res, "if (event.target.hasAttribute('data-unsaved-ignore')) return;")
        self.assertNotContains(res, 'window.confirm(')
        self.assertContains(res, 'Unsaved changes in the poll editor will also be discarded.')
        self.assertContains(res, 'novalidate data-poll-deadline-form data-poll-editor')
        self.assertContains(res, 'window.validatePollEditor = validate')
        self.assertContains(res, 'window.snapshotPollEditorState')
        self.assertContains(res, 'window.DevPerf.confirm')
        self.assertContains(res, 'Keep as draft')
        self.assertContains(res, "editor.getAttribute('action') || window.location.href")
        self.assertNotContains(res, 'fetch(editor.action')
        self.assertContains(res, 'custom-select-menu')
        self.assertContains(res, "option.value===''&&select.hasAttribute('data-hide-empty-option')")
        self.assertContains(res, 'window.initCustomSelects(typeGroup)')
        self.assertNotContains(res, 'poll-answer-type-menu')
        self.assertNotContains(res, 'window.enhancePollAnswerType')
        self.assertRegex(res.content.decode(), r'<select(?=[^>]*name="allow_multiple")(?=[^>]*hidden)[^>]*>')

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
        edit_page = self.client.get(reverse('standalone_poll_list'), {'poll': str(poll.uuid)})
        self.assertContains(edit_page, 'data-confirm-title="Publish poll?"')
        self.assertContains(edit_page, 'name="action" value="publish"')

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

    def test_confirmed_decision_is_shown_in_results_panel_only(self):
        poll = self._create_poll(status='open')
        question = poll.questions.first()
        option = question.options.first()
        manage_url = reverse('standalone_poll_list')
        public_url = reverse('standalone_poll_public', args=[poll.uuid])
        self.public_client.get(public_url)
        self.public_client.post(public_url, {
            'action': 'vote', f'question-{question.pk}': [str(option.pk)],
        })
        self.client.post(manage_url, {'action': 'close', 'poll': str(poll.uuid)})
        self.client.post(manage_url, {
            'action': 'confirm', 'poll': str(poll.uuid), 'question': question.pk,
            'decision_note': 'Use the selected option.',
        })

        page = self.public_client.get(public_url)
        left_panel = page.content.decode().split(
            '<section class="s-card poll-response-card"', 1)[1].split(
                '<aside class="s-card poll-live-results-panel"', 1)[0]
        self.assertNotIn(f'Decision: {option.label}', left_panel)
        self.assertContains(page, f'Decision: {option.label}')
        self.assertContains(page, 'Use the selected option.')

    def test_standalone_teacher_can_break_a_tie_only_among_tied_top_options(self):
        poll = self._create_poll(status='open')
        question = poll.questions.first()
        options = list(question.options.all())
        public_url = reverse('standalone_poll_public', args=[poll.uuid])
        for index, option in enumerate(options[:2]):
            voter_client = self.public_client if index == 0 else Client()
            voter_client.get(public_url)
            voter_client.post(public_url, {
                'action': 'vote', f'question-{question.pk}': [str(option.pk)],
            })
        manage_url = reverse('standalone_poll_list')
        self.client.post(manage_url, {'action': 'close', 'poll': str(poll.uuid)})

        detail = self.client.get(manage_url, {'poll': str(poll.uuid)})
        self.assertContains(detail, 'The top options are tied at 1 selection.')
        self.assertContains(detail, 'Choose among the tied top options')
        tied_options = detail.context['results']['questions'][0]['tied_options']
        self.assertEqual({option.pk for option in tied_options}, {options[0].pk, options[1].pk})
        rejected = self.client.post(manage_url, {
            'action': 'confirm', 'poll': str(poll.uuid), 'question': question.pk,
            'option': options[2].pk,
        })
        self.assertContains(rejected, 'For a tie, choose only among the tied top options.')
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'closed')
        self.assertIsNone(question.confirmed_option)

        accepted = self.client.post(manage_url, {
            'action': 'confirm', 'poll': str(poll.uuid), 'question': question.pk,
            'option': options[0].pk, 'decision_note': 'Teacher broke the tie.',
        })
        self.assertEqual(accepted.status_code, 302)
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'confirmed')
        self.assertEqual(question.confirmed_option, options[0])
        self.assertEqual(question.decision_note, 'Teacher broke the tie.')

    def test_ajax_save_and_owner_scoped_poll_delete(self):
        url = reverse('standalone_poll_list')
        data = {'action': 'save', 'question': 'AJAX poll?', 'option_lines': 'A\nB',
            'timezone_name': 'UTC', 'allow_abstain': 'on'}
        response = self.client.post(url, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        poll = ClassroomPoll.objects.get(uuid=response.json()['uuid'])
        accept_response = self.client.post(url, {
            **data, 'question': 'Accept header poll?'}, HTTP_ACCEPT='application/json')
        self.assertEqual(accept_response.status_code, 200)
        self.assertTrue(accept_response.json()['ok'])
        other_poll = self._create_poll(question='Other poll?')
        single_delete = self.client.post(reverse('standalone_poll_delete'), {
            'action': 'delete', 'poll': str(other_poll.uuid)})
        self.assertEqual(single_delete.status_code, 302)
        self.assertFalse(ClassroomPoll.objects.filter(pk=other_poll.pk).exists())
        sign_in(self.client, self.other_user, self.org)
        self.client.post(reverse('standalone_poll_delete'), {'action': 'delete', 'poll': str(poll.uuid)})
        self.assertTrue(ClassroomPoll.objects.filter(pk=poll.pk).exists())
        sign_in(self.client, self.user, self.org)
        self.client.post(reverse('standalone_poll_delete'), {'action': 'bulk-delete',
            'polls': [str(poll.uuid), str(other_poll.uuid)]})
        self.assertFalse(ClassroomPoll.objects.filter(pk__in=[poll.pk, other_poll.pk]).exists())

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

        res = self.client.post(url, {'action': 'upload-questions', 'file': buf},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['ok'])
        self.assertIn('Review import', res.json()['html'])

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
        original_question_count = imported_poll.questions.count()
        buf.seek(0)
        self.client.post(url, {'action': 'upload-questions', 'poll': str(imported_poll.uuid), 'file': buf})
        repeated_import = self.client.post(url, {'action': 'apply-question-import', 'poll': str(imported_poll.uuid)})
        self.assertEqual(repeated_import.status_code, 200)
        self.assertContains(repeated_import, 'already in this poll')
        imported_poll.refresh_from_db()
        self.assertEqual(imported_poll.questions.count(), original_question_count)

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
        self.assertNotIn('poll-answer-track', res.json()['html'])
        self.assertNotIn('%', res.json()['html'])
        response_page = self.public_client.get(vote_url)
        self.assertNotContains(response_page, 'role="progressbar"')

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
