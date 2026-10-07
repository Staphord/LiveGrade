"""Editing a session's setup after it has gone live, and refusing every edit once it is closed."""
from unittest import mock

from django.urls import reverse

from assessments.models import (AssessmentSession, Evaluation, EvaluationScore, GroupMembership,
                                PresentationGroup, PresentationTurn, RubricCategory, SessionChange, Student)
from assessments.tests.test_edges_lecturer_views import AJAX, LecturerTestCase, _messages


class SetupTabAndPageTests(LecturerTestCase):

    def test_a_live_session_shows_a_setup_tab_and_a_draft_or_closed_one_does_not(self):
        for status, shown in (('draft', False), ('live', True), ('closed', False)):
            self.set_status(status)
            page = self.client.get(reverse('assessment_results', args=[self.session.pk]))
            self.assertEqual(self.url('assessment_setup') in page.content.decode(), shown, status)

    def test_a_live_session_opens_its_setup_with_a_banner_and_no_go_live_step(self):
        self.set_status('live')
        page = self.client.get(self.url('assessment_setup'))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['live_mode'])
        self.assertContains(page, 'This session is live.')
        self.assertContains(page, 'Back to live control')
        self.assertNotContains(page, 'id="go-live-section"')
        self.assertNotContains(page, 'Ready to go live?')

    def test_a_draft_opens_its_setup_and_can_choose_the_step(self):
        for step, expected in (('rubric', 'rubric'), ('basics', 'basics'), ('nonsense', 'groups'), (None, 'groups')):
            page = self.client.get(self.url('assessment_setup'), {'step': step} if step else {})
            self.assertEqual(page.context['active_step'], expected)
        self.assertContains(self.client.get(self.url('assessment_setup')), 'Ready to go live?')

    def test_a_closed_session_has_no_setup_page(self):
        self.set_status('closed')
        response = self.client.get(self.url('assessment_setup'))
        self.assertRedirects(response, self.url('assessment_session_detail'), fetch_redirect_response=False)
        self.assertTrue(any('read-only record' in m for m in _messages(response)))

    def test_the_live_setup_lists_recent_changes(self):
        self.set_status('live')
        SessionChange.objects.create(session=self.session, by_user=self.lecturer, summary='Added group "Green".')
        page = self.client.get(self.url('assessment_setup'))
        self.assertContains(page, 'Recent changes (1)')
        self.assertContains(page, 'Added group &quot;Green&quot;.')

    def test_the_live_basics_are_all_editable_and_have_no_weight_split(self):
        self.set_status('live')
        page = self.client.get(self.url('assessment_session_edit'))
        for field in ('name', 'identify_by', 'ungraded_penalty'):
            self.assertContains(page, f'name="{field}"')
        self.assertNotContains(page, 'are fixed once a session is live')
        self.assertNotContains(page, 'weight_percent')


class LiveStudentEditTests(LecturerTestCase):

    def setUp(self):
        super().setUp()
        self.set_status('live')

    def test_a_student_can_be_added_and_edited_and_each_edit_is_logged_and_announced(self):
        with mock.patch('assessments.setup_rules.broadcast_session_event') as broadcast:
            self.client.post(self.url('assessment_roster'), {'full_name': 'New One', 'student_id': 'N1'})
            student = Student.objects.get(student_id='N1')
            self.client.post(self.url('assessment_roster_edit', student.pk), {'full_name': 'New Two', 'student_id': 'N1'})
        self.assertEqual([c.summary for c in SessionChange.objects.order_by('pk')],
                         ['Added student New One.', 'Edited student New Two.'])
        self.assertEqual(broadcast.call_count, 2)

    def test_a_student_with_no_grades_can_be_removed(self):
        response = self.client.post(self.url('assessment_roster_delete', self.ann.pk))
        self.assertFalse(Student.objects.filter(pk=self.ann.pk).exists())
        self.assertTrue(any('Student removed' in m for m in _messages(response)))
        self.assertIn('Removed student Ann Able.', SessionChange.objects.get().summary)

    def test_a_student_who_has_graded_cannot_be_removed(self):
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        response = self.client.post(self.url('assessment_roster_delete', self.cat.pk))
        self.assertTrue(Student.objects.filter(pk=self.cat.pk).exists())
        self.assertTrue(any('stay in the record' in m for m in _messages(response)))
        self.assertFalse(SessionChange.objects.exists())

    def test_the_students_panel_is_open_after_a_student_action(self):
        page = self.client.get(self.url('assessment_roster'))
        self.assertContains(page, 'class="collapse show" id="students-body"')


