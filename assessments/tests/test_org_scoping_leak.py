from django.test import TestCase

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession


class OrgScopingLeakTests(TestCase):
    """One org's assessment sessions must never be visible from another."""

    def setUp(self):
        self.org_a = make_org('Org A')
        self.org_b = make_org('Org B')
        self.lecturer_a = make_user('lect_a')
        self.lecturer_b = make_user('lect_b')
        self.session_a = AssessmentSession.objects.create(
            organization_id=self.org_a.pk, name='Org A session', created_by=self.lecturer_a)

    def test_for_organization_excludes_other_org(self):
        visible_to_b = AssessmentSession.objects.for_organization(self.org_b.as_claim())
        self.assertNotIn(self.session_a, visible_to_b)

    def test_lecturer_b_cannot_open_session_a(self):
        sign_in(self.client, self.lecturer_b, self.org_b)
        response = self.client.get(f'/assessments/{self.session_a.pk}/')
        self.assertEqual(response.status_code, 404)
