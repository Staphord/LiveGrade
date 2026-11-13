"""Co-Assessment setup: rubric table and edit, spreadsheet imports, review step."""
from decimal import Decimal
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains import coassessment_import as imports
from service_domains.models import (
    CoAssessmentCriterion, CoAssessmentExaminer, CoAssessmentGroup, CoAssessmentParticipant,
    CoAssessmentSession,
)

Status = CoAssessmentSession.Status


def upload(headers, rows, name='data.xlsx'):
    return SimpleUploadedFile(name, workbook_bytes(headers, rows).getvalue())


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'coassessment'])
class SetupFlowCase(TestCase):
    def setUp(self):
        self.org = make_org('Setup Flow Org')
        self.owner = make_user('setup-flow-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Defense Day')
        sign_in(self.client, self.owner, self.org)

    def url(self, name):
        return reverse(name, args=[self.session.pk])

    def criterion(self, name, weight, scope='group', order=1):
        return CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name=name, weight=Decimal(weight), scope=scope, order=order)

    def run_import(self, name, file, page, apply=True):
        """Upload, read the preview from the step page, optionally apply it."""
        self.client.post(self.url(name), {'file': file})
        state = self.client.get(self.url(page)).context['import_state']
        if apply:
            self.client.post(self.url(name), {'action': 'apply', 'preview_token': state['token']})
        return state


class FormulaCellTests(SetupFlowCase):
    """Text cells that start with = are refused wherever they could be exported later."""

    def rows(self, rows):
        return mock.patch.object(imports, '_read_bounded_rows', return_value=rows)

    def test_rubric_examiner_and_roster_formulas_are_refused(self):
        with self.rows([['Criterion', 'Weight'], ['=1+1', 100]]):
            self.assertIn('Formula cells', imports.parse_rubric(mock.Mock(size=1))['rows'][0]['errors'][-1])
        with self.rows([['Name', 'Email'], ['=cmd', 'a@example.edu']]):
            self.assertIn('Formula cells', ' '.join(imports.parse_examiners(mock.Mock(size=1))['rows'][0]['errors']))
        group = {'name': '=G', 'topic': '', 'members': [{'full_name': 'A', 'student_id': '', 'email': ''}]}
        plan = imports.plan_roster(self.session, {'fatal': None, 'groups': [group]})
        self.assertIn('Formula cells', plan['errors'][0][1])


