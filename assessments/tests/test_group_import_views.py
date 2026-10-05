"""Importing groups and students through the pages: upload, preview, apply; and deleting groups in bulk."""
from unittest import mock

from django.urls import reverse

from assessments.models import (AssessmentSession, Evaluation, GroupMembership, PresentationGroup,
                                PresentationTurn, SessionChange, Student)
from assessments.tests.test_edges_lecturer_views import LecturerTestCase, _messages

from .xlsx_helpers import GROUP_HEADERS, XLSX, xlsx_upload


def groups_file(*rows, **kwargs):
    return xlsx_upload(GROUP_HEADERS, *rows, **kwargs)


class GroupImportFlowTests(LecturerTestCase):
    """The fixture identifies students by ID, so new students in these files carry one."""

    def upload(self, *rows):
        return self.client.post(self.url('assessment_group_import'), {'file': groups_file(*rows)})

    def test_uploading_a_file_opens_a_review_window_on_the_groups_step_and_saves_nothing(self):
        response = self.upload(['Green', 'Eve Fox, Fay Gold', 'E1, F1', 'Maps', 'Room 7'])
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertFalse(PresentationGroup.objects.filter(name='Green').exists())
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'id="group-import-modal"')
        self.assertContains(page, 'Review import')
        self.assertContains(page, 'Green')
        self.assertContains(page, 'Maps')
        self.assertContains(page, 'Room 7')
        self.assertContains(page, 'Eve Fox')
        self.assertEqual(page.context['import_plan']['summary']['new_students'], 2)
        self.assertContains(page, 'Import 1 group')

    def test_the_buttons_are_in_the_window_s_header_above_everything_that_scrolls(self):
        self.upload(['Green', 'Eve Fox', 'E1'])
        html = self.client.get(self.url('assessment_groups')).content.decode()
        modal = html[html.index('id="group-import-modal"'):]
        self.assertLess(modal.index('value="apply"'), modal.index('class="modal-body"'))
        self.assertLess(modal.index('value="cancel"'), modal.index('class="modal-body"'))

    def test_the_window_cannot_be_dismissed_by_a_stray_click_or_key(self):
        self.upload(['Green', 'Eve Fox', 'E1'])
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'data-bs-backdrop="static" data-bs-keyboard="false"')

    def test_the_window_is_only_on_the_groups_step_and_only_while_a_file_waits(self):
        self.assertNotContains(self.client.get(self.url('assessment_groups')), 'id="group-import-modal"')
        self.upload(['Green', 'Eve Fox', 'E1'])
        self.assertNotContains(self.client.get(self.url('assessment_rubric')), 'id="group-import-modal"')
        self.client.post(self.url('assessment_group_import_preview'), {'action': 'cancel'})
        self.assertNotContains(self.client.get(self.url('assessment_groups')), 'id="group-import-modal"')

    def test_confirming_creates_the_groups_and_students_and_says_so(self):
        self.upload(['Green', 'Eve Fox, Fay Gold', 'E1, F1'], ['Empty', None])
        response = self.client.post(self.url('assessment_group_import_preview'), {'action': 'apply'})
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertEqual(PresentationGroup.objects.get(name='Green').member_count(), 2)
        self.assertTrue(PresentationGroup.objects.filter(name='Empty').exists())
        self.assertTrue(Student.objects.filter(student_id='E1', full_name='Eve Fox').exists())
        self.assertTrue(any('Imported 2 new group(s) and 2 new student(s)' in m for m in _messages(response)))
        self.assertEqual(self.client.get(self.url('assessment_group_import_preview')).status_code, 302)

    def test_cancelling_drops_the_file_and_changes_nothing(self):
        self.upload(['Green', 'Eve Fox', 'E1'])
        response = self.client.post(self.url('assessment_group_import_preview'), {'action': 'cancel'})
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertFalse(PresentationGroup.objects.filter(name='Green').exists())
        self.assertEqual(self.client.get(self.url('assessment_group_import_preview')).status_code, 302)

    def test_problem_groups_are_skipped_and_named_while_the_rest_import(self):
        self.upload(['Green', 'Eve Fox', 'E1'], ['Bad', 'Ann Able', 'S1'])
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'already in &quot;Red&quot;')
        self.assertContains(page, 'Needs attention')
        plan = page.context['import_plan']
        self.assertEqual((plan['summary']['ready'], plan['summary']['skipped']), (1, 1))
        response = self.client.post(self.url('assessment_group_import_preview'), {'action': 'apply'})
        messages = _messages(response)
        self.assertTrue(any('Skipped "Bad"' in m for m in messages))
        self.assertFalse(PresentationGroup.objects.filter(name='Bad').exists())

    def test_rows_that_could_not_be_read_are_listed_on_the_preview(self):
        self.upload([None, 'Orphan Name'], ['Green', 'Eve Fox', 'E1'])
        self.assertContains(self.client.get(self.url('assessment_groups')),
                            'Members are listed but the group name is empty.')

    def test_a_file_with_nothing_new_says_so(self):
        self.upload(['Red', 'Ann Able, Bob Best', 'S1, S2'])
        response = self.client.post(self.url('assessment_group_import_preview'), {'action': 'apply'})
        self.assertTrue(any('Nothing new to import' in m for m in _messages(response)))

    def test_a_file_where_nothing_can_be_imported_has_a_disabled_button(self):
        self.upload(['Bad', 'Ann Able', 'S1'])
        page = self.client.get(self.url('assessment_groups'))
        html = page.content.decode()
        self.assertIn('disabled', html[html.index('value="apply"'):html.index('value="apply"') + 160])

    def test_no_file_or_an_unreadable_one_goes_back_with_a_message(self):
        response = self.client.post(self.url('assessment_group_import'), {})
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertTrue(any('.xlsx' in m or 'required' in m.lower() or 'file' in m.lower() for m in _messages(response)))
        empty = self.client.post(self.url('assessment_group_import'), {'file': xlsx_upload(name='empty.xlsx')})
        self.assertTrue(any('The file is empty.' in m for m in _messages(empty)))
        missing = self.client.post(self.url('assessment_group_import'),
                                   {'file': xlsx_upload(['Colour'], ['red'], name='x.xlsx')})
        self.assertTrue(any('Group and Name columns' in m for m in _messages(missing)))

    def test_the_old_preview_address_just_leads_to_the_window_on_the_groups_step(self):
        self.upload(['Green', 'Eve Fox', 'E1'])
        response = self.client.get(self.url('assessment_group_import_preview'))
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertContains(self.client.get(self.url('assessment_groups')), 'id="group-import-modal"')

    def test_opening_the_preview_without_a_file_goes_back_to_setup(self):
        response = self.client.get(self.url('assessment_group_import_preview'))
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertTrue(any('Choose a file' in m for m in _messages(response)))

    def test_a_file_chosen_for_another_session_is_not_applied_here(self):
        self.upload(['Green', 'Eve Fox', 'E1'])
        other = AssessmentSession.objects.create(organization_id=self.org.pk, name='Other', created_by=self.lecturer)
        response = self.client.post(reverse('assessment_group_import_preview', args=[other.pk]), {'action': 'apply'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PresentationGroup.objects.filter(name='Green').exists())

    def test_the_sample_file_downloads_as_a_workbook_that_imports_cleanly(self):
        response = self.client.get(self.url('assessment_group_import_sample'))
        self.assertEqual(response['Content-Type'], XLSX)
        self.assertIn('attachment', response['Content-Disposition'])
        self.session.identify_by = 'full_name'
        self.session.save()
        from io import BytesIO

        from assessments.group_import import parse_groups
        self.assertEqual(len(parse_groups(BytesIO(response.content))['groups']), 3)

    def test_a_live_session_can_take_an_import_and_it_is_logged(self):
        self.set_status('live')
        self.upload(['Green', 'Eve Fox', 'E1'])
        with mock.patch('assessments.setup_rules.broadcast_session_event') as broadcast:
            self.client.post(self.url('assessment_group_import_preview'), {'action': 'apply'})
        self.assertIn('Imported 1 group(s) and 1 student(s)', SessionChange.objects.get().summary)
        broadcast.assert_called_once()

    def test_a_closed_session_cannot_be_imported_into(self):
        self.set_status('closed')
        for name, method in (('assessment_group_import', 'post'), ('assessment_group_import_preview', 'get'),
                             ('assessment_group_import_preview', 'post')):
            response = getattr(self.client, method)(self.url(name), {'file': groups_file(['Green', 'Eve Fox', 'E1'])})
            self.assertRedirects(response, self.url('assessment_session_detail'), fetch_redirect_response=False)
        self.assertFalse(PresentationGroup.objects.filter(name='Green').exists())

    def test_the_groups_page_offers_the_one_file_import_and_no_roster_import(self):
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'Import groups &amp; students')
        self.assertContains(page, 'Manage students')
        self.assertContains(page, self.url('assessment_group_import_sample'))
        self.assertNotContains(page, 'roster/import')


