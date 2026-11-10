"""Classroom dashboard and session-browser data are scoped and data-driven."""
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession, GroupMembership, ParticipationRecord, PresentationGroup,
    PresentationTurn, Student,
)
from assessments.views.lecturer_views import assessment_dashboard


class ClassroomWorkspaceUiTests(TestCase):

    def setUp(self):
        self.org = make_org('Classroom UI')
        self.user = make_user('classroom-ui-owner')
        self.other = make_user('classroom-ui-other', first_name='Ben')
        sign_in(self.client, self.user, self.org)

    def create_session(self, name, status=AssessmentSession.Status.DRAFT, owner=None, service='classroom'):
        return AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=owner or self.user,
            name=name, status=status, service=service)

    def test_dashboard_metrics_and_visuals_are_derived_from_classroom_records(self):
        live = self.create_session('Live anatomy', AssessmentSession.Status.LIVE)
        draft = self.create_session('Draft biology')
        closed = self.create_session('Completed chemistry', AssessmentSession.Status.CLOSED)
        live_students = [Student.objects.create(assessment_session=live, full_name=f'Live {i}')
                         for i in range(2)]
        Student.objects.create(assessment_session=draft, full_name='Draft learner')
        Student.objects.create(assessment_session=closed, full_name='Closed learner')
        live_group = PresentationGroup.objects.create(assessment_session=live, name='Live group')
        PresentationGroup.objects.create(assessment_session=live, name='Practice group', is_practice=True)
        PresentationGroup.objects.create(assessment_session=draft, name='Draft group')
        PresentationTurn.objects.create(assessment_session=live, group=live_group,
            status=PresentationTurn.Status.ACTIVE)
        ParticipationRecord.objects.create(assessment_session=live, student=live_students[0])

        response = self.client.get(reverse('assessment_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<span class="topbar-title">Classroom dashboard</span>', html=False)
        self.assertNotContains(response, 'CLASSROOM OVERVIEW')
        self.assertNotContains(response, '<h1>Classroom dashboard</h1>', html=False)
        self.assertContains(response, 'A live summary of your sessions, rosters, and current classroom activity.')
        stats = response.context['dashboard_stats']
        self.assertEqual(stats, {
            'total': 3, 'live_count': 1, 'draft_count': 1, 'closed_count': 1,
            'student_count': 4, 'group_count': 2,
            'live_joined_count': 1, 'live_roster_count': 2,
        })
        distribution = {item['key']: item for item in response.context['stage_distribution']}
        self.assertEqual({key: value['count'] for key, value in distribution.items()},
                         {'live': 1, 'draft': 1, 'closed': 1})
        self.assertEqual({key: value['percentage'] for key, value in distribution.items()},
                         {'live': 33.3, 'draft': 33.3, 'closed': 33.3})
        self.assertIn('conic-gradient(', response.context['stage_pie_background'])
        self.assertEqual(response.context['largest_roster'], 2)
        self.assertEqual(response.context['live_sessions'][0].live_active_group, live_group)
        self.assertEqual(response.context['live_sessions'][0].live_joined_count, 1)
        self.assertContains(response, 'Live in the classroom')
        self.assertContains(response, f'class="classroom-live-card-link" href="/classroom/app/{live.pk}/"')
        self.assertContains(response, 'class="classroom-stage-pie"')
        self.assertContains(response, '.classroom-dashboard a,.classroom-dashboard a:hover{text-decoration:none}')
        self.assertContains(response, '0 students')
        self.assertContains(response, '2 students</span>')
        self.assertNotContains(response, 'role="progressbar"')
        self.assertContains(response, 'Students on rosters')
        self.assertNotContains(response, 'Roster ready')
        self.assertNotContains(response, 'Verified')
        self.assertContains(response, 'data-session-row-href="' + reverse('assessment_session_detail', args=[live.pk]) + '"')
        self.assertContains(response, 'data-session-row-href="' + reverse('assessment_session_edit', args=[draft.pk]) + '"')
        self.assertContains(response, 'data-session-row-href="' + reverse('assessment_results', args=[closed.pk]) + '"')
        self.assertContains(response, 'Actions for Live anatomy')
        self.assertContains(response, 'Save as template')
        self.assertContains(response, 'Delete session')
        self.assertContains(response, 'data-confirm-tone="danger"')

    def test_dashboard_is_limited_to_owned_classroom_sessions_in_the_active_organization(self):
        mine = self.create_session('My classroom')
        self.create_session('Colleague classroom', owner=self.other)
        self.create_session('Other service', service='panel')
        other_org = make_org('Different organization')
        AssessmentSession.objects.create(organization_id=other_org.pk, created_by=self.user,
            name='Different organization session')

        response = self.client.get(reverse('assessment_dashboard'))
        self.assertEqual(response.context['dashboard_stats']['total'], 1)
        self.assertEqual([session.pk for session in response.context['recent_sessions']], [mine.pk])

    def test_dashboard_rejects_a_non_classroom_service_context(self):
        request = RequestFactory().get('/classroom/app/')
        request.user = self.user
        request.session = self.client.session
        request.service = 'panel'
        with self.assertRaises(Http404):
            assessment_dashboard(request)

    def test_dashboard_preview_stays_short_and_links_to_the_full_session_list(self):
        for number in range(7):
            self.create_session(f'Session {number}')

        response = self.client.get(reverse('assessment_dashboard'))
        self.assertEqual(len(response.context['recent_sessions']), 5)
        self.assertContains(response, 'View all sessions')
        self.assertContains(response, f'href="{reverse("assessment_session_list")}"')

    def test_empty_dashboard_has_real_zero_state_and_zero_distribution(self):
        response = self.client.get(reverse('assessment_dashboard'))
        self.assertEqual(response.context['dashboard_stats']['total'], 0)
        self.assertEqual(response.context['dashboard_stats']['student_count'], 0)
        self.assertEqual(response.context['stage_distribution'][0]['percentage'], 0)
        self.assertContains(response, 'You haven’t created a classroom session yet')
        self.assertContains(response, 'Create a session')

    def test_session_table_search_status_filter_and_actions(self):
        self.create_session('Biology lab', AssessmentSession.Status.DRAFT)
        self.create_session('Biology showcase', AssessmentSession.Status.CLOSED)
        self.create_session('Chemistry lab', AssessmentSession.Status.LIVE)

        response = self.client.get(reverse('assessment_session_list'), {
            'q': 'Biology', 'status': AssessmentSession.Status.DRAFT,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<span class="topbar-title">Classroom sessions</span>', html=False)
        self.assertContains(response, '<h1 class="visually-hidden">Classroom sessions</h1>', html=False)
        self.assertNotContains(response, '<h1>Classroom sessions</h1>', html=False)
        self.assertEqual([session.name for session in response.context['sessions']], ['Biology lab'])
        self.assertEqual(response.context['status_counts']['total'], 3)
        self.assertEqual(response.context['status_counts']['draft'], 1)
        self.assertContains(response, '<table', html=False)
        self.assertContains(response, 'Search by session name')
        self.assertContains(response, 'data-session-auto-filter')
        self.assertContains(response, 'setTimeout(submitFilters, 350)')
        self.assertContains(response, "status.addEventListener('change', submitFilters)")
        self.assertContains(response, "headers: {'X-Requested-With': 'XMLHttpRequest'}")
        self.assertContains(response, 'results.replaceWith(nextResults)')
        self.assertContains(response, "window.history.replaceState({}, '', url.pathname + url.search + url.hash)")
        self.assertNotContains(response, 'Apply filters')
        self.assertContains(response, 'Actions for Biology lab')
        self.assertContains(response, 'Edit session')
        self.assertContains(response, 'Delete session')
        self.assertContains(response, 'data-confirm-tone="danger"')
        self.assertContains(response, 'data-session-row-href="' + reverse('assessment_session_edit', args=[
            AssessmentSession.objects.get(name='Biology lab').pk]) + '"')
        self.assertContains(response, 'data-session-row-navigation')
        self.assertContains(response, 'data-session-select-all')
        self.assertContains(response, 'data-session-select')
        self.assertContains(response, '.session-select-column .form-check-input{width:1em;height:1em')
        self.assertContains(response, 'border:2px solid var(--text-muted)')
        self.assertContains(response, '.session-table-name:hover{text-decoration:none}')
        self.assertContains(response, 'data-session-page-size')
        self.assertContains(response, 'data-bulk-delete-button hidden')
        self.assertContains(response, 'Delete selected')
        self.assertContains(response, 'data-confirm-title="Delete selected sessions?"')
        self.assertContains(response, '.session-actions,[data-bs-toggle]')
        self.assertContains(response, 'window.location.assign(href)')

        all_rows = self.client.get(reverse('assessment_session_list'))
        self.assertContains(all_rows, 'data-session-row-href="' + reverse('assessment_session_detail', args=[
            AssessmentSession.objects.get(name='Chemistry lab').pk]) + '"')
        self.assertContains(all_rows, 'data-session-row-href="' + reverse('assessment_results', args=[
            AssessmentSession.objects.get(name='Biology showcase').pk]) + '"')

        partial = self.client.get(reverse('assessment_session_list'), {
            'q': 'Biology', 'status': AssessmentSession.Status.DRAFT,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(partial.status_code, 200)
        self.assertIn('X-Requested-With', partial['Vary'])
        self.assertContains(partial, 'id="session-results"')
        self.assertContains(partial, 'Biology lab')
        self.assertNotContains(partial, 'Chemistry lab')
        self.assertNotContains(partial, '<html', html=False)

    def test_session_list_uses_shared_page_size_default_and_options(self):
        for number in range(27):
            self.create_session(f'Paginated session {number:02d}')

        response = self.client.get(reverse('assessment_session_list'))
        self.assertEqual(response.context['per_page'], 25)
        self.assertEqual(response.context['page_sizes'], [25, 50, 100])
        self.assertEqual(response.context['paginator'].num_pages, 2)
        self.assertEqual(len(response.context['sessions']), 25)
        for size in (25, 50, 100):
            self.assertContains(response, f'<option value="{size}"', html=False)
        self.assertNotContains(response, '<option value="10"', html=False)

        larger_page = self.client.get(reverse('assessment_session_list'), {'per_page': 50})
        self.assertEqual(larger_page.context['per_page'], 50)
        self.assertEqual(larger_page.context['paginator'].num_pages, 1)
        self.assertEqual(len(larger_page.context['sessions']), 27)

        invalid_size = self.client.get(reverse('assessment_session_list'), {'per_page': 10})
        self.assertEqual(invalid_size.context['per_page'], 25)

    def test_bulk_delete_removes_only_selected_owned_classroom_sessions(self):
        first = self.create_session('Biology first')
        second = self.create_session('Biology second')
        keep = self.create_session('Keep this session')
        colleague = self.create_session('Colleague session', owner=self.other)
        other_service = self.create_session('Other service', service='panel')

        response = self.client.post(reverse('assessment_session_bulk_delete'), {
            'session_ids': [str(first.pk), str(second.pk)],
            'q': 'Biology', 'status': AssessmentSession.Status.DRAFT,
            'per_page': '25',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('assessment_session_list') +
                         '?q=Biology&status=draft&per_page=25')
        self.assertFalse(AssessmentSession.objects.filter(pk__in=[first.pk, second.pk]).exists())
        self.assertTrue(AssessmentSession.objects.filter(pk=keep.pk).exists())
        self.assertTrue(AssessmentSession.objects.filter(pk=colleague.pk).exists())
        self.assertTrue(AssessmentSession.objects.filter(pk=other_service.pk).exists())

    def test_bulk_delete_rejects_a_mixed_owned_and_unowned_selection_atomically(self):
        mine = self.create_session('My session')
        colleague = self.create_session('Colleague session', owner=self.other)
        other_service = self.create_session('Other service', service='panel')

        response = self.client.post(reverse('assessment_session_bulk_delete'), {
            'session_ids': [str(mine.pk), str(colleague.pk), str(other_service.pk)],
        })

        self.assertEqual(response.status_code, 302)
        for session in (mine, colleague, other_service):
            self.assertTrue(AssessmentSession.objects.filter(pk=session.pk).exists())

    def test_bulk_delete_requires_valid_selected_ids(self):
        existing = self.create_session('Keep this session')
        for selected_ids in ([], ['bad-id'], ['9' * 30], ['1'] * 101):
            with self.subTest(selected_ids_count=len(selected_ids)):
                response = self.client.post(reverse('assessment_session_bulk_delete'), {
                    'session_ids': selected_ids,
                })
                self.assertEqual(response.status_code, 302)
        self.assertTrue(AssessmentSession.objects.filter(pk=existing.pk).exists())

    def test_overseer_can_return_to_all_sessions_after_deleting_owned_rows(self):
        mine = self.create_session('My session')
        colleague = self.create_session('Colleague session', owner=self.other)
        self.org.oversee = True
        sign_in(self.client, self.user, self.org)

        response = self.client.post(reverse('assessment_session_bulk_delete'), {
            'session_ids': [str(mine.pk)], 'scope': 'all', 'per_page': '10',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('assessment_session_list') +
                         '?per_page=25&scope=all')
        self.assertFalse(AssessmentSession.objects.filter(pk=mine.pk).exists())
        self.assertTrue(AssessmentSession.objects.filter(pk=colleague.pk).exists())

    def test_dashboard_uses_an_empty_roster_state_when_rosters_have_no_students(self):
        self.create_session('Empty roster')
        response = self.client.get(reverse('assessment_dashboard'))
        self.assertContains(response, 'Roster data will appear after students are added to a session.')
        self.assertNotContains(response, 'class="classroom-roster-list"')

    def test_overseer_can_search_all_sessions_by_owner_but_regular_user_cannot_expand_scope(self):
        self.create_session('Ann lesson')
        ben_session = self.create_session('Ben lesson', owner=self.other)
        ben_live = self.create_session('Ben live', AssessmentSession.Status.LIVE, owner=self.other)
        response = self.client.get(reverse('assessment_session_list'), {'scope': 'all'})
        self.assertEqual([session.name for session in response.context['sessions']], ['Ann lesson'])

        self.org.oversee = True
        sign_in(self.client, self.user, self.org)
        response = self.client.get(reverse('assessment_session_list'), {'scope': 'all', 'q': 'Ben'})
        self.assertEqual([session.pk for session in response.context['sessions']], [ben_live.pk, ben_session.pk])
        self.assertContains(response, 'Ben lesson')
        self.assertContains(response, 'Ben')
        self.assertContains(response, 'data-session-row-href="' + reverse('assessment_participation', args=[ben_live.pk]) + '"')
        self.assertContains(response, 'data-session-row-href="' + reverse('assessment_results', args=[ben_session.pk]) + '"')