class RubricStepTests(SetupFlowCase):
    def test_rubric_is_a_table_with_edit_and_delete_and_collapsed_forms(self):
        self.criterion('Content', '60')
        self.criterion('Delivery', '40', 'individual', 2)
        html = self.client.get(self.url('coassessment_setup_rubric')).content.decode()
        self.assertIn('<table class="table', html)
        self.assertIn('Edit Content', html)
        self.assertIn('Delete Delivery', html)
        self.assertIn('Weights total 100%', html)
        self.assertIn('data-inline-region', html)
        self.assertNotIn('<summary', html)
        self.assertIn('id="rubric-form"', html)
        self.assertIn('id="rubric-import"', html)
        self.assertNotIn('—', html)

    def test_remaining_and_over_messages(self):
        self.criterion('Content', '30')
        html = self.client.get(self.url('coassessment_setup_rubric')).content.decode()
        self.assertIn('70.00% left to assign', html)
        self.criterion('Extra', '90', order=2)
        html = self.client.get(self.url('coassessment_setup_rubric')).content.decode()
        self.assertIn('is over 100%', html)

    def test_add_edit_and_delete_criterion(self):
        url = self.url('coassessment_setup_rubric')
        self.client.post(url, {'action': 'add_criterion', 'name': 'Content', 'weight': '100',
                               'scope': 'group', 'description': 'Depth'})
        crit = self.session.criteria.get()
        resp = self.client.get(f'{url}?edit={crit.pk}')
        self.assertEqual(resp.context['editing'], crit)
        self.assertContains(resp, 'Save changes')
        self.assertContains(resp, 'Edit criterion')

        self.client.post(url, {'action': 'update_criterion', 'criterion_id': crit.pk,
                               'name': 'Content depth', 'weight': '80', 'scope': 'individual'})
        crit.refresh_from_db()
        self.assertEqual((crit.name, crit.weight, crit.scope), ('Content depth', Decimal('80.00'), 'individual'))

        self.client.post(url, {'action': 'delete_criterion', 'criterion_id': crit.pk})
        self.assertFalse(self.session.criteria.exists())
        self.client.post(url, {'action': 'delete_criterion', 'criterion_id': 99999})

    def test_invalid_add_rerenders_with_errors_and_values(self):
        resp = self.client.post(self.url('coassessment_setup_rubric'),
                                {'action': 'add_criterion', 'name': '', 'weight': '5'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['form'].errors)
        self.assertContains(resp, 'id="rubric-form"')

    def test_duplicate_name_is_reported_on_the_form(self):
        self.criterion('Content', '50')
        resp = self.client.post(self.url('coassessment_setup_rubric'),
                                {'action': 'add_criterion', 'name': 'Content', 'weight': '10'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('already in the rubric', str(resp.context['form'].errors['name']))
        self.assertEqual(self.session.criteria.count(), 1)
        other = self.criterion('Other', '10', order=2)
        resp = self.client.post(self.url('coassessment_setup_rubric'), {
            'action': 'update_criterion', 'criterion_id': other.pk, 'name': ' content ', 'weight': '10'})
        self.assertTrue(resp.context['form'].errors['name'])
        self.client.post(self.url('coassessment_setup_rubric'), {
            'action': 'update_criterion', 'criterion_id': other.pk, 'name': 'Other', 'weight': '20'})
        other.refresh_from_db()
        self.assertEqual(other.weight, Decimal('20.00'))

    def test_update_of_unknown_criterion_and_bad_edit_param(self):
        url = self.url('coassessment_setup_rubric')
        resp = self.client.post(url, {'action': 'update_criterion', 'criterion_id': 999, 'name': 'x', 'weight': '1'})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self.client.get(f'{url}?edit=abc').context['editing'])
        self.assertIsNone(self.client.get(f'{url}?edit=999').context['editing'])

    def test_description_is_a_one_line_growing_field_with_a_word_cap(self):
        html = self.client.get(self.url('coassessment_setup_rubric')).content.decode()
        self.assertIn('data-autogrow', html)
        self.assertIn('data-word-limit="40"', html)
        self.assertIn('data-word-count', html)


class RubricImportTests(SetupFlowCase):
    HEADERS = ['Criterion', 'Scope', 'Weight', 'Description']

    def test_import_replaces_the_rubric(self):
        self.criterion('Old', '100')
        state = self.run_import('coassessment_rubric_import', upload(self.HEADERS, [
            ['Content', 'Group', 60, 'Depth'], ['Q&A', 'Individual', 40, '']]), 'coassessment_setup_rubric')
        self.assertTrue(state['plan']['ok'])
        self.assertIn('will replace the current rubric', state['plan']['summary'])
        self.assertIn('1 existing criterion will be removed', state['plan']['summary'])
        self.assertEqual(list(self.session.criteria.values_list('name', 'scope', 'order')),
                         [('Content', 'group', 1), ('Q&A', 'individual', 2)])

    def test_preview_only_does_not_write_and_open_state_follows_the_preview(self):
        state = self.run_import('coassessment_rubric_import', upload(self.HEADERS, [
            ['Content', '', 100, '']]), 'coassessment_setup_rubric', apply=False)
        self.assertEqual(state['plan']['rows'][0]['cells'][1], 'Group / project')
        self.assertFalse(self.session.criteria.exists())
        self.assertContains(self.client.get(self.url('coassessment_setup_rubric')), 'Spreadsheet preview')

    def test_every_row_problem_is_reported(self):
        state = self.run_import('coassessment_rubric_import', upload(self.HEADERS, [
            ['', 'Nonsense', 'x', ''], ['Dup', 'Group', 30, ''], ['Dup', 'Group', 30, ''],
            ['N' * 141, 'Group', 0.001, 'D' * 1001]]), 'coassessment_setup_rubric', apply=False)
        messages = ' '.join(message for _, message in state['plan']['errors'])
        for fragment in ('Scope must be', 'Weight must be', 'name is required',
                         'repeated from row 3', 'longer than 140', 'longer than 1000'):
            self.assertIn(fragment, messages)
        self.assertFalse(state['plan']['ok'])
        self.assertEqual(state['token'], '')

    def test_weights_must_total_100_and_file_needs_rows(self):
        state = self.run_import('coassessment_rubric_import', upload(self.HEADERS, [
            ['A', 'Group', 40, '']]), 'coassessment_setup_rubric', apply=False)
        self.assertIn('this file totals 40', state['plan']['errors'][0][1])
        state = self.run_import('coassessment_rubric_import', upload(self.HEADERS, []),
                                'coassessment_setup_rubric', apply=False)
        self.assertFalse(state['plan']['ok'])

    def test_fatal_files(self):
        page = 'coassessment_setup_rubric'
        for file, text in (
            (upload(['Criterion'], [['A']]), 'Criterion and Weight headers'),
            (SimpleUploadedFile('bad.xlsx', b'not a workbook'), None),
        ):
            self.client.post(self.url('coassessment_rubric_import'), {'file': file})
            state = self.client.get(self.url(page)).context['import_state']
            self.assertTrue(state['plan'] or state['errors'])
            if text:
                self.assertIn(text, state['plan']['errors'][0][1])
        self.client.post(self.url('coassessment_rubric_import'), {})
        self.assertTrue(self.client.get(self.url(page)).context['import_state']['errors'])

    def test_parse_limits(self):
        big = mock.Mock(size=11 * 1024 * 1024)
        self.assertIn('10 MB', imports.parse_rubric(big)['fatal'])
        rows = [[f'C{i}', 'Group', 1, ''] for i in range(imports.MAX_CRITERIA + 1)]
        self.assertIn('more than 50 criteria', imports.parse_rubric(upload(self.HEADERS, rows))['fatal'])
        with mock.patch.object(imports, '_read_bounded_rows', return_value=imports.TOO_MANY_PHYSICAL_ROWS):
            self.assertIn('physical row limit', imports.parse_rubric(upload(self.HEADERS, []))['fatal'])
        self.assertIn('empty', imports.parse_rubric(upload([], []))['fatal'])
        self.assertIn('10 MB', imports.parse_roster(big)['fatal'])

    def test_apply_refuses_after_going_live_and_for_other_owners(self):
        parsed = imports.parse_rubric(upload(self.HEADERS, [['A', 'Group', 100, '']]))
        org = {'id': self.org.pk}
        other = make_user('someone-else')
        with self.assertRaises(PermissionDenied):
            imports.apply_rubric(self.session, parsed, organization=org, user=other)
        self.session.status = Status.ACTIVE
        self.session.save()
        with self.assertRaisesMessage(ValidationError, 'before the session goes live'):
            imports.apply_rubric(self.session, parsed, organization=org, user=self.owner)
        self.session.status = Status.DRAFT
        self.session.save()
        bad = imports.parse_rubric(upload(self.HEADERS, [['A', 'Group', 50, '']]))
        with self.assertRaisesMessage(ValidationError, 'has errors'):
            imports.apply_rubric(self.session, bad, organization=org, user=self.owner)

    def test_sample_and_ownership(self):
        resp = self.client.get(self.url('coassessment_rubric_sample'))
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertEqual(resp['Cache-Control'], 'private, no-store')
        stranger = make_user('stranger')
        self.client.logout()
        sign_in(self.client, stranger, self.org)
        self.assertEqual(self.client.get(self.url('coassessment_rubric_sample')).status_code, 404)
        self.assertEqual(self.client.post(self.url('coassessment_rubric_import'), {}).status_code, 404)
        self.assertEqual(self.client.get(self.url('coassessment_setup_review')).status_code, 404)


class RosterStepTests(SetupFlowCase):
    HEADERS = ['Group Name', 'Name', 'Student ID', 'Topic']

    def test_groups_are_visible_sections_with_counts(self):
        group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, name='Aurora', order=1)
        CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, group=group,
            full_name='Ann Lee')
        resp = self.client.get(self.url('coassessment_setup_roster'))
        html = resp.content.decode()
        self.assertEqual(resp.context['student_count'], 1)
        self.assertIn(f'id="group-{group.pk}"', html)
        self.assertIn('1 student', html)
        self.assertNotIn('<summary', html)
        self.assertNotIn('—', html)

    def test_import_and_add_sections_are_always_visible(self):
        html = self.client.get(self.url('coassessment_setup_roster')).content.decode()
        self.assertIn('id="roster-import"', html)
        self.assertIn('id="roster-add"', html)
        self.assertNotIn('<summary', html)

    def test_update_group_and_errors(self):
        url = self.url('coassessment_setup_roster')
        self.client.post(url, {'action': 'add_group', 'name': 'Aurora', 'topic': 'Map'})
        group = self.session.groups.get()
        self.client.post(url, {'action': 'update_group', 'group_id': group.pk,
                               'name': 'Aurora 2', 'topic': 'Maps'})
        group.refresh_from_db()
        self.assertEqual((group.name, group.topic), ('Aurora 2', 'Maps'))
        self.client.post(url, {'action': 'update_group', 'group_id': group.pk, 'name': '', 'topic': ''})
        group.refresh_from_db()
        self.assertEqual(group.name, 'Aurora 2')
        self.client.post(url, {'action': 'update_group', 'group_id': 999, 'name': 'x'})
        self.client.post(url, {'action': 'delete_group', 'group_id': 999})
        self.client.post(url, {'action': 'delete_group', 'group_id': group.pk})
        self.assertFalse(self.session.groups.exists())

    def test_import_adds_groups_and_leaves_existing_ones(self):
        CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, name='Aurora',
            topic='Keep', order=1)
        state = self.run_import('coassessment_roster_import', upload(self.HEADERS, [
            ['Aurora', 'Ann Lee, Bo Ng', 'A1, A2', 'Changed'], ['Meridian', 'Di Park', 'B1', '']]),
            'coassessment_setup_roster')
        self.assertIn('Adds students to the existing group', state['plan']['rows'][0]['cells'][2])
        self.assertEqual(self.session.groups.count(), 2)
        self.assertEqual(self.session.groups.get(name='Aurora').topic, 'Keep')
        self.assertEqual(self.session.participants.count(), 3)

    def test_roster_problems_are_reported(self):
        state = self.run_import('coassessment_roster_import', upload(self.HEADERS, [
            ['G' * 141, 'Ann Lee', 'A1', 'T' * 256], ['Other', 'Bo Ng', 'A1', ''],
            ['Long', 'N' * 151, 'I' * 41, ''],
            ['Crowd', ', '.join(f'S{i}' for i in range(51)), '', '']]),
            'coassessment_setup_roster', apply=False)
        messages = ' '.join(message for _, message in state['plan']['errors'])
        for fragment in ('Group name is longer', 'Topic is longer', 'also listed under',
                         'too long', 'at most 50 students'):
            self.assertIn(fragment, messages)

    def test_roster_fatal_and_empty_and_oversize(self):
        self.client.post(self.url('coassessment_roster_import'), {'file': upload(['Nothing'], [['x']])})
        plan = self.client.get(self.url('coassessment_setup_roster')).context['import_state']['plan']
        self.assertIn('"Group" or "Team"', plan['errors'][0][1])
        empty = imports.plan_roster(self.session, {'fatal': None, 'groups': []})
        self.assertIn('no group rows', empty['errors'][0][1])
        many = [{'name': f'G{i}', 'topic': '', 'members': []} for i in range(imports.MAX_GROUPS + 1)]
        self.assertIn('more than 100 groups', imports.plan_roster(self.session, {'fatal': None, 'groups': many})['errors'][0][1])
        with self.assertRaises(PermissionDenied):
            imports.apply_roster(self.session, {'fatal': None, 'groups': many[:1]},
                                 organization={'id': self.org.pk}, user=make_user('x-user'))
        with self.assertRaises(ValidationError):
            imports.apply_roster(self.session, {'fatal': None, 'groups': many},
                                 organization={'id': self.org.pk}, user=self.owner)


