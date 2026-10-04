"""A session is private to the lecturer who created it.

The route-by-route proof (every id address answers a colleague exactly as it
answers a missing id) is in ``test_cross_org_access``. This is the list, the
creation, and the queryset the rule is built from.
"""
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession


class OwnershipTestCase(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.other_org = make_org('Elsewhere')
        self.ann = make_user('ann')
        self.ben = make_user('ben')
        self.ann_session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Ann demo', created_by=self.ann)
        self.ben_session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Ben demo', created_by=self.ben)
        self.elsewhere = AssessmentSession.objects.create(
            organization_id=self.other_org.pk, name='Ann elsewhere', created_by=self.ann)


class OwnedByTests(OwnershipTestCase):

    def test_a_person_is_given_only_what_they_created(self):
        self.assertEqual(
            list(AssessmentSession.objects.for_organization(self.org.as_claim()).owned_by(self.ann)),
            [self.ann_session])

    def test_the_organization_still_applies_as_well(self):
        everything_of_ann = AssessmentSession.objects.owned_by(self.ann)
        self.assertEqual(set(everything_of_ann), {self.ann_session, self.elsewhere})
        in_one_org = AssessmentSession.objects.for_organization(self.org.as_claim()).owned_by(self.ann)
        self.assertEqual(list(in_one_org), [self.ann_session])

    def test_nobody_owns_anything_so_the_filter_never_widens_to_all(self):
        for nobody in (None, AnonymousUser()):
            with self.subTest(user=nobody):
                self.assertEqual(AssessmentSession.objects.count(), 3)
                self.assertEqual(list(AssessmentSession.objects.owned_by(nobody)), [])

    def test_a_session_whose_creator_is_unknown_is_nobodys(self):
        orphan = AssessmentSession.objects.create(organization_id=self.org.pk, name='No owner')
        for person in (self.ann, self.ben):
            self.assertNotIn(orphan, AssessmentSession.objects.owned_by(person))


class SessionListTests(OwnershipTestCase):

    def names(self, user, org):
        self.client.logout()
        sign_in(self.client, user, org)
        return [s.name for s in self.client.get(reverse('assessment_session_list')).context['sessions']]

    def test_each_lecturer_sees_only_their_own_sessions(self):
        self.assertEqual(self.names(self.ann, self.org), ['Ann demo'])
        self.assertEqual(self.names(self.ben, self.org), ['Ben demo'])

    def test_the_same_person_sees_each_organizations_sessions_in_that_organization(self):
        self.assertEqual(self.names(self.ann, self.other_org), ['Ann elsewhere'])

    def test_somebody_who_has_created_nothing_sees_an_empty_list(self):
        self.assertEqual(self.names(make_user('newcomer'), self.org), [])

    def test_the_empty_list_invites_them_to_create_one(self):
        self.client.logout()
        sign_in(self.client, make_user('newcomer'), self.org)
        self.assertContains(self.client.get(reverse('assessment_session_list')),
                            'No assessment sessions yet')


class CreationTests(OwnershipTestCase):

    def test_a_new_session_belongs_to_the_lecturer_who_made_it_and_to_nobody_else(self):
        sign_in(self.client, self.ann, self.org)
        self.client.post(reverse('assessment_session_create'), {
            'name': 'Fresh', 'identify_by': 'full_name',
            'group_weight_percent': '60', 'individual_weight_percent': '40'})
        fresh = AssessmentSession.objects.get(name='Fresh')
        self.assertEqual(fresh.created_by, self.ann)
        self.assertIn(fresh, AssessmentSession.objects.owned_by(self.ann))
        self.assertNotIn(fresh, AssessmentSession.objects.owned_by(self.ben))


class ColleagueCannotOpenTests(OwnershipTestCase):

    def test_a_colleague_gets_a_404_not_a_403_and_cannot_change_anything(self):
        sign_in(self.client, self.ben, self.org)
        url = reverse('assessment_session_detail', args=[self.ann_session.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.post(reverse('assessment_session_delete', args=[self.ann_session.pk])).status_code, 404)
        self.assertTrue(AssessmentSession.objects.filter(pk=self.ann_session.pk).exists())

    def test_the_owner_still_can(self):
        sign_in(self.client, self.ann, self.org)
        self.assertEqual(
            self.client.get(reverse('assessment_session_detail', args=[self.ann_session.pk])).status_code, 200)
