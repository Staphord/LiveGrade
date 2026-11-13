"""Showcase rubric spreadsheet import, the draft launch page and the setup-flow fixes."""
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains import showcase_rubric_import as rubric
from service_domains.models import (
    ShowcaseEvent, ShowcaseExhibit, ShowcaseJudgeCriterion,
)
from django.core.exceptions import PermissionDenied, ValidationError

from service_domains.operations import (
    replace_showcase_judge_criteria, save_showcase_judge_criterion,
    showcase_rubric_total, transition_showcase_event,
)
from service_domains.views import (
    SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY, SHOWCASE_RUBRIC_IMPORT_SESSION_KEY,
    SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT,
)

HEADERS = ['Criterion', 'Description', 'Weight', 'Scale', 'Max points', 'Checklist items']
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def sheet(rows, headers=HEADERS):
    return SimpleUploadedFile('rubric.xlsx', workbook_bytes(headers, rows).getvalue(), content_type=XLSX)


class ParseRubricTests(SimpleTestCase):
    def parse(self, rows, headers=HEADERS):
        return rubric.parse_rubric(sheet(rows, headers))

    def test_valid_rows_cover_every_scale(self):
        parsed = self.parse([
            ['Innovation', 'Idea', 40, 'Points', 50, ''],
            ['Delivery', '', 30, 'five stars', '', ''],
            ['Pass', '', 10, 'Pass or fail', '', ''],
            ['Safety', '', 20, 'Checklist', '', 'Guards\nLabels'],
        ])

        self.assertIsNone(parsed['fatal'])
        self.assertEqual(parsed['errors'], [])
        rows = parsed['rows']
        self.assertEqual([r['scale'] for r in rows], ['points', 'stars', 'pass_fail', 'checklist'])
        self.assertEqual(rows[0]['max_points'], '50')
        self.assertEqual(rows[3]['checklist_items'], ['Guards', 'Labels'])

    def test_blank_scale_defaults_to_points_with_100_max(self):
        row = self.parse([['A', '', 100, '', '', '']])['rows'][0]
        self.assertEqual((row['scale'], row['max_points'], row['errors']), ('points', '100', []))

    def test_row_level_errors(self):
        rows = self.parse([
            ['', 'x', 'abc', 'Mystery', 'bad', ''],
            ['Bad weight', 'd', 101, 'Points', 5000, ''],
            ['Stars', '', 10, 'Stars', 5, 'item'],
            ['Check', '', 10, 'Checklist', '', ''],
            ['N' * 121, 'D' * 1001, 1.234, 'Checklist', '', '\n'.join(['i'] * 21)],
            ['Long item', '', 10, 'Checklist', '', 'z' * 241],
        ])['rows']
        joined = [' | '.join(r['errors']) for r in rows]

        self.assertIn('name is required', joined[0])
        self.assertIn('Weight must be', joined[0])
        self.assertIn('Scale must be', joined[0])
        self.assertIn('Max points must be', joined[0])
        self.assertIn('Weight must be', joined[1])
        self.assertIn('Max points must be', joined[1])
        self.assertIn('Max points apply only', joined[2])
        self.assertIn('Checklist items apply only', joined[2])
        self.assertIn('A checklist needs', joined[3])
        self.assertIn('longer than 120', joined[4])
        self.assertIn('Description is longer', joined[4])
        self.assertIn('Weight must be', joined[4])
        self.assertIn('A checklist needs', joined[4])
        self.assertIn('A checklist needs', joined[5])

    def test_formula_cells_are_rejected(self):
        raw = [HEADERS, ['=cmd()', 'd', 10, 'Points', '', ''], ['Ok', '=1+1', 90, 'Points', '', '']]
        with patch.object(rubric, '_read_bounded_rows', return_value=raw):
            rows = rubric.parse_rubric(SimpleUploadedFile('r.xlsx', b'x'))['rows']
        self.assertTrue(all('Formula cells' in ' '.join(r['errors']) for r in rows))

    def test_non_finite_weight_and_duplicate_names(self):
        rows = self.parse([['Same', '', 'NaN', '', '', ''], ['same', '', 50, '', '', '']])['rows']
        self.assertIn('Weight must be', rows[0]['errors'][0])
        self.assertTrue(all('repeated in rows 2, 3' in r['errors'][-1] for r in rows))

    def test_fatal_files(self):
        self.assertEqual(rubric.parse_rubric(SimpleUploadedFile('x.xlsx', b'not a zip'))['fatal'],
                         rubric.UNREADABLE)
        self.assertIn('empty', self.parse([], HEADERS[:0] or [''])['fatal'])
        self.assertIn('Criterion and Weight', self.parse([['a']], ['Name'])['fatal'])
        self.assertIn('more than 50', self.parse([[f'c{i}', '', 1, '', '', ''] for i in range(51)])['fatal'])
        with patch.object(rubric, 'MAX_UPLOAD_BYTES', 1):
            self.assertIn('10 MB', self.parse([['a', '', 1, '', '', '']])['fatal'])
        with patch.object(rubric, '_read_bounded_rows', return_value=rubric.TOO_MANY_PHYSICAL_ROWS):
            self.assertIn('physical row limit', self.parse([['a', '', 1, '', '', '']])['fatal'])

    def test_sample_workbook_is_a_valid_rubric(self):
        parsed = rubric.parse_rubric(SimpleUploadedFile('s.xlsx', rubric.sample_workbook().getvalue()))
        self.assertEqual(parsed['errors'], [])
        self.assertEqual(sum(Decimal(r['weight']) for r in parsed['rows']), 100)


