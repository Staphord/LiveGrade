"""Rubric from a spreadsheet, and the whole-rubric table editor."""
from decimal import Decimal
from io import BytesIO

from django.urls import reverse

from assessments import rubric_import
from assessments.models import Evaluation, EvaluationScore, PresentationTurn, RubricCategory, SessionChange
from assessments.tests.test_edges_lecturer_views import LecturerTestCase, _messages

from .xlsx_helpers import XLSX, xlsx_buffer, xlsx_upload

HEADERS = ['Category', 'Description', 'Scope', 'Max points', 'Weight %']
GOOD = [['Technical', 'Quality', 'Group', 10, 30], ['Delivery', '', 'Group', 10, 30], ['Teamwork', 'Part', 'Individual', 5, 40]]


def rubric_file(*rows, **kwargs):
    return xlsx_upload(HEADERS, *rows, name='rubric.xlsx', **kwargs)


class ParseRubricTests(LecturerTestCase):

    def test_rows_are_read_as_text_with_their_line_numbers(self):
        parsed = rubric_import.parse_rubric(xlsx_buffer(HEADERS, *GOOD))
        self.assertIsNone(parsed['fatal'])
        self.assertEqual([r['row'] for r in parsed['rows']], [2, 3, 4])
        self.assertEqual(parsed['rows'][0]['name'], 'Technical')
        self.assertEqual(parsed['rows'][0]['weight'], '30')

    def test_unrecognised_empty_and_oversized_files_are_refused(self):
        self.assertIn('Category, Max points, Weight %', rubric_import.parse_rubric(xlsx_buffer(['Colour'], ['red']))['fatal'])
        self.assertIn('Weight %', rubric_import.parse_rubric(xlsx_buffer(['Category', 'Max points'], ['a', 1]))['fatal'])
        self.assertEqual(rubric_import.parse_rubric(xlsx_buffer())['fatal'], 'The file is empty.')
        many = [HEADERS] + [[f'C{i}', '', 'Group', 1, 1] for i in range(51)]
        self.assertIn('at most 50', rubric_import.parse_rubric(xlsx_buffer(*many))['fatal'])

    def test_the_downloadable_sample_is_itself_a_valid_rubric(self):
        parsed = rubric_import.parse_rubric(rubric_import.sample_workbook())
        verdict = rubric_import.check_import(parsed['rows'], self.session, 'replace')
        self.assertTrue(verdict['ok'], verdict['errors'])

    def test_percentage_formatted_weights_mean_the_points_typed(self):
        buffer = xlsx_buffer(HEADERS, ['A', '', 'Group', 10, 0.6], ['B', '', 'Individual', 5, 0.4], percent_columns=['E'])
        verdict = rubric_import.check_import(rubric_import.parse_rubric(buffer)['rows'], self.session, 'replace')
        self.assertTrue(verdict['ok'], verdict['errors'])


class CheckImportTests(LecturerTestCase):

    def check(self, *rows, mode='replace'):
        raw = [{'row': n, 'name': r[0], 'description': r[1], 'scope': r[2], 'max_points': str(r[3]), 'weight': str(r[4])}
               for n, r in enumerate(rows, start=2)]
        return rubric_import.check_import(raw, self.session, mode)

    def test_a_balanced_rubric_passes(self):
        self.assertTrue(self.check(*GOOD)['ok'])

    def test_field_problems_are_reported_by_row_before_any_total_is_judged(self):
        verdict = self.check(['', '', 'Group', 10, 30], ['B', '', 'Sideways', 'x', 200])
        self.assertFalse(verdict['ok'])
        self.assertEqual({row for row, _ in verdict['errors']}, {2, 3})
        self.assertEqual(verdict['totals'], {})

    def test_totals_that_miss_the_split_are_named_with_the_gap(self):
        verdict = self.check(['A', '', 'Group', 10, 50], ['B', '', 'Individual', 5, 40])
        self.assertFalse(verdict['ok'])
        self.assertIn('short by 10%', verdict['errors'][0][1])

    def test_adding_is_judged_on_the_rubric_as_it_would_become(self):
        self.assertFalse(self.check(['More', '', 'Group', 10, 5], mode='add')['ok'])
        RubricCategory.objects.filter(pk=self.quality.pk).update(weight=50)
        self.assertTrue(self.check(['More', '', 'Group', 10, 10], mode='add')['ok'])
        duplicate = self.check(['Quality', '', 'Group', 10, 10], mode='add')
        self.assertIn('appears twice', duplicate['errors'][0][1])

    def test_applying_a_rubric_that_no_longer_passes_is_refused(self):
        raw = [{'row': 2, 'name': 'A', 'description': '', 'scope': 'Group', 'max_points': '10', 'weight': '5'}]
        with self.assertRaises(ValueError):
            rubric_import.apply_import(self.session, raw, 'replace')

    def test_replace_swaps_the_rubric_and_add_extends_it(self):
        raw = [{'row': n, 'name': r[0], 'description': r[1], 'scope': r[2], 'max_points': str(r[3]), 'weight': str(r[4])}
               for n, r in enumerate(GOOD, start=2)]
        self.assertEqual(rubric_import.apply_import(self.session, raw, 'replace'), 3)
        self.assertEqual(sorted(RubricCategory.objects.values_list('name', flat=True)), ['Delivery', 'Teamwork', 'Technical'])
        RubricCategory.objects.filter(name='Delivery').update(weight=20)
        added = [{'row': 2, 'name': 'Extra', 'description': '', 'scope': 'Group', 'max_points': '5', 'weight': '10'}]
        self.assertEqual(rubric_import.apply_import(self.session, added, 'add'), 1)
        self.assertEqual(RubricCategory.objects.count(), 4)
        self.assertEqual(RubricCategory.objects.get(name='Extra').order, 4)