class LiveGroupEditTests(LecturerTestCase):

    def setUp(self):
        super().setUp()
        self.set_status('live')

    def test_a_group_can_be_added_and_edited_while_live(self):
        self.client.post(self.url('assessment_groups'), {'name': 'Green', 'max_size': 3, 'topic': 'T', 'location': 'L'})
        green = PresentationGroup.objects.get(name='Green')
        self.assertEqual((green.topic, green.location, green.order), ('T', 'L', 3))
        self.client.post(self.url('assessment_group_edit', green.pk), {'name': 'Greener', 'max_size': 3})
        self.assertEqual([c.summary for c in SessionChange.objects.order_by('pk')],
                         ['Added group "Green".', 'Edited group "Greener".'])

    def test_members_move_in_and_out_of_a_group_that_has_not_presented(self):
        free = Student.objects.create(assessment_session=self.session, full_name='Free One', student_id='F1')
        url = self.url('assessment_group_members', self.blue.pk)
        self.client.post(url, {'action': 'add', 'student_id': free.pk})
        self.assertTrue(GroupMembership.objects.filter(group=self.blue, student=free).exists())
        self.client.post(url, {'action': 'remove', 'student_id': free.pk})
        self.assertFalse(GroupMembership.objects.filter(group=self.blue, student=free).exists())
        self.assertEqual(SessionChange.objects.count(), 2)

    def test_members_of_a_group_that_has_presented_are_fixed(self):
        PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        free = Student.objects.create(assessment_session=self.session, full_name='Free One', student_id='F1')
        url = self.url('assessment_group_members', self.red.pk)
        response = self.client.post(url, {'action': 'add', 'student_id': free.pk})
        self.assertTrue(any('members are fixed' in m for m in _messages(response)))
        self.client.post(url, {'action': 'remove', 'student_id': self.ann.pk})
        self.assertTrue(GroupMembership.objects.filter(group=self.red, student=self.ann).exists())
        self.assertFalse(GroupMembership.objects.filter(group=self.red, student=free).exists())

    def test_the_drag_and_drop_call_gets_the_reason_back_as_json(self):
        PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        response = self.client.post(self.url('assessment_group_members', self.red.pk),
                                    {'action': 'remove', 'student_id': self.ann.pk}, **AJAX)
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('members are fixed', data['error'])

    def test_a_presented_group_s_card_says_why_it_is_locked(self):
        PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'class="group-lock"')
        self.assertContains(page, 'members are fixed')

    def test_a_group_that_has_not_presented_shows_no_lock_note(self):
        page = self.client.get(self.url('assessment_groups'))
        self.assertNotContains(page, 'class="group-lock"')


class LiveRubricGuardTests(LecturerTestCase):

    def test_the_one_row_rubric_forms_are_refused_while_live_and_the_table_is_offered(self):
        self.set_status('live')
        for response in (
                self.client.post(self.url('assessment_rubric'), {'name': 'X', 'scope': 'group', 'weight': 1}),
                self.client.post(self.url('assessment_rubric_edit', self.quality.pk), {'name': 'X'}),
                self.client.post(self.url('assessment_rubric_delete', self.quality.pk))):
            self.assertRedirects(response, self.url('assessment_rubric'), fetch_redirect_response=False)
            self.assertTrue(any('edit the rubric as a table' in m for m in _messages(response)))
        self.assertEqual(RubricCategory.objects.count(), 2)
        page = self.client.get(self.url('assessment_rubric'))
        self.assertNotContains(page, 'id="rubric-form"')
        self.assertNotContains(page, 'py-0 rubric-edit-btn')
        self.assertContains(page, 'Edit rubric as a table')

    def test_a_draft_still_has_the_one_row_forms(self):
        page = self.client.get(self.url('assessment_rubric'))
        self.assertContains(page, 'id="rubric-form"')
        self.assertContains(page, 'py-0 rubric-edit-btn')


