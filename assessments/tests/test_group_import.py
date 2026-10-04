from io import BytesIO

import openpyxl
from accounts.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from accounts.testing import make_org, make_user, sign_in
from assessments.excel import import_groups
from assessments.models import AssessmentSession, PresentationGroup, Student


def _xlsx(headers, rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile('groups.xlsx', buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class ImportGroupsFunctionTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026')
        self.a = Student.objects.create(assessment_session=self.session, full_name='Alice Wang', student_id='A1')
        self.b = Student.objects.create(assessment_session=self.session, full_name='Bob Otieno', student_id='B1')
        self.c = Student.objects.create(assessment_session=self.session, full_name='Cathy Mwangi', student_id='C1')

    def test_valid_group_created_by_student_id(self):
        f = _xlsx(['Group', 'Student ID'], [['Group 1', 'A1'], ['Group 1', 'B1']])
        result = import_groups(self.session, f)
        self.assertEqual(result['groups_created'], 1)
        self.assertEqual(result['members_added'], 2)
        self.assertEqual(result['flagged'], [])
        group = PresentationGroup.objects.get(assessment_session=self.session, name='Group 1')
        self.assertEqual(set(group.member_ids()), {self.a.pk, self.b.pk})

    def test_valid_group_created_by_full_name(self):
        f = _xlsx(['Group', 'Student Name'], [['Group 1', 'Alice Wang'], ['Group 1', 'Cathy Mwangi']])
        result = import_groups(self.session, f)
        self.assertEqual(result['groups_created'], 1)
        group = PresentationGroup.objects.get(assessment_session=self.session, name='Group 1')
        self.assertEqual(set(group.member_ids()), {self.a.pk, self.c.pk})

    def test_group_with_unknown_student_is_flagged_not_created(self):
        f = _xlsx(['Group', 'Student ID'], [['Group 1', 'A1'], ['Group 1', 'ZZZ']])
        result = import_groups(self.session, f)
        self.assertEqual(result['groups_created'], 0)
        self.assertEqual(len(result['flagged']), 1)
        self.assertEqual(result['flagged'][0]['group'], 'Group 1')
        self.assertIn('ZZZ', result['flagged'][0]['issues'][0])
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 1').exists())

    def test_valid_and_invalid_groups_both_reported_independently(self):
        f = _xlsx(['Group', 'Student ID'], [
            ['Group 1', 'A1'], ['Group 1', 'B1'],
            ['Group 2', 'C1'], ['Group 2', 'NOPE'],
        ])
        result = import_groups(self.session, f)
        self.assertEqual(result['groups_created'], 1)
        self.assertEqual(result['members_added'], 2)
        self.assertEqual(len(result['flagged']), 1)
        self.assertEqual(result['flagged'][0]['group'], 'Group 2')
        self.assertTrue(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 1').exists())
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 2').exists())

    def test_reimport_does_not_duplicate_memberships(self):
        f1 = _xlsx(['Group', 'Student ID'], [['Group 1', 'A1'], ['Group 1', 'B1']])
        import_groups(self.session, f1)
        f2 = _xlsx(['Group', 'Student ID'], [['Group 1', 'A1'], ['Group 1', 'B1']])
        result = import_groups(self.session, f2)
        self.assertEqual(result['groups_created'], 0)
        self.assertEqual(result['members_added'], 0)
        group = PresentationGroup.objects.get(assessment_session=self.session, name='Group 1')
        self.assertEqual(group.memberships.count(), 2)

    def test_missing_columns_reports_row_error(self):
        f = _xlsx(['Foo', 'Bar'], [['x', 'y']])
        result = import_groups(self.session, f)
        self.assertEqual(result['groups_created'], 0)
        self.assertEqual(len(result['row_errors']), 1)

    def test_empty_file(self):
        workbook = openpyxl.Workbook()
        buffer = BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        f = SimpleUploadedFile('empty.xlsx', buffer.read())
        # openpyxl always writes a header-less default sheet with no rows for
        # a brand-new workbook, so this exercises the "no rows at all" path.
        result = import_groups(self.session, f)
        self.assertEqual(result['groups_created'], 0)


class GroupImportViewTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.lecturer = make_user('lecturer')
        sign_in(self.client, self.lecturer, self.org)
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026', created_by=self.lecturer)

    def test_import_requires_roster_first(self):
        f = _xlsx(['Group', 'Student ID'], [['Group 1', 'A1']])
        response = self.client.post(
            f'/assessments/{self.session.pk}/groups/import/', {'file': f})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PresentationGroup.objects.filter(assessment_session=self.session).exists())

    def test_import_creates_group_end_to_end(self):
        Student.objects.create(assessment_session=self.session, full_name='Alice Wang', student_id='A1')
        Student.objects.create(assessment_session=self.session, full_name='Bob Otieno', student_id='B1')
        f = _xlsx(['Group', 'Student ID'], [['Group 1', 'A1'], ['Group 1', 'B1']])
        response = self.client.post(
            f'/assessments/{self.session.pk}/groups/import/', {'file': f})
        self.assertRedirects(response, f'/assessments/{self.session.pk}/groups/')
        self.assertTrue(PresentationGroup.objects.filter(assessment_session=self.session, name='Group 1').exists())
