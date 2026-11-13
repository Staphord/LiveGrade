"""Co-assessment live control: one tab list, status-aware console, public join screen."""
from decimal import Decimal
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    CoAssessmentCriterion, CoAssessmentGroup, CoAssessmentParticipant, CoAssessmentSession,
    CoAssessmentTurn)

Status = CoAssessmentSession.Status
SERVICES = ['classroom', 'panel', 'coassessment']


@override_settings(LIVEGRADE_LIVE_SERVICES=SERVICES)
class LiveWorkspaceCase(TestCase):
    def setUp(self):
        self.org = make_org('Live workspace org')
        self.owner = make_user('live-workspace-owner')
        self.session = CoAssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='Defense Day', status=Status.ACTIVE)
        CoAssessmentCriterion.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='All', weight=Decimal('100'), order=1)
        self.group = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Aurora', topic='Campus map', order=1)
        self.second = CoAssessmentGroup.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            name='Meridian', order=2)
        CoAssessmentParticipant.objects.create(
            organization_id=self.org.pk, created_by=self.owner, session=self.session,
            group=self.group, full_name='Ann Lee', student_id='A1')
        sign_in(self.client, self.owner, self.org)

    def url(self, name, *extra):
        return reverse(name, args=[self.session.pk, *extra])

    def set_status(self, status):
        self.session.status = status
        self.session.save()

    def public_url(self, suffix=''):
        return reverse('coassessment_public_screen', kwargs={'code': self.session.public_join_code}) + suffix


class TabsAndConsoleTests(LiveWorkspaceCase):
    def test_every_workspace_page_shares_one_tab_list(self):
        pages = {'live': self.url('coassessment_live_console'),
                 'results': self.url('coassessment_results'),
                 'setup': self.url('coassessment_setup_policy')}
        for active, path in pages.items():
            html = self.client.get(path).content.decode()
            self.assertEqual(html.count('aria-label="Co-assessment workspace"'), 1, active)
            self.assertEqual(html.count('aria-current="page"'), 1 + html.count('aria-current="page"') - 1, active)
            for label in ('Live control', 'Edit setup', 'Results &amp; moderation'):
                self.assertIn(label, html, active)

    def test_tabs_follow_the_session_status(self):
        self.set_status(Status.DRAFT)
        html = self.client.get(self.url('coassessment_live_console')).content.decode()
        self.assertNotIn('Results &amp; moderation', html)
        self.assertIn('Not live yet', html)
        self.assertIn(self.url('coassessment_setup_review'), html)
        self.set_status(Status.RELEASED)
        html = self.client.get(self.url('coassessment_live_console')).content.decode()
        self.assertNotIn('Edit setup', html)
        self.assertIn('Results are released', html)

    def test_closed_states_show_only_their_status(self):
        for status, text in ((Status.REVIEW, 'All teams have presented'), (Status.SIGNED_OFF, 'Results are signed off')):
            self.set_status(status)
            html = self.client.get(self.url('coassessment_live_console')).content.decode()
            self.assertIn(text, html)
            for gone in ('id="co-stage"', 'Lock turn and call next', 'id="coassessment-conn-badge"'):
                self.assertNotIn(gone, html)
            self.assertIn(self.public_url(), html)

    def test_live_console_has_the_public_screen_button_regions_and_socket(self):
        html = self.client.get(self.url('coassessment_live_console')).content.decode()
        self.assertEqual(html.count(f'href="{self.public_url()}"'), 1)
        self.assertIn('Open public screen', html)
        for region in ('stage', 'quorum', 'lineup'):
            self.assertIn(f'data-inline-region="{region}"', html)
        self.assertIn('/ws/coassessment/', html)
        self.assertIn(self.session.public_join_code, html)
        self.assertNotIn('Stage Projector', html)
        self.assertNotIn('—', html)

    def test_console_shows_the_team_on_stage_and_its_clock(self):
        self.client.post(self.url('coassessment_activate_turn', self.group.pk))
        page = self.client.get(self.url('coassessment_live_console'))
        self.assertContains(page, 'On the floor')
        self.assertContains(page, 'Ann Lee')
        self.assertContains(page, 'data-presentation="')
        self.client.post(self.url('coassessment_pause_turn'))
        self.assertContains(self.client.get(self.url('coassessment_live_console')), 'data-paused="1"')

    def test_stage_actions_are_refused_unless_the_session_is_live(self):
        self.set_status(Status.REVIEW)
        for name, extra in (('coassessment_activate_turn', (self.group.pk,)),
                            ('coassessment_advance_turn', ()), ('coassessment_pause_turn', ())):
            response = self.client.post(self.url(name, *extra))
            self.assertRedirects(response, self.url('coassessment_live_console'), fetch_redirect_response=False)
        self.assertFalse(CoAssessmentTurn.objects.filter(session=self.session).exists())

    def test_pause_and_resume_messages_and_idle_pause(self):
        self.client.post(self.url('coassessment_pause_turn'))
        self.client.post(self.url('coassessment_activate_turn', self.group.pk))
        paused = self.client.post(self.url('coassessment_pause_turn'), follow=True)
        self.assertContains(paused, 'Presentation paused.')
        resumed = self.client.post(self.url('coassessment_pause_turn'), follow=True)
        self.assertContains(resumed, 'Presentation resumed.')


