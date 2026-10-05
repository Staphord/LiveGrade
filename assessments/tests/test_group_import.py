"""One spreadsheet creates the groups and their students together."""
from django.test import TestCase

from accounts.testing import make_org
from assessments.group_import import MAX_ROWS, apply_import, parse_groups, plan_import, sample_workbook
from assessments.models import (AssessmentSession, GroupMembership, PresentationGroup, PresentationTurn,
                                Student)
from assessments.sheets import UNREADABLE

from .xlsx_helpers import GROUP_HEADERS, xlsx_buffer


def parse(*rows):
    return parse_groups(xlsx_buffer(*rows))


class ParseGroupsTests(TestCase):

    def test_members_in_one_cell_are_split_on_commas_with_or_without_a_space(self):
        parsed = parse(GROUP_HEADERS, ['Aurora', 'Ann Lee, Bo Ng,Cy Oh', None, 'Campus map', 'Room 1'])
        group = parsed['groups'][0]
        self.assertEqual([m['name'] for m in group['members']], ['Ann Lee', 'Bo Ng', 'Cy Oh'])
        self.assertEqual((group['topic'], group['location']), ('Campus map', 'Room 1'))
        self.assertIsNone(parsed['fatal'])

    def test_semicolons_and_line_breaks_also_separate_people_and_spacing_is_tidied(self):
        parsed = parse(GROUP_HEADERS, ['Aurora', '  Ann   Lee ;Bo Ng\nCy Oh  '])
        self.assertEqual([m['name'] for m in parsed['groups'][0]['members']], ['Ann Lee', 'Bo Ng', 'Cy Oh'])

    def test_capitals_are_kept_as_typed(self):
        parsed = parse(GROUP_HEADERS, ['Aurora', 'zed quillfeather, McTavish Brindle'])
        self.assertEqual([m['name'] for m in parsed['groups'][0]['members']], ['zed quillfeather', 'McTavish Brindle'])

    def test_ids_in_their_own_column_pair_with_names_by_position(self):
        parsed = parse(GROUP_HEADERS, ['Aurora', 'Ann Lee, Bo Ng', 'A101, A102'])
        self.assertEqual([(m['name'], m['student_id']) for m in parsed['groups'][0]['members']],
                         [('Ann Lee', 'A101'), ('Bo Ng', 'A102')])

    def test_numeric_ids_typed_into_excel_stay_whole_numbers(self):
        parsed = parse(GROUP_HEADERS, ['Aurora', 'Ann Lee', 1001.0])
        self.assertEqual(parsed['groups'][0]['members'][0]['student_id'], '1001')

    def test_a_different_number_of_ids_and_names_is_a_problem_for_that_group(self):
        group = parse(GROUP_HEADERS, ['Aurora', 'Ann Lee, Bo Ng', 'A101'])['groups'][0]
        self.assertIn('1 ID(s)', group['issues'][0])
        self.assertTrue(all(m['student_id'] == '' for m in group['members']))

    def test_topic_location_and_ids_are_all_optional(self):
        parsed = parse(['Group', 'Members'], ['Aurora', 'Ann Lee'])
        group = parsed['groups'][0]
        self.assertEqual((group['topic'], group['location'], group['members'][0]['student_id']), ('', '', ''))

    def test_other_header_wordings_are_understood(self):
        parsed = parse(['Team', 'Students', 'Reg No', 'Project', 'Room'], ['Aurora', 'Ann Lee', 'A1', 'Map', 'R1'])
        group = parsed['groups'][0]
        self.assertEqual((group['topic'], group['location']), ('Map', 'R1'))
        self.assertEqual(group['members'][0]['student_id'], 'A1')

    def test_a_group_with_no_members_is_kept_with_a_warning(self):
        group = parse(GROUP_HEADERS, ['Zephyr', None, None, None, 'Room 1'])['groups'][0]
        self.assertEqual(group['members'], [])
        self.assertIn('created empty', group['warnings'][0])

    def test_a_group_repeated_on_several_rows_is_merged_first_topic_wins(self):
        parsed = parse(GROUP_HEADERS, ['Aurora', 'Ann Lee', None, 'Map'], ['aurora', 'Bo Ng', None, 'Other'])
        self.assertEqual(len(parsed['groups']), 1)
        self.assertEqual(parsed['groups'][0]['topic'], 'Map')
        self.assertEqual(parsed['groups'][0]['rows'], [2, 3])
        self.assertEqual(len(parsed['groups'][0]['members']), 2)

    def test_a_person_listed_twice_in_a_group_counts_once_with_a_warning(self):
        group = parse(GROUP_HEADERS, ['Aurora', 'Ann Lee, ann lee'])['groups'][0]
        self.assertEqual(len(group['members']), 1)
        self.assertIn('listed twice', group['warnings'][0])

    def test_members_without_a_group_name_are_reported_and_blank_rows_ignored(self):
        parsed = parse(GROUP_HEADERS, [None, 'Ann Lee'], [None, None, None], ['Aurora', 'Bo Ng'])
        self.assertEqual(parsed['problems'], [(2, 'Members are listed but the group name is empty.')])
        self.assertEqual(len(parsed['groups']), 1)

    def test_a_row_with_only_a_topic_or_location_and_no_group_is_ignored_quietly(self):
        parsed = parse(GROUP_HEADERS, [None, None, None, 'Orphan topic', 'Room 9'], ['Aurora', 'Ann Lee'])
        self.assertEqual(parsed['problems'], [])
        self.assertEqual([g['name'] for g in parsed['groups']], ['Aurora'])

    def test_overlong_values_are_problems_for_the_group(self):
        group = parse(GROUP_HEADERS, ['G' * 101, 'N' * 151, 'I' * 41, 'T' * 201, 'L' * 121],
                      )['groups'][0]
        text = ' '.join(group['issues'])
        for part in ('Group name', 'Topic', 'Location', 'longer than 150', 'ID'):
            self.assertIn(part, text)

    def test_a_file_that_is_not_a_workbook_empty_or_without_the_columns_is_refused(self):
        from io import BytesIO
        self.assertEqual(parse_groups(BytesIO(b'junk'))['fatal'], UNREADABLE)
        self.assertEqual(parse_groups(xlsx_buffer())['fatal'], 'The file is empty.')
        self.assertIn('Group and Name columns', parse(['Colour', 'Shape'], ['red', 'round'])['fatal'])
        self.assertIn('Group and Name columns', parse(['Group'], ['Red'])['fatal'])

    def test_too_many_rows_are_refused(self):
        rows = [GROUP_HEADERS] + [[f'G{i}', f'Person {i}'] for i in range(MAX_ROWS + 1)]
        self.assertIn(str(MAX_ROWS), parse(*rows)['fatal'])

    def test_the_downloadable_sample_reads_back_cleanly(self):
        parsed = parse_groups(sample_workbook())
        self.assertIsNone(parsed['fatal'])
        self.assertEqual([g['name'] for g in parsed['groups']], ['Aurora', 'Meridian', 'Horizon'])
        self.assertEqual(parsed['problems'], [])
        self.assertTrue(all(not g['issues'] for g in parsed['groups']))


