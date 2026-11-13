"""Showcase dashboard and event workspace UI tests."""
from decimal import Decimal
import uuid
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    ShowcaseCategory,
    ShowcaseCertificate,
    ShowcaseConsentAudit,
    ShowcaseEvent,
    ShowcaseEventAudit,
    ShowcaseExhibit,
    ShowcaseJudgeAssignment,
    ShowcaseJudgeEvaluation,
    ShowcaseJudgeExhibitAssignment,
    ShowcaseJudgeInvitation,
    ShowcaseRating,
)
from service_domains.operations import (
    delete_showcase_events,
    issue_showcase_judge_invitation,
    replace_showcase_judge_assignments,
)


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'polls', 'team', 'showcase', 'competition'])
class ShowcaseWorkspaceUiTests(TestCase):

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.org = make_org(f'Showcase UI Org {suffix}')
        self.other_org = make_org(f'Other Showcase Org {suffix}')
        self.owner = make_user(f'showcase-ui-owner-{suffix}')
        self.other = make_user(f'showcase-ui-other-{suffix}')
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        return getattr(self.client, method)(url, *args, **kwargs)

    def create_event(self, name, status=ShowcaseEvent.Status.DRAFT, owner=None, org=None):
        return ShowcaseEvent.objects.create(
            organization_id=(org or self.org).id,
            created_by=owner or self.owner,
            name=name,
            status=status,
            public_results=(status == ShowcaseEvent.Status.PUBLISHED),
        )

    def test_dashboard_metrics_and_visuals(self):
        e_draft = self.create_event('Draft Expo', ShowcaseEvent.Status.DRAFT)
        e_open = self.create_event('Open Fair', ShowcaseEvent.Status.OPEN)
        e_closed = self.create_event('Closed Showcase', ShowcaseEvent.Status.CLOSED)
        e_published = self.create_event('Published Gala', ShowcaseEvent.Status.PUBLISHED)

        # Add exhibits
        ex1 = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=e_open, name='Solar Rover', order=1,
        )
        ex2 = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=e_open, name='Wind Turbine', order=2,
        )
        ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=e_draft, name='Draft Robot', order=1,
        )

        # Add ratings
        ShowcaseRating.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=e_open, exhibit=ex1, device_digest='digest-1', stars=4,
        )
        ShowcaseRating.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=e_open, exhibit=ex2, device_digest='digest-2', stars=5,
        )

        # Add judge invitation and assignment
        invitation, token = issue_showcase_judge_invitation(
            e_open, 'judge@example.test', timezone.now() + timezone.timedelta(days=1),
            organization=self.org.as_claim(), user=self.owner,
        )
        assignment = ShowcaseJudgeExhibitAssignment.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=e_open, invitation=invitation, exhibit=ex1, active=True,
        )
        ShowcaseJudgeEvaluation.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            assignment=assignment, raw_scores={'score': 90}, score=Decimal('90.00'),
        )

        response = self.request('get', reverse('showcase_event_list'))
        self.assertEqual(response.status_code, 200)

        # Verify topbar and hero
        self.assertContains(response, '<span class="topbar-title">Showcase dashboard</span>', html=False)
        self.assertContains(response, 'A live summary of your events, exhibits, visitor ratings, and judge scorecards.')
        self.assertNotContains(response, '<h1>Showcase dashboard</h1>', html=False)
        self.assertContains(response, 'Browse events')
        self.assertContains(response, 'Create event')

        # Verify 6 KPIs
        self.assertEqual(response.context['total_events'], 4)
        self.assertEqual(response.context['open_events'], 1)
        self.assertEqual(response.context['status_counts']['published'], 1)
        self.assertEqual(response.context['total_exhibits'], 3)
        self.assertEqual(response.context['total_ratings'], 2)
        self.assertEqual(response.context['judge_submitted'], 1)
        self.assertEqual(response.context['judge_assigned'], 1)

        # Verify live panel
        self.assertContains(response, 'Open for ratings')
        self.assertContains(response, '1 open event')
        self.assertContains(response, 'View open events')
        self.assertContains(response, 'href="/showcase/organizer/events/?status=open"')
        self.assertContains(response, '2 exhibits')
        self.assertContains(response, '2 visitor ratings')
        self.assertContains(response, '4.5★')
        self.assertContains(response, 'Judge cards: 1 of 1 submitted')

        # Verify stage breakdown conic pie
        dist = {item['key']: item for item in response.context['stage_distribution']}
        self.assertEqual(dist['draft']['count'], 1)
        self.assertEqual(dist['open']['count'], 1)
        self.assertEqual(dist['closed']['count'], 1)
        self.assertEqual(dist['published']['count'], 1)
        for item in dist.values():
            self.assertEqual(item['percentage'], 25.0)
        self.assertIn('conic-gradient(', response.context['stage_pie_background'])

        # Verify largest exhibit bars
        self.assertEqual(response.context['largest_exhibits'], 2)
        top_bar_event = response.context['exhibit_events'][0]
        self.assertEqual(top_bar_event.pk, e_open.pk)
        self.assertEqual(top_bar_event.exhibit_bar_percent, 100.0)

        # Verify recent events table
        self.assertEqual(len(response.context['recent_events']), 4)
        self.assertContains(response, 'Recent events')
        self.assertContains(response, 'Your five most recently created events.')
        self.assertContains(response, 'View all events')

    def test_dashboard_empty_state_when_zero_events(self):
        delete_showcase_events(list(ShowcaseEvent.objects.all()))
        response = self.request('get', reverse('showcase_event_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_events'], 0)
        self.assertEqual(response.context['total_exhibits'], 0)
        self.assertEqual(response.context['total_ratings'], 0)
        self.assertEqual(response.context['judge_submitted'], 0)
        self.assertEqual(response.context['judge_assigned'], 0)
        self.assertEqual(response.context['stage_distribution'][0]['percentage'], 0)

        self.assertContains(response, 'None of your events are open right now')
        self.assertContains(response, 'Open events will appear here with live ratings.')
        self.assertContains(response, 'Exhibit data will appear after exhibits are added to an event.')
        self.assertContains(response, 'Set up your first Showcase event')
        self.assertContains(response, 'Start event setup')

    def test_table_search_status_filter_and_counts(self):
        self.create_event('Science Fair', ShowcaseEvent.Status.DRAFT)
        self.create_event('Science Expo', ShowcaseEvent.Status.OPEN)
        self.create_event('Art Gallery', ShowcaseEvent.Status.CLOSED)

        response = self.request('get', reverse('showcase_event_table'), {
            'q': 'Science', 'status': 'open',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status_counts']['total'], 3)
        self.assertEqual(response.context['status_counts']['draft'], 1)
        self.assertEqual(response.context['status_counts']['open'], 1)
        self.assertEqual(response.context['status_counts']['closed'], 1)
        self.assertEqual(response.context['filtered_count'], 1)
        self.assertEqual([e.name for e in response.context['events']], ['Science Expo'])
        self.assertContains(response, '1 matching event')

        all_resp = self.request('get', reverse('showcase_event_table'), {'status': 'unknown_status'})
        self.assertEqual(all_resp.status_code, 200)
        self.assertEqual(all_resp.context['filtered_count'], 3)

        long_q = 'Science' + 'x' * 95
        long_resp = self.request('get', reverse('showcase_event_table'), {'q': long_q})
        self.assertEqual(len(long_resp.context['search_query']), 100)

    def test_table_ajax_fragment(self):
        self.create_event('Tech Showcase', ShowcaseEvent.Status.OPEN)
        self.create_event('Art Exhibit', ShowcaseEvent.Status.DRAFT)

        response = self.request('get', reverse('showcase_event_table'), {
            'q': 'Tech',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertIn('X-Requested-With', response.headers.get('Vary', ''))
        self.assertContains(response, 'id="showcase-results"')
        self.assertContains(response, 'Tech Showcase')
        self.assertNotContains(response, 'Art Exhibit')
        self.assertNotContains(response, '<html', html=False)

    def test_row_navigation_and_table_columns(self):
        e_draft = self.create_event('Draft Expo', ShowcaseEvent.Status.DRAFT)
        e_open = self.create_event('Open Expo', ShowcaseEvent.Status.OPEN)

        response = self.request('get', reverse('showcase_event_table'))
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, f'data-showcase-row-href="{reverse("showcase_event_manage", args=(e_draft.pk,))}"')
        self.assertContains(response, f'data-showcase-row-href="{reverse("showcase_live_control", args=(e_open.pk,))}"')

        self.assertContains(response, 'Updated')
        self.assertContains(response, 'Showcase events with their status, exhibits, visitor ratings')

    def test_showcase_specific_average_rating_and_judge_zero_assigned(self):
        event_rated = self.create_event('Rated Event', ShowcaseEvent.Status.OPEN)
        exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event_rated, name='Rated Item', order=1,
        )
        ShowcaseRating.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event_rated, exhibit=exhibit, device_digest='digest-a', stars=5,
        )

        event_unrated = self.create_event('Unrated Event', ShowcaseEvent.Status.DRAFT)

        response = self.request('get', reverse('showcase_event_table'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '5.0★')
        self.assertContains(response, '0 / 0')

    def test_kebab_visibility_per_status(self):
        e_draft = self.create_event('Draft Kebab', ShowcaseEvent.Status.DRAFT)
        e_open = self.create_event('Open Kebab', ShowcaseEvent.Status.OPEN)
        e_closed = self.create_event('Closed Kebab', ShowcaseEvent.Status.CLOSED)
        e_published = self.create_event('Published Kebab', ShowcaseEvent.Status.PUBLISHED)

        response = self.request('get', reverse('showcase_event_table'))
        self.assertEqual(response.status_code, 200)

        # Manage event present for all statuses
        for event in (e_draft, e_open, e_closed, e_published):
            self.assertContains(response, reverse('showcase_event_manage', args=(event.pk,)))
            self.assertContains(response, reverse('showcase_event_labels', args=(event.pk,)))
            self.assertContains(response, reverse('showcase_event_delete', args=(event.pk,)))

        # Open live monitor present only for open event
        self.assertContains(response, reverse('showcase_live_monitor', args=(e_open.pk,)))
        self.assertNotContains(response, reverse('showcase_live_monitor', args=(e_draft.pk,)))
        self.assertNotContains(response, reverse('showcase_live_monitor', args=(e_closed.pk,)))
        self.assertNotContains(response, reverse('showcase_live_monitor', args=(e_published.pk,)))

        # Import exhibits present only for draft and open
        self.assertContains(response, reverse('showcase_exhibit_import', args=(e_draft.pk,)))
        self.assertContains(response, reverse('showcase_exhibit_import', args=(e_open.pk,)))
        self.assertNotContains(response, reverse('showcase_exhibit_import', args=(e_closed.pk,)))
        self.assertNotContains(response, reverse('showcase_exhibit_import', args=(e_published.pk,)))

        # View public results present only for published with rel="noopener"
        self.assertContains(response, reverse('showcase_public_results', args=(e_published.public_id,)))
        self.assertContains(response, 'rel="noopener"')
        self.assertNotContains(response, reverse('showcase_public_results', args=(e_draft.public_id,)))

    def test_creation_flow_step_zero_database_rows_on_get(self):
        create_url = reverse('showcase_event_create')
        initial_count = ShowcaseEvent.objects.count()

        # GET: zero rows created, step 1 active, subsequent steps disabled
        get_response = self.request('get', create_url)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(ShowcaseEvent.objects.count(), initial_count)
        self.assertContains(get_response, 'aria-current="step"')
        self.assertContains(get_response, 'disabled')

        # POST invalid: 0 rows created, 400 status
        bad_response = self.request('post', create_url, {'name': ''})
        self.assertEqual(bad_response.status_code, 400)
        self.assertEqual(ShowcaseEvent.objects.count(), initial_count)
        self.assertContains(bad_response, 'This field is required', status_code=400)

        # POST valid: creates event, redirects to showcase_event_manage
        valid_response = self.request('post', create_url, {
            'name': 'Green Tech Showcase',
            'audience_voting': '1',
            'voting_mode': 'single',
            'minimum_judges_per_exhibit': '1',
        })
        self.assertEqual(valid_response.status_code, 302)
        created = ShowcaseEvent.objects.get(name='Green Tech Showcase')
        self.assertEqual(created.created_by, self.owner)
        self.assertEqual(created.organization_id, self.org.id)
        self.assertEqual(valid_response['Location'], f"{reverse('showcase_event_manage', args=(created.pk,))}?step=exhibits")

    def test_row_delete_and_permissions(self):
        event = self.create_event('Event To Delete')
        delete_url = reverse('showcase_event_delete', args=(event.pk,))

        # GET is rejected (405)
        self.assertEqual(self.request('get', delete_url).status_code, 405)

        # Non-owner gets 404
        sign_in(self.client, self.other, self.org)
        self.assertEqual(self.request('post', delete_url).status_code, 404)
        self.assertTrue(ShowcaseEvent.objects.filter(pk=event.pk).exists())

        # Other organization gets 404
        sign_in(self.client, self.owner, self.other_org)
        self.assertEqual(self.request('post', delete_url).status_code, 404)
        self.assertTrue(ShowcaseEvent.objects.filter(pk=event.pk).exists())

        # Owner deletes
        sign_in(self.client, self.owner, self.org)
        response = self.request('post', delete_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('showcase_event_table'))
        self.assertFalse(ShowcaseEvent.objects.filter(pk=event.pk).exists())

    def test_bulk_delete_open_event_with_all_cascades(self):
        event = self.create_event('Open Cascaded Event', ShowcaseEvent.Status.OPEN)
        category = ShowcaseCategory.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, name='Innovation',
        )
        exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, category=category, name='Solar Device', order=1,
        )
        ShowcaseRating.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, exhibit=exhibit, device_digest='digest-cascade', stars=5,
        )
        invitation, _ = issue_showcase_judge_invitation(
            event, 'cascade_judge@example.test', timezone.now() + timezone.timedelta(days=1),
            organization=self.org.as_claim(), user=self.owner,
        )
        assignment = ShowcaseJudgeExhibitAssignment.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, invitation=invitation, exhibit=exhibit, active=True,
        )
        ShowcaseJudgeEvaluation.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            assignment=assignment, raw_scores={'score': 85}, score=Decimal('85.00'),
        )
        ShowcaseEventAudit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, actor_user=self.owner, from_status='draft', to_status='open',
        )
        ShowcaseConsentAudit.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, actor_user=self.owner, reason='All consent forms verified.',
            confirmed_at=timezone.now(),
        )
        ShowcaseCertificate.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            event=event, exhibit=exhibit, category=category,
            award_type='category', award_title='Top Innovation',
        )

        bulk_url = reverse('showcase_event_bulk_delete')
        response = self.request('post', bulk_url, {
            'event_ids': [str(event.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], f"{reverse('showcase_event_table')}?per_page=25")
        self.assertFalse(ShowcaseEvent.objects.filter(pk=event.pk).exists())
        self.assertFalse(ShowcaseExhibit.objects.filter(pk=exhibit.pk).exists())
        self.assertFalse(ShowcaseCertificate.objects.filter(event_id=event.pk).exists())

    def test_bulk_delete_validations_and_atomicity(self):
        e1 = self.create_event('Event 1')
        e2 = self.create_event('Event 2')
        other_event = self.create_event('Other Event', owner=self.other)
        bulk_url = reverse('showcase_event_bulk_delete')

        # GET rejected
        self.assertEqual(self.request('get', bulk_url).status_code, 405)

        # Invalid IDs rejected
        for bad_ids in ([], ['bad'], ['9' * 20], ['1'] * 101):
            with self.subTest(bad_ids=bad_ids):
                res = self.request('post', bulk_url, {'event_ids': bad_ids})
                self.assertEqual(res.status_code, 302)
                self.assertTrue(ShowcaseEvent.objects.filter(pk=e1.pk).exists())

        # Unowned ID mixed in: atomically rejects whole batch
        mixed_res = self.request('post', bulk_url, {
            'event_ids': [str(e1.pk), str(other_event.pk)],
        })
        self.assertEqual(mixed_res.status_code, 302)
        self.assertTrue(ShowcaseEvent.objects.filter(pk=e1.pk).exists())
        self.assertTrue(ShowcaseEvent.objects.filter(pk=other_event.pk).exists())

        # Duplicate IDs handled cleanly
        dup_res = self.request('post', bulk_url, {
            'event_ids': [str(e1.pk), str(e1.pk)],
            'q': 'Event',
            'status': 'draft',
            'per_page': '50',
        })
        self.assertEqual(dup_res.status_code, 302)
        self.assertEqual(dup_res['Location'], f"{reverse('showcase_event_table')}?q=Event&status=draft&per_page=50")
        self.assertFalse(ShowcaseEvent.objects.filter(pk=e1.pk).exists())
        self.assertTrue(ShowcaseEvent.objects.filter(pk=e2.pk).exists())

    def test_pagination_boundaries_and_page_sizes(self):
        delete_showcase_events(list(ShowcaseEvent.objects.all()))
        events = [self.create_event(f'Event {i:02d}') for i in range(25)]

        res25 = self.request('get', reverse('showcase_event_table'))
        self.assertEqual(res25.context['paginator'].num_pages, 1)
        self.assertNotContains(res25, 'class="showcase-pagination"')

        e26 = self.create_event('Event 25')
        res26 = self.request('get', reverse('showcase_event_table'))
        self.assertEqual(res26.context['paginator'].num_pages, 2)
        self.assertContains(res26, 'Page 1 of 2 · 26 events')

        # Invalid per_page falls back to 25
        res_bad_per_page = self.request('get', reverse('showcase_event_table'), {'per_page': '10'})
        self.assertEqual(res_bad_per_page.context['per_page'], 25)

        # Valid page sizes: 50
        res50 = self.request('get', reverse('showcase_event_table'), {'per_page': '50'})
        self.assertEqual(res50.context['per_page'], 50)
        self.assertEqual(res50.context['paginator'].num_pages, 1)

        # Bad page numbers
        res_page_x = self.request('get', reverse('showcase_event_table'), {'page': 'x'})
        self.assertEqual(res_page_x.context['page_obj'].number, 1)
        res_page_high = self.request('get', reverse('showcase_event_table'), {'page': '999'})
        self.assertEqual(res_page_high.context['page_obj'].number, 2)

        # Deleting last item of last page lands on valid page
        delete_res = self.request('post', reverse('showcase_event_delete', args=(e26.pk,)))
        self.assertEqual(delete_res.status_code, 302)
        res_after = self.request('get', reverse('showcase_event_table'), {'page': '2'})
        self.assertEqual(res_after.context['page_obj'].number, 1)

    def test_sidebar_active_states(self):
        event = self.create_event('Sidebar Event')
        list_url = reverse('showcase_event_list')
        table_url = reverse('showcase_event_table')
        manage_url = reverse('showcase_event_manage', args=(event.pk,))
        monitor_url = reverse('showcase_live_monitor', args=(event.pk,))
        labels_url = reverse('showcase_event_labels', args=(event.pk,))
        import_url = reverse('showcase_exhibit_import', args=(event.pk,))
        create_url = reverse('showcase_event_create')

        res_list = self.request('get', list_url)
        self.assertContains(res_list, 'Showcase Service')
        self.assertContains(res_list, 'Showcase dashboard')
        self.assertContains(res_list, 'Events')
        self.assertNotContains(res_list, 'Create event</span>')

        # Active state on list
        self.assertContains(res_list, f'href="{list_url}" class="nav-link active" aria-current="page"')
        self.assertNotContains(res_list, f'href="{table_url}" class="nav-link active"')

        # Active state on table
        res_table = self.request('get', table_url)
        self.assertContains(res_table, f'href="{table_url}" class="nav-link active" aria-current="page"')
        self.assertNotContains(res_table, f'href="{list_url}" class="nav-link active"')

        # Active state on manage
        res_manage = self.request('get', manage_url)
        self.assertContains(res_manage, f'href="{table_url}" class="nav-link active" aria-current="page"')

        # Active state on live monitor (open event)
        event.status = ShowcaseEvent.Status.OPEN
        event.save(update_fields=('status',))
        res_monitor = self.request('get', monitor_url)
        self.assertEqual(res_monitor.status_code, 302)

        # Active state on import
        res_import = self.request('get', import_url)
        self.assertContains(res_import, f'href="{table_url}" class="nav-link active" aria-current="page"')

        # Create page has no active dashboard or events link, no create event item in sidebar
        res_create = self.request('get', create_url)
        self.assertNotContains(res_create, f'href="{list_url}" class="nav-link active"')
        self.assertNotContains(res_create, f'href="{table_url}" class="nav-link active"')
        self.assertNotContains(res_create, 'Create event</span>')

    def test_query_count_is_independent_of_row_count(self):
        delete_showcase_events(list(ShowcaseEvent.objects.all()))
        for i in range(3):
            e = self.create_event(f'Small Set {i}')
            ShowcaseExhibit.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                event=e, name=f'Ex {i}', order=1,
            )

        with CaptureQueriesContext(connection) as ctx_small:
            self.request('get', reverse('showcase_event_table'))
        count_small = len(ctx_small.captured_queries)

        for i in range(12):
            e = self.create_event(f'Large Set {i}')
            ShowcaseExhibit.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                event=e, name=f'Ex Large {i}', order=1,
            )

        with CaptureQueriesContext(connection) as ctx_large:
            self.request('get', reverse('showcase_event_table'))
        count_large = len(ctx_large.captured_queries)

        self.assertEqual(count_small, count_large)

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        for path in (
            reverse('showcase_event_list'),
            reverse('showcase_event_table'),
            reverse('showcase_event_create'),
        ):
            with self.subTest(path=path):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 302)
                self.assertIn(res['Location'], ['/showcase/', '/accounts/sign-in/'])

        res_post = self.client.post(reverse('showcase_event_bulk_delete'))
        self.assertEqual(res_post.status_code, 302)
        self.assertIn(res_post['Location'], ['/showcase/', '/accounts/sign-in/'])

    def test_showcase_live_control_console_lifecycle_and_actions(self):
        event = self.create_event('Live Science Expo', status=ShowcaseEvent.Status.OPEN)
        exhibit1 = ShowcaseExhibit.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            event=event,
            name='Solar Rover',
            order=1,
        )
        exhibit2 = ShowcaseExhibit.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            event=event,
            name='Hydro Filter',
            order=2,
        )
        invitation = ShowcaseJudgeInvitation.objects.create(
            organization_id=self.org.id,
            created_by=self.owner,
            event=event,
            email='judge@example.com',
            token_digest=ShowcaseJudgeInvitation.digest_token('a' * 32),
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )

        # GET console
        url = reverse('showcase_live_control', kwargs={'event_id': event.pk})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Exhibition Live Command')
        self.assertContains(res, 'Solar Rover')
        self.assertContains(res, 'Hydro Filter')

        # GET live state JSON
        state_url = reverse('showcase_live_state', kwargs={'event_id': event.pk})
        res_state = self.client.get(state_url)
        self.assertEqual(res_state.status_code, 200)
        state_data = res_state.json()
        self.assertTrue(state_data.get('ok'))
        self.assertIn('hero_html', state_data)
        self.assertIn('stage_html', state_data)
        self.assertIn('table_html', state_data)
        self.assertIn('projector_html', state_data)

        # Action: toggle visitor voting
        action_url = reverse('showcase_live_action', kwargs={'event_id': event.pk})
        res_action = self.client.post(action_url, {'action': 'toggle_visitor_voting'})
        self.assertEqual(res_action.status_code, 200)
        event.refresh_from_db()
        self.assertTrue(event.workflow_snapshot['floor_state']['visitor_voting_paused'])

        # Action: toggle judge scoring
        res_action = self.client.post(action_url, {'action': 'toggle_judge_scoring'})
        self.assertEqual(res_action.status_code, 200)
        event.refresh_from_db()
        self.assertTrue(event.workflow_snapshot['floor_state']['judge_scoring_paused'])

        # Action: stage_set_exhibit
        res_action = self.client.post(action_url, {'action': 'stage_set_exhibit', 'exhibit_id': str(exhibit1.id)})
        self.assertEqual(res_action.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.workflow_snapshot['stage_state']['active_exhibit_id'], exhibit1.id)

        # Action: stage_adjust_timer
        res_action = self.client.post(action_url, {'action': 'stage_adjust_timer', 'delta': 60})
        self.assertEqual(res_action.status_code, 200)
        event.refresh_from_db()
        from service_domains.operations import showcase_pitch_timer
        pitch = showcase_pitch_timer(event.workflow_snapshot['stage_state'], timezone.now().timestamp())
        self.assertGreaterEqual(pitch['remaining'], 239)
        self.assertEqual(pitch['state'], 'running')

        # Action: stage_toggle_pause
        res_action = self.client.post(action_url, {'action': 'stage_toggle_pause'})
        self.assertEqual(res_action.status_code, 200)
        event.refresh_from_db()
        self.assertTrue(event.workflow_snapshot['stage_state']['timer_paused'])

        # Action: set_projector_mode
        res_action = self.client.post(action_url, {'action': 'set_projector_mode', 'mode': 'leaderboard'})
        self.assertEqual(res_action.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.workflow_snapshot['floor_state']['projector_mode'], 'leaderboard')

        # Action: assign roving judge
        res_action = self.client.post(action_url, {
            'action': 'assign_roving_judge',
            'judge_invitation_id': str(invitation.id),
            'exhibit_id': str(exhibit2.id),
        })
        self.assertEqual(res_action.status_code, 200)
        self.assertTrue(
            ShowcaseJudgeExhibitAssignment.objects.filter(
                invitation=invitation, exhibit=exhibit2
            ).exists()
        )

        # Tenant isolation check
        other_client = self.client_class()
        sign_in(other_client, self.other, self.other_org)
        res_other = other_client.get(url)
        self.assertEqual(res_other.status_code, 404)

    def test_setup_stepper_uses_the_shared_partial_for_create_and_manage(self):
        created = self.client.get(reverse('showcase_event_create'))
        steps = created.context['stepper_steps']
        self.assertEqual(len(steps), 6)
        self.assertTrue(steps[0]['current'])
        self.assertEqual(steps[1]['desc'], 'Import your exhibit list')
        self.assertTrue(all(s['disabled'] for s in steps[1:]))
        self.assertContains(created, 'aria-label="Event setup steps"')
        self.assertNotContains(created, 'sc-stepper')

        event = self.create_event('Stepper Expo')
        manage = self.client.get(reverse('showcase_event_manage', kwargs={'event_id': event.pk}))
        steps = manage.context['stepper_steps']
        self.assertEqual([s['key'] for s in steps][0], 'details')
        self.assertTrue(steps[0]['current'])
        self.assertContains(manage, 'DevPerfStepper.setCurrent(root,active)')

    def test_showcase_event_manage_stepper_edit_mode_and_back_routing(self):
        # Open event setup edit view
        event = self.create_event('Open Innovation Showcase', ShowcaseEvent.Status.OPEN)
        manage_url = reverse('showcase_event_manage', kwargs={'event_id': event.pk})

        # Verify open event lands on vertical stepper in edit mode
        res = self.client.get(manage_url)
        self.assertEqual(res.status_code, 200)
        content = res.content.decode('utf-8')
        self.assertIn('panel-step-layout', content)
        self.assertIn('stepper-nav', content)
        self.assertIn('name="name"', content)
        self.assertNotIn('class="showcase-nav"', content)
        self.assertNotIn('Exhibition Live Command', content)

        # Default back link for open event points to live control
        live_control_url = reverse('showcase_live_control', kwargs={'event_id': event.pk})
        self.assertIn(live_control_url, content)
        self.assertIn('Back to live control', content)

        # Origin query parameter routing: from table
        res_table = self.client.get(f'{manage_url}?from=table')
        self.assertEqual(res_table.status_code, 200)
        table_content = res_table.content.decode('utf-8')
        self.assertIn(reverse('showcase_event_table'), table_content)
        self.assertIn('All events', table_content)

        # Origin query parameter routing: from live
        res_live = self.client.get(f'{manage_url}?from=live')
        self.assertEqual(res_live.status_code, 200)
        live_content = res_live.content.decode('utf-8')
        self.assertIn(live_control_url, live_content)
        self.assertIn('Back to live control', live_content)

        # Draft event default back link points to showcases dashboard
        draft_event = self.create_event('Draft Expo', ShowcaseEvent.Status.DRAFT)
        draft_manage_url = reverse('showcase_event_manage', kwargs={'event_id': draft_event.pk})
        res_draft = self.client.get(draft_manage_url)
        self.assertEqual(res_draft.status_code, 200)
        draft_content = res_draft.content.decode('utf-8')
        self.assertIn(reverse('showcase_event_list'), draft_content)
        self.assertIn('All showcases', draft_content)

        # Save details on open event in edit mode
        post_res = self.client.post(manage_url, {
            'action': 'edit',
            'name': 'Updated Showcase Name',
            'branding_tagline': 'New tagline for event',
            'from': 'live',
        })
        self.assertEqual(post_res.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.name, 'Updated Showcase Name')
        self.assertEqual(event.branding.get('tagline'), 'New tagline for event')

    def test_showcase_live_control_backend_timer_and_silent_state(self):
        event = self.create_event('Live Pitch Event', ShowcaseEvent.Status.OPEN)
        category = ShowcaseCategory.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=event, name='AI & Data', order=1
        )
        exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=event, category=category,
            name='Autonomous Assistant', order=1
        )
        action_url = reverse('showcase_live_action', kwargs={'event_id': event.pk})
        state_url = reverse('showcase_live_state', kwargs={'event_id': event.pk})

        # Set exhibit on main stage
        set_res = self.client.post(action_url, {'action': 'stage_set_exhibit', 'exhibit_id': str(exhibit.pk)})
        self.assertEqual(set_res.status_code, 200)

        # Poll live state silently
        state_res = self.client.get(state_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(state_res.status_code, 200)
        data = state_res.json()
        self.assertTrue(data['ok'])
        self.assertIn('hero_html', data)
        self.assertIn('stage_html', data)
        self.assertIn('table_html', data)
        self.assertIn('projector_html', data)
        self.assertFalse(data['timer_paused'])
        self.assertGreaterEqual(data['pitch_seconds_remaining'], 179)

        # Pause countdown
        pause_res = self.client.post(action_url, {'action': 'stage_toggle_pause'})
        self.assertEqual(pause_res.status_code, 200)
        pause_data = pause_res.json()
        self.assertTrue(pause_data['timer_paused'])

        # Adjust paused timer
        adj_res = self.client.post(action_url, {'action': 'stage_adjust_timer', 'delta': 30})
        self.assertEqual(adj_res.status_code, 200)
        adj_data = adj_res.json()
        self.assertGreaterEqual(adj_data['pitch_seconds_remaining'], 209)

        # Resume countdown
        resume_res = self.client.post(action_url, {'action': 'stage_toggle_pause'})
        self.assertEqual(resume_res.status_code, 200)
        resume_data = resume_res.json()
        self.assertFalse(resume_data['timer_paused'])
