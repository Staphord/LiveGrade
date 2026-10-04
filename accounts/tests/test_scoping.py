from django.http import Http404
from django.test import RequestFactory, TestCase

from accounts.access import ORGS_KEY
from accounts.scoping import get_scoped_or_404
from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession


class ScopingTests(TestCase):
    """The rule the whole service leans on: no organization means no rows."""

    def setUp(self):
        self.acme = make_org('Acme')
        self.globex = make_org('Globex')
        self.acme_session = AssessmentSession.objects.create(organization_id=self.acme.pk, name='A')
        self.globex_session = AssessmentSession.objects.create(organization_id=self.globex.pk, name='G')

    def request(self, **session):
        request = RequestFactory().get('/')
        request.session = session
        return request

    def test_an_organization_sees_only_its_own_rows(self):
        rows = AssessmentSession.objects.for_organization(self.acme.as_claim())
        self.assertEqual(list(rows), [self.acme_session])

    def test_no_organization_means_no_rows_never_all_of_them(self):
        self.assertEqual(AssessmentSession.objects.count(), 2)
        self.assertEqual(list(AssessmentSession.objects.for_organization(None)), [])

    def test_a_request_without_an_organization_sees_nothing(self):
        self.assertEqual(list(AssessmentSession.objects.for_request(self.request())), [])

    def test_a_request_sees_the_rows_of_its_active_organization(self):
        request = self.request(**{ORGS_KEY: [self.globex.as_claim()]})
        self.assertEqual(list(AssessmentSession.objects.for_request(request)), [self.globex_session])

    def test_another_organizations_row_is_a_404_not_a_403(self):
        request = self.request(**{ORGS_KEY: [self.acme.as_claim()]})
        self.assertEqual(get_scoped_or_404(AssessmentSession, request, pk=self.acme_session.pk), self.acme_session)
        with self.assertRaises(Http404):
            get_scoped_or_404(AssessmentSession, request, pk=self.globex_session.pk)


class SignInHelperTests(TestCase):

    def test_the_chosen_active_organization_is_remembered(self):
        acme, globex = make_org('Acme'), make_org('Globex')
        sign_in(self.client, make_user('t'), acme, globex, active=globex)
        self.assertEqual(self.client.session['livegrade_active_org'], globex.pk)
