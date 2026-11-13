"""FA-03: owner duplicates setup into an isolated draft, never an assessment record."""
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.access import ORGS_KEY
from accounts.testing import make_org, make_user, sign_in
from assessments.duplication import duplicate_session
from assessments.models import (
    AssessmentSession, Evaluation, EvaluationScore, GroupMembership, ParticipationRecord,
    PresentationGroup, PresentationTurn, RubricCategory, SessionChange, Student,
)
from assessments.views import assessment_session_duplicate


class DuplicateTests(TestCase):
    def setUp(self):
        self.org = make_org('University', oversee=True)
        self.owner = make_user('owner')
        sign_in(self.client, self.owner, self.org)
        self.source = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Original',
            identify_by='student_id', status='closed', ungraded_penalty=Decimal('2.75'),
            instructions='Use the rubric carefully.', default_presentation_seconds=120, default_turn_seconds=45, transition_gap_seconds=8,
            voting_paused=True, joining_locked=True, closed_at=timezone.now())
        self.group = self.source.groups.create(name='Group A', topic='Research', location='Room 1', order=2, max_size=3)
        self.student = self.source.students.create(full_name='Student One', student_id='S1',
                                                   email='student@example.test', programme='Computing')
        self.member = GroupMembership.objects.create(group=self.group, student=self.student)
        self.category = self.source.rubric_categories.create(name='Technical', description='Evidence',
                                                             scope='group', weight=60, order=3)
        self.category.level_descriptors.create(score=30, description='Partly supported by evidence.')
        self.source.rubric_categories.create(name='Delivery', scope='individual', weight=40, order=1)
        self.turn = self.source.presentation_turns.create(group=self.group, status='closed')
        self.evaluation = self.turn.evaluations.create(evaluator=self.student)
        EvaluationScore.objects.create(evaluation=self.evaluation, rubric_category=self.category, value=35, max_value=60)
        ParticipationRecord.objects.create(assessment_session=self.source, student=self.student)
        SessionChange.objects.create(session=self.source, by_user=self.owner, summary='Original history')
        self.source.pending_next_group = self.group
        self.source.designated_next_group = self.group
        self.source.pending_transition_at = timezone.now()
        self.source.save()
        self.url = reverse('assessment_session_duplicate', args=[self.source.pk])

    def copy(self, **options):
        response = self.client.post(self.url, {'name': 'Next class', **options})
        self.assertEqual(response.status_code, 302)
        copy = AssessmentSession.objects.exclude(pk=self.source.pk).get()
        self.assertRedirects(response, reverse('assessment_groups', args=[copy.pk]))
        return copy

    def test_get_is_confirmation_only_and_shows_every_choice(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Create draft copy')
        self.assertContains(response, 'name="include_roster"')
        self.assertContains(response, 'name="include_groups"')
        self.assertContains(response, 'Original (copy)')
        self.assertContains(response, 'Creates a draft with this session’s setup.')
        self.assertFalse(response.context['form']['include_roster'].value())
        self.assertFalse(response.context['form']['include_groups'].value())
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_default_copy_has_settings_rubric_and_fresh_identity_only(self):
        copy = self.copy()
        self.assertEqual(copy.organization_id, self.source.organization_id)
        self.assertEqual(copy.service, 'classroom')
        self.assertEqual(copy.created_by, self.owner)
        self.assertNotEqual(copy.uuid, self.source.uuid)
        for field in ('identify_by', 'instructions', 'ungraded_penalty', 'default_presentation_seconds',
                      'default_turn_seconds', 'transition_gap_seconds'):
            self.assertEqual(getattr(copy, field), getattr(self.source, field))
        self.assertEqual(list(copy.rubric_categories.values_list('name', 'description', 'scope', 'weight', 'order')),
                         list(self.source.rubric_categories.values_list('name', 'description', 'scope', 'weight', 'order')))
        copied = copy.rubric_categories.get(name='Technical')
        self.assertEqual(list(copied.level_descriptors.values_list('score', 'description')),
                         [(Decimal('30.00'), 'Partly supported by evidence.')])
        copied.level_descriptors.update(description='Changed only in the copy')
        self.assertEqual(self.category.level_descriptors.get().description, 'Partly supported by evidence.')
        self.assertFalse(copy.students.exists())
        self.assertFalse(copy.groups.exists())
        self.assert_clean_draft(copy)

    def assert_clean_draft(self, copy):
        self.assertEqual(copy.status, 'draft')
        self.assertFalse(copy.voting_paused)
        self.assertFalse(copy.joining_locked)
        for field in ('closed_at', 'pending_next_group_id', 'pending_transition_at', 'designated_next_group_id'):
            self.assertIsNone(getattr(copy, field))
        self.assertFalse(copy.presentation_turns.exists())
        self.assertFalse(copy.participation_records.exists())
        self.assertFalse(copy.changes.exists())
        self.assertFalse(Evaluation.objects.filter(presentation_turn__assessment_session=copy).exists())
        self.assertFalse(EvaluationScore.objects.filter(rubric_category__assessment_session=copy).exists())

    def test_roster_and_groups_copy_memberships_to_new_rows(self):
        copy = self.copy(include_roster='on', include_groups='on')
        student, group = copy.students.get(), copy.groups.get()
        self.assertNotEqual(student.pk, self.student.pk)
        self.assertNotEqual(group.pk, self.group.pk)
        for field in ('full_name', 'student_id', 'email', 'programme'):
            self.assertEqual(getattr(student, field), getattr(self.student, field))
        for field in ('name', 'topic', 'location', 'order', 'max_size'):
            self.assertEqual(getattr(group, field), getattr(self.group, field))
        self.assertEqual(list(group.memberships.values_list('student_id', flat=True)), [student.pk])
        self.assert_clean_draft(copy)

    def test_roster_can_be_copied_without_groups(self):
        copy = self.copy(include_roster='on')
        self.assertEqual(copy.students.count(), 1)
        self.assertFalse(copy.groups.exists())
        self.assertFalse(GroupMembership.objects.filter(student__assessment_session=copy).exists())

    def test_groups_can_be_copied_without_personal_data(self):
        copy = self.copy(include_groups='on')
        self.assertEqual(copy.groups.count(), 1)
        self.assertFalse(copy.students.exists())
        self.assertFalse(copy.groups.get().memberships.exists())

    def test_source_and_results_remain_unchanged_and_copy_is_independent(self):
        before = AssessmentSession.objects.filter(pk=self.source.pk).values().get()
        copy = self.copy(include_roster='on', include_groups='on')
        self.assertEqual(AssessmentSession.objects.filter(pk=self.source.pk).values().get(), before)
        copy.rubric_categories.update(name='Changed')
        copy.students.update(full_name='Changed')
        copy.groups.update(name='Changed')
        self.category.refresh_from_db()
        self.student.refresh_from_db()
        self.group.refresh_from_db()
        self.assertEqual(self.category.name, 'Technical')
        self.assertEqual(self.student.full_name, 'Student One')
        self.assertEqual(self.group.name, 'Group A')
        self.assertEqual(self.evaluation.scores.get().value, Decimal('35'))
        self.assertEqual(self.source.changes.count(), 1)

    def test_draft_and_live_sources_can_be_copied(self):
        for status in ('draft', 'live'):
            self.source.status = status
            self.source.save()
            copy = self.copy()
            self.assert_clean_draft(copy)
            copy.delete()

    def test_invalid_name_does_not_create_partial_copy(self):
        for name in ('', ' ', 'x' * 151):
            response = self.client.post(self.url, {'name': name, 'include_roster': 'on'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_server_ignores_submitted_owner_scope_and_runtime_state(self):
        copy = self.copy(service='panel', organization_id='99999', created_by='99999', status='live',
                         voting_paused='on', uuid=str(self.source.uuid))
        self.assertEqual(copy.service, 'classroom')
        self.assertEqual(copy.organization_id, self.org.id)
        self.assertEqual(copy.created_by, self.owner)
        self.assert_clean_draft(copy)

    def test_error_in_copy_rolls_back_every_new_row(self):
        with patch('assessments.duplication.RubricCategory.objects.create',
                   side_effect=ValidationError('Copy could not be completed.')):
            response = self.client.post(self.url, {'name': 'Next class', 'include_roster': 'on'})
        self.assertContains(response, 'Copy could not be completed.')
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_foreign_membership_fails_without_copying_or_leaking_personal_data(self):
        other = AssessmentSession.objects.create(organization_id=99999, name='Other')
        outsider = other.students.create(full_name='PRIVATE OUTSIDER')
        GroupMembership.objects.create(group=self.group, student=outsider)
        response = self.client.post(self.url, {'name': 'Next', 'include_roster': 'on', 'include_groups': 'on'})
        self.assertContains(response, 'Correct its membership before duplicating.')
        self.assertNotContains(response, 'PRIVATE OUTSIDER')
        self.assertEqual(AssessmentSession.objects.count(), 2)
        self.assertEqual(Student.objects.count(), 2)

    def test_other_owner_even_overseer_cannot_copy_by_get_or_post(self):
        other = make_user('overseer')
        sign_in(self.client, other, self.org)
        for method in ('get', 'post'):
            response = getattr(self.client, method)(self.url, {'name': 'Stolen'})
            missing = getattr(self.client, method)(reverse('assessment_session_duplicate', args=[999999]), {'name': 'Stolen'})
            self.assertEqual(response.status_code, missing.status_code)
            self.assertEqual(response.status_code, 404)
        self.assertEqual(AssessmentSession.objects.count(), 1)
        page = self.client.get(reverse('assessment_session_list') + '?scope=all')
        self.assertNotContains(page, f'href="{self.url}"')

    def test_helper_refuses_an_oversight_queryset_without_ownership(self):
        with self.assertRaises(Http404):
            duplicate_session(AssessmentSession.objects.for_organization(self.org.as_claim()).for_service('classroom'),
                              self.source.pk, owner=make_user('other'), name='No')
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_missing_service_fails_closed(self):
        request = RequestFactory().post(self.url, {'name': 'No scope'})
        request.user = self.owner
        request.session = {ORGS_KEY: [self.org.as_claim()]}
        with self.assertRaises(Http404):
            assessment_session_duplicate(request, self.source.pk)
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_csrf_is_required_and_unsupported_methods_are_refused(self):
        client = Client(enforce_csrf_checks=True)
        sign_in(client, self.owner, self.org)
        self.assertEqual(client.post(self.url, {'name': 'No token'}).status_code, 403)
        self.assertEqual(self.client.delete(self.url).status_code, 405)
        self.assertEqual(AssessmentSession.objects.count(), 1)

    def test_owner_sees_copy_action_in_list_and_session(self):
        self.assertContains(self.client.get(reverse('assessment_session_list')), f'data-action-url="{self.url}"')
        self.assertContains(self.client.get(reverse('assessment_session_detail', args=[self.source.pk])), f'data-action-url="{self.url}"')

    def test_max_length_source_name_has_valid_default_copy_name(self):
        self.source.name = 'x' * 150
        self.source.save()
        form = self.client.get(self.url).context['form']
        self.assertEqual(len(form['name'].value()), 150)