class ExaminerStepTests(SetupFlowCase):
    HEADERS = ['Name', 'Email', 'Chair']

    def examiner(self, name='Dr A', email='a@example.edu'):
        return CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name=name, email=email, invite_token=f'tok-{email}', token_digest=f'dig-{email}')

    def test_examiners_show_their_link_without_dropdowns(self):
        ex = self.examiner()
        html = self.client.get(self.url('coassessment_setup_examiners')).content.decode()
        self.assertIn(f'id="examiner-{ex.pk}"', html)
        self.assertIn('tokenLink', html)
        self.assertIn('1</strong> of 1 needed', html)
        self.assertNotIn('Publish', html)
        self.assertNotIn('<summary', html)

    def test_import_invites_each_examiner_by_email(self):
        with mock.patch('service_domains.coassessment_views.send_coassessment_examiner_invitation_email') as task, \
                self.captureOnCommitCallbacks(execute=True):
            state = self.run_import('coassessment_examiner_import', upload(self.HEADERS, [
                ['Dr Amara', 'AMARA@example.edu', 'Yes'], ['Prof Ben', 'ben@example.edu', '']]),
                'coassessment_setup_examiners')
        self.assertIn('2 examiners will be invited', state['plan']['summary'])
        self.assertEqual(task.delay.call_count, 2)
        chair = self.session.examiners.get(email='amara@example.edu')
        self.assertTrue(chair.is_chair)
        self.assertEqual(len(chair.invite_token), 43)
        self.assertFalse(self.session.examiners.get(email='ben@example.edu').is_chair)

    def test_import_problems(self):
        self.examiner(email='have@example.edu')
        state = self.run_import('coassessment_examiner_import', upload(self.HEADERS, [
            ['', 'not-an-email', 'maybe'], ['Have', 'have@example.edu', ''],
            ['Twice', 't@example.edu', ''], ['Twice', 't@example.edu', ''],
            ['N' * 121, 'n@example.edu', '']]),
            'coassessment_setup_examiners', apply=False)
        messages = ' '.join(message for _, message in state['plan']['errors'])
        for fragment in ('Chair must be', 'name is required', 'valid email', 'already invited',
                         'repeated from row 4', 'longer than 120'):
            self.assertIn(fragment, messages)

    def test_import_fatal_empty_and_ownership(self):
        self.client.post(self.url('coassessment_examiner_import'), {'file': upload(['Name'], [['x']])})
        plan = self.client.get(self.url('coassessment_setup_examiners')).context['import_state']['plan']
        self.assertIn('Name and Email headers', plan['errors'][0][1])
        self.assertIn('no examiner rows', imports.plan_examiners(
            self.session, {'fatal': None, 'rows': []})['errors'][0][1])
        self.assertIn('empty', imports.parse_examiners(upload([], []))['fatal'])
        parsed = imports.parse_examiners(upload(self.HEADERS, [['A', 'a@example.edu', 'No']]))
        with self.assertRaises(PermissionDenied):
            imports.apply_examiners(self.session, parsed, organization={'id': self.org.pk},
                                    user=make_user('nobody'))
        with self.assertRaises(ValidationError):
            imports.apply_examiners(self.session, {'fatal': 'nope', 'rows': []},
                                    organization={'id': self.org.pk}, user=self.owner)

    def test_sample_download(self):
        self.assertIn('spreadsheetml', self.client.get(self.url('coassessment_examiner_sample'))['Content-Type'])

    def test_rotate_and_delete_ignore_unknown_examiners(self):
        url = self.url('coassessment_setup_examiners')
        self.assertEqual(self.client.post(url, {'action': 'rotate_token', 'examiner_id': 999}).status_code, 302)
        self.assertEqual(self.client.post(url, {'action': 'delete_examiner', 'examiner_id': 999}).status_code, 302)