class RubricPlanTests(TestCase):
    def setUp(self):
        self.org = make_org('Rubric plan org')
        self.owner = make_user('rubric-plan-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Plan expo')

    def parsed(self, rows):
        return rubric.parse_rubric(sheet(rows))

    def test_plan_requires_total_of_100_and_counts_replacements(self):
        short = rubric.plan_rubric(self.event, self.parsed([['A', '', 60, '', '', '']]))
        self.assertFalse(short['ok'])
        self.assertIn('totals 60', short['errors'][0][1])

        replace_showcase_judge_criteria(self.event,
            [{'name': 'Old', 'weight': 100}], organization=self.org.as_claim(), user=self.owner)
        ok = rubric.plan_rubric(self.event, self.parsed([['A', '', 100, '', '', '']]))
        self.assertTrue(ok['ok'])
        self.assertEqual((ok['replaces'], ok['criteria_to_create']), (1, 1))

    def test_plan_reports_fatal_and_empty_files(self):
        fatal = rubric.plan_rubric(self.event, {'fatal': 'Broken.'})
        self.assertEqual((fatal['ok'], fatal['errors']), (False, [(None, 'Broken.')]))
        empty = rubric.plan_rubric(self.event, {'fatal': None, 'rows': [], 'errors': []})
        self.assertFalse(empty['ok'])
        self.assertIn('no criteria rows', empty['errors'][0][1])

    def test_apply_replaces_the_rubric_and_rejects_bad_plans(self):
        from django.core.exceptions import ValidationError
        replace_showcase_judge_criteria(self.event,
            [{'name': 'Old', 'weight': 100}], organization=self.org.as_claim(), user=self.owner)
        rubric.apply_rubric_import(self.event, self.parsed([
            ['Idea', 'd', 60, 'Points', 20, ''], ['Safety', '', 40, 'Checklist', '', 'One\nTwo'],
        ]), organization=self.org.as_claim(), user=self.owner)

        saved = list(ShowcaseJudgeCriterion.objects.filter(event=self.event))
        self.assertEqual([c.name for c in saved], ['Idea', 'Safety'])
        self.assertEqual(saved[0].max_points, Decimal('20'))
        self.assertEqual(saved[1].checklist_items, ['One', 'Two'])
        with self.assertRaises(ValidationError):
            rubric.apply_rubric_import(self.event, self.parsed([['A', '', 10, '', '', '']]),
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(ShowcaseJudgeCriterion.objects.filter(event=self.event).count(), 2)


class RubricImportViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Rubric view org')
        self.owner = make_user('rubric-view-owner')
        self.other = make_user('rubric-view-other')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Rubric expo')
        self.import_url = reverse('showcase_rubric_import', args=(self.event.pk,))
        self.manage_url = reverse('showcase_event_manage', args=(self.event.pk,))
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def preview(self, rows, **extra):
        return self.request('post', self.import_url, {'file': sheet(rows), **extra})

    def token(self):
        return self.client.session[SHOWCASE_RUBRIC_IMPORT_SESSION_KEY]

    def criteria_url(self, suffix=''):
        return f'{self.manage_url}?step=criteria{suffix}'

    def test_criteria_step_has_the_established_dropzone_and_manual_form(self):
        page = self.request('get', self.criteria_url())

        self.assertContains(page, 'data-import-dropzone')
        self.assertContains(page, 'id="showcase-rubric-dropzone"')
        self.assertContains(page, 'Import rubric spreadsheet')
        self.assertContains(page, 'Preview rubric')
        self.assertContains(page, reverse('showcase_rubric_import_sample', args=(self.event.pk,)))
        self.assertContains(page, 'Add a criterion')
        self.assertContains(page, '.import-dropzone input[type=file]{position:absolute')

    def test_preview_then_apply_replaces_the_rubric(self):
        response = self.preview([['Idea', '', 60, '', '', ''], ['Talk', '', 40, 'Stars', '', '']])
        self.assertRedirects(response, self.criteria_url(), fetch_redirect_response=False)
        self.assertEqual(ShowcaseJudgeCriterion.objects.count(), 0)

        page = self.request('get', self.criteria_url())
        self.assertContains(page, 'Rubric preview (2 criteria, 100% total)')
        self.assertContains(page, 'Ready to import')
        self.assertContains(page, 'Import 2 criteria')
        self.assertEqual(page.context['active_step'], 'criteria')

        applied = self.request('post', self.import_url,
                               {'action': 'apply', 'preview_token': self.token()})
        self.assertRedirects(applied, self.criteria_url(), fetch_redirect_response=False)
        self.assertEqual(ShowcaseJudgeCriterion.objects.filter(event=self.event).count(), 2)
        self.assertNotIn(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, self.client.session)

    def test_preview_shows_replacement_note_and_problems(self):
        replace_showcase_judge_criteria(self.event, [{'name': 'Old', 'weight': 100}],
            organization=self.org.as_claim(), user=self.owner)
        self.preview([['Idea', '', 100, '', '', '']])
        self.assertContains(self.request('get', self.criteria_url()), 'replaces the 1 saved criterion')

        self.preview([['Idea', '', 50, '', '', '']])
        page = self.request('get', self.criteria_url())
        self.assertContains(page, 'Needs attention')
        self.assertContains(page, 'must total exactly 100%')
        self.assertNotContains(page, 'Import 1 criterion')

    def test_cancel_clears_the_preview(self):
        self.preview([['Idea', '', 100, '', '', '']])
        response = self.request('post', self.import_url, {'action': 'cancel', 'from': 'live'})

        self.assertRedirects(response, self.criteria_url('&from=live'), fetch_redirect_response=False)
        self.assertNotIn(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, self.client.session)

    def test_invalid_uploads_show_one_time_errors(self):
        wrong = SimpleUploadedFile('rubric.csv', b'a,b')
        self.request('post', self.import_url, {'file': wrong})
        page = self.request('get', self.criteria_url())
        self.assertContains(page, 'Choose an Excel .xlsx file.')
        self.assertNotContains(self.request('get', self.criteria_url()), 'Choose an Excel .xlsx file.')

        self.request('post', self.import_url, {'action': 'preview'})
        self.assertContains(self.request('get', self.criteria_url()), 'This field is required.')

    def test_unreadable_file_previews_as_a_fatal_error(self):
        self.request('post', self.import_url, {'file': SimpleUploadedFile('r.xlsx', b'junk')})
        page = self.request('get', self.criteria_url())
        self.assertContains(page, 'Needs attention')

    def test_apply_with_bad_or_foreign_tokens(self):
        for token, expected in (('', 'This field is required.'), ('garbage', 'expired or is invalid')):
            self.request('post', self.import_url, {'action': 'apply', 'preview_token': token})
            self.assertContains(self.request('get', self.criteria_url()), expected)
        self.assertEqual(ShowcaseJudgeCriterion.objects.count(), 0)

        parsed = rubric.parse_rubric(sheet([['Idea', '', 100, '', '', '']]))
        foreign = signing.dumps({'event': self.event.pk, 'organization': self.org.id,
                                 'user': self.other.pk, 'parsed': parsed},
                                salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT)
        self.assertEqual(self.request('post', self.import_url,
            {'action': 'apply', 'preview_token': foreign}).status_code, 404)
        malformed = signing.dumps({'event': self.event.pk, 'organization': self.org.id,
                                   'user': self.owner.pk, 'parsed': 'nope'},
                                  salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT)
        self.assertEqual(self.request('post', self.import_url,
            {'action': 'apply', 'preview_token': malformed}).status_code, 404)
        self.assertEqual(ShowcaseJudgeCriterion.objects.count(), 0)

    def test_apply_after_the_event_opens_is_refused(self):
        self.preview([['Idea', '', 100, '', '', '']])
        token = self.token()
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.OPEN)

        self.assertEqual(self.request('post', self.import_url,
            {'action': 'apply', 'preview_token': token}).status_code, 404)
        self.assertEqual(ShowcaseJudgeCriterion.objects.count(), 0)

    def test_apply_with_an_invalid_plan_reports_the_error(self):
        parsed = rubric.parse_rubric(sheet([['Idea', '', 50, '', '', '']]))
        stale = signing.dumps({'event': self.event.pk, 'organization': self.org.id,
                               'user': self.owner.pk, 'parsed': parsed},
                              salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT)
        self.request('post', self.import_url, {'action': 'apply', 'preview_token': stale})

        self.assertContains(self.request('get', self.criteria_url()), 'The rubric import has errors')
        self.assertEqual(ShowcaseJudgeCriterion.objects.count(), 0)

    def test_stored_previews_and_errors_are_scoped_to_the_owner_and_event(self):
        session = self.client.session
        session[SHOWCASE_RUBRIC_IMPORT_SESSION_KEY] = 'tampered'
        session[SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY] = {
            'event': self.event.pk, 'organization': self.org.id, 'user': self.other.pk,
            'errors': ['Someone else error']}
        session.save()
        page = self.request('get', self.criteria_url())
        self.assertNotContains(page, 'Someone else error')
        self.assertNotIn(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, self.client.session)

        other_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other event')
        parsed = rubric.parse_rubric(sheet([['Idea', '', 100, '', '', '']]))
        session = self.client.session
        session[SHOWCASE_RUBRIC_IMPORT_SESSION_KEY] = signing.dumps(
            {'event': other_event.pk, 'organization': self.org.id, 'user': self.owner.pk,
             'parsed': parsed}, salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT)
        session[SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY] = {
            'event': self.event.pk, 'organization': self.org.id, 'user': self.owner.pk,
            'errors': 'not a list'}
        session.save()
        self.assertNotContains(self.request('get', self.criteria_url()), 'Rubric preview')
        self.assertNotIn(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, self.client.session)

    def test_oversized_stored_preview_is_ignored_and_only_drafts_load_one(self):
        session = self.client.session
        session[SHOWCASE_RUBRIC_IMPORT_SESSION_KEY] = 'x' * 150_001
        session.save()
        self.assertNotContains(self.request('get', self.criteria_url()), 'Rubric preview')
        oversized = self.request('post', self.import_url,
            {'action': 'apply', 'preview_token': 'x' * 150_001})
        self.assertRedirects(oversized, self.criteria_url(), fetch_redirect_response=False)

    def test_sample_download_and_access_control(self):
        sample = self.request('get', reverse('showcase_rubric_import_sample', args=(self.event.pk,)))
        self.assertEqual(sample['Content-Type'], XLSX)
        self.assertIn('showcase-rubric-sample.xlsx', sample['Content-Disposition'])
        self.assertEqual(self.request('get', self.import_url).status_code, 405)

        sign_in(self.client, self.other, self.org)
        self.assertEqual(self.request('post', self.import_url, {'action': 'cancel'}).status_code, 404)
        self.assertEqual(self.request('get',
            reverse('showcase_rubric_import_sample', args=(self.event.pk,))).status_code, 404)