class PublicScreenTests(LiveWorkspaceCase):
    def anonymous(self):
        return Client()

    def test_public_screen_needs_no_login_and_shows_the_join_qr_and_pin(self):
        html = self.anonymous().get(self.public_url()).content.decode()
        code = self.session.public_join_code
        self.assertNotIn(f'PIN', html)
        self.assertIn('student ID or full name', html)
        self.assertIn(reverse('coassessment_public_qr', kwargs={'code': code}), html)
        self.assertIn(reverse('coassessment_peer_join', kwargs={'code': code}), html)
        self.assertIn('/ws/coassessment/', html)
        self.assertIn('Defense Day', html)
        self.assertEqual(html.count(reverse('coassessment_public_qr', kwargs={'code': code})), 1)

    def test_lowercase_code_works_and_unknown_codes_are_not_found(self):
        lower = reverse('coassessment_public_screen', kwargs={'code': self.session.public_join_code.lower()})
        self.assertEqual(self.anonymous().get(lower).status_code, 200)
        for name in ('coassessment_public_screen', 'coassessment_public_state', 'coassessment_public_qr'):
            self.assertEqual(self.anonymous().get(reverse(name, kwargs={'code': 'NOPE0000'})).status_code, 404)

    def test_qr_is_a_png_of_the_join_link(self):
        response = self.anonymous().get(self.public_url('qr.png'))
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertTrue(response.content.startswith(b'\x89PNG'))
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_public_state_exposes_only_the_stage_and_clock(self):
        self.client.post(self.url('coassessment_activate_turn', self.group.pk))
        data = self.anonymous().get(self.public_url('state/')).json()
        self.assertEqual(data['active_group_name'], 'Aurora')
        self.assertTrue(data['has_active_turn'])
        for private in ('panel_count', 'peer_count', 'panel_total', 'session_id', 'active_group_id'):
            self.assertNotIn(private, data)

    def test_screen_shows_the_team_on_stage_and_released_standings(self):
        self.client.post(self.url('coassessment_activate_turn', self.group.pk))
        html = self.anonymous().get(self.public_url()).content.decode()
        self.assertIn('On stage now', html)
        self.assertIn('Campus map', html)
        self.set_status(Status.RELEASED)
        html = self.anonymous().get(self.public_url()).content.decode()
        self.assertIn('Results released', html)

    def test_owner_shortcut_requires_the_owner_and_redirects_to_the_public_screen(self):
        owner_route = self.url('coassessment_stage_display')
        self.assertRedirects(self.client.get(owner_route), self.public_url(), fetch_redirect_response=False)
        stranger = make_user('live-stranger')
        self.client.logout()
        sign_in(self.client, stranger, self.org)
        self.assertEqual(self.client.get(owner_route).status_code, 404)


class StageActionTests(LiveWorkspaceCase):
    def test_calling_a_team_that_already_presented_is_refused_with_a_reason(self):
        self.client.post(self.url('coassessment_activate_turn', self.group.pk))
        self.client.post(self.url('coassessment_activate_turn', self.second.pk))
        response = self.client.post(self.url('coassessment_activate_turn', self.group.pk), follow=True)
        self.assertContains(response, 'already presented')

    def test_locking_the_last_turn_moves_the_session_to_review_and_the_results(self):
        self.client.post(self.url('coassessment_advance_turn'))
        self.client.post(self.url('coassessment_advance_turn'))
        response = self.client.post(self.url('coassessment_advance_turn'))
        self.assertRedirects(response, self.url('coassessment_results'), fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, Status.REVIEW)

    def test_other_owners_and_unknown_sessions_cannot_drive_the_stage(self):
        stranger = make_user('stage-stranger')
        self.client.logout()
        sign_in(self.client, stranger, self.org)
        for name, extra in (('coassessment_activate_turn', (self.group.pk,)),
                            ('coassessment_advance_turn', ()), ('coassessment_pause_turn', ())):
            self.assertEqual(self.client.post(self.url(name, *extra)).status_code, 404, name)