class ReviewStepTests(SetupFlowCase):
    def test_review_lists_blockers_and_disables_go_live(self):
        resp = self.client.get(self.url('coassessment_setup_review'))
        self.assertFalse(resp.context['can_publish'])
        html = resp.content.decode()
        self.assertIn('Rubric totals 100%', html)
        self.assertIn('At least one presenting team', html)
        self.assertIn('disabled aria-disabled="true"', html)
        self.assertIn('Required', html)

    def test_review_allows_go_live_when_required_items_are_done(self):
        self.criterion('All', '100')
        CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, name='Aurora', order=1)
        resp = self.client.get(self.url('coassessment_setup_review'))
        self.assertTrue(resp.context['can_publish'])
        self.assertContains(resp, 'Recommended')
        self.assertContains(resp, 'student ID or full name')
        self.assertNotContains(resp, self.session.public_join_code)
        self.assertNotContains(resp, 'disabled aria-disabled="true"')
        self.client.post(self.url('coassessment_publish'))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, Status.ACTIVE)

    def test_review_after_going_live_points_to_the_console_and_publish_is_idempotent(self):
        self.session.status = Status.REVIEW
        self.session.save()
        self.assertContains(self.client.get(self.url('coassessment_setup_review')), 'Open the stage console')
        resp = self.client.post(self.url('coassessment_publish'))
        self.assertRedirects(resp, self.url('coassessment_live_console'), fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, Status.REVIEW)

    def test_stepper_has_five_steps_with_review_last_and_examiners_optional(self):
        steps = self.client.get(self.url('coassessment_setup_review')).context['stepper_steps']
        self.assertEqual([step['key'] for step in steps],
                         ['policy', 'rubric', 'roster', 'examiners', 'review'])
        self.assertTrue(steps[-1]['final'] and steps[-1]['current'])
        self.assertTrue(steps[3]['optional'])


