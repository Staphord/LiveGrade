"""Co-assessment pages at their edges: other owners, refused steps, odd input, and exports."""
import hashlib
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from service_domains import coassessment_operations as ops
from service_domains.models import (
    CoAssessmentCriterion, CoAssessmentExaminer, CoAssessmentGroup, CoAssessmentParticipant,
    CoAssessmentScorecard, CoAssessmentSession, CoAssessmentTurn)

Status = CoAssessmentSession.Status
CROSS_SERVICES = ['classroom', 'panel', 'coassessment']


@override_settings(LIVEGRADE_LIVE_SERVICES=CROSS_SERVICES)
class ViewFixture(TestCase):
    def setUp(self):
        self.org = make_org('Views org')
        self.owner = make_user('views-owner')
        self.intruder = make_user('views-intruder')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Defense', status=Status.ACTIVE)
        self.crit_group = self.criterion('Architecture', CoAssessmentCriterion.Scope.GROUP, 60, 1)
        self.crit_ind = self.criterion('Q and A', CoAssessmentCriterion.Scope.INDIVIDUAL, 40, 2)
        self.group = self.make_group('Aurora', 1)
        self.second = self.make_group('Borealis', 2)
        self.empty = self.make_group('Empty', 3)
        self.alice = self.person('Alice', 'S1', self.group)
        self.bob = self.person('Bob', 'S2', self.group)
        self.pat = self.person('Pat Peer', 'S3', self.second)
        self.token = 'examiner-token'
        self.examiner = CoAssessmentExaminer.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            display_name='Dr Cho', email='cho@example.org', invite_token=self.token,
            token_digest=hashlib.sha256(self.token.encode()).hexdigest())
        sign_in(self.client, self.owner, self.org)

    def criterion(self, name, scope, weight, order):
        return CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name=name, scope=scope, weight=Decimal(weight), order=order)

    def make_group(self, name, order):
        return CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, name=name, order=order)

    def person(self, name, student_id, group):
        return CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            group=group, full_name=name, student_id=student_id)

    def url(self, name, *extra):
        return reverse(name, args=[self.session.pk, *extra])

    def stage(self, group):
        return ops.activate_stage_turn(self.session, group, user=self.owner)

    def present_everyone(self):
        for group in (self.group, self.second, self.empty):
            self.stage(group)
        ops.conclude_stage_turn(self.session, user=self.owner)


class OtherOwnerTests(ViewFixture):
    def test_another_organizer_gets_not_found_everywhere(self):
        turn = self.stage(self.group)
        self.client.logout()
        sign_in(self.client, self.intruder, self.org)
        gets = [self.url('coassessment_results'), self.url('coassessment_export_csv'),
                self.url('coassessment_live_state')]
        posts = [self.url('coassessment_update_timers'), self.url('coassessment_sign_off'),
                 self.url('coassessment_release'), self.url('coassessment_moderate', turn.pk)]
        for path in gets:
            self.assertEqual(self.client.get(path).status_code, 404, path)
        for path in posts:
            self.assertEqual(self.client.post(path, {}).status_code, 404, path)


