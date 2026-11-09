"""FA-06 / Classroom's FR-014 slice: reusable configuration, never assessment data."""
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.access import ORGS_KEY
from accounts.testing import make_org, make_user, sign_in
from assessments.duplication import RUBRIC_FIELDS, SESSION_SETTINGS, values
from assessments.models import (
    AssessmentSession, EvaluationScore, GroupMembership, SessionTemplate, TemplateRubricCategory,
)
from assessments.session_templates import (
    blueprint, save_draft_from_template, save_session_template, starters_for, template_queryset,
)
from assessments.views import assessment_session_create, session_template_save


class SessionTemplateTests(TestCase):
    def setUp(self):
        self.org = make_org('University', oversee=True)
        self.owner = make_user('owner')
        sign_in(self.client, self.owner, self.org)
        self.source = AssessmentSession.objects.create(
            name='Source', organization_id=self.org.id, created_by=self.owner, status='closed',
            identify_by='student_id', ungraded_penalty=Decimal('2.75'), instructions='Use evidence.',
            default_presentation_seconds=180, default_turn_seconds=60, transition_gap_seconds=5,
            joining_locked=True, voting_paused=True)
        self.student = self.source.students.create(full_name='DO-NOT-COPY-ROSTER', student_id='PRIVATE-ID',
                                                   email='private@example.test')
        self.group = self.source.groups.create(name='DO-NOT-COPY-GROUP')
        GroupMembership.objects.create(group=self.group, student=self.student)
        self.category = self.source.rubric_categories.create(name='Evidence', description='Source rubric',
                                                              scope='group', weight=60, order=2)
        self.category.level_descriptors.create(score=30, description='Partly supported by evidence.')
        self.source.rubric_categories.create(name='Contribution', scope='individual', weight=40, order=1)
        turn = self.source.presentation_turns.create(group=self.group, status='closed')
        evaluation = turn.evaluations.create(evaluator=self.student)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=self.category, value=45, max_value=60)
        self.save_url = reverse('assessment_template_save', args=[self.source.pk])
        self.new_url = reverse('assessment_session_create')
        self.list_url = reverse('assessment_template_list')

    def saved(self):
        response = self.client.post(self.save_url, {'name': 'Reusable class', 'description': 'For next term'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'message': 'Template saved.'})
        return SessionTemplate.objects.get(name='Reusable class')

    def create(self, template, **overrides):
        token = f'saved:{template.pk}' if isinstance(template, SessionTemplate) else 'starter:' + template.key
        return self.client.post(self.new_url, {'name': 'New term', 'template': token,
            'identify_by': 'full_name', 'ungraded_penalty': '0.50', 'instructions': 'Updated notice', **overrides})

    def new_session(self):
        return AssessmentSession.objects.exclude(pk=self.source.pk).get()

    def test_save_confirmation_is_read_only_and_explains_scope(self):
        response = self.client.get(self.save_url)
        self.assertContains(response, 'Save setup only. Students, groups, and results are excluded.')
        self.assertContains(response, 'name="name"')
        self.assertContains(response, 'name="description"')
        self.assertFalse(SessionTemplate.objects.exists())

    def test_save_copies_exact_setup_and_no_assessment_records(self):
        template = self.saved()
        self.assertEqual(template.organization_id, self.org.id)
        self.assertEqual(template.service, 'classroom')
        self.assertEqual(template.created_by, self.owner)
        self.assertEqual(values(template, SESSION_SETTINGS), values(self.source, SESSION_SETTINGS))
        self.assertEqual([values(row, RUBRIC_FIELDS) for row in template.rubric_categories.all()],
                         [values(row, RUBRIC_FIELDS) for row in self.source.rubric_categories.all()])
        self.assertEqual(list(template.rubric_categories.get(name='Evidence').level_descriptors.values_list(
            'score', 'description')), [(Decimal('30.00'), 'Partly supported by evidence.')])
        self.assertEqual(self.source.students.count(), 1)
        self.assertEqual(self.source.presentation_turns.count(), 1)
        self.assertEqual(EvaluationScore.objects.get().value, 45)
        self.assertNotIn('students', {field.name for field in template._meta.get_fields()})
        self.assertNotIn('groups', {field.name for field in template._meta.get_fields()})
        self.assertNotIn('source_session', {field.name for field in template._meta.get_fields()})

    def test_create_makes_new_owned_draft_with_selected_rubric_and_timers(self):
        template = self.saved()
        response = self.create(template)
        new = self.new_session()
        self.assertRedirects(response, reverse('assessment_groups', args=[new.pk]))
        self.assertEqual(new.created_by, self.owner)
        self.assertEqual(new.organization_id, self.org.id)
        self.assertEqual(new.service, 'classroom')
        self.assertEqual(new.status, 'draft')
        self.assertNotEqual(new.uuid, self.source.uuid)
        self.assertFalse(new.joining_locked)
        self.assertFalse(new.voting_paused)
        self.assertIsNone(new.closed_at)
        self.assertIsNone(new.pending_transition_at)
        self.assertFalse(new.students.exists())
        self.assertFalse(new.groups.exists())
        self.assertFalse(new.presentation_turns.exists())
        self.assertFalse(new.participation_records.exists())
        self.assertFalse(new.changes.exists())
        self.assertEqual(new.default_presentation_seconds, 180)
        self.assertEqual(new.default_turn_seconds, 60)
        self.assertEqual(new.transition_gap_seconds, 5)
        self.assertEqual([values(row, RUBRIC_FIELDS) for row in new.rubric_categories.all()],
                         [values(row, RUBRIC_FIELDS) for row in template.rubric_categories.all()])
        self.assertEqual(list(new.rubric_categories.get(name='Evidence').level_descriptors.values_list(
            'score', 'description')), [(Decimal('30.00'), 'Partly supported by evidence.')])
        self.assertEqual(EvaluationScore.objects.count(), 1)

    def test_owner_can_override_basics_before_creating_from_template(self):
        self.create(self.saved())
        new = self.new_session()
        self.assertEqual(new.name, 'New term')
        self.assertEqual(new.identify_by, 'full_name')
        self.assertEqual(new.ungraded_penalty, Decimal('0.50'))
        self.assertEqual(new.instructions, 'Updated notice')

    def test_get_selection_preloads_form_and_previews_rubric_without_writes(self):
        template = self.saved()
        response = self.client.get(self.new_url, {'template': f'saved:{template.pk}'})
        self.assertEqual(response.context['form']['identify_by'].value(), 'student_id')
        self.assertEqual(response.context['form']['ungraded_penalty'].value(), Decimal('2.75'))
        self.assertEqual(response.context['form']['instructions'].value(), 'Use evidence.')
        self.assertContains(response, 'Evidence - 60%')
        self.assertContains(response, 'name="template" value="saved:')
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_source_and_template_changes_do_not_affect_created_sessions(self):
        template = self.saved()
        self.create(template)
        new = self.new_session()
        self.category.level_descriptors.update(description='Changed source guidance')
        template.rubric_categories.get(name='Evidence').level_descriptors.update(
            description='Changed template guidance')
        self.source.rubric_categories.update(weight=25)
        self.source.instructions = 'Source changed'
        self.source.save()
        template.rubric_categories.update(name='Changed template')
        template.instructions = 'Template changed'
        template.save()
        new.refresh_from_db()
        self.assertEqual(new.instructions, 'Updated notice')
        self.assertTrue(new.rubric_categories.filter(name='Evidence', weight=60).exists())
        self.assertEqual(new.rubric_categories.get(name='Evidence').level_descriptors.get().description,
                         'Partly supported by evidence.')
        self.assertNotEqual(blueprint(template)[0]['instructions'], self.source.instructions)

    def test_corrupt_template_descriptor_cannot_create_a_partial_session(self):
        template = self.saved()
        template.rubric_categories.get(name='Evidence').level_descriptors.update(score=70)
        response = self.create(template)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_template_remains_usable_after_source_deletion(self):
        template = self.saved()
        self.source.delete()
        response = self.create(template)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AssessmentSession.objects.get().rubric_categories.count(), 2)

    def test_deleting_template_keeps_source_and_created_sessions(self):
        template = self.saved()
        self.create(template)
        new = self.new_session()
        response = self.client.post(reverse('assessment_template_delete', args=[template.pk]))
        self.assertRedirects(response, self.list_url)
        self.assertFalse(SessionTemplate.objects.exists())
        self.assertFalse(TemplateRubricCategory.objects.exists())
        self.assertEqual(AssessmentSession.objects.count(), 2)
        self.assertEqual(new.rubric_categories.count(), 2)
        self.assertEqual(self.source.presentation_turns.count(), 1)
        self.assertEqual(EvaluationScore.objects.count(), 1)

    def test_blank_creation_stays_compatible_and_does_not_load_any_template(self):
        self.saved()
        response = self.client.post(self.new_url, {'name': 'Blank', 'identify_by': 'full_name', 'ungraded_penalty': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.new_session().rubric_categories.exists())
        response = self.client.get(self.new_url)
        self.assertIsNone(response.context['selected_template'])

    def test_starters_are_service_specific_valid_and_independent(self):
        starters = starters_for('classroom')
        self.assertEqual(len(starters), 3)
        self.assertEqual(starters_for('panel'), ())
        self.assertEqual(starters_for(None), ())
        for starter in starters:
            with self.subTest(starter=starter.key):
                response = self.create(starter)
                self.assertEqual(response.status_code, 302)
                new = self.new_session()
                self.assertEqual(new.rubric_weight_total(), 100)
                self.assertEqual(new.rubric_categories.count(), len(starter.categories))
                self.assertFalse(new.students.exists())
                new.rubric_categories.update(name='Owner changed this')
                _, original = blueprint(starter)
                self.assertNotEqual(original[0]['name'], 'Owner changed this')
                new.delete()
        settings, categories = blueprint(starters[0])
        settings['instructions'] = 'Changed'
        categories[0]['name'] = 'Changed'
        self.assertNotEqual(blueprint(starters[0])[0]['instructions'], 'Changed')
        self.assertNotEqual(blueprint(starters[0])[1][0]['name'], 'Changed')

    def test_library_and_picker_show_only_current_owners_scoped_templates(self):
        mine = self.saved()
        other = make_user('colleague')
        hidden = [
            SessionTemplate.objects.create(name='PRIVATE-COLLEAGUE', organization_id=self.org.id, created_by=other),
            SessionTemplate.objects.create(name='PRIVATE-OTHER-ORG', organization_id=9999, created_by=self.owner),
            SessionTemplate.objects.create(name='PRIVATE-PANEL', organization_id=self.org.id, created_by=self.owner, service='panel'),
        ]
        for url in (self.new_url, self.list_url):
            response = self.client.get(url)
            self.assertContains(response, mine.name)
            for template in hidden:
                self.assertNotContains(response, template.name)
            self.assertNotContains(response, 'DO-NOT-COPY-ROSTER')
            self.assertNotContains(response, 'private@example.test')

    def test_invalid_and_out_of_scope_selections_are_indistinguishable_and_create_nothing(self):
        hidden = SessionTemplate.objects.create(name='PRIVATE-TEMPLATE', organization_id=9999, created_by=self.owner)
        for token in (f'saved:{hidden.pk}', 'saved:999999', 'starter:unknown', 'panel:1', 'https://evil.example/'):
            with self.subTest(token=token):
                response = self.client.get(self.new_url, {'template': token})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['template_picker'].errors)
                self.assertNotContains(response, hidden.name)
                response = self.client.post(self.new_url, {'template': token, 'name': 'No',
                    'identify_by': 'full_name', 'ungraded_penalty': '1'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['template_picker'].errors)
                self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_deleted_template_selection_is_rejected_without_falling_back_to_blank(self):
        template = self.saved()
        token = f'saved:{template.pk}'
        self.client.get(self.new_url, {'template': token})
        template.delete()
        response = self.client.post(self.new_url, {'name': 'No', 'template': token,
            'identify_by': 'full_name', 'ungraded_penalty': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['template_picker'].errors)
        self.assertEqual(response.context['template_selection'], token)
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_caller_cannot_choose_scope_owner_or_runtime_state(self):
        template = self.saved()
        self.create(template, service='panel', organization_id=9999, created_by=9999, status='live',
                    uuid=str(self.source.uuid), joining_locked='on')
        new = self.new_session()
        self.assertEqual(new.service, 'classroom')
        self.assertEqual(new.organization_id, self.org.id)
        self.assertEqual(new.created_by, self.owner)
        self.assertEqual(new.status, 'draft')
        self.assertFalse(new.joining_locked)
        self.assertNotEqual(new.uuid, self.source.uuid)

    def test_template_save_ignores_caller_scope_and_rubric_payload(self):
        self.client.post(self.save_url, {'name': 'Saved', 'service': 'panel', 'organization_id': 9999,
                                        'created_by': 9999, 'rubric': 'Ignore client data'})
        template = SessionTemplate.objects.get()
        self.assertEqual(template.service, 'classroom')
        self.assertEqual(template.organization_id, self.org.id)
        self.assertEqual(template.created_by, self.owner)
        self.assertEqual(template.rubric_categories.count(), 2)

    def test_colleague_even_overseer_cannot_save_use_or_delete_template(self):
        template = self.saved()
        sign_in(self.client, make_user('overseer'), self.org)
        for method in ('get', 'post'):
            self.assertEqual(getattr(self.client, method)(self.save_url, {'name': 'Stolen'}).status_code, 404)
        self.assertEqual(self.create(template).status_code, 200)
        self.assertEqual(AssessmentSession.objects.count(), 1)
        self.assertEqual(self.client.post(reverse('assessment_template_delete', args=[template.pk])).status_code, 404)
        self.assertTrue(SessionTemplate.objects.filter(pk=template.pk).exists())

    def test_missing_scope_fails_closed(self):
        template = self.saved()
        request = RequestFactory().post(self.save_url, {'name': 'No scope'})
        request.user = self.owner
        request.session = {ORGS_KEY: [self.org.as_claim()]}
        self.assertFalse(template_queryset(request).exists())
        with self.assertRaises(Http404):
            session_template_save(request, self.source.pk)
        with self.assertRaises(Http404):
            assessment_session_create(request)
        self.assertEqual(SessionTemplate.objects.get(), template)

    def test_helper_cannot_capture_another_owners_source_from_an_oversight_queryset(self):
        with self.assertRaises(Http404):
            save_session_template(AssessmentSession.objects.for_organization(self.org.as_claim()).for_service('classroom'),
                                  self.source.pk, owner=make_user('other'), name='No')
        self.assertFalse(SessionTemplate.objects.exists())

    def test_helper_rejects_template_scope_mismatch_and_non_draft_targets(self):
        template = self.saved()
        for overrides in ({'service': 'panel'}, {'organization_id': 9999}, {'created_by': make_user('other')},
                          {'service': ''}):
            instance = AssessmentSession(name='No', service='classroom', organization_id=self.org.id, created_by=self.owner)
            for key, value in overrides.items():
                setattr(instance, key, value)
            with self.assertRaises(Http404):
                save_draft_from_template(instance, template)
        with self.assertRaises(ValidationError):
            save_draft_from_template(self.source, template)
        with self.assertRaises(ValidationError):
            save_draft_from_template(AssessmentSession(name='No', organization_id=self.org.id), template)
        with self.assertRaises(ValidationError):
            save_draft_from_template(AssessmentSession(name='No', organization_id=self.org.id, created_by=self.owner, status='live'), template)
        with self.assertRaises(Http404):
            save_draft_from_template(AssessmentSession(name='No', service='panel', organization_id=self.org.id, created_by=self.owner),
                                     starters_for('classroom')[0])
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_invalid_save_fields_and_invalid_source_rubric_do_not_write(self):
        for data in ({'name': ''}, {'name': 'x' * 151}, {'name': 'Valid', 'description': 'x' * 501}):
            response = self.client.post(self.save_url, data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.category.weight = 70
        self.category.save()
        response = self.client.post(self.save_url, {'name': 'Overweight'})
        self.assertContains(response, 'over 100%')
        self.assertFalse(SessionTemplate.objects.exists())

    def test_duplicate_criterion_names_are_not_saved(self):
        self.source.rubric_categories.create(name='evidence', scope='group', weight=1)
        response = self.client.post(self.save_url, {'name': 'No'})
        self.assertContains(response, 'appears twice')
        self.assertFalse(SessionTemplate.objects.exists())

    def test_corrupt_template_rubric_cannot_create_partial_session(self):
        template = self.saved()
        template.rubric_categories.update(weight=70)
        response = self.create(template)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_save_failure_rolls_back_new_template_and_categories(self):
        with patch('assessments.session_templates.TemplateRubricCategory.objects.create',
                   side_effect=ValidationError('Copy failed.')):
            response = self.client.post(self.save_url, {'name': 'No'})
        self.assertContains(response, 'Copy failed.')
        self.assertFalse(SessionTemplate.objects.exists())
        self.assertEqual(self.source.rubric_categories.count(), 2)

    def test_create_failure_rolls_back_new_draft(self):
        template = self.saved()
        with patch('assessments.session_templates.RubricCategory.objects.create',
                   side_effect=ValidationError('Copy failed.')):
            response = self.create(template)
        self.assertContains(response, 'Copy failed.')
        self.assertEqual(AssessmentSession.objects.count(), 1)
        self.assertEqual(template.rubric_categories.count(), 2)

    def test_descriptor_failure_rolls_back_new_draft(self):
        template = self.saved()
        with patch('assessments.session_templates.LevelDescriptor.objects.bulk_create',
                   side_effect=ValidationError('Descriptor copy failed.')):
            response = self.create(template)
        self.assertContains(response, 'Descriptor copy failed.')
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_empty_or_partial_draft_configuration_can_be_saved_and_reviewed_later(self):
        self.source.rubric_categories.all().delete()
        template = self.saved()
        response = self.create(template)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.new_session().can_go_live())
        self.assertFalse(self.new_session().rubric_categories.exists())

    def test_xss_in_names_descriptions_and_notice_remains_plain_text(self):
        template = self.saved()
        template.name = '<script>alert(1)</script>'
        template.description = '<img src=x onerror=alert(1)>'
        template.instructions = '<script>alert(2)</script>'
        template.save()
        for response in (self.client.get(self.list_url), self.client.get(self.new_url, {'template': f'saved:{template.pk}'})):
            self.assertContains(response, '&lt;script&gt;')
            self.assertNotContains(response, '<script>alert(')
            self.assertNotContains(response, '<img src=x')

    def test_save_and_delete_require_csrf_and_delete_get_is_refused(self):
        template = self.saved()
        client = Client(enforce_csrf_checks=True)
        sign_in(client, self.owner, self.org)
        self.assertEqual(client.post(self.save_url, {'name': 'No csrf'}).status_code, 403)
        delete = reverse('assessment_template_delete', args=[template.pk])
        self.assertEqual(client.post(delete).status_code, 403)
        self.assertEqual(self.client.get(delete).status_code, 405)
        self.assertEqual(self.client.post(self.list_url).status_code, 405)
        self.assertTrue(SessionTemplate.objects.filter(pk=template.pk).exists())

    def test_owner_navigation_exposes_save_and_library_actions(self):
        for route in ('assessment_session_detail', 'assessment_session_edit'):
            self.assertContains(self.client.get(reverse(route, args=[self.source.pk]), follow=True), f'data-action-url="{self.save_url}"')
        self.assertContains(self.client.get(reverse('assessment_session_list')), f'data-action-url="{self.save_url}"')
        self.assertContains(self.client.get(reverse('assessment_session_list')), f'href="{self.list_url}"')


class TemplateMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, name):
        target = [('assessments', name)]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_forward_and_reverse_keep_existing_session_ids_and_marks(self):
        old = self.migrate('0007_practice_entry')
        session = old.get_model('assessments', 'AssessmentSession').objects.create(organization_id=2, name='Existing')
        student = old.get_model('assessments', 'Student').objects.create(assessment_session=session, full_name='Student')
        group = old.get_model('assessments', 'PresentationGroup').objects.create(assessment_session=session, name='Group')
        category = old.get_model('assessments', 'RubricCategory').objects.create(assessment_session=session, name='Criterion', weight=100)
        turn = old.get_model('assessments', 'PresentationTurn').objects.create(assessment_session=session, group=group, status='closed')
        evaluation = old.get_model('assessments', 'Evaluation').objects.create(presentation_turn=turn, evaluator=student)
        score = old.get_model('assessments', 'EvaluationScore').objects.create(evaluation=evaluation, rubric_category=category, value=75, max_value=100)
        new = self.migrate('0008_session_templates')
        template = new.get_model('assessments', 'SessionTemplate').objects.create(organization_id=2, name='New template')
        new.get_model('assessments', 'TemplateRubricCategory').objects.create(template=template, name='Example', weight=100)
        self.assertEqual(new.get_model('assessments', 'AssessmentSession').objects.get(pk=session.pk).uuid, session.uuid)
        self.assertEqual(new.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
        before = self.migrate('0007_practice_entry')
        self.assertEqual(before.get_model('assessments', 'AssessmentSession').objects.get(pk=session.pk).uuid, session.uuid)
        self.assertEqual(before.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