class RubricImportViewTests(LecturerTestCase):

    def upload(self, *rows, mode='replace'):
        return self.client.post(self.url('assessment_rubric_import'), {'file': rubric_file(*rows), 'mode': mode})

    def test_upload_preview_confirm_replaces_the_rubric(self):
        response = self.upload(*GOOD)
        self.assertRedirects(response, self.url('assessment_rubric'), fetch_redirect_response=False)
        page = self.client.get(self.url('assessment_rubric'))
        self.assertContains(page, 'id="rubric-import-modal"')
        self.assertContains(page, 'Technical')
        self.assertContains(page, 'The rubric is valid')
        self.assertContains(page, '<strong>replace</strong>')
        self.assertContains(page, 'of 60% group weight')
        self.assertTrue(RubricCategory.objects.filter(name='Quality').exists())
        done = self.client.post(self.url('assessment_rubric_import_preview'), {'action': 'apply'})
        self.assertRedirects(done, self.url('assessment_rubric'), fetch_redirect_response=False)
        self.assertEqual(sorted(RubricCategory.objects.values_list('name', flat=True)), ['Delivery', 'Teamwork', 'Technical'])
        self.assertTrue(any('Imported 3 rubric categories' in m for m in _messages(done)))

    def test_adding_to_the_rubric_keeps_what_is_there(self):
        RubricCategory.objects.filter(pk=self.quality.pk).update(weight=50)
        self.upload(['Extra', '', 'Group', 10, 10], mode='add')
        page = self.client.get(self.url('assessment_rubric'))
        self.assertContains(page, '<strong>add</strong>')
        self.client.post(self.url('assessment_rubric_import_preview'), {'action': 'apply'})
        self.assertEqual(RubricCategory.objects.count(), 3)
        self.assertTrue(RubricCategory.objects.filter(name='Quality').exists())

    def test_a_rubric_with_a_weight_problem_shows_it_and_cannot_be_imported(self):
        self.upload(['A', '', 'Group', 10, 50], ['B', '', 'Individual', 5, 40])
        page = self.client.get(self.url('assessment_rubric'))
        self.assertContains(page, 'short by 10%')
        html = page.content.decode()
        self.assertIn('disabled', html[html.index('value="apply"'):html.index('value="apply"') + 160])
        self.assertContains(page, 'dp-stat is-bad')
        done = self.client.post(self.url('assessment_rubric_import_preview'), {'action': 'apply'})
        self.assertTrue(any('nothing was imported' in m for m in _messages(done)))
        self.assertTrue(RubricCategory.objects.filter(name='Quality').exists())

    def test_row_level_problems_are_shown_against_their_rows(self):
        self.upload(['', '', 'Group', 10, 30])
        page = self.client.get(self.url('assessment_rubric'))
        self.assertContains(page, 'Row 2: Category name is empty.')

    def test_cancelling_changes_nothing(self):
        self.upload(*GOOD)
        self.client.post(self.url('assessment_rubric_import_preview'), {'action': 'cancel'})
        self.assertTrue(RubricCategory.objects.filter(name='Quality').exists())
        self.assertEqual(self.client.get(self.url('assessment_rubric_import_preview')).status_code, 302)

    def test_no_file_or_a_bad_file_goes_back_with_a_message(self):
        response = self.client.post(self.url('assessment_rubric_import'), {'mode': 'replace'})
        self.assertRedirects(response, self.url('assessment_rubric'), fetch_redirect_response=False)
        self.assertTrue(_messages(response))
        bad = self.client.post(self.url('assessment_rubric_import'),
                               {'file': xlsx_upload(['Colour'], ['red'], name='x.xlsx'), 'mode': 'replace'})
        self.assertTrue(any('Could not find' in m for m in _messages(bad)))

    def test_the_old_preview_address_just_leads_to_the_window_on_the_rubric_step(self):
        self.upload(*GOOD)
        response = self.client.get(self.url('assessment_rubric_import_preview'))
        self.assertRedirects(response, self.url('assessment_rubric'), fetch_redirect_response=False)

    def test_a_waiting_file_is_dropped_once_votes_make_it_impossible(self):
        self.upload(*GOOD)
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        self.set_status('live')
        page = self.client.get(self.url('assessment_rubric'))
        self.assertNotContains(page, 'id="rubric-import-modal"')
        self.assertNotIn('rubric_import', self.client.session)

    def test_the_rubric_window_shows_each_scope_s_total_against_its_target(self):
        self.upload(*GOOD)
        page = self.client.get(self.url('assessment_rubric'))
        self.assertEqual([(t['label'], t['ok']) for t in page.context['rubric_review']['totals']],
                         [('Group', True), ('Individual', True)])
        self.assertContains(page, 'dp-stat is-good')

    def test_the_preview_without_an_upload_goes_back(self):
        response = self.client.get(self.url('assessment_rubric_import_preview'))
        self.assertTrue(any('Choose a file' in m for m in _messages(response)))

    def test_the_sample_downloads(self):
        response = self.client.get(self.url('assessment_rubric_import_sample'))
        self.assertEqual(response['Content-Type'], XLSX)
        self.assertEqual(len(rubric_import.parse_rubric(BytesIO(response.content))['rows']), 3)

    def test_a_live_session_with_no_votes_can_take_a_new_rubric_and_it_is_announced(self):
        self.set_status('live')
        self.upload(*GOOD)
        self.client.post(self.url('assessment_rubric_import_preview'), {'action': 'apply'})
        self.assertIn('rubric category', SessionChange.objects.get().summary)

    def test_once_votes_exist_the_rubric_cannot_be_replaced(self):
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        self.set_status('live')
        for response in (self.upload(*GOOD), self.client.get(self.url('assessment_rubric_import_preview'))):
            self.assertRedirects(response, self.url('assessment_rubric'), fetch_redirect_response=False)
            self.assertTrue(any('cannot be added or removed' in m for m in _messages(response)))
        self.assertTrue(RubricCategory.objects.filter(name='Quality').exists())

    def test_a_closed_session_cannot_change_its_rubric(self):
        self.set_status('closed')
        self.assertRedirects(self.upload(*GOOD), self.url('assessment_session_detail'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(self.url('assessment_rubric_import_preview')),
                             self.url('assessment_session_detail'), fetch_redirect_response=False)

    def test_the_rubric_page_offers_import_the_table_and_the_format(self):
        page = self.client.get(self.url('assessment_rubric'))
        self.assertContains(page, 'Import rubric')
        self.assertContains(page, 'Edit rubric as a table')
        self.assertContains(page, self.url('assessment_rubric_import_sample'))
        self.assertContains(page, 'Weights must add up')


class RubricTableTests(LecturerTestCase):

    def post(self, *rows, **kwargs):
        data = {'rows-total': str(len(rows))}
        for index, row in enumerate(rows):
            for field in ('id', 'name', 'description', 'scope', 'max_points', 'weight'):
                data[f'rows-{index}-{field}'] = row.get(field, '')
        return self.client.post(self.url('assessment_rubric_table'), data, **kwargs)

    def existing(self):
        return [{'id': c.pk, 'name': c.name, 'description': c.description, 'scope': c.scope,
                 'max_points': str(c.max_points), 'weight': str(c.weight)} for c in RubricCategory.objects.order_by('pk')]

    def test_the_table_lists_the_current_rubric(self):
        page = self.client.get(self.url('assessment_rubric_table'))
        self.assertContains(page, 'Quality')
        self.assertContains(page, 'Teamwork')
        self.assertContains(page, 'Group weights')

    def test_edits_are_saved_together(self):
        rows = self.existing()
        rows[0].update(name='Quality of work', weight='55')
        rows[1].update(weight='40')
        rows.append({'name': 'Extra', 'scope': 'group', 'max_points': '5', 'weight': '5'})
        response = self.post(*rows)
        self.assertRedirects(response, self.url('assessment_rubric'), fetch_redirect_response=False)
        self.quality.refresh_from_db()
        self.assertEqual((self.quality.name, self.quality.weight), ('Quality of work', Decimal('55')))
        self.assertTrue(RubricCategory.objects.filter(name='Extra', weight=5).exists())

    def test_a_draft_may_be_saved_short_but_not_over(self):
        rows = self.existing()
        rows[0]['weight'] = '10'
        self.assertEqual(self.post(*rows).status_code, 302)
        rows[0]['weight'] = '70'
        over = self.post(*rows)
        self.assertEqual(over.status_code, 200)
        self.assertContains(over, 'over this session')
        self.assertContains(over, 'value="70"')

    def test_a_live_session_must_keep_its_weights_exact(self):
        self.set_status('live')
        rows = self.existing()
        rows[0]['weight'] = '50'
        response = self.post(*rows)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'short by 10%')
        rows[0]['weight'] = '60'
        rows[0]['name'] = 'Quality renamed'
        done = self.post(*rows)
        self.assertEqual(done.status_code, 302)
        self.assertIn('Edited the rubric', SessionChange.objects.get().summary)

    def test_a_removed_row_deletes_that_category_and_a_new_one_adds(self):
        rows = self.existing()[:1]
        rows[0]['weight'] = '60'
        self.assertEqual(self.post(*rows).status_code, 302)
        self.assertFalse(RubricCategory.objects.filter(pk=self.teamwork.pk).exists())

    def test_field_problems_come_back_with_row_numbers_and_what_was_typed(self):
        rows = self.existing()
        rows[1].update(name='', weight='abc')
        response = self.post(*rows)
        self.assertContains(response, 'Row 2: Category name is empty.')
        self.assertContains(response, 'value="abc"')
        self.assertContains(response, 'has-problem')

    def test_a_category_that_vanished_meanwhile_is_reported(self):
        rows = self.existing()
        rows[0]['id'] = 999999
        self.assertContains(self.post(*rows), 'no longer exists')

    def test_scores_fix_a_category_s_scale_and_scope(self):
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=self.quality, value=5)
        page = self.client.get(self.url('assessment_rubric_table'))
        self.assertContains(page, 'This category already has scores.')
        rows = self.existing()
        rows[0]['max_points'] = '20'
        self.assertContains(self.post(*rows), 'already has scores')
        rows[0].update(max_points='10', weight='60')
        rows[0]['name'] = 'Renamed'
        self.assertEqual(self.post(*rows).status_code, 302)

    def test_a_category_with_no_scores_yet_can_change_its_scale_and_scope(self):
        rows = self.existing()
        rows[0]['max_points'] = '20'
        rows[1].update(scope='group', weight='0')
        rows[0]['weight'] = '60'
        self.assertEqual(self.post(*rows).status_code, 302)
        self.quality.refresh_from_db()
        self.teamwork.refresh_from_db()
        self.assertEqual((self.quality.max_points, self.teamwork.scope), (20, 'group'))

    def test_votes_stop_categories_being_added_or_removed(self):
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        self.set_status('live')
        page = self.client.get(self.url('assessment_rubric_table'))
        self.assertContains(page, 'cannot be added or removed')
        self.assertNotContains(page, 'id="rt-add"')
        rows = self.existing()[:1]
        rows[0]['weight'] = '60'
        response = self.post(*rows)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be added or removed')
        self.assertTrue(RubricCategory.objects.filter(pk=self.teamwork.pk).exists())

    def test_an_unreadable_submission_changes_nothing(self):
        for data in ({'rows-total': 'abc'}, {}):
            response = self.client.post(self.url('assessment_rubric_table'), data)
            self.assertRedirects(response, self.url('assessment_rubric_table'), fetch_redirect_response=False)
            self.assertTrue(any('not understood' in m for m in _messages(response)))
        self.assertEqual(RubricCategory.objects.count(), 2)

    def test_blank_rows_are_skipped_and_a_live_rubric_cannot_be_emptied(self):
        rows = self.existing() + [{}]
        rows[0]['weight'] = '60'
        self.assertEqual(self.post(*rows).status_code, 302)
        self.set_status('live')
        response = self.post({})
        self.assertContains(response, 'needs at least one rubric category')
        self.assertEqual(RubricCategory.objects.count(), 2)

    def test_a_draft_can_be_cleared_to_start_again(self):
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(RubricCategory.objects.count(), 0)

    def test_a_closed_session_has_no_table(self):
        self.set_status('closed')
        self.assertRedirects(self.client.get(self.url('assessment_rubric_table')), self.url('assessment_session_detail'),
                             fetch_redirect_response=False)
        self.assertRedirects(self.post(*self.existing()), self.url('assessment_session_detail'), fetch_redirect_response=False)
