from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, EvaluationScore, PresentationTurn,
                                RubricCategory, SessionChange)


class ClassroomPublicLeaderboardTests(TestCase):
    def setUp(self):
        self.org = make_org('Public leaderboard organization')
        self.owner = make_user('leaderboard-owner')
        sign_in(self.client, self.owner, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner,
            service='classroom', name='Room session', status='closed')
        self.red_rater = self.session.students.create(full_name='Private rater Red')
        self.blue_rater = self.session.students.create(full_name='Private rater Blue')
        self.red = self.session.groups.create(name='Red presentation team', order=1)
        self.blue = self.session.groups.create(name='Blue presentation team', order=2)
        self.criterion = RubricCategory.objects.create(
            assessment_session=self.session, name='Quality', weight=100)
        self._score(self.red, self.blue_rater, '80')
        self._score(self.blue, self.red_rater, '90')
        self.public_page = reverse('assessment_join_screen', args=[self.session.uuid])
        self.public_state = reverse('assessment_join_screen_state', args=[self.session.uuid])
        self.owner_release = reverse('assessment_public_results', args=[self.session.pk])

    def _score(self, group, evaluator, value):
        turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=group, status='closed')
        evaluation = turn.evaluations.create(evaluator=evaluator)
        EvaluationScore.objects.create(
            evaluation=evaluation, rubric_category=self.criterion,
            value=Decimal(value), max_value=Decimal('100'))

    def test_unreleased_scores_are_absent_from_projector_and_state(self):
        page = self.client.get(self.public_page)
        state = self.client.get(self.public_state)
        self.assertEqual(page.status_code, 200)
        self.assertIn('no-store', page['Cache-Control'])
        self.assertContains(page, 'Red presentation team')
        self.assertNotContains(page, '>80.0<')
        self.assertTrue(state.json()['public_leaderboard_released'])
        self.assertFalse(state.json()['public_show_scores'])
        self.assertTrue(all('score' not in row for row in state.json()['public_leaderboard']))
        self.assertIn('no-store', state['Cache-Control'])

    def test_owner_release_publishes_only_sorted_aggregate_group_scores(self):
        response = self.client.post(self.owner_release, {'action': 'release'})
        self.assertRedirects(response, reverse('assessment_results', args=[self.session.pk]))
        state = self.client.get(self.public_state).json()
        self.assertTrue(state['public_leaderboard_released'])
        self.assertEqual(state['public_leaderboard'], [
            {'rank': 1, 'group_name': 'Blue presentation team', 'score': '90.0'},
            {'rank': 2, 'group_name': 'Red presentation team', 'score': '80.0'},
        ])
        self.assertNotIn('Private rater', str(state))
        page = self.client.get(self.public_page)
        self.assertContains(page, 'Presentation group leaderboard')
        self.assertContains(page, 'Blue presentation team')
        self.assertContains(page, 'Final group rankings.')
        self.assertEqual(SessionChange.objects.filter(session=self.session).count(), 1)
        change = SessionChange.objects.get(session=self.session)
        self.assertEqual(change.by_user, self.owner)
        self.assertIn('Released aggregate group scores', change.summary)

    def test_owner_can_revoke_publication_and_projector_hides_scores_on_refresh(self):
        self.client.post(self.owner_release, {'action': 'release'})
        response = self.client.post(self.owner_release, {'action': 'revoke'})
        self.assertEqual(response.status_code, 302)
        state_response = self.client.get(self.public_state)
        self.assertTrue(state_response.json()['public_leaderboard_released'])
        self.assertFalse(state_response.json()['public_show_scores'])
        self.assertTrue(all('score' not in row for row in state_response.json()['public_leaderboard']))
        self.assertIn('no-store', state_response['Cache-Control'])
        self.assertEqual(SessionChange.objects.filter(session=self.session).count(), 2)

    def test_duplicate_release_and_revoke_actions_are_idempotent(self):
        self.client.post(self.owner_release, {'action': 'release'})
        self.client.post(self.owner_release, {'action': 'release'})
        self.assertEqual(SessionChange.objects.filter(session=self.session).count(), 1)
        self.client.post(self.owner_release, {'action': 'revoke'})
        self.client.post(self.owner_release, {'action': 'revoke'})
        self.assertEqual(SessionChange.objects.filter(session=self.session).count(), 2)

    def test_equal_scores_share_a_rank(self):
        blue_evaluation = self.blue.turns.get().evaluations.get()
        EvaluationScore.objects.filter(evaluation=blue_evaluation).update(value=Decimal('80'))
        self.client.post(self.owner_release, {'action': 'release'})
        leaderboard = self.client.get(self.public_state).json()['public_leaderboard']
        self.assertEqual([row['rank'] for row in leaderboard], [1, 1])

    def test_published_state_with_incomplete_scores_fails_closed(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(
            public_results_released_at=timezone.now())
        EvaluationScore.objects.filter(evaluation__presentation_turn__group=self.blue).delete()
        state = self.client.get(self.public_state).json()
        self.assertTrue(state['public_leaderboard_released'])
        self.assertFalse(state['public_show_scores'])
        self.assertEqual([row['rank'] for row in state['public_leaderboard']], [1, None])

    def test_release_is_refused_until_session_is_closed_and_all_groups_are_scored(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        response = self.client.post(self.owner_release, {'action': 'release'})
        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertIsNone(self.session.public_results_released_at)
        self.assertFalse(SessionChange.objects.filter(session=self.session).exists())

        AssessmentSession.objects.filter(pk=self.session.pk).update(status='closed')
        EvaluationScore.objects.filter(evaluation__presentation_turn__group=self.blue).delete()
        response = self.client.post(self.owner_release, {'action': 'release'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.client.get(self.public_state).json()['public_show_scores'])
        self.assertFalse(self.session.changes.exists())

    def test_other_service_sessions_cannot_use_the_classroom_projector(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(service='competition')
        self.assertEqual(self.client.get(self.public_page).status_code, 404)
        self.assertEqual(self.client.get(self.public_state).status_code, 404)
        self.assertEqual(self.client.get(reverse(
            'assessment_join_screen_qr', args=[self.session.uuid])).status_code, 404)

    def test_another_owner_cannot_release_or_revoke_this_sessions_leaderboard(self):
        other_owner = make_user('other-leaderboard-owner')
        sign_in(self.client, other_owner, self.org)
        response = self.client.post(self.owner_release, {'action': 'release'})
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(AssessmentSession.objects.get(pk=self.session.pk).public_results_released_at)
        self.assertFalse(SessionChange.objects.filter(session=self.session).exists())

    def test_live_completed_groups_are_ranked_without_scores_or_identities(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        state = self.client.get(self.public_state).json()
        self.assertTrue(state['public_leaderboard_provisional'])
        self.assertEqual(state['public_leaderboard'], [
            {'rank': 1, 'group_name': self.blue.name}, {'rank': 2, 'group_name': self.red.name}])
        self.assertNotIn('Private rater', str(state))

    def test_active_group_is_unranked_until_turn_closes(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        turn = self.blue.turns.get()
        turn.status = 'active'
        turn.save(update_fields=['status'])
        state = self.client.get(self.public_state).json()
        self.assertEqual(state['public_leaderboard'], [
            {'rank': 1, 'group_name': self.red.name}, {'rank': None, 'group_name': self.blue.name}])
        turn.status = 'closed'
        turn.save(update_fields=['status'])
        self.assertEqual(self.client.get(self.public_state).json()['public_leaderboard'][0]['group_name'], self.blue.name)

    def test_live_toggle_is_idempotent_audited_and_broadcast_after_commit(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        url = reverse('assessment_leaderboard_scores', args=[self.session.pk])
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            result = self.client.post(url, {'action': 'show'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(result.json(), {'public_show_scores': True})
        self.assertEqual(len(callbacks), 1)
        state = self.client.get(self.public_state).json()
        self.assertEqual(state['public_leaderboard'][0]['score'], '90.0')
        self.client.post(url, {'action': 'show'})
        self.assertEqual(self.session.changes.count(), 1)
        self.client.post(url, {'action': 'hide'})
        state = self.client.get(self.public_state).json()
        self.assertFalse(state['public_show_scores'])
        self.assertTrue(all('score' not in row for row in state['public_leaderboard']))
        self.assertEqual(self.session.changes.count(), 2)
        self.assertEqual(self.client.post(url, {'action': 'invalid'}).status_code, 404)

    def test_live_control_contains_score_switch_and_polls_link(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        page = self.client.get(reverse('assessment_session_detail', args=[self.session.pk]))
        self.assertContains(page, 'Show public scores')
        self.assertContains(page, reverse('assessment_polls', args=[self.session.pk]))
        self.assertFalse(self.client.get(reverse('assessment_live_state', args=[self.session.pk])).json()['public_show_scores'])

    def test_practice_groups_are_excluded_and_missing_scores_are_not_zero(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        self.session.groups.create(name='Private practice', is_practice=True)
        empty = self.session.groups.create(name='Unscored group')
        state = self.client.get(self.public_state).json()
        self.assertNotIn('Private practice', str(state['public_leaderboard']))
        self.assertEqual(state['public_leaderboard'][-1], {'rank': None, 'group_name': empty.name})

    def test_draft_screen_has_no_standings_and_empty_live_board_stays_visible(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='draft')
        self.assertEqual(self.client.get(self.public_state).json()['public_leaderboard'], [])
        self.assertFalse(self.client.get(self.public_state).json()['public_leaderboard_released'])
        self.session.groups.all().delete()
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        state = self.client.get(self.public_state).json()
        self.assertTrue(state['public_leaderboard_released'])
        self.assertEqual(state['public_leaderboard'], [])

    def test_public_group_score_uses_actual_rubric_maximum(self):
        self.criterion.weight = 60
        self.criterion.save(update_fields=['weight'])
        self.client.post(reverse('assessment_leaderboard_scores', args=[self.session.pk]), {'action': 'show'})
        state = self.client.get(self.public_state).json()
        self.assertEqual(state['public_group_points_max'], '60.00')
        self.assertEqual(state['public_leaderboard'][0]['score'], '54.0')
        self.assertContains(self.client.get(self.public_page), 'Group points (out of 60.00)')