class PolicyStepTests(SetupFlowCase):
    def test_policy_shows_every_section_and_description_grows(self):
        html = self.client.get(self.url('coassessment_setup_policy')).content.decode()
        self.assertIn('id="policy-advanced"', html)
        self.assertNotIn('<summary', html)
        self.assertIn('data-word-limit="60"', html)
        self.assertIn('data-inline-region', html)

    def test_policy_errors_show_on_the_form(self):
        resp = self.client.post(self.url('coassessment_setup_policy'), {
            'name': 'Defense Day', 'panel_weight': '70', 'peer_weight': '30',
            'minimum_panel_evaluators': '0', 'minimum_peer_evaluators': '3',
            'moderation_threshold': '15'})
        self.assertContains(resp, 'id="policy-advanced"')
        self.assertTrue(resp.context['form'].errors['minimum_panel_evaluators'])


class StepActionEdgeTests(SetupFlowCase):
    def examiner(self):
        return CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr A', email='a@example.edu', invite_token='tok', token_digest='dig')

    def test_invalid_team_and_examiner_forms_rerender(self):
        resp = self.client.post(self.url('coassessment_setup_roster'), {'action': 'add_group', 'name': ''})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="roster-add"')
        self.assertTrue(resp.context['group_form'].errors)
        resp = self.client.post(self.url('coassessment_setup_examiners'), {
            'action': 'invite_examiner', 'display_name': '', 'email': 'bad'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="examiner-add"')
        self.assertTrue(resp.context['form'].errors)
        self.assertEqual(self.client.post(self.url('coassessment_setup_examiners'), {'action': 'nope'}).status_code, 200)

    def test_remove_unknown_student_and_invite_then_revoke(self):
        self.assertEqual(self.client.post(self.url('coassessment_setup_roster'), {
            'action': 'remove_member', 'participant_id': 999}).status_code, 302)
        with mock.patch('service_domains.coassessment_views.send_coassessment_examiner_invitation_email') as task, \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url('coassessment_setup_examiners'), {
                'action': 'invite_examiner', 'display_name': 'Dr B', 'email': 'b@example.edu'})
        task.delay.assert_called_once()
        ex = self.session.examiners.get()
        self.client.post(self.url('coassessment_setup_examiners'), {'action': 'delete_examiner', 'examiner_id': ex.pk})
        self.assertFalse(self.session.examiners.exists())

    def test_a_broken_mail_queue_never_blocks_an_invitation(self):
        with mock.patch('service_domains.coassessment_views.send_coassessment_examiner_invitation_email') as task, \
                self.captureOnCommitCallbacks(execute=True):
            task.delay.side_effect = RuntimeError('broker down')
            self.client.post(self.url('coassessment_setup_examiners'), {
                'action': 'invite_examiner', 'display_name': 'Dr C', 'email': 'c@example.edu'})
        self.assertEqual(self.session.examiners.count(), 1)

    def test_policy_saves_edits_and_unknown_sessions_are_not_found(self):
        url = self.url('coassessment_setup_policy')
        resp = self.client.post(url, {
            'name': 'Renamed', 'panel_weight': '60', 'peer_weight': '40', 'minimum_panel_evaluators': '1',
            'minimum_peer_evaluators': '3', 'moderation_threshold': '15'})
        self.assertRedirects(resp, self.url('coassessment_setup_rubric'), fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.name, 'Renamed')
        self.assertEqual(self.client.get(reverse('coassessment_setup_policy', args=[99999])).status_code, 404)
        self.assertEqual(self.client.post(reverse('coassessment_publish', args=[99999])).status_code, 404)

    def test_publish_blocks_without_rubric_or_teams(self):
        resp = self.client.post(self.url('coassessment_publish'))
        self.assertRedirects(resp, self.url('coassessment_setup_rubric'), fetch_redirect_response=False)
        self.criterion('All', '100')
        resp = self.client.post(self.url('coassessment_publish'))
        self.assertRedirects(resp, self.url('coassessment_setup_roster'), fetch_redirect_response=False)


class ExaminerEmailTests(SetupFlowCase):
    def test_resend_queues_the_same_link_without_rotating_it(self):
        ex = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr A', email='a@example.edu', invite_token='keep-me', token_digest='dig')
        with mock.patch('service_domains.coassessment_views.send_coassessment_examiner_invitation_email') as task, \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url('coassessment_setup_examiners'),
                             {'action': 'resend_invitation', 'examiner_id': ex.pk})
            self.client.post(self.url('coassessment_setup_examiners'),
                             {'action': 'resend_invitation', 'examiner_id': 999})
        task.delay.assert_called_once()
        ex.refresh_from_db()
        self.assertEqual(ex.invite_token, 'keep-me')

    def test_the_invitation_email_carries_the_private_link_in_text_and_html(self):
        from django.core import mail
        from service_domains.tasks import send_coassessment_examiner_invitation_email
        ex = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr A', email='a@example.edu', invite_token='tok123', token_digest='d1',
            is_chair=True)
        self.assertEqual(send_coassessment_examiner_invitation_email.apply(args=(ex.pk, 'http://testserver')).get(), 'sent')
        message = mail.outbox[0]
        self.assertEqual(message.to, ['a@example.edu'])
        self.assertIn('Co-Assessment invitation', message.subject)
        self.assertIn('/coassessment/examiner/tok123/', message.body)
        self.assertIn('panel chair', message.body)
        self.assertIn('/coassessment/examiner/tok123/', message.alternatives[0][0])