class ShowcaseSetupFlowTests(TestCase):
    def setUp(self):
        self.org = make_org('Flow org')
        self.owner = make_user('showcase-flow-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Flow expo', minimum_judges_per_exhibit=0)
        self.manage_url = reverse('showcase_event_manage', args=(self.event.pk,))
        self.live_url = reverse('showcase_live_control', args=(self.event.pk,))
        self.transition_url = reverse('showcase_event_transition', args=(self.event.pk,))
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def add_exhibit(self):
        return ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Pump')

    def test_create_page_drops_the_what_happens_next_note(self):
        page = self.request('get', reverse('showcase_event_create'))
        self.assertNotContains(page, 'What happens next')

    def test_later_steps_are_not_ticked_before_they_are_done(self):
        steps = {s['key']: s for s in self.request('get', self.manage_url).context['stepper_steps']}
        self.assertTrue(steps['details']['done'])
        for key in ('exhibits', 'criteria', 'judges-consent'):
            self.assertFalse(steps[key]['done'], key)

        self.add_exhibit()
        steps = {s['key']: s for s in self.request('get', self.manage_url).context['stepper_steps']}
        self.assertTrue(steps['exhibits']['done'])
        self.assertTrue(steps['judges-consent']['done'])

    def test_review_step_is_read_only_and_points_to_live_control(self):
        page = self.request('get', f'{self.manage_url}?step=open')

        self.assertContains(page, 'id="review-heading"')
        self.assertContains(page, 'You open the event from live control')
        self.assertContains(page, f'href="{self.live_url}"')
        self.assertContains(page, 'Finish the required steps')
        self.assertNotContains(page, 'Open Showcase event')
        self.assertNotContains(page, reverse('showcase_event_transition', args=(self.event.pk,)))

        self.add_exhibit()
        replace_showcase_judge_criteria(self.event, [{'name': 'A', 'weight': 100}],
            organization=self.org.as_claim(), user=self.owner)
        ready = self.request('get', f'{self.manage_url}?step=open')
        self.assertContains(ready, 'Setup is complete. Open live control')

    def test_review_step_for_running_and_published_events(self):
        self.add_exhibit()
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.OPEN)
        open_page = self.request('get', self.manage_url)
        self.assertContains(open_page, 'Run it from live control.')
        self.assertContains(open_page, 'Go to live control')

        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.PUBLISHED)
        published = self.request('get', self.manage_url)
        self.assertContains(published, 'View results and awards')

    def test_draft_console_lists_blockers_and_disables_opening(self):
        page = self.request('get', self.live_url)

        self.assertTemplateUsed(page, 'service_domains/showcase_live_control.html')
        self.assertFalse(page.context['event_ready'])
        self.assertContains(page, 'Before you open')
        self.assertContains(page, 'Finish the required setup steps above')
        self.assertContains(page, 'Needs attention: ')
        self.assertContains(page, 'Optional: ')
        self.assertContains(page, 'id="confirm-showcase-open-btn"')
        self.assertContains(page, 'Finish the required setup steps first')
        self.assertContains(page, 'disabled title=')
        self.assertNotContains(page, 'id="showcase-live-stage"')
        self.assertNotContains(page, 'id="showcase-exhibit-radar-table"')

    def test_ready_draft_console_enables_opening_and_open_action_opens_the_event(self):
        self.add_exhibit()
        replace_showcase_judge_criteria(self.event, [{'name': 'A', 'weight': 100}],
            organization=self.org.as_claim(), user=self.owner)
        ready = self.request('get', self.live_url)
        self.assertTrue(ready.context['event_ready'])
        self.assertContains(ready, 'Setup is complete: 1 exhibit ready.')
        self.assertContains(ready, 'Complete: ')
        self.assertNotContains(ready, 'disabled title="Finish the required setup steps first"')

        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        opened = self.request('post', action_url, {'action': 'open_event'})
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.json()['redirect'], self.live_url)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.OPEN)
        self.assertContains(self.request('get', self.live_url), 'id="confirm-showcase-close-ratings-btn"')

    def test_open_action_refuses_unready_events_and_other_states(self):
        action_url = reverse('showcase_live_action', args=(self.event.pk,))
        blocked = self.request('post', action_url, {'action': 'open_event'})
        self.assertEqual(blocked.status_code, 400)
        self.assertIn('Add at least one exhibit', blocked.json()['error'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.DRAFT)

        other_actions = self.request('post', action_url, {'action': 'toggle_visitor_voting'})
        self.assertEqual(other_actions.status_code, 400)
        self.assertEqual(other_actions.json()['error'], 'Event is not active.')

        self.add_exhibit()
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.OPEN)
        again = self.request('post', action_url, {'action': 'open_event'})
        self.assertEqual(again.status_code, 400)

    def test_console_tab_list_has_no_print_labels_tab(self):
        self.add_exhibit()
        for status in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN):
            ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=status)
            page = self.request('get', self.live_url).content.decode()
            tabs = page[page.index('class="page-tabs'):page.index('showcase-live-inline-error')]
            self.assertNotIn('Print labels', tabs)
            self.assertIn('Edit setup', tabs)
            self.assertIn('Results &amp; Awards', tabs) if status == ShowcaseEvent.Status.OPEN else self.assertNotIn('Results', tabs)

    def test_failed_open_through_the_transition_endpoint_returns_to_the_console(self):
        response = self.request('post', self.transition_url, {
            'target_status': ShowcaseEvent.Status.OPEN, 'reason': 'No exhibits yet'})
        self.assertRedirects(response, self.live_url, fetch_redirect_response=False)

    def test_transition_endpoint_opens_and_redirects_to_the_console(self):
        self.add_exhibit()
        opened = self.request('post', self.transition_url, {
            'target_status': ShowcaseEvent.Status.OPEN, 'reason': 'Doors open'})
        self.assertRedirects(opened, self.live_url, fetch_redirect_response=False)

    def test_transitions_after_opening_return_to_results_with_errors_surfaced(self):
        self.add_exhibit()
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.CLOSED)
        published = self.request('post', self.transition_url, {
            'target_status': ShowcaseEvent.Status.PUBLISHED, 'reason': 'Results are final'})
        self.assertRedirects(published, reverse('showcase_event_results', args=(self.event.pk,)),
                             fetch_redirect_response=False)

        invalid = self.request('post', self.transition_url, {'target_status': 'bogus'})
        self.assertRedirects(invalid, reverse('showcase_event_results', args=(self.event.pk,)),
                             fetch_redirect_response=False)

    def test_results_page_can_return_published_results_to_review(self):
        self.add_exhibit()
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(
            status=ShowcaseEvent.Status.PUBLISHED, public_results=True)
        results_url = reverse('showcase_event_results', args=(self.event.pk,))
        self.assertContains(self.request('get', results_url), 'Return results to review')

        response = self.request('post', results_url, {'action': 'revoke_results'})
        self.assertRedirects(response, results_url, fetch_redirect_response=False)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.CLOSED)

        again = self.request('post', results_url, {'action': 'revoke_results'})
        self.assertEqual(again.status_code, 200)


