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
        ws.append(['What is your role?', 'Multiple choice', 'Frontend'])
        ws.append(['What is your role?', 'Multiple choice', 'Backend'])
        ws.append(['What is your role?', 'Multiple choice', 'DevOps'])
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

    def test_real_csrf_form_submissions_for_import_cancel_logout_and_voting(self):
        import re

        def browser_origin_for_response(response, default_origin='http://testserver'):
            # Per WHATWG Fetch, browsers serialize Origin as "null" on form POST
            # navigations when the document Referrer-Policy is "no-referrer".
            if response.headers.get('Referrer-Policy') == 'no-referrer':
                return 'null'
            return default_origin

        def extract_csrf_token(response):
            match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode())
            self.assertIsNotNone(match)
            return match.group(1)

        csrf_client = Client(enforce_csrf_checks=True)
        sign_in(csrf_client, self.user, self.org)

        manage_url = reverse('standalone_poll_list')
        page = csrf_client.get(manage_url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        origin = browser_origin_for_response(page)
        csrf_token = extract_csrf_token(page)

        sample_res = csrf_client.get(reverse('standalone_poll_sample'))
        self.assertEqual(sample_res.status_code, 200)
        sample_file = io.BytesIO(sample_res.content)
        sample_file.name = 'poll-questions-sample.xlsx'

        preview_res = csrf_client.post(
            manage_url,
            {'action': 'upload-questions', 'file': sample_file, 'csrfmiddlewaretoken': csrf_token},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
            HTTP_ORIGIN='http://testserver',
        )
        self.assertEqual(preview_res.status_code, 200)
        self.assertTrue(preview_res.json()['ok'])

        apply_res = csrf_client.post(
            manage_url,
            {'action': 'apply-question-import', 'csrfmiddlewaretoken': csrf_token},
            HTTP_ORIGIN=origin,
        )
        self.assertEqual(apply_res.status_code, 302)
        created_poll = ClassroomPoll.objects.filter(standalone=True, created_by=self.user).first()
        self.assertIsNotNone(created_poll)

        page = csrf_client.get(f'{manage_url}?poll={created_poll.uuid}')
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        origin = browser_origin_for_response(page)
        csrf_token = extract_csrf_token(page)

        sample_file_2 = io.BytesIO(sample_res.content)
        sample_file_2.name = 'poll-questions-sample.xlsx'
        csrf_client.post(
            manage_url,
            {
                'action': 'upload-questions',
                'poll': str(created_poll.uuid),
                'file': sample_file_2,
                'csrfmiddlewaretoken': csrf_token,
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
            HTTP_ORIGIN='http://testserver',
        )

        cancel_res = csrf_client.post(
            manage_url,
            {
                'action': 'cancel-question-import',
                'poll': str(created_poll.uuid),
                'csrfmiddlewaretoken': csrf_token,
            },
            HTTP_ORIGIN=origin,
        )
        self.assertEqual(cancel_res.status_code, 302)
        self.assertNotIn(IMPORT_KEY, csrf_client.session)

        publish_res = csrf_client.post(
            manage_url,
            {
                'action': 'publish',
                'poll': str(created_poll.uuid),
                'csrfmiddlewaretoken': csrf_token,
            },
            HTTP_ORIGIN=origin,
        )
        self.assertEqual(publish_res.status_code, 302)

        public_csrf_client = Client(enforce_csrf_checks=True)
        vote_url = reverse('standalone_poll_public', args=[created_poll.uuid])
        vote_page = public_csrf_client.get(vote_url)
        self.assertEqual(vote_page.status_code, 200)
        self.assertEqual(vote_page['Referrer-Policy'], 'same-origin')
        vote_origin = browser_origin_for_response(vote_page)
        vote_csrf = extract_csrf_token(vote_page)
        vote_payload = {
            'action': 'vote',
            'csrfmiddlewaretoken': vote_csrf,
        }
        for q in created_poll.questions.all():
            vote_payload[f'question-{q.pk}'] = [str(q.options.first().pk)]
        vote_res = public_csrf_client.post(
            vote_url,
            vote_payload,
            HTTP_ORIGIN=vote_origin,
        )
        self.assertEqual(vote_res.status_code, 302)

        logout_res = csrf_client.post(
            reverse('oidc_logout'),
            {'csrfmiddlewaretoken': csrf_token},
            HTTP_ORIGIN=origin,
        )
        self.assertEqual(logout_res.status_code, 302)

    def test_standalone_poll_detail_design_and_confirm_action_hidden_inputs(self):
        poll = self._create_poll(status='open')
        question = poll.questions.first()
        option = question.options.first()
        vote_url = reverse('standalone_poll_public', args=[poll.uuid])
        self.public_client.get(vote_url)
        self.public_client.post(vote_url, {
            'action': 'vote', f'question-{question.pk}': [str(option.pk)],
        })

        manage_url = reverse('standalone_poll_list')
        open_detail = self.client.get(manage_url, {'poll': str(poll.uuid)})
        self.assertEqual(open_detail.status_code, 200)
        self.assertContains(open_detail, 'class="poll-detail-nav"')
        self.assertContains(open_detail, 'href="#poll-detail-results"')
        self.assertContains(open_detail, 'href="#poll-detail-decisions"')
        self.assertContains(open_detail, 'href="#poll-detail-sharing"')
        self.assertContains(open_detail, 'id="poll-detail-results"')
        self.assertContains(open_detail, 'id="poll-detail-decisions"')
        self.assertContains(open_detail, 'id="poll-detail-sharing"')
        self.assertContains(open_detail, 'data-copy-poll-link')
        self.assertContains(open_detail, 'id="poll-actions-heading"')
        self.assertContains(open_detail, '<input type="hidden" name="action" value="cancel">', html=True)

        state_res = self.client.get(reverse('standalone_poll_state', args=[poll.uuid]))
        self.assertEqual(state_res.status_code, 200)
        self.assertNotIn('poll-answer-track', state_res.json()['html'])
        self.assertNotIn('%', state_res.json()['html'])

        close_res = self.client.post(manage_url, {'action': 'close', 'poll': str(poll.uuid)}, follow=True)
        self.assertContains(close_res, 'Poll updated.')

        closed_detail = self.client.get(manage_url, {'poll': str(poll.uuid)})
        self.assertContains(closed_detail, '<input type="hidden" name="action" value="confirm">', html=True)
        self.assertContains(closed_detail, f'Approve “{option.label}”')

        confirm_res = self.client.post(manage_url, {
            'action': 'confirm',
            'poll': str(poll.uuid),
            'question': str(question.pk),
            'decision_note': 'Approved outcome.',
        }, follow=True)
        self.assertContains(confirm_res, 'Poll updated.')
        poll.refresh_from_db()
        question.refresh_from_db()
        self.assertEqual(poll.status, 'confirmed')
        self.assertEqual(question.confirmed_option, option)
        self.assertEqual(question.decision_note, 'Approved outcome.')