class PlanAndApplyTests(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='Demo')

    def plan(self, *rows):
        return plan_import(self.session, parse(GROUP_HEADERS, *rows))

    def apply(self, *rows):
        return apply_import(self.session, parse(GROUP_HEADERS, *rows))

    def test_students_and_groups_are_created_together_with_topic_and_location(self):
        counts = self.apply(['Aurora', 'Ann Lee, Bo Ng', None, 'Map', 'Room 1'], ['Meridian', 'Di Park'])
        self.assertEqual((counts['groups_created'], counts['students_created'], counts['members_added']), (2, 3, 3))
        group = PresentationGroup.objects.get(name='Aurora')
        self.assertEqual((group.topic, group.location, group.order), ('Map', 'Room 1', 1))
        self.assertEqual(PresentationGroup.objects.get(name='Meridian').order, 2)
        self.assertEqual(group.member_count(), 2)
        self.assertEqual(Student.objects.count(), 3)

    def test_an_empty_group_is_created(self):
        counts = self.apply(['Zephyr', None])
        self.assertEqual(counts['groups_created'], 1)
        self.assertEqual(PresentationGroup.objects.get(name='Zephyr').member_count(), 0)

    def test_the_plan_writes_nothing_and_counts_what_would_happen(self):
        plan = self.plan(['Aurora', 'Ann Lee, Bo Ng'])
        self.assertEqual(plan['summary'], {'groups': 1, 'ready': 1, 'skipped': 0, 'new_groups': 1,
                                           'new_students': 2, 'matched_students': 0})
        self.assertFalse(Student.objects.exists())

    def test_an_existing_student_is_matched_by_name_when_the_file_has_no_id(self):
        bo = Student.objects.create(assessment_session=self.session, full_name='Bo Ng')
        counts = self.apply(['Aurora', 'bo  NG, Cy Oh'])
        self.assertEqual((counts['students_created'], counts['members_added']), (1, 2))
        self.assertTrue(GroupMembership.objects.filter(student=bo).exists())
        self.assertEqual(Student.objects.count(), 2)

    def test_matching_by_id_ignores_how_the_name_is_spelled(self):
        ann = Student.objects.create(assessment_session=self.session, full_name='Ann Lee', student_id='A1')
        counts = self.apply(['Aurora', 'Annie Lee', 'a1'])
        self.assertEqual((counts['students_created'], counts['members_added']), (0, 1))
        self.assertEqual(GroupMembership.objects.get().student, ann)

    def test_a_student_without_an_id_yet_gets_it_filled_in_from_the_file(self):
        ann = Student.objects.create(assessment_session=self.session, full_name='Ann Lee')
        self.apply(['Aurora', 'Ann Lee', 'A1'])
        ann.refresh_from_db()
        self.assertEqual(ann.student_id, 'A1')
        self.assertEqual(Student.objects.count(), 1)

    def test_two_existing_students_with_the_same_name_make_a_name_only_member_ambiguous(self):
        Student.objects.create(assessment_session=self.session, full_name='Ann Lee', student_id='A1')
        Student.objects.create(assessment_session=self.session, full_name='Ann Lee', student_id='A2')
        entry = self.plan(['Aurora', 'Ann Lee'])['groups'][0]
        self.assertFalse(entry['ready'])
        self.assertIn('matches 2 students', entry['issues'][0])

    def test_joining_by_id_needs_an_id_for_every_new_student(self):
        self.session.identify_by = AssessmentSession.IdentifyBy.STUDENT_ID
        self.session.save()
        entry = self.plan(['Aurora', 'Ann Lee, Bo Ng', 'A1, B1'], ['Meridian', 'Di Park'])['groups']
        self.assertTrue(entry[0]['ready'])
        self.assertIn('no Student ID', entry[1]['issues'][0])

    def test_joining_by_name_flags_a_new_student_whose_name_is_already_taken(self):
        Student.objects.create(assessment_session=self.session, full_name='Ann Lee', student_id='A1')
        entry = self.plan(['Aurora', 'Ann Lee', 'A2'])['groups'][0]
        self.assertIn('more than one student when joining by name', entry['issues'][0])

    def test_two_different_new_people_with_the_same_name_are_flagged_when_joining_by_name(self):
        plan = self.plan(['Aurora', 'Ann Lee', 'A1'], ['Meridian', 'Ann Lee', 'A2'])
        self.assertTrue(all('more than one student' in ' '.join(g['issues']) for g in plan['groups']))

    def test_the_same_person_in_two_groups_flags_both(self):
        plan = self.plan(['Aurora', 'Ann Lee'], ['Meridian', 'Ann Lee, Bo Ng'])
        self.assertEqual(plan['summary']['ready'], 0)
        self.assertIn('also listed under "Meridian"', plan['groups'][0]['issues'][0])
        self.assertEqual(self.apply(['Aurora', 'Ann Lee'], ['Meridian', 'Ann Lee, Bo Ng'])['groups_created'], 0)
        self.assertFalse(PresentationGroup.objects.exists())

    def test_a_student_already_in_another_group_flags_the_group(self):
        self.apply(['Aurora', 'Ann Lee'])
        entry = self.plan(['Meridian', 'Ann Lee'])['groups'][0]
        self.assertIn('already in "Aurora"', entry['issues'][0])

    def test_reimporting_a_group_adds_only_the_new_member(self):
        self.apply(['Aurora', 'Ann Lee'])
        counts = self.apply(['aurora', 'Ann Lee, Bo Ng', None, 'New topic'])
        self.assertEqual((counts['groups_created'], counts['students_created'], counts['members_added']), (0, 1, 1))
        self.assertEqual(counts['groups_updated'], 1)
        group = PresentationGroup.objects.get()
        self.assertEqual((group.member_count(), group.topic), (2, 'New topic'))

    def test_reimporting_the_same_file_changes_nothing(self):
        self.apply(['Aurora', 'Ann Lee', None, 'Map'])
        counts = self.apply(['Aurora', 'Ann Lee', None, 'Map'])
        self.assertEqual((counts['groups_created'], counts['groups_updated'], counts['members_added']), (0, 0, 0))

    def test_a_capped_group_refuses_more_new_members_than_it_has_room_for(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Aurora', max_size=1)
        GroupMembership.objects.create(group=group, student=Student.objects.create(
            assessment_session=self.session, full_name='Ann Lee'))
        entry = self.plan(['Aurora', 'Ann Lee, Bo Ng, Cy Oh'])['groups'][0]
        self.assertIn('allows 1 member(s)', entry['issues'][0])

    def test_a_capped_group_takes_new_members_that_fit(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Aurora', max_size=3)
        GroupMembership.objects.create(group=group, student=Student.objects.create(
            assessment_session=self.session, full_name='Ann Lee'))
        counts = self.apply(['Aurora', 'Ann Lee, Bo Ng, Cy Oh'])
        self.assertEqual(counts['members_added'], 2)

    def test_a_group_that_has_presented_cannot_change_its_members(self):
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Aurora')
        PresentationTurn.objects.create(assessment_session=self.session, group=group, status='closed')
        entry = self.plan(['Aurora', 'Ann Lee'])['groups'][0]
        self.assertIn('presented or is presenting', entry['issues'][0])

    def test_a_group_with_a_problem_is_skipped_whole_while_the_others_import(self):
        counts = self.apply(['Aurora', 'Ann Lee, Bo Ng', 'A1'], ['Meridian', 'Di Park'])
        self.assertEqual(counts['skipped'], ['Aurora'])
        self.assertEqual(counts['groups_created'], 1)
        self.assertEqual(Student.objects.filter(full_name__in=['Ann Lee', 'Bo Ng']).count(), 0)
