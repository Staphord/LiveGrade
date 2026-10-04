"""Somebody DevPerf has given the oversee permission can see every lecturer's
session in the organization - and do very little with them.

What they may do: list them, open results and participation read-only, and hand a
session to another lecturer. What they may not: run, edit, delete or export. The
route-by-route proof that everything else stays private is in
``test_cross_org_access`` (``OverseerInTheSameOrganizationTests``); this is the
list, the read-only pages, the hand-over and the wording.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.directory import record_sign_in
from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, SessionTransfer


class OversightTestCase(TestCase):

    def setUp(self):
        self.org = make_org('Uni', oversee=True)          # as seen by the overseer
        self.plain = make_org('Uni')                       # the same organization, no oversight
        self.plain.pk = self.plain.id = self.org.pk
        self.olga = make_user('olga', first_name='Olga', last_name='Overseer')
        self.ann = make_user('ann', first_name='Ann', last_name='Owner')
        self.ben = make_user('ben', first_name='Ben', last_name='Other')
        for person in (self.olga, self.ann, self.ben):
            record_sign_in(person, [self.plain.as_claim()])
        self.mine = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Olga class', created_by=self.olga, status='closed')
        self.anns_live = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Ann live', created_by=self.ann, status='live')
        self.anns_closed = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Ann closed', created_by=self.ann, status='closed')
        self.bens_draft = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Ben draft', created_by=self.ben)
        self.unowned = AssessmentSession.objects.create(organization_id=self.org.pk, name='Orphan')
        self.elsewhere = AssessmentSession.objects.create(
            organization_id=self.org.pk + 1000, name='Other org', created_by=self.ann)

    def as_overseer(self):
        sign_in(self.client, self.olga, self.org)

    def listing(self, query=''):
        response = self.client.get(reverse('assessment_session_list') + query)
        return [s.name for s in response.context['sessions']], response


class SessionListTests(OversightTestCase):

    def test_the_default_list_is_still_only_their_own(self):
        self.as_overseer()
        names, _ = self.listing()
        self.assertEqual(names, ['Olga class'])

    def test_all_sessions_lists_every_lecturers_in_the_organization_but_no_other(self):
        self.as_overseer()
        names, _ = self.listing('?scope=all')
        self.assertEqual(sorted(names),
                         ['Ann closed', 'Ann live', 'Ben draft', 'Olga class', 'Orphan'])

    def test_each_card_names_its_owner(self):
        self.as_overseer()
        _, response = self.listing('?scope=all')
        for text in ('Yours', 'Ann Owner', 'Ben Other', 'No owner'):
            self.assertContains(response, text)

    def test_the_two_tabs_are_offered_and_the_right_one_is_active(self):
        self.as_overseer()
        _, mine = self.listing()
        self.assertContains(mine, 'All sessions in Uni')
        self.assertContains(mine, 'My sessions')
        _, everything = self.listing('?scope=all')
        self.assertContains(everything, 'page-tab active')

    def test_somebody_without_oversight_is_not_offered_it_and_asking_does_nothing(self):
        sign_in(self.client, self.ann, self.plain)
        names, response = self.listing('?scope=all')
        self.assertEqual(sorted(names), ['Ann closed', 'Ann live'])
        self.assertNotContains(response, 'All sessions in')

    def test_oversight_in_one_organization_does_not_carry_to_another(self):
        elsewhere = make_org('Elsewhere')
        sign_in(self.client, self.olga, self.org, elsewhere, active=elsewhere)
        names, _ = self.listing('?scope=all')
        self.assertEqual(names, [])

    def test_an_organization_nobody_has_made_a_session_in_says_so(self):
        quiet = make_org('Quiet', oversee=True)
        sign_in(self.client, self.olga, quiet)
        _, response = self.listing('?scope=all')
        self.assertContains(response, 'Nobody has created a session yet')

    def test_other_peoples_cards_offer_only_what_an_overseer_may_do(self):
        self.as_overseer()
        _, response = self.listing('?scope=all')
        live = reverse('assessment_participation', args=[self.anns_live.pk])
        closed = reverse('assessment_results', args=[self.anns_closed.pk])
        self.assertContains(response, f'href="{live}"')
        self.assertContains(response, f'href="{closed}"')
        for forbidden in (
                reverse('assessment_session_detail', args=[self.anns_live.pk]),
                reverse('assessment_session_edit', args=[self.bens_draft.pk]),
                reverse('assessment_session_delete', args=[self.anns_closed.pk])):
            self.assertNotContains(response, f'href="{forbidden}"')
            self.assertNotContains(response, f'action="{forbidden}"')
        self.assertContains(response, reverse('assessment_transfer', args=[self.anns_closed.pk]))

    def test_their_own_cards_keep_every_action(self):
        self.as_overseer()
        _, response = self.listing('?scope=all')
        self.assertContains(response, reverse('assessment_session_delete', args=[self.mine.pk]))
        self.assertContains(response, reverse('assessment_session_detail', args=[self.mine.pk]))


class ReadOnlyPagesTests(OversightTestCase):

    def test_results_and_participation_open_for_somebody_elses_session(self):
        self.as_overseer()
        for name in ('assessment_results', 'assessment_participation'):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=[self.anns_closed.pk])).status_code, 200)

    def test_their_refresh_endpoints_work_too(self):
        self.as_overseer()
        for name in ('assessment_results_refresh', 'assessment_participation_refresh'):
            with self.subTest(name):
                response = self.client.get(reverse(name, args=[self.anns_live.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], 'live')

    def test_the_live_control_tab_and_the_export_button_are_not_shown_on_somebody_elses(self):
        self.as_overseer()
        detail = reverse('assessment_session_detail', args=[self.anns_closed.pk])
        results = self.client.get(reverse('assessment_results', args=[self.anns_closed.pk]))
        self.assertNotContains(results, f'href="{detail}"')
        self.assertNotContains(results, reverse('assessment_results_export', args=[self.anns_closed.pk]))
        live_detail = reverse('assessment_session_detail', args=[self.anns_live.pk])
        participation = self.client.get(reverse('assessment_participation', args=[self.anns_live.pk]))
        self.assertNotContains(participation, f'href="{live_detail}"')

    def test_the_owner_still_sees_the_tab_and_the_export_button(self):
        sign_in(self.client, self.ann, self.plain)
        detail = reverse('assessment_session_detail', args=[self.anns_closed.pk])
        results = self.client.get(reverse('assessment_results', args=[self.anns_closed.pk]))
        self.assertContains(results, f'href="{detail}"')
        self.assertContains(results, reverse('assessment_results_export', args=[self.anns_closed.pk]))

    def test_an_overseer_on_their_own_session_sees_both_too(self):
        self.as_overseer()
        results = self.client.get(reverse('assessment_results', args=[self.mine.pk]))
        self.assertContains(results, f'href="{reverse("assessment_session_detail", args=[self.mine.pk])}"')
        self.assertContains(results, reverse('assessment_results_export', args=[self.mine.pk]))

    def test_an_ordinary_colleague_still_cannot_open_them(self):
        sign_in(self.client, self.ben, self.plain)
        for name in ('assessment_results', 'assessment_participation',
                     'assessment_results_refresh', 'assessment_participation_refresh'):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=[self.anns_closed.pk])).status_code, 404)

    def test_a_session_with_no_owner_can_be_read_by_an_overseer(self):
        self.as_overseer()
        self.assertEqual(self.client.get(reverse('assessment_results', args=[self.unowned.pk])).status_code, 200)

    def test_another_organizations_session_is_not_reachable_even_by_an_overseer(self):
        self.as_overseer()
        self.assertEqual(self.client.get(reverse('assessment_results', args=[self.elsewhere.pk])).status_code, 404)


class WhatOversightDoesNotAllowTests(OversightTestCase):

    def test_everything_else_about_somebody_elses_session_is_a_404(self):
        self.as_overseer()
        pk = self.anns_closed.pk
        for name in ('assessment_session_detail', 'assessment_session_edit', 'assessment_live_state',
                     'assessment_roster', 'assessment_rubric', 'assessment_groups',
                     'assessment_results_export', 'assessment_qr'):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=[pk])).status_code, 404)
        for name in ('assessment_session_delete', 'assessment_go_live', 'assessment_close_session',
                     'assessment_toggle_pause', 'assessment_close_turn'):
            with self.subTest(name):
                self.assertEqual(self.client.post(reverse(name, args=[pk])).status_code, 404)
        self.assertTrue(AssessmentSession.objects.filter(pk=pk).exists())
        self.anns_closed.refresh_from_db()
        self.assertEqual(self.anns_closed.status, 'closed')


class HandOverTests(OversightTestCase):

    def url(self, session):
        return reverse('assessment_transfer', args=[session.pk])

    def test_an_overseer_can_hand_somebody_elses_session_to_a_colleague(self):
        self.as_overseer()
        response = self.client.post(self.url(self.anns_closed), {'new_owner': self.ben.pk})
        self.assertRedirects(response, reverse('assessment_session_list') + '?scope=all')
        self.anns_closed.refresh_from_db()
        self.assertEqual(self.anns_closed.created_by, self.ben)
        log = SessionTransfer.objects.get()
        self.assertEqual((log.from_user, log.to_user, log.by_user), (self.ann, self.ben, self.olga))

    def test_the_previous_owner_loses_it(self):
        self.as_overseer()
        self.client.post(self.url(self.anns_closed), {'new_owner': self.ben.pk})
        sign_in(self.client, self.ann, self.plain)
        self.assertEqual(self.client.get(reverse('assessment_results', args=[self.anns_closed.pk])).status_code, 404)

    def test_an_overseer_can_take_a_session_for_themselves(self):
        self.as_overseer()
        self.client.post(self.url(self.anns_closed), {'new_owner': self.olga.pk})
        names, _ = self.listing()
        self.assertIn('Ann closed', names)

    def test_a_session_with_no_owner_can_be_given_an_owner(self):
        self.as_overseer()
        self.client.post(self.url(self.unowned), {'new_owner': self.ben.pk})
        self.unowned.refresh_from_db()
        self.assertEqual(self.unowned.created_by, self.ben)
        self.assertIsNone(SessionTransfer.objects.get().from_user)

    def test_the_page_tells_an_overseer_who_owns_it_now(self):
        self.as_overseer()
        page = self.client.get(self.url(self.anns_closed))
        self.assertContains(page, 'belongs to <strong>Ann Owner</strong>')
        self.assertNotContains(page, 'you will no longer see or control it')
        self.assertContains(page, 'href="' + reverse('assessment_session_list') + '?scope=all"')

    def test_an_unowned_session_is_described_as_belonging_to_nobody(self):
        self.as_overseer()
        self.assertContains(self.client.get(self.url(self.unowned)), 'belongs to <strong>nobody</strong>')

    def test_the_owners_own_page_keeps_its_warning_and_plain_back_link(self):
        sign_in(self.client, self.ann, self.plain)
        page = self.client.get(self.url(self.anns_closed))
        self.assertContains(page, 'you will no longer see or control it')
        self.assertNotContains(page, '?scope=all')

    def test_an_ordinary_colleague_cannot_hand_over_somebody_elses(self):
        sign_in(self.client, self.ben, self.plain)
        self.assertEqual(self.client.post(self.url(self.anns_closed), {'new_owner': self.ben.pk}).status_code, 404)
        self.anns_closed.refresh_from_db()
        self.assertEqual(self.anns_closed.created_by, self.ann)

    def test_an_overseer_of_another_organization_cannot(self):
        elsewhere = make_org('Elsewhere', oversee=True)
        sign_in(self.client, self.olga, elsewhere)
        self.assertEqual(self.client.post(self.url(self.anns_closed), {'new_owner': self.ben.pk}).status_code, 404)

    def test_the_recipient_must_still_be_a_known_colleague(self):
        stranger = make_user('stranger')
        self.as_overseer()
        response = self.client.post(self.url(self.anns_closed), {'new_owner': stranger.pk})
        self.assertContains(response, 'Choose one of the lecturers in the list.')
        self.anns_closed.refresh_from_db()
        self.assertEqual(self.anns_closed.created_by, self.ann)
