"""Poll and transfer pages at their edges: refused saves, odd ids, redirects and screens."""
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments import poll_import
from assessments.models import AssessmentSession, ClassroomPoll
from assessments.tests import test_classroom_polls as classroom_polls
from assessments.tests import test_standalone_polls as standalone_polls
from assessments.tests import test_transfers as transfers

AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

class ClassroomPollPageTests(TestCase):
    setUp = classroom_polls.ClassroomPollTests.setUp
    data = classroom_polls.ClassroomPollTests.data
    create = classroom_polls.ClassroomPollTests.create
    publish = classroom_polls.ClassroomPollTests.publish

    def test_a_refused_save_is_returned_as_json_for_the_editor(self):
        response = self.client.post(self.manage, {'action': 'save', 'question': ''}, **AJAX)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])

    def test_saving_can_return_to_the_page_the_organizer_was_leaving(self):
        response = self.client.post(self.manage, self.data(leave_to='/classroom/app/sessions/'))
        self.assertRedirects(response, '/classroom/app/sessions/', fetch_redirect_response=False)

    def test_imported_questions_are_added_to_the_selected_draft(self):
        poll = self.create()
        upload = SimpleUploadedFile('questions.xlsx', poll_import.sample_workbook().getvalue())
        self.client.post(self.manage, {'action': 'upload-questions', 'poll': poll.uuid, 'file': upload})
        response = self.client.post(self.manage, {'action': 'apply-question-import', 'poll': poll.uuid}, follow=True)
        self.assertContains(response, 'Added 2 questions to the poll draft.')

    def test_deleting_polls_by_id_and_refusing_bad_ids(self):
        poll = self.create()
        delete = reverse('assessment_poll_delete', args=[self.session.pk])
        self.assertEqual(self.client.post(delete, {'action': 'delete', 'poll': poll.uuid}).status_code, 302)
        self.assertFalse(self.session.polls.exists())
        self.assertEqual(self.client.post(delete, {'polls': ['not-a-uuid']}).status_code, 404)

    def test_polls_exist_only_in_classroom_sessions(self):
        team = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Team', service='team')
        delete = reverse('assessment_poll_delete', args=[team.pk])
        self.assertEqual(self.client.post(delete, {'polls': []}).status_code, 404)
        with patch('portal.middleware.service_for_path', return_value='team'):
            self.assertEqual(self.client.post(delete, {'polls': []}).status_code, 404)

    def test_the_projector_screen_shows_an_open_poll(self):
        poll = self.publish()
        response = Client().get(reverse('classroom_poll_qr_screen', args=[poll.uuid]))
        self.assertEqual(response.status_code, 200)


class StandalonePollPageTests(TestCase):
    setUp = standalone_polls.StandalonePollsEndToEndTests.setUp
    _create_poll = standalone_polls.StandalonePollsEndToEndTests._create_poll

    def data(self, **overrides):
        data = {'action': 'save', 'question': 'Where?', 'allow_multiple': 'false', 'option_lines': 'Hall\nLibrary',
                'timezone_name': 'Africa/Dar_es_Salaam', 'allow_abstain': 'on'}
        data.update(overrides)
        return data

    def test_a_refused_save_is_returned_as_json_for_the_editor(self):
        response = self.client.post(reverse('standalone_poll_list'), {'action': 'save', 'question': ''}, **AJAX)
        self.assertEqual(response.status_code, 400)

    def test_saving_can_return_to_the_page_the_organizer_was_leaving(self):
        response = self.client.post(reverse('standalone_poll_list'), self.data(leave_to='/polls/app/'))
        self.assertRedirects(response, '/polls/app/', fetch_redirect_response=False)

    def test_bad_ids_cannot_be_deleted(self):
        self.assertEqual(self.client.post(reverse('standalone_poll_delete'), {'polls': ['x']}).status_code, 404)

    def test_qr_can_be_shown_inline_and_the_screen_renders(self):
        poll = self._create_poll(status='open')
        shown = self.public_client.get(reverse('standalone_poll_qr', args=[poll.uuid]) + '?display=1')
        self.assertNotIn('Content-Disposition', shown)
        screen = self.public_client.get(reverse('standalone_poll_qr_screen', args=[poll.uuid]))
        self.assertEqual(screen.status_code, 200)


class BulkDeleteEdgeTests(TestCase):
    def setUp(self):
        self.org, self.owner = make_org('Bulk org'), make_user('bulk-owner')
        sign_in(self.client, self.owner, self.org)

    def test_a_bad_page_size_falls_back_to_the_default(self):
        response = self.client.post(reverse('assessment_session_bulk_delete'), {'per_page': 'many'})
        self.assertEqual(response.status_code, 302)

    def test_bulk_delete_belongs_to_classroom(self):
        with patch('portal.middleware.service_for_path', return_value='team'):
            response = self.client.post(reverse('assessment_session_bulk_delete'), {'per_page': '10'})
        self.assertEqual(response.status_code, 404)


class TransferPageTests(transfers.TransferTestCase):
    def test_an_ajax_hand_over_returns_where_to_go(self):
        sign_in(self.client, self.ann, self.org)
        response = self.client.post(reverse('assessment_transfer', args=[self.session.pk]),
                                    {'new_owner': str(self.ben.pk)}, **AJAX)
        self.assertEqual(response.status_code, 200)
        self.assertIn('redirect_url', response.json())
