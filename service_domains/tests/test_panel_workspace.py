"""Panel dashboard and assessment browser UI tests."""
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
    PanelHearing,
    PanelInvitation,
    PanelResultSnapshot,
    PanelScoringPanel,
)
from service_domains.operations import issue_panel_invitation
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
        self.assertContains(response, 'data-panel-row-href="' + reverse('panel_hearing_manage', args=[open_hearing.pk]) + '"')
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
        self.assertContains(response, 'Review and open')

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

