"""The lecturer's console: setup steps, live control and reporting, driven
through the real views as an organization admin.

Each test names a path the main flow tests walk past - a refusal, a message,
a fragment returned to the page's own scripts. Scheduling is replaced, since
nothing here may need a broker, and what is checked is what the view stores
and says.
"""
import datetime
from io import BytesIO
from unittest import mock

import openpyxl
from accounts.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_user, sign_in
from assessments.models import (AssessmentSession, Evaluation, GroupMembership,
                                ParticipationRecord, PresentationGroup,
                                PresentationTurn, RubricCategory, Student)
from assessments.tests.test_edges_core import SessionFixture

AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def upload(buffer, name='file.xlsx'):
    return SimpleUploadedFile(
        name, buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class LecturerTestCase(SessionFixture):

    def setUp(self):
        super().setUp()
        self.lecturer = make_user('lect')
        sign_in(self.client, self.lecturer, self.org)
        # Sessions are private to their creator: this lecturer owns the fixture's.
        AssessmentSession.objects.filter(pk=self.session.pk).update(created_by=self.lecturer)
        self.session.refresh_from_db()
        for target in ('close_voting_window', 'auto_advance_turn', 'activate_pending_group',
                       'open_scheduled_voting', 'finish_presentation'):
            patcher = mock.patch(f'assessments.tasks.{target}.apply_async')
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, name, *extra):
        return reverse(name, args=[self.session.pk, *extra])

    def set_status(self, status):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status=status)
        self.session.refresh_from_db()


class SessionPagesTests(LecturerTestCase):

    def test_live_sessions_lead_the_list_with_who_is_presenting_and_who_joined(self):
        self.set_status('live')
        self.active_turn(self.red)
        ParticipationRecord.objects.create(assessment_session=self.session, student=self.cat)
        draft = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Later', created_by=self.lecturer)
        response = self.client.get(reverse('assessment_session_list'))
        sessions = response.context['sessions']
        self.assertEqual(sessions[0], self.session)
        self.assertEqual(sessions[0].live_active_group, self.red)
        self.assertEqual(sessions[0].live_joined_count, 1)
        self.assertEqual(sessions[1], draft)

    def test_a_new_session_is_created_in_the_lecturers_organization(self):
        response = self.client.post(reverse('assessment_session_create'), {
            'name': 'Fresh', 'identify_by': 'full_name',
            'group_weight_percent': '60', 'individual_weight_percent': '40'})
        created = AssessmentSession.objects.get(name='Fresh')
        self.assertRedirects(response, reverse('assessment_groups', args=[created.pk]),
                             fetch_redirect_response=False)
        self.assertEqual((created.organization_id, created.created_by), (self.org.pk, self.lecturer))

    def test_an_invalid_new_session_is_shown_again(self):
        response = self.client.post(reverse('assessment_session_create'), {'name': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssessmentSession.objects.filter(name='').exists())

    def test_the_new_session_form_opens(self):
        self.assertEqual(self.client.get(reverse('assessment_session_create')).status_code, 200)

    def test_a_live_session_can_be_renamed_but_not_have_its_settings_changed(self):
        self.set_status('live')
        response = self.client.post(self.url('assessment_session_edit'), {
            'name': 'Renamed live', 'identify_by': 'full_name',
            'group_weight_percent': '10', 'individual_weight_percent': '90'})
        self.assertRedirects(response, self.url('assessment_session_detail'),
                             fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.name, 'Renamed live')
        self.assertEqual((self.session.identify_by, self.session.group_weight_percent),
                         ('student_id', 60))
        self.assertEqual(self.client.get(self.url('assessment_session_edit')).status_code, 200)

    def test_a_closed_session_cannot_be_edited_at_all(self):
        self.set_status('closed')
        response = self.client.post(self.url('assessment_session_edit'), {'name': 'Sneaky'})
        self.assertRedirects(response, self.url('assessment_session_detail'),
                             fetch_redirect_response=False)
        self.assertTrue(any('read-only record' in m for m in _messages(response)))
        self.session.refresh_from_db()
        self.assertEqual(self.session.name, 'Demo day')

    def test_a_draft_session_can_be_edited(self):
        url = self.url('assessment_session_edit')
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {'name': ''}).status_code, 200)
        response = self.client.post(url, {
            'name': 'Renamed', 'identify_by': 'student_id',
            'group_weight_percent': '50', 'individual_weight_percent': '50'})
        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.name, 'Renamed')

    def test_a_draft_detail_opens_on_the_next_unfinished_step(self):
        steps = {}
        steps['complete'] = self.client.get(self.url('assessment_session_detail')).context['active_step']
        RubricCategory.objects.all().delete()
        steps['no rubric'] = self.client.get(self.url('assessment_session_detail')).context['active_step']
        PresentationGroup.objects.all().delete()
        steps['no groups'] = self.client.get(self.url('assessment_session_detail')).context['active_step']
        self.assertEqual(steps, {'complete': 'go-live', 'no rubric': 'rubric', 'no groups': 'groups'})