def criterion_payload(**overrides):
    payload = {'action': 'save_criterion', 'setup_step': 'criteria', 'name': 'Innovation',
               'description': 'Originality', 'weight': '40', 'scale': 'points',
               'max_points': '100', 'checklist_items_text': ''}
    payload.update(overrides)
    return payload


class ShowcaseCriteriaOperationTests(TestCase):
    def setUp(self):
        self.org = make_org('Criteria ops org')
        self.owner = make_user('criteria-ops-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Criteria ops expo', minimum_judges_per_exhibit=0)

    def save(self, name='Idea', weight='40', event=None, **kwargs):
        data = {'name': name, 'description': '', 'weight': Decimal(weight), 'scale': 'points',
                'max_points': Decimal('100'), 'checklist_items': []}
        return save_showcase_judge_criterion(event or self.event, data,
            organization=self.org.as_claim(), user=self.owner, **kwargs)

    def test_add_update_and_running_total(self):
        first = self.save('Idea', '40')
        second = self.save('  Delivery  ', '30')
        self.assertEqual((first.order, second.order, second.name), (0, 1, 'Delivery'))
        self.assertEqual(showcase_rubric_total(self.event), Decimal('70'))

        updated = self.save('Idea renamed', '60', criterion_id=first.pk)
        self.assertEqual(updated.pk, first.pk)
        self.assertEqual(self.event.judge_criteria.count(), 2)
        self.assertEqual(showcase_rubric_total(self.event), Decimal('90'))
        self.assertEqual(self.save('Idea renamed', '60', criterion_id=first.pk).pk, first.pk)

    def test_rejects_duplicates_overweight_and_the_fifty_limit(self):
        self.save('Idea', '40')
        with self.assertRaisesMessage(ValidationError, 'names must be unique'):
            self.save(' IDEA ', '10')
        with self.assertRaisesMessage(ValidationError, 'only 60% of the rubric'):
            self.save('Other', '61')
        self.assertEqual(self.event.judge_criteria.count(), 1)

        for index in range(49):
            self.save(f'Zero {index}', '0')
        with self.assertRaisesMessage(ValidationError, 'at most 50'):
            self.save('One too many', '0')

    def test_denies_non_draft_unknown_criterion_and_other_owners(self):
        self.save('Idea', '40')
        with self.assertRaises(PermissionDenied):
            self.save('Ghost', '10', criterion_id=999999)
        with self.assertRaises(PermissionDenied):
            save_showcase_judge_criterion(self.event, {}, organization=self.org.as_claim(),
                user=make_user('criteria-ops-stranger'))
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.OPEN)
        with self.assertRaises(PermissionDenied):
            self.save('Late', '10')

    def test_opening_requires_the_rubric_to_total_exactly_100(self):
        ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, name='Pump')
        self.save('Idea', '60')
        with self.assertRaisesMessage(ValidationError, 'total 60'):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.org.as_claim(), user=self.owner, reason='Go')
        self.save('Delivery', '40')
        transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
            organization=self.org.as_claim(), user=self.owner, reason='Go')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.OPEN)


