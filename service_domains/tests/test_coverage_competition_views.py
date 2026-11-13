"""Competition arena routes at their edges: refused actions, odd input, and screens before start."""
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from service_domains.models import Competition, CompetitionLeaderboardTieDecision
from service_domains.operations import start_competition
from service_domains.tests import test_competition_live_control as live_control

AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'polls', 'team', 'showcase', 'competition'])
class ArenaCase(TestCase):
    setUp = live_control.CompetitionLiveControlTests.setUp

    def route(self, name):
        return reverse(name, kwargs={'competition_id': self.competition.pk})

    def start(self):
        start_competition(self.competition, organization=self.org.as_claim(), user=self.owner)

    def act(self, data, **extra):
        return self.client.post(self.route('competition_live_action'), data, **extra)


class TableAndSetupTests(ArenaCase):
    def test_bulk_delete_without_a_selection_keeps_the_page_settings(self):
        response = self.client.post(reverse('competition_bulk_delete'), {'per_page': 'lots'})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse('competition_bulk_delete'), {'per_page': '12345'})
        self.assertIn('per_page=', response['Location'])

    def test_saving_details_from_an_unknown_step_returns_to_details(self):
        response = self.client.post(self.route('competition_manage'), {
            'action': 'update_competition', 'name': 'Renamed', 'audience_weight': '10',
            'minimum_judges_per_entry': '1', 'minimum_audience_ballots_per_track': '1',
            'setup_step': 'nonsense'})
        self.assertRedirects(response, f"{self.route('competition_manage')}?step=details&saved=1",
                             fetch_redirect_response=False)


class ScreenTests(ArenaCase):
    def test_arena_screen_waits_for_the_competition_to_start(self):
        self.assertRedirects(self.client.get(self.route('competition_projector')),
                             self.route('competition_manage'), fetch_redirect_response=False)

    def test_qr_points_at_the_join_page_when_nothing_is_on_stage(self):
        self.competition.rounds.all().delete()
        response = self.client.get(self.route('competition_projector_qr'))
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_state_feed_reports_a_competition_that_is_not_live(self):
        response = self.client.get(self.route('competition_live_state'))
        self.assertFalse(response.json()['ok'])


class LiveActionTests(ArenaCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_nudging_a_judge_who_has_not_finished_confirms(self):
        response = self.act({'action': 'nudge_judge', 'judge_id': self.judge.pk})
        self.assertIn('Nudge sent to judge judge@arena.org.', [str(m) for m in get_messages(response.wsgi_request)])

    def test_nudging_an_unknown_judge_changes_nothing(self):
        response = self.act({'action': 'nudge_judge', 'judge_id': '999999'}, **AJAX)
        self.assertTrue(response.json()['ok'])

    def test_a_boundary_tie_is_recorded_with_its_reason(self):
        response = self.act({
            'action': 'resolve_tie', 'track_id': self.track.pk, 'tied_score': '85.50',
            'reason': 'Panel discussion', 'ordered_entry_ids': [self.entry1.pk, self.entry2.pk]})
        self.assertRedirects(response, self.route('competition_live_control'), fetch_redirect_response=False)
        self.assertEqual(CompetitionLeaderboardTieDecision.objects.count(), 1)
        ajax = self.act({'action': 'resolve_tie', 'track_id': self.track.pk, 'tied_score': '85.50',
                         'reason': '', 'ordered_entry_ids': []}, **AJAX)
        self.assertEqual(CompetitionLeaderboardTieDecision.objects.count(), 1)
        self.assertIn(ajax.status_code, (200, 400))

    def test_advancing_and_concluding_follow_the_request_style(self):
        with patch('service_domains.competition_views.create_next_competition_round'):
            plain = self.act({'action': 'advance_round', 'next_round_name': 'Semi'})
        self.assertRedirects(plain, self.route('competition_live_control'), fetch_redirect_response=False)
        with patch('service_domains.competition_views.create_next_competition_round'):
            self.assertTrue(self.act({'action': 'advance_round'}, **AJAX).json()['ok'])
        with patch('service_domains.competition_views.complete_competition'):
            redirected = self.act({'action': 'conclude_competition'})
            ajax = self.act({'action': 'conclude_competition'}, **AJAX)
        self.assertRedirects(redirected, self.route('competition_manage'), fetch_redirect_response=False)
        self.assertEqual(ajax.json()['status'], 'complete')

    def test_unknown_actions_are_refused(self):
        plain = self.act({'action': 'nonsense'})
        self.assertRedirects(plain, self.route('competition_live_control'), fetch_redirect_response=False)
        ajax = self.act({'action': 'nonsense'}, **AJAX)
        self.assertEqual(ajax.status_code, 400)

    def test_refusals_are_shown_on_the_page_or_returned_as_json(self):
        with patch('service_domains.competition_views.toggle_competition_audience_voting',
                   side_effect=ValidationError('Voting cannot change now')):
            plain = self.act({'action': 'toggle_audience_voting'}, follow=True)
            ajax = self.act({'action': 'toggle_audience_voting'}, **AJAX)
        self.assertContains(plain, 'Voting cannot change now')
        self.assertEqual(ajax.json()['error'], 'Voting cannot change now')
        with patch('service_domains.competition_views.toggle_competition_pitch_pause', side_effect=PermissionDenied):
            denied = self.act({'action': 'toggle_pitch_pause'}, **AJAX)
        self.assertEqual(denied.status_code, 400)

    def test_starting_twice_does_not_create_a_second_round(self):
        before = self.competition.rounds.count()
        response = self.act({'action': 'start_competition'}, **AJAX)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.competition.rounds.count(), before)
        self.assertEqual(Competition.objects.get(pk=self.competition.pk).status, Competition.Status.ACTIVE)