class RosterViewTests(LecturerTestCase):

    def test_adding_a_student_with_a_missing_required_field_is_reported(self):
        # This session identifies by student ID, so a name alone is refused.
        response = self.client.post(self.url('assessment_roster'), {'full_name': 'No Id'})
        self.assertTrue(any('student ID' in m for m in _messages(response)))
        self.assertFalse(Student.objects.filter(full_name='No Id').exists())

    def test_adding_a_student_with_a_valid_row(self):
        response = self.client.post(self.url('assessment_roster'), {
            'full_name': 'New One', 'student_id': 'S9'})
        self.assertRedirects(response, self.url('assessment_roster'), fetch_redirect_response=False)
        self.assertTrue(Student.objects.filter(student_id='S9').exists())

    def test_the_search_returns_fragments(self):
        data = self.client.get(self.url('assessment_roster_search'), {'q': 'Ann'}).json()
        self.assertIn('table_html', data)
        self.assertIn('count_html', data)

    def test_editing_a_student(self):
        url = self.url('assessment_roster_edit', self.ann.pk)
        self.client.post(url, {'full_name': 'Ann Renamed', 'student_id': 'S1'})
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.full_name, 'Ann Renamed')

    def test_an_edit_that_breaks_a_rule_is_reported_and_not_saved(self):
        response = self.client.post(self.url('assessment_roster_edit', self.ann.pk),
                                    {'full_name': 'Ann Able', 'student_id': ''})
        self.assertTrue(any('student ID' in m for m in _messages(response)))
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.student_id, 'S1')

    def test_an_edit_that_fails_the_form_reports_each_error(self):
        response = self.client.post(self.url('assessment_roster_edit', self.ann.pk),
                                    {'full_name': 'x' * 400, 'student_id': 'S1'})
        self.assertTrue(_messages(response))
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.full_name, 'Ann Able')

    def test_removing_a_student(self):
        response = self.client.post(self.url('assessment_roster_delete', self.ann.pk))
        self.assertRedirects(response, self.url('assessment_roster'), fetch_redirect_response=False)
        self.assertFalse(Student.objects.filter(pk=self.ann.pk).exists())

    def test_a_student_of_another_session_cannot_be_edited_or_removed_through_this_one(self):
        from assessments.models import AssessmentSession as Session
        other = Session.objects.create(organization_id=self.org.pk, name='Other')
        outsider = self.student('Out Sider', 'X1', session=other)
        self.assertEqual(
            self.client.post(self.url('assessment_roster_delete', outsider.pk)).status_code, 404)
        self.assertEqual(
            self.client.post(self.url('assessment_roster_edit', outsider.pk), {}).status_code, 404)
        self.assertTrue(Student.objects.filter(pk=outsider.pk).exists())



class RubricViewTests(LecturerTestCase):

    def test_a_category_that_breaks_the_budget_reports_why(self):
        response = self.client.post(self.url('assessment_rubric_edit', self.quality.pk), {
            'name': 'Quality', 'scope': 'group', 'max_points': '10', 'weight': '99'})
        self.assertTrue(_messages(response))
        self.quality.refresh_from_db()
        self.assertEqual(self.quality.weight, 60)

    def test_a_category_can_be_removed(self):
        self.client.post(self.url('assessment_rubric_delete', self.teamwork.pk))
        self.assertFalse(RubricCategory.objects.filter(pk=self.teamwork.pk).exists())