class ShowcaseCriteriaTableTests(TestCase):
    def setUp(self):
        self.org = make_org('Criteria table org')
        self.owner = make_user('criteria-table-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Table expo', minimum_judges_per_exhibit=0)
        self.manage_url = reverse('showcase_event_manage', args=(self.event.pk,))
        self.criteria_url = f'{self.manage_url}?step=criteria'
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def add(self, **overrides):
        return self.request('post', self.manage_url, criterion_payload(**overrides))

    def test_empty_rubric_shows_the_empty_state_and_add_form(self):
        page = self.request('get', self.criteria_url)

        self.assertContains(page, 'No criteria yet.')
        self.assertContains(page, '0% of 100%')
        self.assertContains(page, 'Add a criterion')
        self.assertContains(page, 'Add criterion')
        self.assertNotContains(page, 'Cancel edit')
        self.assertIsNone(page.context['editing_criterion'])

    def test_criteria_are_listed_in_a_table_with_edit_and_delete_actions(self):
        self.add(name='Innovation', weight='40')
        self.add(name='Safety', weight='30', scale='checklist',
                 checklist_items_text='Guards\nLabels')
        self.add(name='Talk', weight='10', scale='stars')
        page = self.request('get', self.criteria_url)

        for heading in ('Criterion', 'What to assess', 'Weight', 'Scale', 'Actions'):
            self.assertContains(page, f'>{heading}</th>')
        self.assertContains(page, 'Innovation')
        self.assertContains(page, '40%')
        self.assertContains(page, '(max 100)')
        self.assertContains(page, '(2 items)')
        self.assertContains(page, 'Five stars')
        self.assertContains(page, '80% of 100%')
        self.assertContains(page, '20% still to assign.')
        first = self.event.judge_criteria.get(name='Innovation')
        self.assertContains(page, f'edit_criterion={first.pk}')
        self.assertContains(page, 'aria-label="Delete Innovation"')
        self.assertContains(page, 'name="action" value="remove_criterion"')
        self.assertContains(page, 'data-confirm="Delete the criterion Innovation?')

    def test_step_is_only_done_at_exactly_100(self):
        self.add(name='Idea', weight='60')
        page = self.request('get', self.criteria_url)
        steps = {s['key']: s for s in page.context['stepper_steps']}
        self.assertFalse(steps['criteria']['done'])
        self.assertEqual(steps['criteria']['desc'], 'Weights total 60%; they must reach 100%')
        self.assertContains(page, '40% still to assign.')

        self.add(name='Delivery', weight='40')
        page = self.request('get', self.criteria_url)
        steps = {s['key']: s for s in page.context['stepper_steps']}
        self.assertTrue(steps['criteria']['done'])
        self.assertContains(page, 'Rubric is complete.')
        self.assertContains(page, 'text-bg-success')

    def test_partial_rubric_blocks_readiness_even_when_judging_is_optional(self):
        ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, name='Pump')
        self.assertTrue(self.request('get', self.manage_url).context['event_ready'])
        self.add(name='Idea', weight='60')
        self.assertFalse(self.request('get', self.manage_url).context['event_ready'])

    def test_adding_a_criterion_redirects_to_the_step_with_a_success_message(self):
        response = self.add(name='Idea', weight='40')
        self.assertRedirects(response, self.criteria_url, fetch_redirect_response=False)
        self.assertEqual(self.event.judge_criteria.get().name, 'Idea')

        with_from = self.request('post', self.manage_url, criterion_payload(
            name='Other', weight='10', **{'from': 'live'}))
        self.assertRedirects(with_from, f'{self.criteria_url}&from=live', fetch_redirect_response=False)

    def test_edit_mode_populates_the_form_and_update_keeps_one_row(self):
        self.add(name='Safety', weight='30', scale='checklist', checklist_items_text='Guards\nLabels')
        criterion = self.event.judge_criteria.get()

        page = self.request('get', f'{self.criteria_url}&edit_criterion={criterion.pk}&from=live')
        self.assertEqual(page.context['editing_criterion'], criterion)
        self.assertEqual(page.context['active_step'], 'criteria')
        self.assertContains(page, 'Edit criterion: Safety')
        self.assertContains(page, 'Update criterion')
        self.assertContains(page, 'Cancel edit')
        self.assertContains(page, f'name="criterion_id" value="{criterion.pk}"')
        self.assertContains(page, 'value="Safety"')
        self.assertContains(page, 'Guards\nLabels')
        self.assertContains(page, 'aria-current="true"')
        self.assertContains(page, 'name="from" value="live"')

        updated = self.request('post', self.manage_url, criterion_payload(
            name='Safety v2', weight='55', scale='points', criterion_id=str(criterion.pk)))
        self.assertRedirects(updated, self.criteria_url, fetch_redirect_response=False)
        criterion.refresh_from_db()
        self.assertEqual((criterion.name, criterion.weight, criterion.scale, criterion.checklist_items),
                         ('Safety v2', Decimal('55.00'), 'points', []))
        self.assertEqual(self.event.judge_criteria.count(), 1)

    def test_stale_or_malformed_edit_links_fall_back_to_add_mode(self):
        self.add(name='Idea', weight='40')
        for value in ('999999', 'abc', ''):
            page = self.request('get', f'{self.criteria_url}&edit_criterion={value}')
            self.assertIsNone(page.context['editing_criterion'], value)
            self.assertContains(page, 'Add a criterion')

    def test_invalid_submit_keeps_edit_mode_and_entered_values(self):
        self.add(name='Idea', weight='40')
        criterion = self.event.judge_criteria.get()
        response = self.request('post', self.manage_url, criterion_payload(
            name='Idea', weight='not-a-number', criterion_id=str(criterion.pk)))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['active_step'], 'criteria')
        self.assertEqual(response.context['editing_criterion'], criterion)
        self.assertContains(response, 'Edit criterion: Idea', status_code=400)
        self.assertContains(response, 'Enter a number.', status_code=400)
        criterion.refresh_from_db()
        self.assertEqual(criterion.weight, Decimal('40.00'))

    def test_business_rule_errors_are_shown_without_losing_the_form(self):
        self.add(name='Idea', weight='70')
        over = self.add(name='Other', weight='40')
        self.assertEqual(over.status_code, 400)
        self.assertContains(over, 'only 30% of the rubric', status_code=400)
        self.assertContains(over, 'value="Other"', status_code=400)
        duplicate = self.add(name='idea', weight='10')
        self.assertContains(duplicate, 'names must be unique', status_code=400)
        self.assertEqual(self.event.judge_criteria.count(), 1)

    def test_unknown_or_foreign_criterion_ids_and_locked_events_are_refused(self):
        self.assertEqual(self.add(criterion_id='999999').status_code, 404)
        self.add(name='Idea', weight='40')
        criterion = self.event.judge_criteria.get()
        stranger = make_user('criteria-table-stranger')
        sign_in(self.client, stranger, self.org)
        self.assertEqual(self.add(criterion_id=str(criterion.pk)).status_code, 404)

        sign_in(self.client, self.owner, self.org)
        with patch('service_domains.views.save_showcase_judge_criterion', side_effect=PermissionDenied):
            self.assertEqual(self.add(name='Racing').status_code, 404)
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.OPEN)
        self.assertEqual(self.add(name='Late').status_code, 404)

    def test_open_event_shows_a_read_only_table(self):
        self.add(name='Idea', weight='100')
        criterion = self.event.judge_criteria.get()
        ShowcaseEvent.objects.filter(pk=self.event.pk).update(status=ShowcaseEvent.Status.OPEN)
        page = self.request('get', f'{self.criteria_url}&edit_criterion={criterion.pk}')

        self.assertIsNone(page.context['editing_criterion'])
        self.assertContains(page, 'Judge criteria are locked after the event opens.')
        self.assertContains(page, 'Idea')
        self.assertNotContains(page, '>Actions</th>')
        self.assertNotContains(page, 'name="action" value="save_criterion"')

    def test_deleting_a_criterion_returns_to_add_mode(self):
        self.add(name='Idea', weight='40')
        criterion = self.event.judge_criteria.get()
        response = self.request('post', self.manage_url, {
            'action': 'remove_criterion', 'criterion_id': str(criterion.pk), 'setup_step': 'criteria'})
        self.assertRedirects(response, self.criteria_url, fetch_redirect_response=False)
        self.assertEqual(self.event.judge_criteria.count(), 0)


