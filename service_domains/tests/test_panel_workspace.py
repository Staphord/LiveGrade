"""Panel dashboard and assessment browser UI tests."""
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession
from service_domains.models import (
    CombinedPresentationAssessment,
    PanelCandidate,
    PanelCriterion,
    PanelCriterionLevel,
    PanelDecision,
    PanelHearing,
    PanelInvitation,
    PanelResultSnapshot,
    PanelScore,
    PanelScoringPanel,
)
from service_domains.operations import (
    issue_panel_invitation,
    open_panel_hearing,
    set_panel_chamber_active_candidate,
    adjust_panel_chamber_timer,
    toggle_panel_chamber_pause,
    get_panel_live_state_context,
)
from service_domains.views import panel_hearing_list


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'polls', 'team', 'showcase', 'competition'])
class PanelWorkspaceUiTests(TestCase):

    def setUp(self):
        self.org = make_org('Panel UI Org')
        self.owner = make_user('panel-ui-owner')
        self.other = make_user('panel-ui-other', first_name='OtherUser')
        sign_in(self.client, self.owner, self.org)

    def create_hearing(self, name, status=PanelHearing.Status.DRAFT, owner=None, org=None, workflow=PanelHearing.Workflow.SCORED):
        return PanelHearing.objects.create(
            organization_id=(org or self.org).pk,
            created_by=owner or self.owner,
            name=name,
            status=status,
            workflow=workflow,
        )

    def test_dashboard_metrics_and_visuals_are_derived_from_panel_records(self):
        # Create hearings across different lifecycle statuses
        open_hearing = self.create_hearing('Open Hearing', status=PanelHearing.Status.OPEN)
        draft_hearing = self.create_hearing('Draft Hearing', status=PanelHearing.Status.DRAFT)
        review_hearing = self.create_hearing('Review Hearing', status=PanelHearing.Status.REVIEW)
        signed_hearing = self.create_hearing('Signed Off Hearing', status=PanelHearing.Status.SIGNED_OFF)
        released_hearing = self.create_hearing('Released Hearing', status=PanelHearing.Status.RELEASED)

        # Add candidates
        c1 = PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=open_hearing, display_name='Candidate 1')
        c2 = PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=open_hearing, display_name='Candidate 2')
        open_hearing.active_candidate = c1
        open_hearing.save(update_fields=('active_candidate',))
        PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=draft_hearing, display_name='Draft Cand')

        # Add invitations and failed delivery attempts
        inv1, _ = issue_panel_invitation(open_hearing, 'examiner1@example.test', timezone.now() + timezone.timedelta(days=1), organization=self.org.as_claim(), user=self.owner)
        inv1.confirmed_at = timezone.now()
        inv1.save(update_fields=('confirmed_at',))

        inv_fail, _ = issue_panel_invitation(draft_hearing, 'fail@example.test', timezone.now() + timezone.timedelta(days=1), organization=self.org.as_claim(), user=self.owner)
        inv_fail.delivery_status = PanelInvitation.DeliveryStatus.FAILED
        inv_fail.save(update_fields=('delivery_status',))

        response = self.client.get(reverse('panel_hearing_list'))
        self.assertEqual(response.status_code, 200)

        # Verify page title and header
        self.assertContains(response, '<span class="topbar-title">Panel dashboard</span>', html=False)
        self.assertContains(response, 'A live summary of your assessments, candidates, and examiner activity.')

        # Verify dashboard statistics
        stats = response.context['dashboard_stats']
        self.assertEqual(stats['total'], 5)
        self.assertEqual(stats['live'], 1)
        self.assertEqual(stats['drafts'], 1)
        self.assertEqual(stats['review'], 1)
        self.assertEqual(stats['release'], 1)
        self.assertEqual(stats['completed'], 1)
        self.assertEqual(stats['total_candidates'], 3)
        self.assertEqual(stats['total_examiners'], 1)

        # Verify stage breakdown conic gradient and percentages
        distribution = {item['key']: item for item in response.context['stage_distribution']}
        self.assertEqual(distribution['open']['count'], 1)
        self.assertEqual(distribution['draft']['count'], 1)
        self.assertEqual(distribution['review']['count'], 1)
        self.assertEqual(distribution['signed_off']['count'], 1)
        self.assertEqual(distribution['released']['count'], 1)
        for item in distribution.values():
            self.assertEqual(item['percentage'], 20.0)
        self.assertIn('conic-gradient(', response.context['stage_pie_background'])

        # Verify candidate bar distributions
        self.assertEqual(response.context['largest_candidates'], 2)
        top_candidate_hearing = response.context['candidate_hearings'][0]
        self.assertEqual(top_candidate_hearing.pk, open_hearing.pk)
        self.assertEqual(top_candidate_hearing.candidate_bar_percent, 100.0)

        # Verify live panel display
        self.assertContains(response, 'Open Hearing')
        self.assertContains(response, 'Reviewing: Candidate 1')

        # Verify attention item for failed invitation
        self.assertContains(response, '1 invitation email could not be delivered')

        # Verify recent hearings and row URLs
        self.assertEqual(len(response.context['recent_hearings']), 5)
        self.assertContains(response, 'data-panel-row-href="' + reverse('panel_hearing_live', args=[open_hearing.pk]) + '"')
        self.assertContains(response, 'data-panel-row-href="' + reverse('panel_hearing_manage', args=[draft_hearing.pk]) + '"')
        self.assertContains(response, 'data-panel-row-href="' + reverse('panel_results_review', args=[review_hearing.pk]) + '"')
        self.assertContains(response, 'data-panel-row-href="' + reverse('panel_results_review', args=[signed_hearing.pk]) + '"')
        self.assertContains(response, 'data-panel-row-href="' + reverse('panel_results_review', args=[released_hearing.pk]) + '"')

        # Verify row kebab action buttons
        self.assertContains(response, 'Actions for Open Hearing')
        self.assertContains(response, 'Delete assessment')
        self.assertContains(response, 'data-confirm-tone="danger"')

    def test_dashboard_is_limited_to_owned_panel_hearings_in_the_active_organization(self):
        mine = self.create_hearing('My Hearing')
        self.create_hearing('Colleague Hearing', owner=self.other)
        other_org = make_org('Other Org')
        self.create_hearing('Other Org Hearing', org=other_org)

        response = self.client.get(reverse('panel_hearing_list'))
        self.assertEqual(response.context['dashboard_stats']['total'], 1)
        self.assertEqual([h.pk for h in response.context['recent_hearings']], [mine.pk])

    def test_dashboard_rejects_non_panel_service_context(self):
        request = RequestFactory().get('/panel/app/hearings/')
        request.user = self.owner
        request.session = self.client.session
        request.service = 'classroom'
        with self.assertRaises(Http404):
            panel_hearing_list(request)

    def test_dashboard_preview_limits_recent_to_five_and_links_to_all(self):
        for index in range(7):
            self.create_hearing(f'Hearing {index}')

        response = self.client.get(reverse('panel_hearing_list'))
        self.assertEqual(len(response.context['recent_hearings']), 5)
        self.assertContains(response, 'View all assessments')
        self.assertContains(response, reverse('panel_hearing_table'))

    def test_empty_dashboard_renders_zero_state(self):
        response = self.client.get(reverse('panel_hearing_list'))
        self.assertEqual(response.context['dashboard_stats']['total'], 0)
        self.assertContains(response, 'You haven’t created an assessment yet')
        self.assertContains(response, 'Create an assessment')
        self.assertContains(response, reverse('panel_hearing_create'))

    def test_table_search_status_filter_and_actions(self):
        h_draft = self.create_hearing('Biology Draft', status=PanelHearing.Status.DRAFT)
        h_open = self.create_hearing('Biology Open', status=PanelHearing.Status.OPEN)
        h_other = self.create_hearing('Physics Open', status=PanelHearing.Status.OPEN)

        # Filter by name search and status
        response = self.client.get(reverse('panel_hearing_table'), {
            'q': 'Biology',
            'status': PanelHearing.Status.DRAFT,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<span class="topbar-title">Panel assessments</span>', html=False)
        self.assertEqual([h.name for h in response.context['hearings']], ['Biology Draft'])
        self.assertEqual(response.context['status_counts']['total'], 3)
        self.assertEqual(response.context['status_counts']['draft'], 1)
        self.assertEqual(response.context['status_counts']['open'], 2)

        # Check table elements and controls
        self.assertContains(response, 'Search by assessment name')
        self.assertContains(response, 'data-panel-auto-filter')
        self.assertContains(response, 'setTimeout(submitFilters, 350)')
        self.assertContains(response, 'data-panel-select-all')
        self.assertContains(response, 'data-panel-select')
        self.assertContains(response, 'data-bulk-delete-button')
        self.assertContains(response, 'Delete selected')
        self.assertContains(response, 'Actions for Biology Draft')
        self.assertContains(response, 'Continue setup')

    def test_ajax_table_returns_only_results_partial(self):
        self.create_hearing('Biochem Draft', status=PanelHearing.Status.DRAFT)
        self.create_hearing('Physics Review', status=PanelHearing.Status.REVIEW)

        partial = self.client.get(reverse('panel_hearing_table'), {
            'q': 'Biochem',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(partial.status_code, 200)
        self.assertIn('X-Requested-With', partial['Vary'])
        self.assertContains(partial, 'id="panel-results"')
        self.assertContains(partial, 'Biochem Draft')
        self.assertNotContains(partial, 'Physics Review')
        self.assertNotContains(partial, '<html', html=False)

    def test_single_row_delete_removes_hearing_and_cascades_relations(self):
        hearing = self.create_hearing('Delete Candidate')
        cand = PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=hearing, display_name='Cand 1')
        crit = PanelCriterion.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=hearing, name='Crit 1')
        PanelCriterionLevel.objects.create(organization_id=self.org.pk, created_by=self.owner, criterion=crit, score=50, description='Pass')

        response = self.client.post(reverse('panel_hearing_delete', args=[hearing.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('panel_hearing_table'))
        self.assertFalse(PanelHearing.objects.filter(pk=hearing.pk).exists())
        self.assertFalse(PanelCandidate.objects.filter(pk=cand.pk).exists())
        self.assertFalse(PanelCriterion.objects.filter(pk=crit.pk).exists())

    def test_bulk_delete_removes_only_selected_owned_hearings(self):
        h1 = self.create_hearing('Hearing 1')
        h2 = self.create_hearing('Hearing 2')
        keep = self.create_hearing('Keep Hearing')
        colleague = self.create_hearing('Colleague Hearing', owner=self.other)

        response = self.client.post(reverse('panel_hearing_bulk_delete'), {
            'hearing_ids': [str(h1.pk), str(h2.pk)],
            'q': 'Hearing',
            'status': PanelHearing.Status.DRAFT,
            'per_page': '25',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('panel_hearing_table') + '?q=Hearing&status=draft&per_page=25')
        self.assertFalse(PanelHearing.objects.filter(pk__in=[h1.pk, h2.pk]).exists())
        self.assertTrue(PanelHearing.objects.filter(pk=keep.pk).exists())
        self.assertTrue(PanelHearing.objects.filter(pk=colleague.pk).exists())

    def test_bulk_delete_open_hearing_succeeds_for_owner(self):
        open_hearing = self.create_hearing('Open Hearing to Delete', status=PanelHearing.Status.OPEN)
        cand = PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=open_hearing, display_name='Active Cand')
        open_hearing.active_candidate = cand
        open_hearing.save(update_fields=('active_candidate',))

        response = self.client.post(reverse('panel_hearing_bulk_delete'), {
            'hearing_ids': [str(open_hearing.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PanelHearing.objects.filter(pk=open_hearing.pk).exists())

    def test_invalid_pagination_parameters_fallback_to_defaults(self):
        for index in range(27):
            self.create_hearing(f'Paginated Hearing {index:02d}')

        # Invalid per_page falls back to 25
        res_bad_size = self.client.get(reverse('panel_hearing_table'), {'per_page': 'abc'})
        self.assertEqual(res_bad_size.context['per_page'], 25)
        self.assertEqual(res_bad_size.context['paginator'].num_pages, 2)

        res_unlisted_size = self.client.get(reverse('panel_hearing_table'), {'per_page': '10'})
        self.assertEqual(res_unlisted_size.context['per_page'], 25)

        # Invalid page falls back to page 1 or last page
        res_bad_page = self.client.get(reverse('panel_hearing_table'), {'page': 'xyz'})
        self.assertEqual(res_bad_page.context['page_obj'].number, 1)

        res_high_page = self.client.get(reverse('panel_hearing_table'), {'page': '9999'})
        self.assertEqual(res_high_page.context['page_obj'].number, 2)

        # Unknown status filter is ignored
        res_bad_status = self.client.get(reverse('panel_hearing_table'), {'status': 'unknown_status'})
        self.assertEqual(res_bad_status.context['status_filter'], '')
        self.assertEqual(res_bad_status.context['filtered_count'], 27)

    def test_bulk_delete_requires_valid_selected_ids(self):
        existing = self.create_hearing('Persisted Hearing')
        for selected_ids in ([], ['bad-id'], ['9' * 30], ['1'] * 101):
            with self.subTest(selected_ids_count=len(selected_ids)):
                response = self.client.post(reverse('panel_hearing_bulk_delete'), {
                    'hearing_ids': selected_ids,
                })
                self.assertEqual(response.status_code, 302)
        self.assertTrue(PanelHearing.objects.filter(pk=existing.pk).exists())

    def test_single_delete_nonexistent_id_returns_404(self):
        response = self.client.post(reverse('panel_hearing_delete', args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_boundary_pagination_counts(self):
        # Exactly 25 hearings produce single page
        for index in range(25):
            self.create_hearing(f'Boundary Hearing {index:02d}')

        res_25 = self.client.get(reverse('panel_hearing_table'))
        self.assertEqual(res_25.context['paginator'].num_pages, 1)
        self.assertEqual(len(res_25.context['hearings']), 25)

        # 26th hearing produces second page
        self.create_hearing('Hearing 26')
        res_26 = self.client.get(reverse('panel_hearing_table'))
        self.assertEqual(res_26.context['paginator'].num_pages, 2)

    def test_search_query_boundary_lengths(self):
        # 100 char and 101 char queries
        q_100 = 'a' * 100
        q_101 = 'a' * 101

        res_100 = self.client.get(reverse('panel_hearing_table'), {'q': q_100})
        self.assertEqual(res_100.status_code, 200)
        self.assertEqual(res_100.context['search_query'], q_100)

        res_101 = self.client.get(reverse('panel_hearing_table'), {'q': q_101})
        self.assertEqual(res_101.status_code, 200)
        self.assertEqual(res_101.context['search_query'], q_100)

    def test_page_sizes_allowed_and_rendered(self):
        for index in range(52):
            self.create_hearing(f'Item {index:02d}')

        for size in (25, 50, 100):
            response = self.client.get(reverse('panel_hearing_table'), {'per_page': str(size)})
            self.assertEqual(response.context['per_page'], size)
            self.assertContains(response, f'<option value="{size}"', html=False)

    def test_anonymous_user_redirected_to_login(self):
        self.client.logout()
        hearing = self.create_hearing('Protected Hearing')

        for url in (
            reverse('panel_hearing_list'),
            reverse('panel_hearing_table'),
            reverse('panel_hearing_create'),
        ):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302)
            self.assertEqual(res['Location'], '/panel/')

        for post_url, data in (
            (reverse('panel_hearing_delete', args=[hearing.pk]), {}),
            (reverse('panel_hearing_bulk_delete'), {'hearing_ids': [str(hearing.pk)]}),
        ):
            res = self.client.post(post_url, data)
            self.assertEqual(res.status_code, 302)
            self.assertEqual(res['Location'], '/panel/')

    def test_cross_organization_delete_rejected(self):
        other_org = make_org('Foreign Org')
        foreign_hearing = self.create_hearing('Foreign Hearing', org=other_org)

        response = self.client.post(reverse('panel_hearing_delete', args=[foreign_hearing.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PanelHearing.objects.filter(pk=foreign_hearing.pk).exists())

    def test_bulk_delete_rejects_mixed_owned_and_unowned_atomically(self):
        mine = self.create_hearing('My Hearing')
        colleague = self.create_hearing('Colleague Hearing', owner=self.other)

        response = self.client.post(reverse('panel_hearing_bulk_delete'), {
            'hearing_ids': [str(mine.pk), str(colleague.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PanelHearing.objects.filter(pk=mine.pk).exists())
        self.assertTrue(PanelHearing.objects.filter(pk=colleague.pk).exists())

    def test_delete_routes_require_post(self):
        hearing = self.create_hearing('Hearing To Keep')
        get_delete = self.client.get(reverse('panel_hearing_delete', args=[hearing.pk]))
        self.assertEqual(get_delete.status_code, 405)

        get_bulk = self.client.get(reverse('panel_hearing_bulk_delete'))
        self.assertEqual(get_bulk.status_code, 405)

    def test_delete_blocked_when_linked_to_combined_assessment(self):
        hearing = self.create_hearing('Linked Panel Hearing')
        classroom_session = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Classroom Source',
            service='classroom',
        )
        CombinedPresentationAssessment.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            name='Combined Assessment Link',
            classroom_session=classroom_session,
            panel_hearing=hearing,
        )

        # Single row delete blocked
        res_single = self.client.post(reverse('panel_hearing_delete', args=[hearing.pk]))
        self.assertEqual(res_single.status_code, 302)
        self.assertTrue(PanelHearing.objects.filter(pk=hearing.pk).exists())

        # Bulk delete blocked
        res_bulk = self.client.post(reverse('panel_hearing_bulk_delete'), {
            'hearing_ids': [str(hearing.pk)],
        })
        self.assertEqual(res_bulk.status_code, 302)
        self.assertTrue(PanelHearing.objects.filter(pk=hearing.pk).exists())

    def test_duplicate_ids_in_bulk_delete_handled_properly(self):
        hearing = self.create_hearing('Duplicate ID Target')
        response = self.client.post(reverse('panel_hearing_bulk_delete'), {
            'hearing_ids': [str(hearing.pk), str(hearing.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PanelHearing.objects.filter(pk=hearing.pk).exists())

    def test_filtered_to_zero_empty_state_in_table(self):
        self.create_hearing('Astronomy Hearing')
        response = self.client.get(reverse('panel_hearing_table'), {'q': 'Nonexistent'})
        self.assertContains(response, 'No matching assessments')
        self.assertContains(response, 'Try a different assessment name or status filter.')

    def test_table_first_run_empty_state(self):
        response = self.client.get(reverse('panel_hearing_table'))
        self.assertContains(response, 'No Panel assessments yet')
        self.assertContains(response, 'Add candidates')
        self.assertContains(response, 'Set criteria and invite examiners')
        self.assertContains(response, 'Open and release results')
        self.assertContains(response, reverse('panel_hearing_create'))

    def test_sidebar_active_states_and_oversight_link(self):
        # Normal owner view
        res_dashboard = self.client.get(reverse('panel_hearing_list'))
        self.assertContains(res_dashboard, 'class="nav-link active" aria-current="page">\n      <i class="bi bi-clipboard2-data"')
        self.assertNotContains(res_dashboard, 'Oversight results')
        self.assertNotContains(res_dashboard, '<span class="nav-text">New assessment</span>')

        res_table = self.client.get(reverse('panel_hearing_table'))
        self.assertContains(res_table, 'class="nav-link active" aria-current="page">\n      <i class="bi bi-list-ul"')
        self.assertNotContains(res_table, '<span class="nav-text">New assessment</span>')

        # Test that panel_hearing_create renders setup step 1 directly without creating records on GET
        hearings_before = PanelHearing.objects.count()
        res_create = self.client.get(reverse('panel_hearing_create'))
        self.assertEqual(res_create.status_code, 200)
        self.assertEqual(PanelHearing.objects.count(), hearings_before)
        self.assertContains(res_create, 'Panel assessment setup')
        self.assertContains(res_create, 'Name and schedule')
        self.assertContains(res_create, 'class="nav-link active"')
        self.assertNotContains(res_create, '<span class="nav-text">New assessment</span>')

        # Submitting the step creates the assessment and redirects to manage
        res_post = self.client.post(reverse('panel_hearing_create'), {
            'name': 'Direct Stepper Hearing',
            'workflow': PanelHearing.Workflow.SCORED,
            'advance': '1',
        })
        created_hearing = PanelHearing.objects.get(name='Direct Stepper Hearing')
        self.assertEqual(res_post.status_code, 302)
        self.assertEqual(res_post['Location'], f"{reverse('panel_hearing_manage', args=[created_hearing.pk])}?step=candidates")

        # Manage view has Assessments active in sidebar and does not have New assessment
        res_manage = self.client.get(reverse('panel_hearing_manage', args=[created_hearing.pk]))
        self.assertEqual(res_manage.status_code, 200)
        self.assertContains(res_manage, 'Panel assessment setup')
        self.assertNotContains(res_manage, '<span class="nav-text">New assessment</span>')

        # Overseer view
        self.org.oversee = True
        sign_in(self.client, self.owner, self.org)
        res_overseer = self.client.get(reverse('panel_hearing_list'))
        self.assertContains(res_overseer, 'Oversight results')
        self.assertContains(res_overseer, reverse('panel_oversight_list'))

    def test_duplicate_candidate_reference_uses_standard_field_error(self):
        hearing = self.create_hearing('Duplicate candidate reference')
        PanelCandidate.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            display_name='Existing candidate',
            reference='C-002',
            order=1,
        )

        response = self.client.post(reverse('panel_hearing_manage', args=[hearing.pk]), {
            'action': 'candidate',
            'setup_step': 'candidates',
            'display_name': 'Another candidate',
            'reference': 'C-002',
            'scheduled_time': '10:10',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['active_panel_step'], 'candidates')
        self.assertEqual(PanelCandidate.objects.filter(hearing=hearing).count(), 1)
        rendered = response.content.decode()
        self.assertIn('A candidate with this reference already exists.', rendered)
        self.assertIn('class="inline-message inline-message--plain"', rendered)
        self.assertIn('role="alert"', rendered)
        message_start = rendered.index('id="candidate_reference_inline_message"')
        message_end = rendered.index('</div>', message_start)
        field_error_markup = rendered[message_start:message_end]
        self.assertNotIn('inline-message-icon', field_error_markup)
        self.assertNotIn('alert-danger', field_error_markup)
        self.assertNotIn('panel_candidate_ref_unique', rendered)

    def test_manual_candidate_form_appends_order_and_shows_optional_fields(self):
        hearing = self.create_hearing('Automatic candidate order')
        PanelCandidate.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            display_name='First candidate',
            order=7,
        )
        manage_url = reverse('panel_hearing_manage', args=[hearing.pk])

        response = self.client.get(manage_url, {'step': 'candidates'})

        candidate_form = response.context['forms']['candidate']
        self.assertNotIn('order', candidate_form.fields)
        self.assertEqual(list(candidate_form.fields), ['display_name', 'reference', 'scheduled_time'])
        self.assertContains(response, 'Candidate list')
        self.assertContains(response, 'External reference (optional)')
        self.assertContains(response, 'Start time (optional)')
        self.assertNotContains(response, '<summary>Optional details</summary>')
        self.assertNotContains(response, 'Presentation order')

        saved = self.client.post(manage_url, {
            'action': 'candidate',
            'setup_step': 'candidates',
            'display_name': 'Second candidate',
        })

        self.assertEqual(saved.status_code, 302)
        self.assertEqual(hearing.candidates.get(display_name='Second candidate').order, 8)

    def test_criterion_order_is_assigned_automatically(self):
        hearing = self.create_hearing('Automatic criterion order')
        PanelCriterion.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            name='First criterion',
            order=4,
        )

        response = self.client.post(reverse('panel_hearing_manage', args=[hearing.pk]), {
            'action': 'criterion',
            'setup_step': 'scoring',
            'name': 'Second criterion',
            'description': 'Optional context for examiners.',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(hearing.criteria.get(name='Second criterion').order, 5)

    def test_kebab_export_action_visibility_and_ready_step(self):
        draft_ready = self.create_hearing('Draft Ready Hearing', status=PanelHearing.Status.DRAFT)
        PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=draft_ready, display_name='Cand')
        crit = PanelCriterion.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=draft_ready, name='Crit')
        PanelCriterionLevel.objects.create(organization_id=self.org.pk, created_by=self.owner, criterion=crit, score=100, description='Max')
        default_panel = PanelScoringPanel.objects.create(
            organization_id=self.org.pk, created_by=self.owner, hearing=draft_ready,
            name='Default panel', share=Decimal('100.00'), minimum_examiners=1)
        default_panel.criteria.add(crit)
        inv, _ = issue_panel_invitation(draft_ready, 'ex@example.test', timezone.now() + timezone.timedelta(days=1), organization=self.org.as_claim(), user=self.owner)
        inv.confirmed_at = timezone.now()
        inv.panel = default_panel
        inv.save(update_fields=('confirmed_at', 'panel'))

        review_hearing = self.create_hearing('Review With Export', status=PanelHearing.Status.REVIEW)

        response = self.client.get(reverse('panel_hearing_table'))
        # Review hearing has export option
        self.assertContains(response, 'Export results (.xlsx)')
        self.assertContains(response, reverse('panel_results_export', args=[review_hearing.pk]))

        # Draft ready hearing has row URL and kebab pointing to ?step=ready
        self.assertContains(response, reverse('panel_hearing_manage', args=[draft_ready.pk]) + '?step=ready')
        self.assertContains(response, 'Review and launch')

    def test_oversight_access_and_filtering(self):
        # Regular user without oversee permission gets 404
        res_denied = self.client.get(reverse('panel_oversight_list'))
        self.assertEqual(res_denied.status_code, 404)

        # Grant oversee permission
        self.org.oversee = True
        sign_in(self.client, self.owner, self.org)

        # Hearing in released status with snapshot
        hearing_rel = self.create_hearing('Released Capstone Panel', status=PanelHearing.Status.RELEASED)
        cand = PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=hearing_rel, display_name='Cand 1')
        crit = PanelCriterion.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=hearing_rel, name='Crit 1')
        PanelResultSnapshot.objects.create(
            organization_id=self.org.pk, created_by=self.owner, hearing=hearing_rel,
            candidate=cand, criterion=crit, score=Decimal('85.5'), rater_count=3,
            aggregation='mean')

        # Hearing in signed off status with snapshot
        hearing_sign = self.create_hearing('Signed Off Defense', status=PanelHearing.Status.SIGNED_OFF)
        cand2 = PanelCandidate.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=hearing_sign, display_name='Cand 2')
        crit2 = PanelCriterion.objects.create(organization_id=self.org.pk, created_by=self.owner, hearing=hearing_sign, name='Crit 2')
        PanelResultSnapshot.objects.create(
            organization_id=self.org.pk, created_by=self.owner, hearing=hearing_sign,
            candidate=cand2, criterion=crit2, score=Decimal('90.0'), rater_count=2,
            aggregation='mean')

        # GET oversight list returns 200 with KPI stats
        res = self.client.get(reverse('panel_oversight_list'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Oversight results')
        self.assertContains(res, 'Released Capstone Panel')
        self.assertContains(res, 'Signed Off Defense')
        self.assertEqual(res.context['stats']['total_panels'], 2)
        self.assertEqual(res.context['stats']['total_released'], 1)
        self.assertEqual(res.context['stats']['total_signed_off'], 1)

        # Search by assessment name
        res_search = self.client.get(reverse('panel_oversight_list'), {'q': 'Capstone'})
        self.assertContains(res_search, 'Released Capstone Panel')
        self.assertNotContains(res_search, 'Signed Off Defense')

        # Filter by status
        res_status = self.client.get(reverse('panel_oversight_list'), {'status': 'released'})
        self.assertContains(res_status, 'Released Capstone Panel')
        self.assertNotContains(res_status, 'Signed Off Defense')

        # AJAX request returns fragment
        res_ajax = self.client.get(reverse('panel_oversight_list'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_ajax.status_code, 200)
        self.assertIn('X-Requested-With', res_ajax.headers.get('Vary', ''))
        self.assertContains(res_ajax, 'id="panel-results"')

    def test_panel_live_control_console_lifecycle_and_actions(self):
        # Create open hearing with candidates and examiner
        hearing = self.create_hearing('Live Chamber Defense', status=PanelHearing.Status.OPEN,
                                     workflow=PanelHearing.Workflow.OUTCOME)
        cand1 = PanelCandidate.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            display_name='Candidate Alice',
            order=1
        )
        cand2 = PanelCandidate.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            display_name='Candidate Bob',
            order=2
        )
        hearing.active_candidate = cand1
        hearing.save(update_fields=('active_candidate',))

        panel = PanelScoringPanel.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            name='Primary Committee'
        )
        invitation = PanelInvitation.objects.create(
            organization_id=self.org.pk,
            created_by=self.owner,
            hearing=hearing,
            panel=panel,
            email='examiner1@example.org',
            expires_at=timezone.now() + timezone.timedelta(days=7)
        )

        # GET live control page
        live_url = reverse('panel_hearing_live', args=(hearing.pk,))
        res = self.client.get(live_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Live Chamber Defense')
        self.assertContains(res, 'Candidate Alice')
        self.assertContains(res, 'Candidate Bob')
        self.assertContains(res, 'examiner1@example.org')

        # GET live state JSON endpoint
        state_url = reverse('panel_hearing_live_state', args=(hearing.pk,))
        res_state = self.client.get(state_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_state.status_code, 200)
        state_data = res_state.json()
        self.assertEqual(state_data['status'], 'open')
        self.assertEqual(state_data['active_candidate_id'], cand1.pk)
        self.assertIn('hero_html', state_data)
        self.assertIn('examiners_html', state_data)
        self.assertIn('convergence_html', state_data)
        self.assertIn('docket_html', state_data)
        self.assertIn('display_html', state_data)

        # POST actions: set_phase
        action_url = reverse('panel_hearing_live_action', args=(hearing.pk,))
        res_phase = self.client.post(action_url, {'action': 'set_phase', 'phase': 'qa'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_phase.status_code, 200)
        self.assertEqual(res_phase.json()['active_phase'], 'qa')

        # POST actions: adjust_timer
        res_timer = self.client.post(action_url, {'action': 'adjust_timer', 'delta': '120'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_timer.status_code, 200)
        self.assertEqual(res_timer.json()['phase_seconds_remaining'], 1320)

        # POST actions: toggle_pause
        res_pause = self.client.post(action_url, {'action': 'toggle_pause'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_pause.status_code, 200)
        self.assertTrue(res_pause.json()['timer_paused'])

        # POST actions: switch candidate
        res_cand = self.client.post(action_url, {'action': 'set_candidate', 'candidate_id': str(cand2.pk)}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_cand.status_code, 200)
        self.assertEqual(res_cand.json()['active_candidate_id'], cand2.pk)

        # POST display link toggle
        display_toggle_url = reverse('panel_hearing_live_display_toggle', args=(hearing.pk,))
        res_disp = self.client.post(display_toggle_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_disp.status_code, 200)
        self.assertTrue(res_disp.json()['active'])

        # POST close_hearing
        res_close = self.client.post(action_url, {'action': 'close_hearing'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_close.status_code, 200)
        hearing.refresh_from_db()
        self.assertEqual(hearing.status, PanelHearing.Status.REVIEW)

    def test_setup_stepper_uses_the_shared_partial_for_create_and_manage(self):
        created = self.client.get(reverse('panel_hearing_create'))
        steps = created.context['stepper_steps']
        self.assertEqual([s['key'] for s in steps],
                         ['details', 'candidates', 'scoring', 'examiners', 'ready'])
        self.assertTrue(steps[0]['current'])
        self.assertTrue(all(s['disabled'] for s in steps[1:]))

        hearing = self.create_hearing('Stepper Hearing')
        manage = self.client.get(reverse('panel_hearing_manage', args=(hearing.pk,)) + '?step=candidates')
        steps = {s['key']: s for s in manage.context['stepper_steps']}
        self.assertTrue(steps['candidates']['current'])
        self.assertContains(manage, "querySelector('.stepper-nav')")
        self.assertContains(manage, 'DevPerfStepper.setCurrent(stepLayout,step)')

    def test_hearing_stepper_drops_examiner_steps_for_workflows_without_examiners(self):
        from service_domains.views import _hearing_steps
        completion = {'details': True, 'candidates': False, 'ready': False}

        steps = _hearing_steps(completion, False, 'details')

        self.assertEqual([s['key'] for s in steps], ['details', 'candidates', 'ready'])
        self.assertEqual([s['number'] for s in steps], [1, 2, 3])
        self.assertTrue(steps[0]['done'])

    def test_open_hearing_setup_stepper_rendered_in_edit_mode_and_back_routing(self):
        hearing = self.create_hearing('Open Hearing Setup Edit', status=PanelHearing.Status.OPEN)
        manage_url = reverse('panel_hearing_manage', args=(hearing.pk,))
        live_url = reverse('panel_hearing_live', args=(hearing.pk,))

        res_live = self.client.get(live_url)
        self.assertEqual(res_live.status_code, 200)
        self.assertContains(res_live, f'{manage_url}?from=live')
        self.assertContains(res_live, 'Edit setup')

        res_manage = self.client.get(f'{manage_url}?from=live')
        self.assertEqual(res_manage.status_code, 200)
        self.assertContains(res_manage, 'Hearing info')
        self.assertContains(res_manage, 'Candidates')
        self.assertContains(res_manage, 'Back to Live Chamber')
        self.assertContains(res_manage, f'href="{live_url}"')

        res_update = self.client.post(manage_url, {
            'action': 'update_hearing',
            'name': 'Open Hearing Renamed Live',
            'workflow': hearing.workflow,
            'from': 'live',
        })
        self.assertEqual(res_update.status_code, 302)
        self.assertIn('from=live', res_update['Location'])
        hearing.refresh_from_db()
        self.assertEqual(hearing.name, 'Open Hearing Renamed Live')

    def test_live_chamber_timer_persistence_and_silent_state(self):
        hearing = self.create_hearing('Timer Live Hearing', status=PanelHearing.Status.OPEN)
        cand1 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Cand 1', order=1)
        set_panel_chamber_active_candidate(hearing, cand1.pk, organization=self.org.as_claim(), user=self.owner)

        ctx = get_panel_live_state_context(hearing)
        self.assertEqual(ctx['phase_seconds_remaining'], 900)
        self.assertFalse(ctx['timer_paused'])

        adjust_panel_chamber_timer(hearing, 60, organization=self.org.as_claim(), user=self.owner)
        hearing.refresh_from_db()
        ctx_adjusted = get_panel_live_state_context(hearing)
        self.assertEqual(ctx_adjusted['phase_seconds_remaining'], 960)

        toggle_panel_chamber_pause(hearing, organization=self.org.as_claim(), user=self.owner)
        hearing.refresh_from_db()
        ctx_paused = get_panel_live_state_context(hearing)
        self.assertTrue(ctx_paused['timer_paused'])

        state_url = reverse('panel_hearing_live_state', args=(hearing.pk,))
        res = self.client.get(state_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'open')
        self.assertEqual(data['active_phase'], 'presentation')
        self.assertTrue(data['timer_paused'])
        self.assertIn('hero_html', data)
        self.assertNotIn('assess-stagger', data['hero_html'])

    def test_examiners_and_candidates_dedicated_screen(self):
        hearing = self.create_hearing('Participants Hearing', status=PanelHearing.Status.OPEN)
        cand1 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Candidate Beta', order=1)
        cand2 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Candidate Gamma', order=2)
        invitation, token = issue_panel_invitation(hearing, 'examiner_beta@example.org',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(), user=self.owner)

        participants_url = reverse('panel_hearing_participants', args=(hearing.pk,))
        res = self.client.get(participants_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Examiners &amp; Candidates')
        self.assertContains(res, 'Candidate Beta')
        self.assertContains(res, 'Candidate Gamma')
        self.assertContains(res, 'examiner_beta@example.org')
        self.assertContains(res, reverse('panel_hearing_live', args=(hearing.pk,)))

        post_activate = self.client.post(participants_url, {
            'action': 'activate_candidate',
            'candidate_id': str(cand2.pk),
        })
        self.assertEqual(post_activate.status_code, 302)
        self.assertEqual(post_activate['Location'], reverse('panel_hearing_live', args=(hearing.pk,)))
        hearing.refresh_from_db()
        self.assertEqual(hearing.active_candidate_id, cand2.pk)

    def test_open_hearing_results_and_deliberation_page_accessible_without_404(self):
        hearing = self.create_hearing('Results Review Open', status=PanelHearing.Status.OPEN)
        cand = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Cand Results', order=1)
        results_url = reverse('panel_results_review', args=(hearing.pk,))

        res = self.client.get(results_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Results &amp; Deliberation')
        self.assertContains(res, 'Provisional results and chamber deliberation')
        self.assertContains(res, reverse('panel_hearing_live', args=(hearing.pk,)))
        self.assertNotContains(res, 'Close &amp; review')
        self.assertNotContains(res, 'Back to Live Chamber')

    def test_open_panel_hearing_initializes_active_candidate_and_timer_state(self):
        draft = PanelHearing.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            name='Draft To Open Hearing',
            workflow=PanelHearing.Workflow.SCORED
        )
        cand1 = PanelCandidate.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=draft,
            display_name='Candidate First',
            order=1
        )
        cand2 = PanelCandidate.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=draft,
            display_name='Candidate Second',
            order=2
        )
        crit = PanelCriterion.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=draft,
            name='Criterion 1'
        )
        panel = PanelScoringPanel.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=draft,
            name='Panel 1',
            share=Decimal('100.00'),
            minimum_examiners=1
        )
        crit.panels.add(panel)
        inv = PanelInvitation.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            hearing=draft,
            panel=panel,
            email='evaluator@example.test',
            confirmed_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(days=1)
        )
        opened = open_panel_hearing(draft, organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(opened.status, PanelHearing.Status.OPEN)
        self.assertIsNone(opened.active_candidate_id)
        chamber = opened.workflow_snapshot.get('chamber_state', {})
        self.assertEqual(chamber.get('active_phase'), 'presentation')
        self.assertEqual(chamber.get('phase_duration_seconds'), 900)
        self.assertIsNotNone(chamber.get('phase_started_at'))

    def test_panel_live_chamber_phase_and_timer_transitions(self):
        hearing = self.create_hearing('Phase Transition Test Hearing', status=PanelHearing.Status.OPEN)
        cand1 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Presenter 1', order=1)
        cand2 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Presenter 2', order=2)
        hearing.active_candidate = cand1
        hearing.save(update_fields=('active_candidate',))

        action_url = reverse('panel_hearing_live_action', args=(hearing.pk,))

        # Advance to QA
        res_qa = self.client.post(action_url, {'action': 'set_phase', 'phase': 'qa'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_qa.status_code, 200)
        data_qa = res_qa.json()
        self.assertEqual(data_qa['active_phase'], 'qa')
        self.assertEqual(data_qa['phase_duration_seconds'], 1200)
        self.assertEqual(data_qa['phase_seconds_remaining'], 1200)

        # Advance to Deliberation
        res_delib = self.client.post(action_url, {'action': 'set_phase', 'phase': 'deliberation'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_delib.status_code, 200)
        data_delib = res_delib.json()
        self.assertEqual(data_delib['active_phase'], 'deliberation')
        self.assertEqual(data_delib['phase_duration_seconds'], 600)
        self.assertEqual(data_delib['phase_seconds_remaining'], 600)

        # Advance to Decision
        res_dec = self.client.post(action_url, {'action': 'set_phase', 'phase': 'decision'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_dec.status_code, 200)
        data_dec = res_dec.json()
        self.assertEqual(data_dec['active_phase'], 'decision')
        self.assertEqual(data_dec['phase_duration_seconds'], 300)
        self.assertEqual(data_dec['phase_seconds_remaining'], 300)

        # Conclude candidate advances to next presenter
        res_conclude = self.client.post(action_url, {
            'action': 'conclude_candidate',
            'candidate_id': str(cand1.pk),
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_conclude.status_code, 200)
        data_conclude = res_conclude.json()
        self.assertEqual(data_conclude['active_candidate_id'], cand2.pk)
        self.assertEqual(data_conclude['active_phase'], 'presentation')
        self.assertEqual(data_conclude['phase_duration_seconds'], 900)

    def test_panel_live_chamber_set_phase_durations(self):
        hearing = self.create_hearing('Custom Durations Hearing', status=PanelHearing.Status.OPEN)
        cand = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Presenter 1', order=1)
        hearing.active_candidate = cand
        hearing.save(update_fields=('active_candidate',))
        action_url = reverse('panel_hearing_live_action', args=(hearing.pk,))

        res = self.client.post(action_url, {
            'action': 'set_phase_durations',
            'presentation_minutes': '10',
            'qa_minutes': '25',
            'deliberation_minutes': '12',
            'decision_minutes': '8',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['phase_durations_mins']['presentation'], 10)
        self.assertEqual(data['phase_durations_mins']['qa'], 25)
        self.assertEqual(data['phase_durations_mins']['deliberation'], 12)
        self.assertEqual(data['phase_durations_mins']['decision'], 8)

        # Transitioning to QA uses the configured 25 minutes (1500s)
        res_qa = self.client.post(action_url, {'action': 'set_phase', 'phase': 'qa'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_qa.status_code, 200)
        self.assertEqual(res_qa.json()['phase_duration_seconds'], 1500)

        # Concluding presenter advances to next candidate using saved presentation timer
        cand2 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Presenter 2', order=2)
        res_conclude = self.client.post(action_url, {
            'action': 'conclude_candidate',
            'candidate_id': str(cand.pk),
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_conclude.status_code, 200)
        data_conclude = res_conclude.json()
        self.assertEqual(data_conclude['active_candidate_id'], cand2.pk)
        self.assertEqual(data_conclude['active_phase'], 'presentation')
        self.assertEqual(data_conclude['phase_duration_seconds'], 600)
        self.assertEqual(data_conclude['phase_seconds_remaining'], 600)

    def test_panel_live_docket_and_examiners_elements(self):
        hearing = self.create_hearing('Docket Live UI Test', status=PanelHearing.Status.OPEN)
        cand1 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Active Person', order=1)
        cand2 = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Queued Person', order=2)
        hearing.active_candidate = cand1
        hearing.save(update_fields=('active_candidate',))

        live_url = reverse('panel_hearing_live', args=(hearing.pk,))
        res = self.client.get(live_url)
        self.assertEqual(res.status_code, 200)
        # Call to floor for cand2 has confirmation modal data because cand1 is on floor
        self.assertContains(res, 'data-confirm="Candidate Active Person is currently on the floor. Switch to Queued Person?"')
        self.assertContains(res, 'data-confirm-title="Switch presenter on the floor?"')
        # Conclude presenter button has confirmation modal attributes
        self.assertContains(res, 'data-confirm="Conclude presenter Active Person and advance to the next candidate?"')
        self.assertContains(res, 'data-confirm-title="Conclude presenter?"')
        self.assertContains(res, 'data-confirm-label="Conclude presenter"')
        # Examiners section has no Nudge button and no Actions column
        self.assertNotContains(res, 'data-panel-live-action="nudge_examiner"')
        self.assertNotContains(res, '<th scope="col" class="text-end pe-3">Actions</th>')

    def test_timer_auto_advance_progression_across_all_phases(self):
        hearing = self.create_hearing('Timer Progression Test', status=PanelHearing.Status.OPEN)
        cand = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Auto Cand', order=1)
        hearing.active_candidate = cand
        now = timezone.now()
        # Initialize chamber state with expired presentation phase
        hearing.workflow_snapshot = {
            'chamber_state': {
                'active_phase': 'presentation',
                'phase_duration_seconds': 900,
                'phase_seconds_remaining': 0,
                'phase_started_at': (now - timezone.timedelta(seconds=905)).isoformat(),
                'elapsed_before_pause': 0,
                'timer_paused': False,
            }
        }
        hearing.save(update_fields=('active_candidate', 'workflow_snapshot'))

        # Fetch live state to verify transition from presentation to qa
        state_url = reverse('panel_hearing_live_state', args=(hearing.pk,))
        res = self.client.get(state_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['active_phase'], 'qa')
        self.assertEqual(data['phase_duration_seconds'], 1200)

        # Simulate expiration of qa phase
        hearing.refresh_from_db()
        snapshot = dict(hearing.workflow_snapshot or {})
        snapshot['chamber_state']['phase_started_at'] = (timezone.now() - timezone.timedelta(seconds=1205)).isoformat()
        hearing.workflow_snapshot = snapshot
        hearing.save(update_fields=('workflow_snapshot',))

        # Fetch live state to verify transition from qa to deliberation
        res2 = self.client.get(state_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2['active_phase'], 'deliberation')
        self.assertEqual(data2['phase_duration_seconds'], 600)

        # Simulate expiration of deliberation phase
        hearing.refresh_from_db()
        snapshot = dict(hearing.workflow_snapshot or {})
        snapshot['chamber_state']['phase_started_at'] = (timezone.now() - timezone.timedelta(seconds=605)).isoformat()
        hearing.workflow_snapshot = snapshot
        hearing.save(update_fields=('workflow_snapshot',))

        # Fetch live state to verify transition from deliberation to decision
        res3 = self.client.get(state_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res3.status_code, 200)
        data3 = res3.json()
        self.assertEqual(data3['active_phase'], 'decision')
        self.assertEqual(data3['phase_duration_seconds'], 300)

    def test_results_and_deliberation_shows_provisional_scores_and_recorded_decisions(self):
        hearing = self.create_hearing('Deliberation Tab Test', status=PanelHearing.Status.OPEN)
        cand = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Deliberation Candidate', order=1)
        crit = PanelCriterion.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, name='Subject Knowledge', order=1)
        panel = PanelScoringPanel.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, name='Panel 1', share=Decimal('100.00'), minimum_examiners=1)
        crit.panels.add(panel)
        inv = PanelInvitation.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, panel=panel, email='examiner@test.com',
            expires_at=timezone.now() + timezone.timedelta(days=7))
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            candidate=cand, criterion=crit, invitation=inv, score=Decimal('88.50'))
        PanelDecision.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, candidate=cand, outcome=PanelDecision.Outcome.ACCEPT,
            rationale='Consensus achieved during deliberation.', revision=1)

        results_url = reverse('panel_results_review', args=(hearing.pk,))
        res = self.client.get(results_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Provisional scores by candidate')
        self.assertContains(res, 'Deliberation Candidate')
        self.assertContains(res, '88.50')
        self.assertContains(res, 'Committee decisions')
        self.assertContains(res, 'Consensus achieved during deliberation.')
        self.assertContains(res, 'Accept / pass')
        self.assertNotContains(res, 'No score summaries yet')

    def test_panel_results_export_available_during_open_session(self):
        hearing = self.create_hearing('Export Open Hearing', status=PanelHearing.Status.OPEN)
        cand = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, display_name='Export Candidate', reference='REF-EXP', order=1)
        crit = PanelCriterion.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, name='Delivery', order=1)
        panel = PanelScoringPanel.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, name='Panel 1', share=Decimal('100.00'), minimum_examiners=1)
        crit.panels.add(panel)
        inv = PanelInvitation.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, panel=panel, email='examiner@test.com',
            expires_at=timezone.now() + timezone.timedelta(days=7))
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            candidate=cand, criterion=crit, invitation=inv, score=Decimal('91.00'))
        PanelDecision.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=hearing, candidate=cand, outcome=PanelDecision.Outcome.ACCEPT,
            rationale='Approved unanimously.', revision=1)

        export_url = reverse('panel_results_export', args=(hearing.pk,))
        res = self.client.get(export_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('panel-hearing-', res['Content-Disposition'])


