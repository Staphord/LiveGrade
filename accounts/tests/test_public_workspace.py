"""The shared workspace self-registered lecturers land in.

It is not an organization to them: it is not shown, nobody is a colleague of
anybody in it, sessions cannot be handed on, and nobody oversees it. And a visitor
who has no account sees a landing page with both ways in.
"""
from urllib.parse import parse_qs, urlsplit

from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.access import ORGS_KEY, can_oversee, in_public_workspace, usable_orgs
from accounts.directory import colleagues, record_sign_in
from accounts.models import OrganizationLecturer
from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession


def claim(**over):
    return {'id': 1, 'slug': 'lg', 'name': 'LiveGrade', 'can_run_assessments': True,
            'can_oversee_assessments': False, 'is_public_workspace': True, **over}


class PublicFlagTests(TestCase):

    def test_the_flag_is_read_only_when_it_is_a_literal_true(self):
        self.assertTrue(usable_orgs([claim()])[0]['public'])
        for value in (False, None, 'yes', 1, 'true'):
            with self.subTest(value=value):
                self.assertFalse(usable_orgs([claim(is_public_workspace=value)])[0]['public'])
        self.assertFalse(usable_orgs([{k: v for k, v in claim().items() if k != 'is_public_workspace'}])[0]['public'])

    def test_nobody_oversees_the_shared_workspace_whatever_is_claimed(self):
        entry = usable_orgs([claim(can_oversee_assessments=True)])[0]
        self.assertFalse(entry['can_oversee'])

    def test_an_ordinary_organization_keeps_oversight_when_granted(self):
        entry = usable_orgs([claim(is_public_workspace=False, can_oversee_assessments=True)])[0]
        self.assertTrue(entry['can_oversee'])

    def test_the_helpers_follow_the_active_organization(self):
        request = RequestFactory().get('/')
        request.session = {ORGS_KEY: usable_orgs([claim()])}
        self.assertTrue(in_public_workspace(request))
        self.assertFalse(can_oversee(request))
        request.session = {ORGS_KEY: usable_orgs([claim(is_public_workspace=False)])}
        self.assertFalse(in_public_workspace(request))
        request.session = {}
        self.assertFalse(in_public_workspace(request))


class NoDirectoryOfStrangersTests(TestCase):

    def test_signing_in_to_the_shared_workspace_records_nobody(self):
        ann = make_user('ann')
        record_sign_in(ann, usable_orgs([claim()]))
        self.assertEqual(OrganizationLecturer.objects.count(), 0)
        self.assertEqual(list(colleagues(1)), [])

    def test_a_real_organization_next_to_it_is_still_recorded_and_the_shared_one_is_not(self):
        ann = make_user('ann')
        record_sign_in(ann, usable_orgs([claim(), claim(id=2, name='Uni', is_public_workspace=False)]))
        self.assertEqual(list(OrganizationLecturer.objects.values_list('organization_id', flat=True)), [2])


class WhatTheyseeTests(TestCase):

    def setUp(self):
        self.shared = make_org('LiveGrade')
        self.real = make_org('Uni')
        self.ann = make_user('ann', first_name='Ann', last_name='Owner')

    def login(self, *orgs, public=(), **kw):
        user = self.ann
        self.client.force_login(user)
        session = self.client.session
        session[ORGS_KEY] = [{**o.as_claim(), 'public': o in public} for o in orgs]
        session.save()

    def page(self):
        return self.client.get(reverse('assessment_session_list'))

    def test_alone_in_the_shared_workspace_no_organization_is_shown_at_all(self):
        self.login(self.shared, public=(self.shared,))
        response = self.page()
        self.assertNotContains(response, 'u-label">Organization')
        self.assertNotContains(response, 'name="organization"')
        self.assertNotContains(response, '>LiveGrade</div>')
        self.assertContains(response, 'Ann Owner')

    def test_next_to_a_real_organization_the_shared_one_is_called_personal(self):
        self.login(self.real, self.shared, public=(self.shared,))
        response = self.page()
        self.assertContains(response, 'name="organization"')
        self.assertContains(response, '>Personal</option>')
        self.assertContains(response, '>Uni</option>')
        self.assertNotContains(response, '>LiveGrade</option>')

    def test_a_real_organization_alone_is_still_named(self):
        self.login(self.real)
        self.assertContains(self.page(), 'u-label">Organization')
        self.assertContains(self.page(), '>Uni</div>')

    def test_there_is_no_all_sessions_tab_even_if_it_is_asked_for(self):
        self.client.force_login(self.ann)
        session = self.client.session
        session[ORGS_KEY] = [{**self.shared.as_claim(), 'public': True, 'can_oversee': True}]
        session.save()
        mine = AssessmentSession.objects.create(organization_id=self.shared.pk, name='Mine', created_by=self.ann)
        other = make_user('stranger')
        AssessmentSession.objects.create(organization_id=self.shared.pk, name='Theirs', created_by=other)
        response = self.client.get(reverse('assessment_session_list') + '?scope=all')
        self.assertEqual([s.name for s in response.context['sessions']], ['Mine'])
        self.assertNotContains(response, 'All sessions in')
        self.assertTrue(mine)