class TableTests(ViewFixture):
    def test_bulk_delete_without_a_selection_returns_to_the_filtered_table(self):
        response = self.client.post(reverse('coassessment_bulk_delete'),
                                    {'q': ' Def ', 'status': 'active', 'per_page': 'many'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('q=Def', response['Location'])
        self.assertIn('status=active', response['Location'])
        response = self.client.post(reverse('coassessment_bulk_delete'), {'per_page': '7', 'status': 'nope'})
        self.assertNotIn('status=', response['Location'])


class ConsoleLineupTests(ViewFixture):
    def test_teams_waiting_on_a_pending_turn_are_listed(self):
        CoAssessmentTurn.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session, group=self.second,
            status=CoAssessmentTurn.Status.PENDING)
        self.assertEqual(self.client.get(self.url('coassessment_live_console')).status_code, 200)


class SignOffTests(ViewFixture):
    def test_signing_off_early_is_refused_with_a_reason(self):
        response = self.client.post(self.url('coassessment_sign_off'), follow=True)
        self.assertContains(response, 'complete their presentation turns')

    def test_sign_off_and_release_report_success(self):
        with patch('service_domains.coassessment_views.sign_off_session') as sign_off:
            response = self.client.post(self.url('coassessment_sign_off'), follow=True)
        sign_off.assert_called_once()
        self.assertContains(response, 'signed off successfully')
        with patch('service_domains.coassessment_views.release_session_results') as release:
            response = self.client.post(self.url('coassessment_release'), follow=True)
        release.assert_called_once()
        self.assertContains(response, 'Results released')

    def test_releasing_before_sign_off_is_refused_with_a_reason(self):
        response = self.client.post(self.url('coassessment_release'), follow=True)
        self.assertContains(response, 'must be signed off')

    def test_a_refused_moderation_explains_why(self):
        turn = self.stage(self.group)
        response = self.client.post(self.url('coassessment_moderate', turn.pk),
                                    {'action': 'adjusted', 'final_score': '50', 'justification': ' '}, follow=True)
        self.assertContains(response, 'justification')


class ExportTests(ViewFixture):
    def test_export_lists_members_and_whole_teams(self):
        self.present_everyone()
        csv_text = self.client.get(self.url('coassessment_export_csv')).content.decode()
        self.assertIn('Alice', csv_text)
        self.assertIn('Entire Team', csv_text)


class PeerFlowTests(ViewFixture):
    def join(self, identifier):
        return self.client.post(reverse('coassessment_peer_join', kwargs={'code': self.session.public_join_code}),
                                {'identifier': identifier})

    def setUp(self):
        super().setUp()
        self.client.logout()
        self.peer = Client()

    def test_joining_by_name_and_failing_to_join(self):
        url = reverse('coassessment_peer_join', kwargs={'code': self.session.public_join_code})
        response = self.peer.post(url, {'identifier': 'pat peer'})
        self.assertRedirects(response, reverse('coassessment_peer_scorecard', args=[self.session.pk]))
        missing = Client().post(url, {'identifier': 'nobody at all'})
        self.assertContains(missing, 'was not found')

    def test_scorecard_and_submit_need_a_check_in(self):
        join = reverse('coassessment_peer_join', kwargs={'code': self.session.public_join_code})
        for response in (Client().get(reverse('coassessment_peer_scorecard', args=[self.session.pk])),
                         Client().post(reverse('coassessment_peer_submit', args=[self.session.pk]), {})):
            self.assertRedirects(response, join)

    def check_in(self, student_id='S3'):
        url = reverse('coassessment_peer_join', kwargs={'code': self.session.public_join_code})
        self.peer.post(url, {'identifier': student_id})

    def test_waiting_room_and_closed_submission(self):
        self.check_in()
        scorecard = reverse('coassessment_peer_scorecard', args=[self.session.pk])
        self.assertContains(self.peer.get(scorecard), 'Waiting')
        response = self.peer.post(reverse('coassessment_peer_submit', args=[self.session.pk]), {}, follow=True)
        self.assertContains(response, 'No presentation is currently active')

    def test_individual_scores_are_collected_and_missing_ones_are_named(self):
        self.stage(self.group)
        self.check_in()
        submit = reverse('coassessment_peer_submit', args=[self.session.pk])
        partial = self.peer.post(submit, {
            f'criterion_{self.crit_group.pk}': '80',
            f'individual_criterion_{self.alice.pk}_{self.crit_ind.pk}': '70'}, follow=True)
        self.assertContains(partial, 'Q and A for Bob')
        done = self.peer.post(submit, {
            f'criterion_{self.crit_group.pk}': '80',
            f'individual_criterion_{self.alice.pk}_{self.crit_ind.pk}': '70',
            f'criterion_{self.crit_ind.pk}_{self.bob.pk}': '65'}, follow=True)
        self.assertContains(done, 'has been submitted')

    def test_students_cannot_score_their_own_team(self):
        self.stage(self.group)
        self.check_in('S1')
        response = self.peer.post(reverse('coassessment_peer_submit', args=[self.session.pk]), {
            f'criterion_{self.crit_group.pk}': '80',
            f'individual_criterion_{self.alice.pk}_{self.crit_ind.pk}': '70',
            f'individual_criterion_{self.bob.pk}_{self.crit_ind.pk}': '70'}, follow=True)
        self.assertContains(response, 'prohibited')


class ExaminerPortalTests(ViewFixture):
    def setUp(self):
        super().setUp()
        self.client.logout()
        self.portal = reverse('coassessment_examiner_portal', kwargs={'token': self.token})

    def test_portal_with_nobody_on_stage(self):
        self.assertEqual(Client().get(self.portal).status_code, 200)

    def test_marks_are_recorded_then_shown_back(self):
        self.stage(self.group)
        response = Client().post(self.portal, {
            f'criterion_{self.crit_group.pk}': '85', f'criterion_{self.crit_ind.pk}': '',
            f'individual_criterion_{self.alice.pk}_{self.crit_ind.pk}': '70',
            f'criterion_{self.crit_ind.pk}_{self.bob.pk}': '60',
            f'individual_criterion_{self.bob.pk}_{self.crit_ind.pk}': '', 'qualitative_feedback': 'Strong'})
        self.assertRedirects(response, self.portal)
        shown = Client().get(self.portal)
        self.assertEqual(shown.context['already_submitted'], True)
        self.assertEqual(CoAssessmentScorecard.objects.filter(
            evaluator_tier=CoAssessmentScorecard.Tier.PANEL).count(), 1)

    def test_a_refused_submission_explains_why(self):
        self.stage(self.group)
        with patch('service_domains.coassessment_views.submit_examiner_scorecard',
                   side_effect=ValidationError('Scoring is closed')):
            response = Client().post(self.portal, {f'criterion_{self.crit_group.pk}': ''})
        self.assertContains(response, 'Scoring is closed')


class ServiceScopeTests(SimpleTestCase):
    def test_organizer_pages_do_not_exist_outside_their_service(self):
        from service_domains.coassessment_views import coassessment_dashboard
        request = RequestFactory().get('/')
        request.service = 'classroom'
        with self.assertRaises(Http404):
            coassessment_dashboard(request)