class ShowcaseChecklistInputTests(TestCase):
    """The checklist scale uses growing item boxes backed by one submitted field."""

    def setUp(self):
        self.org = make_org('Checklist input org')
        self.owner = make_user('checklist-input-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Checklist expo', minimum_judges_per_exhibit=0)
        self.manage_url = reverse('showcase_event_manage', args=(self.event.pk,))
        self.criteria_url = f'{self.manage_url}?step=criteria'
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def add(self, **overrides):
        return self.request('post', self.manage_url, criterion_payload(**overrides))

    def test_form_renders_the_growing_item_boxes_hooks(self):
        page = self.request('get', self.criteria_url)

        self.assertContains(page, 'Checklist items')
        self.assertNotContains(page, 'one per line')
        self.assertContains(page, 'A new box appears as you fill the last one.')
        self.assertContains(page, 'name="checklist_items_text"')
        self.assertContains(page, 'dataset.checklistList')
        self.assertContains(page, 'dataset.checklistRow')
        self.assertContains(page, 'const MAX_ITEMS=20')
        self.assertContains(page, 'Math.max(2,lastFilled+2)')

    def test_edit_mode_seeds_the_boxes_from_the_saved_items(self):
        self.add(name='Safety', weight='30', scale='checklist',
                 checklist_items_text='Guards\nLabels\nSigns')
        criterion = self.event.judge_criteria.get()

        page = self.request('get', f'{self.criteria_url}&edit_criterion={criterion.pk}')

        self.assertContains(page, 'Guards\nLabels\nSigns')
        self.assertContains(page, '(3 items)')

    def test_blank_gaps_between_items_are_dropped_on_save(self):
        self.add(name='Safety', weight='30', scale='checklist',
                 checklist_items_text='Guards\n\n  Labels  \n')
        self.assertEqual(self.event.judge_criteria.get().checklist_items, ['Guards', 'Labels'])

    def test_checklist_needs_at_least_one_item_and_at_most_twenty(self):
        empty = self.add(name='Safety', weight='30', scale='checklist', checklist_items_text='')
        self.assertEqual(empty.status_code, 400)
        many = self.add(name='Safety', weight='30', scale='checklist',
                        checklist_items_text='\n'.join(f'Item {n}' for n in range(21)))
        self.assertContains(many, 'at most 20 checklist items', status_code=400)
        self.assertEqual(self.event.judge_criteria.count(), 0)
