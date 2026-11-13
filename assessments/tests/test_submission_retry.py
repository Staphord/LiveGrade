"""PE-02 / NF-012: an accepted atomic submission has one stable retry receipt."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.models import (AssessmentSession, Evaluation, EvaluationScore,
                                Evaluator, GroupMembership, PresentationTurn)
from assessments.services import submit_evaluation
from assessments.tests.participant_flow import join_as


class RetryFixture:
    def setUp(self):
        self.org = make_org('University')
        self.owner = make_user('owner')
        self.session = AssessmentSession.objects.create(name='Class', organization_id=self.org.id,
                                                        created_by=self.owner, status='live')
        self.presenter = self.session.students.create(full_name='Presenter')
        self.evaluator = self.session.students.create(full_name='Evaluator')
        self.other = self.session.students.create(full_name='Other evaluator')
        self.group = self.session.groups.create(name='Group')
        GroupMembership.objects.create(group=self.group, student=self.presenter)
        self.group_cat = self.session.rubric_categories.create(name='Group', weight=60)
        self.ind_cat = self.session.rubric_categories.create(name='Individual', scope='individual', weight=40)
        self.turn = self.session.presentation_turns.create(group=self.group, status='active')
        self.key = uuid.uuid4()
        self.url = reverse('student_evaluate', args=[self.session.uuid])
        join_as(self.client, self.session.uuid, 'Evaluator')

    def submit(self, **kwargs):
        return submit_evaluation(self.turn, self.evaluator, {self.group_cat.pk: Decimal('30')},
                                 {self.presenter.pk: {self.ind_cat.pk: Decimal('20')}}, key=self.key, **kwargs)

    def post(self, **kwargs):
        data = {'turn_id': self.turn.pk, 'submission_key': str(self.key),
                f'group_{self.group_cat.pk}': '30', f'ind_{self.presenter.pk}_{self.ind_cat.pk}': '20'}
        data.update(kwargs)
        return self.client.post(self.url, data, HTTP_ACCEPT='application/json')


class SubmissionRetryTests(RetryFixture, TestCase):
    def test_non_roster_identity_cannot_receive_a_classroom_submission(self):
        open_identity = Evaluator.objects.create(
            assessment_session=self.session, kind=Evaluator.Kind.OPEN)
        with patch('assessments.services.Evaluator.objects.get_or_create',
                   return_value=(open_identity, False)):
            with self.assertRaisesMessage(ValidationError, 'roster evaluator identity is unavailable'):
                self.submit()
        self.assertFalse(Evaluation.objects.exists())

    def test_exact_retry_returns_same_receipt_without_new_rows(self):
        first = self.submit()
        self.assertFalse(first.was_replayed)
        self.assertEqual(first.submission_key, self.key)
        self.assertEqual(len(first.submission_digest), 64)
        self.assertEqual(self.submit().pk, first.pk)
        self.assertTrue(self.submit().was_replayed)
        self.assertEqual(Evaluation.objects.count(), 2)
        self.assertEqual(EvaluationScore.objects.count(), 2)
        self.assertIsNone(Evaluation.objects.get(target_student=self.presenter).submission_key)
        identities = Evaluator.objects.filter(assessment_session=self.session,
            student=self.evaluator, kind=Evaluator.Kind.ROSTER)
        self.assertEqual(identities.count(), 1)
        self.assertEqual(set(Evaluation.objects.values_list('evaluator_id', flat=True)),
                         {self.evaluator.pk})
        self.assertEqual(set(Evaluation.objects.values_list('evaluator_record_id', flat=True)),
                         set(identities.values_list('pk', flat=True)))
        self.assertTrue(all(row.evaluator_record.student_id == row.evaluator_id
                            for row in Evaluation.objects.select_related('evaluator_record')))
        legacy_score_rows = list(EvaluationScore.objects.filter(
            evaluation__evaluator=self.evaluator).order_by('rubric_category_id').values_list(
                'rubric_category_id', 'value'))
        expanded_score_rows = list(EvaluationScore.objects.filter(
            evaluation__evaluator_record=identities.get()).order_by(
                'rubric_category_id').values_list('rubric_category_id', 'value'))
        self.assertEqual(expanded_score_rows, legacy_score_rows)

    def test_numerically_equal_scores_have_the_same_retry_digest(self):
        first = self.submit()
        replay = submit_evaluation(self.turn, self.evaluator, {self.group_cat.pk: Decimal('30.00')},
                                   {self.presenter.pk: {self.ind_cat.pk: Decimal('20.0')}}, key=str(self.key))
        self.assertEqual(replay.pk, first.pk)
        self.assertTrue(replay.was_replayed)

    def test_changed_score_with_same_key_is_refused_and_original_is_unchanged(self):
        self.submit()
        with self.assertRaisesMessage(ValidationError, 'does not match'):
            submit_evaluation(self.turn, self.evaluator, {self.group_cat.pk: Decimal('31')},
                               {self.presenter.pk: {self.ind_cat.pk: Decimal('20')}}, key=self.key)
        self.assertEqual(EvaluationScore.objects.get(rubric_category=self.group_cat).value, 30)
        self.assertEqual(Evaluation.objects.count(), 2)

    def test_key_is_not_reusable_by_another_participant_or_turn(self):
        self.submit()
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.other, {self.group_cat.pk: Decimal('30')},
                               {self.presenter.pk: {self.ind_cat.pk: Decimal('20')}}, key=self.key)
        other_turn = self.session.presentation_turns.create(group=self.group, status='active')
        with self.assertRaises(ValidationError):
            submit_evaluation(other_turn, self.evaluator, {self.group_cat.pk: Decimal('30')},
                               {self.presenter.pk: {self.ind_cat.pk: Decimal('20')}}, key=self.key)
        self.assertEqual(Evaluation.objects.count(), 2)

    def test_foreign_session_evaluator_or_presenter_is_rejected(self):
        other = AssessmentSession.objects.create(name='Private', organization_id=999)
        outsider = other.students.create(full_name='PRIVATE PERSON')
        for evaluator, target in ((outsider, self.presenter), (self.evaluator, outsider)):
            with self.assertRaises(ValidationError):
                submit_evaluation(self.turn, evaluator, {self.group_cat.pk: Decimal('30')},
                                   {target.pk: {self.ind_cat.pk: Decimal('20')}}, key=uuid.uuid4())
        self.assertFalse(Evaluation.objects.exists())

    def test_a_new_key_cannot_overwrite_a_previous_submission(self):
        self.submit()
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.evaluator, {self.group_cat.pk: Decimal('30')}, {}, key=uuid.uuid4())
        self.assertEqual(Evaluation.objects.count(), 2)

    def test_retry_after_expiry_closure_pause_and_changed_weight_confirms_original(self):
        receipt = self.submit()
        self.turn.status = 'closed'
        self.turn.voting_ends_at = timezone.now() - timezone.timedelta(seconds=1)
        self.turn.save()
        self.session.status = 'closed'
        self.session.voting_paused = True
        self.session.save()
        self.group_cat.weight = 1
        self.group_cat.save()
        replay = self.submit()
        self.assertEqual(replay.pk, receipt.pk)
        self.assertTrue(replay.was_replayed)
        self.assertEqual(EvaluationScore.objects.get(rubric_category=self.group_cat).max_value, 60)

    def test_stale_turn_instance_cannot_submit_after_database_closes_or_expires_it(self):
        for update in ({'status': 'closed'}, {'voting_ends_at': timezone.now() - timezone.timedelta(seconds=1)}):
            PresentationTurn.objects.filter(pk=self.turn.pk).update(status='active', voting_ends_at=None)
            PresentationTurn.objects.filter(pk=self.turn.pk).update(**update)
            with self.assertRaises(ValidationError):
                self.submit()
            self.assertFalse(Evaluation.objects.exists())

    def test_partial_invalid_submission_rolls_back_key_and_all_scores(self):
        with self.assertRaises(ValidationError):
            submit_evaluation(self.turn, self.evaluator, {self.group_cat.pk: Decimal('30')},
                               {self.presenter.pk: {self.ind_cat.pk: Decimal('41')}}, key=self.key)
        self.assertFalse(Evaluation.objects.exists())
        self.assertFalse(EvaluationScore.objects.exists())
        self.assertFalse(self.submit().was_replayed)

    def test_bad_keys_and_non_finite_or_extreme_scores_are_refused(self):
        for key in ('bad', 'https://evil.example/', 'x' * 1000):
            with self.assertRaises(ValidationError):
                submit_evaluation(self.turn, self.evaluator, {}, {}, key=key)
        for value in ('NaN', 'Infinity', '-Infinity', '1e99999999', '1e-99999999', '0.001'):
            with self.assertRaises(ValidationError):
                submit_evaluation(self.turn, self.evaluator, {self.group_cat.pk: Decimal(value)}, {}, key=self.key)
        self.assertFalse(Evaluation.objects.exists())

    def test_database_refuses_duplicate_null_target_even_without_submission_service(self):
        Evaluation.objects.create(presentation_turn=self.turn, evaluator=self.evaluator)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Evaluation.objects.create(presentation_turn=self.turn, evaluator=self.evaluator)
        self.assertEqual(Evaluation.objects.count(), 1)

    def test_database_refuses_reused_key_on_another_group_submission(self):
        self.submit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Evaluation.objects.create(presentation_turn=self.turn, evaluator=self.other, submission_key=self.key)
        self.assertEqual(Evaluation.objects.count(), 2)

    def test_json_retry_confirms_once_and_broadcasts_once(self):
        with patch('assessments.views.student_views.broadcast_session_event') as broadcast:
            first = self.post()
            self.assertEqual(first.status_code, 201)
            self.assertFalse(first.json()['replayed'])
            replay = self.post()
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json()['replayed'])
            self.assertEqual(broadcast.call_count, 1)
        self.assertEqual(replay.json()['next_url'], reverse('student_submitted', args=[self.session.uuid]))
        self.assertEqual(set(replay.json()), {'status', 'replayed', 'next_url'})
        self.assertIn('no-store', replay['Cache-Control'])
        self.assertEqual(EvaluationScore.objects.count(), 2)

    def test_json_accepted_retry_still_confirms_when_session_is_closed(self):
        self.assertEqual(self.post().status_code, 201)
        self.turn.status = 'closed'
        self.turn.save()
        self.session.status = 'closed'
        self.session.save()
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().json()['status'], 'accepted')

    def test_json_fresh_expired_paused_and_self_submissions_are_rejected(self):
        self.session.voting_paused = True
        self.session.save()
        self.assertEqual(self.post().status_code, 409)
        self.session.voting_paused = False
        self.session.save()
        self.turn.voting_ends_at = timezone.now() - timezone.timedelta(seconds=1)
        self.turn.save()
        self.assertEqual(self.post().status_code, 410)
        self.assertFalse(Evaluation.objects.exists())

    def test_presenting_phase_reports_not_yet_open_without_storing_scores(self):
        self.turn.presentation_ends_at = timezone.now() + timezone.timedelta(minutes=10)
        self.turn.save()
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'not_open')
        self.assertIn('not opened yet', response.json()['message'])
        self.assertFalse(Evaluation.objects.exists())

    def test_json_does_not_reassign_retry_to_current_turn_or_accept_missing_key(self):
        for overrides in ({'turn_id': ''}, {'turn_id': 'invalid'}, {'turn_id': '999999'}, {'submission_key': ''}):
            response = self.post(**overrides)
            self.assertIn(response.status_code, (400, 410))
            self.assertFalse(Evaluation.objects.exists())

    def test_changed_json_retry_returns_conflict_without_revealing_scores(self):
        self.post()
        response = self.post(**{f'group_{self.group_cat.pk}': '31'})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('scores', response.json())
        self.assertEqual(EvaluationScore.objects.get(rubric_category=self.group_cat).value, 30)

    def test_state_refresh_is_authoritative_private_and_not_cached(self):
        response = self.client.get(self.url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['turn_id'], self.turn.pk)
        self.assertTrue(data['voting_open'])
        self.assertFalse(data['already_submitted'])
        self.assertIn('csrf_token', data)
        self.assertNotIn('scores', data)
        self.assertIn('no-store', response['Cache-Control'])
        self.post()
        self.assertTrue(self.client.get(self.url, HTTP_ACCEPT='application/json').json()['already_submitted'])

    def test_missing_participant_cookie_requires_rejoining_not_owner_signin(self):
        response = Client().get(self.url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'join_required')
        self.assertEqual(response.json()['next_url'], reverse('student_join', args=[self.session.uuid]))

    def test_temporary_database_failure_returns_retryable_response(self):
        with patch('assessments.views.student_views.submit_evaluation', side_effect=OperationalError('private database detail')):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'retry')
        self.assertNotContains(response, 'private database detail', status_code=503)
        self.assertFalse(Evaluation.objects.exists())

    def test_plain_form_shows_safe_retry_message_for_database_failure(self):
        data = {'turn_id': self.turn.pk, f'group_{self.group_cat.pk}': '30',
                f'ind_{self.presenter.pk}_{self.ind_cat.pk}': '20'}
        with patch('assessments.views.student_views.submit_evaluation',
                   side_effect=OperationalError('private database detail')):
            response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The connection could not be confirmed. Keep your scores and retry.')
        self.assertNotContains(response, 'private database detail')

    def test_html_form_supplies_fallback_key_and_plain_post_remains_supported(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="submission_key"')
        self.assertTrue(uuid.UUID(response.context['submission_key']))
        data = {'turn_id': self.turn.pk, 'submission_key': str(self.key),
                f'group_{self.group_cat.pk}': '30', f'ind_{self.presenter.pk}_{self.ind_cat.pk}': '20'}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(EvaluationScore.objects.count(), 2)


class RetryMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self, name):
        targets = [('assessments', name)]
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def old_fixture(self):
        apps = self.migrate('0008_session_templates')
        session = apps.get_model('assessments', 'AssessmentSession').objects.create(organization_id=1, name='Existing')
        student = apps.get_model('assessments', 'Student').objects.create(assessment_session=session, full_name='Existing student')
        group = apps.get_model('assessments', 'PresentationGroup').objects.create(assessment_session=session, name='Group')
        category = apps.get_model('assessments', 'RubricCategory').objects.create(assessment_session=session, name='Criterion', weight=100)
        turn = apps.get_model('assessments', 'PresentationTurn').objects.create(assessment_session=session, group=group)
        evaluation = apps.get_model('assessments', 'Evaluation').objects.create(presentation_turn=turn, evaluator=student)
        score = apps.get_model('assessments', 'EvaluationScore').objects.create(evaluation=evaluation, rubric_category=category, value=75, max_value=100)
        return apps, evaluation, score

    def test_forward_and_reverse_keep_legacy_marks_and_receipt_ids(self):
        _, old, score = self.old_fixture()
        new = self.migrate('0009_safe_submission_retry')
        self.assertIsNone(new.get_model('assessments', 'Evaluation').objects.get(pk=old.pk).submission_key)
        self.assertEqual(new.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
        before = self.migrate('0008_session_templates')
        self.assertEqual(before.get_model('assessments', 'Evaluation').objects.get(pk=old.pk).pk, old.pk)
        self.assertEqual(before.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)

    def test_duplicate_legacy_rows_stop_migration_without_deleting_grades(self):
        apps, old, score = self.old_fixture()
        Model = apps.get_model('assessments', 'Evaluation')
        duplicate = Model.objects.create(presentation_turn_id=old.presentation_turn_id, evaluator_id=old.evaluator_id)
        try:
            with self.assertRaisesMessage(RuntimeError, 'Duplicate legacy group submissions exist'):
                self.migrate('0009_safe_submission_retry')
            self.assertEqual(Model.objects.count(), 2)
            self.assertEqual(apps.get_model('assessments', 'EvaluationScore').objects.get(pk=score.pk).value, 75)
        finally:
            Model.objects.filter(pk=duplicate.pk).delete()  # Only this synthetic fixture; migration never deletes.


@skipUnless(connection.vendor == 'postgresql', 'Real row-lock/concurrency evidence requires PostgreSQL')
class PostgreSQLRetryRaceTests(RetryFixture, TransactionTestCase):
    def test_two_simultaneous_identical_submissions_commit_one_batch(self):
        gate = Barrier(2)
        def worker():
            close_old_connections()
            try:
                gate.wait(timeout=10)
                receipt = self.submit()
                return receipt.pk, receipt.was_replayed
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(len({pk for pk, _ in outcomes}), 1)
        self.assertEqual(sorted(replayed for _, replayed in outcomes), [False, True])
        self.assertEqual(Evaluation.objects.count(), 2)
        self.assertEqual(EvaluationScore.objects.count(), 2)

    def test_simultaneous_different_participants_reusing_one_key_cannot_claim_each_others_receipt(self):
        gate = Barrier(2)
        def worker(evaluator):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                try:
                    receipt = submit_evaluation(self.turn, evaluator, {self.group_cat.pk: Decimal('30')},
                        {self.presenter.pk: {self.ind_cat.pk: Decimal('20')}}, key=self.key)
                    return 'accepted', receipt.evaluator_id
                except ValidationError as error:
                    return error.code, evaluator.pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, [self.evaluator, self.other]))
        self.assertEqual(sorted(status for status, _ in outcomes), ['accepted', 'conflict'])
        winner = next(evaluator for status, evaluator in outcomes if status == 'accepted')
        self.assertEqual(Evaluation.objects.get(target_student__isnull=True).evaluator_id, winner)
        self.assertEqual(Evaluation.objects.count(), 2)
        self.assertEqual(EvaluationScore.objects.count(), 2)