class ClosedSessionIsReadOnlyTests(LecturerTestCase):

    def test_no_setup_write_changes_a_closed_session(self):
        self.set_status('closed')
        before = (Student.objects.count(), PresentationGroup.objects.count(), RubricCategory.objects.count())
        post = [
            ('assessment_session_edit', [], {'name': 'x'}),
            ('assessment_roster', [], {'full_name': 'N', 'student_id': 'N1'}),
            ('assessment_roster_edit', [self.ann.pk], {'full_name': 'Z', 'student_id': 'S1'}),
            ('assessment_roster_delete', [self.ann.pk], {}),
            ('assessment_rubric', [], {'name': 'X', 'scope': 'group', 'weight': 1}),
            ('assessment_rubric_edit', [self.quality.pk], {'name': 'X'}),
            ('assessment_rubric_delete', [self.quality.pk], {}),
            ('assessment_rubric_bulk_delete', [], {'category_ids': [self.quality.pk]}),
            ('assessment_groups', [], {'name': 'G', 'max_size': 2}),
            ('assessment_group_edit', [self.red.pk], {'name': 'G', 'max_size': 2}),
            ('assessment_group_delete', [self.red.pk], {}),
            ('assessment_groups_bulk_delete', [], {'group_ids': [self.red.pk]}),
            ('assessment_group_import', [], {}),
            ('assessment_rubric_import', [], {}),
            ('assessment_rubric_table', [], {'rows-total': '0'}),
        ]
        for name, extra, data in post:
            with self.subTest(name):
                response = self.client.post(self.url(name, *extra), data)
                self.assertRedirects(response, self.url('assessment_session_detail'), fetch_redirect_response=False)
        response = self.client.post(self.url('assessment_group_members', self.red.pk),
                                    {'action': 'remove', 'student_id': self.ann.pk})
        self.assertTrue(GroupMembership.objects.filter(student=self.ann).exists())
        self.assertEqual(before, (Student.objects.count(), PresentationGroup.objects.count(), RubricCategory.objects.count()))
        self.session.refresh_from_db()
        self.assertEqual(self.session.name, 'Demo day')

    def test_pages_that_would_edit_a_closed_session_send_the_lecturer_away(self):
        self.set_status('closed')
        for name in ('assessment_roster', 'assessment_rubric', 'assessment_groups'):
            with self.subTest(name):
                self.assertRedirects(self.client.get(self.url(name)), self.url('assessment_session_detail'),
                                     fetch_redirect_response=False)


class TopicAndLocationOnScreenTests(LecturerTestCase):

    def test_the_presenting_group_s_topic_and_location_show_on_the_console_and_the_student_screen(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(topic='Smart maps', location='Room 7')
        self.set_status('live')
        self.active_turn(self.red)
        console = self.client.get(self.url('assessment_session_detail'))
        self.assertContains(console, 'Smart maps')
        self.assertContains(console, 'Room 7')

    def test_a_student_sees_the_topic_of_the_group_they_are_grading(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(topic='Smart maps')
        self.set_status('live')
        self.active_turn(self.red)
        student_client = self.client_class()
        student_client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'S3'})
        page = student_client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(page, 'Red - Smart maps</h3>')
        self.assertNotContains(page, '- Project')
        self.assertContains(page, 'rubric.changed')

    def test_a_group_with_no_topic_is_headed_by_its_name_alone(self):
        self.set_status('live')
        self.active_turn(self.red)
        student_client = self.client_class()
        student_client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'S3'})
        page = student_client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(page, 'Red</h3>')
        self.assertNotContains(page, '- Project')
        self.assertNotContains(page, 'Red - </h3>')

    def test_a_student_waiting_for_their_own_group_sees_where_it_presents(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(topic='Smart maps', location='Room 7')
        self.set_status('live')
        self.active_turn(self.red)
        student_client = self.client_class()
        student_client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'S1'})
        page = student_client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.assertContains(page, 'Smart maps')
        self.assertContains(page, 'Room 7')