class GroupViewTests(LecturerTestCase):

    def setUp(self):
        super().setUp()
        GroupMembership.objects.all().delete()
        PresentationGroup.objects.all().delete()

    def create(self, **data):
        return self.client.post(self.url('assessment_groups'), data)

    def test_a_group_cannot_take_students_who_are_no_longer_unassigned(self):
        self.group('Taken', 1, self.ann)
        response = self.create(name='New', max_size='2', members=[str(self.ann.pk)])
        self.assertTrue(any('no longer unassigned' in m for m in _messages(response)))
        self.assertFalse(PresentationGroup.objects.filter(name='New').exists())

    def test_a_group_cannot_start_over_its_own_size_limit(self):
        response = self.create(name='Tiny', max_size='1',
                               members=[str(self.bob.pk), str(self.cat.pk)])
        self.assertTrue(any('set the group size to 1' in m for m in _messages(response)))
        self.assertFalse(PresentationGroup.objects.filter(name='Tiny').exists())

    def test_a_group_is_created_with_its_members_at_the_end_of_the_order(self):
        self.create(name='First', max_size='2', members=[str(self.bob.pk)])
        response = self.create(name='Second', max_size='2')
        first, second = PresentationGroup.objects.get(name='First'), PresentationGroup.objects.get(name='Second')
        self.assertEqual((first.order, second.order), (1, 2))
        self.assertEqual(first.member_count(), 1)
        self.assertTrue(any('"Second" created.' in m for m in _messages(response)))

    def test_an_invalid_new_group_is_shown_again(self):
        self.assertEqual(self.create(name='').status_code, 200)

    def test_students_are_moved_into_and_out_of_a_group(self):
        group = self.group('Red', 1)
        url = self.url('assessment_group_members', group.pk)
        self.client.post(url, {'action': 'add', 'student_id': self.ann.pk})
        self.assertEqual(group.member_count(), 1)
        self.client.post(url, {'action': 'remove', 'student_id': self.ann.pk})
        self.assertEqual(group.member_count(), 0)

    def test_moving_a_student_by_drag_and_drop_answers_with_fragments(self):
        group = self.group('Red', 1)
        data = self.client.post(
            self.url('assessment_group_members', group.pk),
            {'action': 'add', 'student_id': self.ann.pk}, **AJAX).json()
        self.assertTrue(data['ok'])
        for fragment in ('group_html', 'unassigned_html', 'stats_html'):
            self.assertIn(fragment, data)

    def test_a_refused_move_says_why_in_json_and_in_the_page(self):
        full = self.group('Full', 1, self.ann)
        PresentationGroup.objects.filter(pk=full.pk).update(max_size=1)
        url = self.url('assessment_group_members', full.pk)
        data = self.client.post(url, {'action': 'add', 'student_id': self.bob.pk}, **AJAX).json()
        self.assertFalse(data['ok'])
        self.assertIn('already full', data['error'])
        plain = self.client.post(url, {'action': 'add', 'student_id': self.bob.pk})
        self.assertTrue(any('already full' in m for m in _messages(plain)))

    def test_an_unknown_action_changes_nothing(self):
        group = self.group('Red', 1, self.ann)
        self.client.post(self.url('assessment_group_members', group.pk),
                         {'action': 'shuffle', 'student_id': self.ann.pk})
        self.assertEqual(group.member_count(), 1)

    def test_a_student_of_another_session_cannot_be_moved_in(self):
        other = AssessmentSession.objects.create(organization_id=self.org.pk, name='Other')
        outsider = self.student('Out Sider', 'X1', session=other)
        group = self.group('Red', 1)
        response = self.client.post(self.url('assessment_group_members', group.pk),
                                    {'action': 'add', 'student_id': outsider.pk})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(group.member_count(), 0)

    def test_editing_a_group_and_deleting_it(self):
        group = self.group('Red', 1)
        self.client.post(self.url('assessment_group_edit', group.pk), {'name': 'Crimson', 'max_size': '2'})
        group.refresh_from_db()
        self.assertEqual(group.name, 'Crimson')
        bad = self.client.post(self.url('assessment_group_edit', group.pk), {'name': ''})
        self.assertTrue(_messages(bad))
        self.client.post(self.url('assessment_group_delete', group.pk))
        self.assertFalse(PresentationGroup.objects.filter(pk=group.pk).exists())