class LandingPageMatchesTheFlowTests(LecturerTestCase):
    """The landing page describes the setup as it works now, not as it once did."""

    def setUp(self):
        super().setUp()
        self.html = self.client_class().get('/').content.decode()

    def test_the_first_step_is_one_file_for_groups_and_students_with_no_separate_roster(self):
        self.assertIn('Create a session and import your groups and students from one Excel file.', self.html)
        self.assertNotIn('class roster', self.html)

    def test_the_second_step_offers_the_rubric_by_hand_or_from_excel_with_the_weights_checked(self):
        self.assertIn('Build your rubric by hand or import it from Excel. We check the weights add up.', self.html)
        self.assertNotIn('Add your rubric and presentation groups', self.html)

    def test_there_are_still_four_steps_in_order(self):
        steps = self.html.count('class="lp-step"')
        self.assertEqual(steps, 4)
        self.assertLess(self.html.index('<b>1</b>'), self.html.index('<b>4</b>'))


class BulkDeleteTests(LecturerTestCase):

    def delete(self, *groups):
        return self.client.post(self.url('assessment_groups_bulk_delete'), {'group_ids': [g.pk for g in groups]})

    def test_one_group_goes_with_its_students(self):
        response = self.delete(self.red)
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertFalse(PresentationGroup.objects.filter(pk=self.red.pk).exists())
        self.assertFalse(Student.objects.filter(pk__in=[self.ann.pk, self.bob.pk]).exists())
        self.assertTrue(PresentationGroup.objects.filter(pk=self.blue.pk).exists())
        self.assertTrue(any('Deleted 1 group and 2 students' in m for m in _messages(response)))

    def test_all_groups_can_be_deleted_at_once(self):
        response = self.delete(self.red, self.blue)
        self.assertEqual((PresentationGroup.objects.count(), Student.objects.count()), (0, 0))
        self.assertTrue(any('Deleted 2 groups and 4 students' in m for m in _messages(response)))

    def test_nothing_selected_is_an_error_not_a_silent_success(self):
        response = self.client.post(self.url('assessment_groups_bulk_delete'), {})
        self.assertTrue(any('Select at least one group' in m for m in _messages(response)))
        self.assertEqual(PresentationGroup.objects.count(), 2)

    def test_junk_ids_and_other_sessions_groups_are_ignored(self):
        other = AssessmentSession.objects.create(organization_id=self.org.pk, name='Other', created_by=self.lecturer)
        foreign = PresentationGroup.objects.create(assessment_session=other, name='Foreign')
        response = self.client.post(self.url('assessment_groups_bulk_delete'), {'group_ids': ['x', str(foreign.pk)]})
        self.assertTrue(any('Select at least one group' in m for m in _messages(response)))
        self.assertTrue(PresentationGroup.objects.filter(pk=foreign.pk).exists())

    def test_a_group_that_has_presented_is_kept_and_the_reason_given(self):
        PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        response = self.delete(self.red, self.blue)
        messages = _messages(response)
        self.assertTrue(PresentationGroup.objects.filter(pk=self.red.pk).exists())
        self.assertFalse(PresentationGroup.objects.filter(pk=self.blue.pk).exists())
        self.assertTrue(any('stays in the record' in m for m in messages))
        self.assertTrue(any('Deleted 1 group and 2 students' in m for m in messages))

    def test_a_group_whose_student_already_graded_is_kept(self):
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        response = self.delete(self.blue)
        self.assertTrue(PresentationGroup.objects.filter(pk=self.blue.pk).exists())
        self.assertTrue(any('erase those grades' in m for m in _messages(response)))

    def test_long_lists_of_deleted_and_kept_groups_are_summarised(self):
        kept, gone = [], []
        for number in range(7):
            group = PresentationGroup.objects.create(assessment_session=self.session, name=f'K{number}', order=10 + number)
            PresentationTurn.objects.create(assessment_session=self.session, group=group, status='closed')
            kept.append(group)
            gone.append(PresentationGroup.objects.create(assessment_session=self.session, name=f'D{number}', order=20 + number))
        self.set_status('live')
        response = self.delete(*kept, *gone)
        messages = _messages(response)
        self.assertTrue(any('and 2 more group(s) could not be deleted' in m for m in messages))
        self.assertIn('and 2 more', SessionChange.objects.get().summary)

    def test_the_old_one_group_endpoint_follows_the_same_rules(self):
        response = self.client.post(self.url('assessment_group_delete', self.red.pk))
        self.assertRedirects(response, self.url('assessment_groups'), fetch_redirect_response=False)
        self.assertFalse(Student.objects.filter(pk=self.ann.pk).exists())

    def test_a_closed_session_cannot_lose_groups(self):
        self.set_status('closed')
        for response in (self.delete(self.red), self.client.post(self.url('assessment_group_delete', self.red.pk))):
            self.assertRedirects(response, self.url('assessment_session_detail'), fetch_redirect_response=False)
        self.assertEqual(PresentationGroup.objects.count(), 2)

    def test_the_cards_have_a_checkbox_each_a_select_all_and_no_delete_button_of_their_own(self):
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'id="groups-select-all"')
        self.assertContains(page, 'class="form-check-input group-select group-select-box"', count=2)
        self.assertContains(page, self.url('assessment_groups_bulk_delete'))
        self.assertNotContains(page, self.url('assessment_group_delete', self.red.pk))

    def test_a_protected_group_s_checkbox_is_disabled_with_the_reason(self):
        PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'has presented or is presenting, so it stays in the record.')
        self.assertContains(page, 'disabled title=')

    def test_topic_and_location_show_on_the_card_and_can_be_edited(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(topic='Maps', location='Room 7')
        page = self.client.get(self.url('assessment_groups'))
        self.assertContains(page, 'Maps')
        self.assertContains(page, 'Room 7')
        self.client.post(self.url('assessment_group_edit', self.red.pk),
                         {'name': 'Red', 'max_size': 5, 'topic': 'New topic', 'location': 'Hall'})
        self.red.refresh_from_db()
        self.assertEqual((self.red.topic, self.red.location), ('New topic', 'Hall'))