class TransferIsOffTests(TestCase):

    def setUp(self):
        self.shared = make_org('LiveGrade')
        self.ann = make_user('ann')
        self.client.force_login(self.ann)
        session = self.client.session
        session[ORGS_KEY] = [{**self.shared.as_claim(), 'public': True}]
        session.save()
        self.session = AssessmentSession.objects.create(
            organization_id=self.shared.pk, name='Mine', created_by=self.ann)
        self.url = reverse('assessment_transfer', args=[self.session.pk])

    def test_the_transfer_page_does_not_exist_for_the_owner_either(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)
        stranger = make_user('stranger')
        self.assertEqual(self.client.post(self.url, {'new_owner': stranger.pk}).status_code, 404)
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ann)

    def test_no_page_offers_it(self):
        for name, args in (('assessment_session_list', ()), ('assessment_session_detail', (self.session.pk,)),
                           ('assessment_roster', (self.session.pk,))):
            with self.subTest(name):
                self.assertNotContains(self.client.get(reverse(name, args=args)), '/transfer/')

    def test_a_live_session_page_does_not_either(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        self.assertNotContains(self.client.get(reverse('assessment_session_detail', args=[self.session.pk])), '/transfer/')


class LandingAndStartTests(TestCase):

    def test_a_visitor_gets_the_landing_page(self):
        response = self.client.get('/')
        self.assertContains(response, 'LiveGrade')
        self.assertContains(response, 'Sign up free')
        self.assertContains(response, 'Create your free account')
        self.assertContains(response, 'How it works')

    def test_somebody_signed_in_goes_straight_to_their_sessions(self):
        org = make_org('Uni')
        sign_in(self.client, make_user('ann'), org)
        self.assertRedirects(self.client.get('/'), reverse('assessment_session_list'), fetch_redirect_response=False)

    def test_somebody_signed_in_without_any_organization_is_told_why_not(self):
        sign_in(self.client, make_user('ann'))
        self.assertContains(self.client.get('/'), 'not available for this account', status_code=403)

    def authorize_query(self, **params):
        response = self.client.get(reverse('oidc_authentication_init'), params)
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response['Location']).query)

    def test_signing_in_asks_devperf_for_its_ordinary_login(self):
        self.assertNotIn('screen_hint', self.authorize_query())

    def test_signing_up_asks_devperf_for_its_registration_screen(self):
        self.assertEqual(self.authorize_query(signup='1')['screen_hint'], ['signup'])

    def test_nothing_else_the_visitor_types_becomes_a_hint(self):
        for value in ('0', 'true', 'login', ''):
            with self.subTest(value=value):
                self.assertNotIn('screen_hint', self.authorize_query(signup=value))
        self.assertNotIn('screen_hint', self.authorize_query(screen_hint='signup'))

    def test_the_request_still_carries_pkce_and_the_scopes(self):
        query = self.authorize_query(signup='1')
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertIn('livegrade', query['scope'][0].split())
