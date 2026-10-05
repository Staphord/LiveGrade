"""What may still change in a session, the spreadsheet helpers, and the rubric rules."""
from decimal import Decimal
from io import BytesIO
from unittest import mock

import openpyxl
from django.test import SimpleTestCase, TestCase

from assessments import rubric_rules, setup_rules
from assessments.models import (AssessmentSession, Evaluation, EvaluationScore, RubricCategory,
                                SessionChange)
from assessments.sheets import cell, cell_text, clean, key, map_headers, read_rows, split_people
from assessments.tests.test_edges_core import SessionFixture

from .xlsx_helpers import xlsx_buffer


class SheetHelperTests(SimpleTestCase):

    def test_cells_become_tidy_text(self):
        self.assertEqual(cell_text(None), '')
        self.assertEqual(cell_text(1001.0), '1001')
        self.assertEqual(cell_text(2.5), '2.5')
        self.assertEqual(clean('  Ann   Lee '), 'Ann Lee')
        self.assertEqual(clean(None), '')
        self.assertEqual(key('  ANN   lee'), 'ann lee')

    def test_people_are_split_on_commas_semicolons_and_line_breaks(self):
        self.assertEqual(split_people('a, b;c\nd,, e '), ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual(split_people(None), [])

    def test_headers_map_to_the_leftmost_matching_column(self):
        mapping = map_headers(['Group', 'Name', 'name', 'Other'], {'group': {'group'}, 'names': {'name'}})
        self.assertEqual(mapping, {'group': 0, 'names': 1})
        self.assertEqual(cell(['g', 'n'], mapping, 'names'), 'n')
        self.assertIsNone(cell(['g'], mapping, 'names'))
        self.assertIsNone(cell(['g'], mapping, 'missing'))

    def test_a_percentage_formatted_cell_is_read_as_the_points_typed(self):
        buffer = xlsx_buffer(['Weight'], [0.3], [30], percent_columns=['A'])
        self.assertEqual(read_rows(buffer, percent_as_points=True), [['Weight'], [30.0], [3000.0]])
        buffer.seek(0)
        self.assertEqual(read_rows(buffer), [['Weight'], [0.3], [30]])

    def test_a_boolean_is_never_treated_as_a_percentage(self):
        book = openpyxl.Workbook()
        book.active.append([True])
        book.active['A1'].number_format = '0%'
        buffer = BytesIO()
        book.save(buffer)
        buffer.seek(0)
        self.assertEqual(read_rows(buffer, percent_as_points=True), [[True]])


class RubricRuleTests(TestCase):

    def setUp(self):
        self.session = AssessmentSession.objects.create(organization_id=1, name='Demo')

    def row(self, **over):
        raw = {'name': 'Quality', 'description': '', 'scope': 'group', 'max_points': '10', 'weight': '60'}
        raw.update(over)
        return rubric_rules.normalise_row(raw, 2)

    def test_a_good_row_is_typed_and_cleaned(self):
        row, errors = self.row(name='  Tech   depth ', scope='Team', weight='60%', max_points='7.5')
        self.assertEqual(errors, [])
        self.assertEqual((row['name'], row['scope'], row['weight'], row['max_points']),
                         ('Tech depth', 'group', Decimal('60'), Decimal('7.5')))

    def test_scope_defaults_to_group_and_individual_aliases_work(self):
        self.assertEqual(self.row(scope='')[0]['scope'], 'group')
        self.assertEqual(self.row(scope='Student')[0]['scope'], 'individual')

    def test_every_field_is_checked_with_a_message_that_names_it(self):
        _, errors = self.row(name='', scope='nonsense', max_points='abc', weight='')
        text = ' '.join(errors)
        for part in ('Category name is empty', 'must be Group or Individual', '"abc" is not a number', 'Weight is empty'):
            self.assertIn(part, text)

    def test_limits(self):
        self.assertIn('longer than 150', self.row(name='x' * 151)[1][0])
        self.assertIn('longer than 255', self.row(description='x' * 256)[1][0])
        self.assertIn('more than 0', self.row(max_points='0')[1][0])
        self.assertIn('more than 0', self.row(max_points='10000')[1][0])
        self.assertIn('between 0 and 100', self.row(weight='101')[1][0])
        self.assertIn('between 0 and 100', self.row(weight='-1')[1][0])
        self.assertIn('at most 2 decimal', self.row(weight='12.345')[1][0])
        self.assertIn('not a number', self.row(weight='nan')[1][0])
        self.assertIn('not a number', self.row(weight='inf')[1][0])

    def test_a_comma_decimal_is_understood(self):
        self.assertEqual(self.row(weight='12,5')[0]['weight'], Decimal('12.5'))

    def rows(self, *specs):
        return [self.row(name=f'C{i}', scope=scope, weight=str(weight))[0]
                for i, (scope, weight) in enumerate(specs)]

    def test_weights_must_add_up_to_each_scope_s_share_exactly_when_strict(self):
        ok = rubric_rules.check_rubric(self.rows(('group', 30), ('group', 30), ('individual', 40)), self.session, strict=True)
        self.assertEqual(ok['errors'], [])
        short = rubric_rules.check_rubric(self.rows(('group', 50)), self.session, strict=True)
        self.assertIn('must add up to 60% (short by 10%)', short['errors'][0][1])
        over = rubric_rules.check_rubric(self.rows(('group', 50), ('group', 20)), self.session, strict=True)
        self.assertIn('over this session\'s 60% group share by 10%', over['errors'][0][1])

    def test_a_draft_may_be_short_but_never_over(self):
        self.assertEqual(rubric_rules.check_rubric(self.rows(('group', 20)), self.session, strict=False)['errors'], [])
        over = rubric_rules.check_rubric(self.rows(('individual', 41)), self.session, strict=False)
        self.assertIn('Individual weights add up to 41%', over['errors'][0][1])

    def test_a_scope_with_no_categories_is_not_judged(self):
        verdict = rubric_rules.check_rubric(self.rows(('group', 60)), self.session, strict=True)
        self.assertEqual(verdict['errors'], [])
        self.assertEqual(verdict['totals']['individual'], 0)

    def test_duplicate_names_in_a_scope_and_too_many_rows_are_errors(self):
        rows = [self.row(name='Same', weight='30')[0], self.row(name='same', weight='30')[0]]
        self.assertIn('appears twice', rubric_rules.check_rubric(rows, self.session, strict=True)['errors'][0][1])
        many = [self.row(name=f'C{i}', weight='1')[0] for i in range(rubric_rules.MAX_ROWS + 1)]
        self.assertIn('at most 50', rubric_rules.check_rubric(many, self.session, strict=False)['errors'][0][1])

    def test_the_same_name_in_two_scopes_is_fine(self):
        rows = [self.row(name='Teamwork', scope='group', weight='60')[0],
                self.row(name='Teamwork', scope='individual', weight='40')[0]]
        self.assertEqual(rubric_rules.check_rubric(rows, self.session, strict=True)['errors'], [])

    def test_weights_print_without_trailing_zeros(self):
        self.assertEqual((rubric_rules.pct(Decimal('60.00')), rubric_rules.pct(Decimal('12.50')), rubric_rules.pct(Decimal('7'))),
                         ('60', '12.5', '7'))


class SetupRulesTests(SessionFixture):

    def graded(self, group, evaluator):
        """``evaluator`` has graded ``group`` (a closed turn, one score)."""
        turn = self.turn(group, 'closed')
        evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=evaluator)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=self.quality, value=5)
        return turn

    def turn(self, group, status):
        from assessments.models import PresentationTurn
        return PresentationTurn.objects.create(assessment_session=self.session, group=group, status=status)

    def test_a_fresh_session_has_nothing_locked(self):
        self.assertIsNone(setup_rules.closed_reason(self.session))
        self.assertFalse(setup_rules.is_live(self.session))
        self.assertFalse(setup_rules.has_votes(self.session))
        self.assertIsNone(setup_rules.group_members_lock(self.red))
        self.assertIsNone(setup_rules.group_delete_lock(self.red))
        self.assertIsNone(setup_rules.student_delete_lock(self.ann))
        self.assertIsNone(setup_rules.category_core_lock(self.quality))
        self.assertIsNone(setup_rules.rubric_structure_lock(self.session))
        self.assertIsNone(setup_rules.live_rubric_lock(self.session))

    def test_status_decides_closed_and_live(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='closed')
        self.session.refresh_from_db()
        self.assertIn('read-only record', setup_rules.closed_reason(self.session))
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        self.session.refresh_from_db()
        self.assertTrue(setup_rules.is_live(self.session))
        self.assertIn('edit the rubric as a table', setup_rules.live_rubric_lock(self.session))

    def test_a_group_that_has_presented_is_fixed_and_cannot_be_deleted(self):
        self.turn(self.red, 'active')
        self.assertIn('members are fixed', setup_rules.group_members_lock(self.red))
        self.assertIn('stays in the record', setup_rules.group_delete_lock(self.red))
        self.assertIsNone(setup_rules.group_members_lock(self.blue))

    def test_a_group_whose_student_has_graded_elsewhere_cannot_be_deleted(self):
        self.graded(self.red, self.cat)
        self.assertIn('erase those grades', setup_rules.group_delete_lock(self.blue))
        self.assertIsNone(setup_rules.group_members_lock(self.blue))

    def test_a_student_who_graded_or_was_graded_stays(self):
        turn = self.graded(self.red, self.cat)
        self.assertIn('stay in the record', setup_rules.student_delete_lock(self.cat))
        Evaluation.objects.create(presentation_turn=turn, evaluator=self.dan, target_student=self.ann)
        self.assertIn('stay in the record', setup_rules.student_delete_lock(self.ann))
        self.assertIsNone(setup_rules.student_delete_lock(self.bob))

    def test_scores_fix_a_category_s_scale_and_votes_fix_the_rubric_s_shape(self):
        self.graded(self.red, self.cat)
        self.assertIn('already has scores', setup_rules.category_core_lock(self.quality))
        self.assertIsNone(setup_rules.category_core_lock(self.teamwork))
        self.assertTrue(setup_rules.has_votes(self.session))
        self.assertIn('cannot be added or removed', setup_rules.rubric_structure_lock(self.session))

    def test_the_page_wide_lock_marks_agree_with_the_single_group_checks(self):
        self.turn(self.red, 'closed')
        self.graded(self.red, self.cat)
        groups = [self.red, self.blue]
        setup_rules.annotate_groups(self.session, groups)
        for group in groups:
            self.assertEqual(bool(group.members_lock), bool(setup_rules.group_members_lock(group)))
            self.assertEqual(bool(group.delete_lock), bool(setup_rules.group_delete_lock(group)))
        self.assertIn('presented', self.red.delete_lock)
        self.assertIn('erase those grades', self.blue.delete_lock)
        self.assertEqual(self.blue.members_lock, '')

    def test_edits_after_go_live_are_logged_and_announced_but_a_draft_stays_quiet(self):
        user = None
        with mock.patch('assessments.setup_rules.broadcast_session_event') as broadcast:
            setup_rules.record_change(self.session, user, 'Draft edit.')
            self.assertFalse(SessionChange.objects.exists())
            broadcast.assert_not_called()
            AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
            self.session.refresh_from_db()
            setup_rules.record_change(self.session, user, 'x' * 300)
            setup_rules.record_change(self.session, user, 'Rubric.', notify='rubric.changed')
        self.assertEqual(SessionChange.objects.count(), 2)
        self.assertEqual(len(SessionChange.objects.last().summary), 255)
        self.assertEqual([c.args[1] for c in broadcast.call_args_list], ['setup.changed', 'rubric.changed'])
        self.assertIn('Rubric.', str(SessionChange.objects.first()))