class LiveControlTests(LecturerTestCase):

    def setUp(self):
        super().setUp()
        self.set_status('live')

    def test_a_session_that_is_not_ready_cannot_go_live(self):
        self.set_status('draft')
        RubricCategory.objects.all().delete()
        response = self.client.post(self.url('assessment_go_live'))
        self.assertTrue(any('Add at least one rubric category' in m for m in _messages(response)))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'draft')

    def test_a_ready_session_goes_live(self):
        self.set_status('draft')
        response = self.client.post(self.url('assessment_go_live'))
        self.assertTrue(any('now live' in m for m in _messages(response)))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'live')

    def test_activating_a_group_with_a_timer_says_when_voting_closes(self):
        response = self.client.post(
            self.url('assessment_activate_group', self.red.pk),
            {'duration_seconds': '90', 'gap_seconds': '5', 'presentation_seconds': ''})
        self.assertTrue(any('1m 30s' in m for m in _messages(response)))
        self.session.refresh_from_db()
        self.assertEqual((self.session.default_turn_seconds, self.session.transition_gap_seconds),
                         (90, 5))

    def test_activating_a_group_with_no_timer(self):
        response = self.client.post(self.url('assessment_activate_group', self.red.pk), {})
        self.assertTrue(any('is now presenting.' in m for m in _messages(response)))
        self.assertEqual(PresentationTurn.objects.get().group, self.red)

    def test_a_group_of_another_session_cannot_be_activated(self):
        other = AssessmentSession.objects.create(organization_id=self.org.pk, name='Other')
        foreign = PresentationGroup.objects.create(assessment_session=other, name='F')
        response = self.client.post(self.url('assessment_activate_group', foreign.pk), {})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(PresentationTurn.objects.exists())

    def test_the_default_timers_are_saved_and_junk_clears_them(self):
        data = self.client.post(self.url('assessment_set_presentation_timer'),
                                {'presentation_seconds': '120'}).json()
        self.assertEqual(data['presentation_seconds'], 120)
        data = self.client.post(self.url('assessment_set_presentation_timer'),
                                {'presentation_seconds': 'soon'}).json()
        self.assertIsNone(data['presentation_seconds'])
        data = self.client.post(self.url('assessment_set_default_turn_seconds'),
                                {'duration_seconds': '45'}).json()
        self.assertEqual(data['duration_seconds'], 45)
        data = self.client.post(self.url('assessment_set_transition_gap'),
                                {'gap_seconds': 'x'}).json()
        self.assertEqual(data['gap_seconds'], 0)

    def test_pausing_and_resuming_voting(self):
        self.active_turn(self.red, voting_ends_at=timezone.now() + datetime.timedelta(minutes=1))
        paused = self.client.post(self.url('assessment_toggle_pause'))
        self.assertTrue(any('Voting paused.' in m for m in _messages(paused)))
        turn = PresentationTurn.objects.get()
        self.assertIsNone(turn.voting_ends_at)
        resumed = self.client.post(self.url('assessment_toggle_pause'))
        self.assertTrue(any('Voting resumed.' in m for m in _messages(resumed)))
        turn.refresh_from_db()
        self.assertIsNotNone(turn.voting_ends_at)

    def test_locking_the_room_by_page_and_by_script(self):
        page = self.client.post(self.url('assessment_toggle_joining'))
        self.assertTrue(any('New joining is locked' in m for m in _messages(page)))
        script = self.client.post(self.url('assessment_toggle_joining'), **AJAX).json()
        self.assertEqual((script['ok'], script['locked']), (True, False))
        again = self.client.post(self.url('assessment_toggle_joining'))
        self.assertTrue(any('New joining is locked' in m for m in _messages(again)))
        final = self.client.post(self.url('assessment_toggle_joining'))
        self.assertTrue(any('New joining is open' in m for m in _messages(final)))

    def test_designating_the_next_group_by_page_and_by_script(self):
        url = self.url('assessment_set_next_group')
        page = self.client.post(url, {'group_id': self.blue.pk})
        self.assertTrue(any('designated as next up' in m for m in _messages(page)))
        script = self.client.post(url, {'group_id': self.blue.pk}, **AJAX).json()
        self.assertEqual(script['group_name'], 'Blue')
        self.session.refresh_from_db()
        self.assertEqual(self.session.designated_next_group, self.blue)

    def test_clearing_the_designation_by_page_and_by_script(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(designated_next_group=self.blue)
        url = self.url('assessment_set_next_group')
        script = self.client.post(url, {'group_id': ''}, **AJAX).json()
        self.assertTrue(script['cleared'])
        page = self.client.post(url, {'group_id': ''})
        self.assertTrue(any('reset to default' in m for m in _messages(page)))
        self.session.refresh_from_db()
        self.assertIsNone(self.session.designated_next_group)

    def test_a_group_of_another_session_cannot_be_designated(self):
        other = AssessmentSession.objects.create(organization_id=self.org.pk, name='Other')
        foreign = PresentationGroup.objects.create(assessment_session=other, name='F')
        response = self.client.post(self.url('assessment_set_next_group'), {'group_id': foreign.pk})
        self.assertEqual(response.status_code, 404)

    def test_adjusting_the_timer_needs_a_number_and_a_running_group(self):
        url = self.url('assessment_adjust_timer')
        junk = self.client.post(url, {'delta_seconds': 'lots'})
        self.assertTrue(any('Invalid time adjustment' in m for m in _messages(junk)))
        nobody = self.client.post(url, {'delta_seconds': '30'})
        self.assertTrue(any('No group is currently presenting' in m for m in _messages(nobody)))

    def test_adjusting_a_turn_with_no_timer_says_there_is_nothing_to_adjust(self):
        self.active_turn(self.red)
        response = self.client.post(self.url('assessment_adjust_timer'), {'delta_seconds': '30'})
        self.assertTrue(any('no running timer to adjust' in m for m in _messages(response)))

    def test_extending_reducing_and_ending_the_timer(self):
        self.active_turn(self.red, voting_ends_at=timezone.now() + datetime.timedelta(minutes=2))
        url = self.url('assessment_adjust_timer')
        longer = self.client.post(url, {'delta_seconds': '30'})
        self.assertTrue(any('extended by 30s' in m for m in _messages(longer)))
        shorter = self.client.post(url, {'delta_seconds': '-30'})
        self.assertTrue(any('reduced by 30s' in m for m in _messages(shorter)))
        over = self.client.post(url, {'delta_seconds': '-3600'})
        self.assertTrue(any('Timer ended' in m for m in _messages(over)))

    def test_closing_voting_and_closing_the_session(self):
        turn = self.active_turn(self.red)
        closed = self.client.post(self.url('assessment_close_turn'))
        self.assertTrue(any('Voting closed for "Red"' in m for m in _messages(closed)))
        turn.refresh_from_db()
        self.assertEqual(turn.status, 'closed')
        nothing = self.client.post(self.url('assessment_close_turn'))
        self.assertEqual(nothing.status_code, 302)
        done = self.client.post(self.url('assessment_close_session'))
        self.assertRedirects(done, self.url('assessment_results'), fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'closed')
        self.assertIsNotNone(self.session.closed_at)


class ReportingTests(LecturerTestCase):

    def vote(self, voter, group):
        turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=group, status='closed',
            opened_at=timezone.now(), closed_at=timezone.now())
        Evaluation.objects.create(presentation_turn=turn, evaluator=voter)

    def test_participation_can_be_filtered_by_status(self):
        self.vote(self.cat, self.red)
        for status, expected in (('full', {'Cat Cole'}), ('partial', set()),
                                 ('none', {'Ann Able', 'Bob Best', 'Dan Dyer'})):
            with self.subTest(status=status):
                rows = self.client.get(self.url('assessment_participation'),
                                       {'status': status}).context['rows']
                self.assertEqual({r['student'].full_name for r in rows}, expected)

    def test_somebody_who_graded_some_but_not_all_is_partial(self):
        self.group('Green', 3)
        self.vote(self.cat, self.red)
        rows = self.client.get(self.url('assessment_participation'),
                               {'status': 'partial'}).context['rows']
        self.assertEqual({r['student'].full_name for r in rows}, {'Cat Cole'})

    def test_the_participation_refresh_is_json(self):
        data = self.client.get(self.url('assessment_participation_refresh')).json()
        self.assertEqual(data['status'], 'draft')
        self.assertIn('cards_html', data)

    def test_results_sort_in_the_direction_asked_for(self):
        url = self.url('assessment_results')
        newest = self.client.get(url, {'sort': 'student', 'dir': 'desc'}).context
        self.assertEqual((newest['sort'], newest['dir']), ('student', 'desc'))
        names = [r['student'].full_name for r in newest['student_rows']]
        self.assertEqual(names, sorted(names, reverse=True))
        default = self.client.get(url).context
        self.assertEqual(default['dir'], 'desc')
        by_group = self.client.get(url, {'sort': 'group'}).context
        self.assertEqual(by_group['dir'], 'asc')

    def test_the_results_refresh_is_json_with_chart_data(self):
        data = self.client.get(self.url('assessment_results_refresh')).json()
        self.assertEqual(data['group_chart']['labels'], ['Red', 'Blue'])
        self.assertEqual(len(data['distribution_buckets']), 10)

    def test_the_export_is_a_workbook_even_for_an_awkward_session_name(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(name='Demo "day"\nTwo')
        response = self.client.get(self.url('assessment_results_export'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('\n', response['Content-Disposition'])
        self.assertEqual(response['Content-Disposition'],
                         'attachment; filename="Demo day Two-results.xlsx"')
        book = openpyxl.load_workbook(BytesIO(response.content))
        self.assertEqual(book.sheetnames, ['Participation', 'Results', 'Group scores'])


class LecturerViewGapsTests(LecturerTestCase):

    def test_a_closed_turn_with_no_start_time_still_appears_in_the_activity(self):
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.red, status='closed',
            opened_at=None, closed_at=timezone.now())
        self.set_status('closed')
        response = self.client.get(self.url('assessment_session_detail'))
        self.assertEqual(response.context['presentation_timeline'][0]['duration_label'], None)
        self.assertIsNone(response.context['avg_turn_seconds'])

    def test_an_edit_that_the_model_refuses_after_the_form_accepts_it_is_reported(self):
        from django.core.exceptions import ValidationError

        def refuse(*args, **kwargs):
            if 'exclude' in kwargs:        # the form's own pass
                return None
            raise ValidationError('The roster is locked.')

        with mock.patch.object(Student, 'full_clean', refuse):
            response = self.client.post(self.url('assessment_roster_edit', self.ann.pk),
                                        {'full_name': 'Ann Changed', 'student_id': 'S1'})
        self.assertTrue(any('The roster is locked.' in m for m in _messages(response)))
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.full_name, 'Ann Able')



class UploadViewSafetyTests(LecturerTestCase):

    def test_a_corrupt_workbook_gets_a_message_not_a_server_error(self):
        for name in ('assessment_group_import', 'assessment_rubric_import'):
            with self.subTest(view=name):
                response = self.client.post(self.url(name),
                                            {'file': upload(BytesIO(b'not a workbook')), 'mode': 'replace'})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(any('could not be read' in m for m in _messages(response)))

    def test_a_file_that_is_not_an_xlsx_is_refused_before_it_is_opened(self):
        for view, patched in (('assessment_group_import', 'assessments.views.setup_views.group_import.parse_groups'),
                              ('assessment_rubric_import', 'assessments.views.setup_views.rubric_import.parse_rubric')):
            for name in ('groups.exe', 'groups.csv', 'groups.xlsx.exe'):
                with self.subTest(view=view, name=name):
                    with mock.patch(patched) as reader:
                        response = self.client.post(
                            self.url(view), {'file': SimpleUploadedFile(name, b'x'), 'mode': 'replace'})
                    reader.assert_not_called()
                    self.assertTrue(any('.xlsx' in m for m in _messages(response)))
