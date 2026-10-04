from django.test import TestCase

from accounts.directory import colleagues, record_sign_in
from accounts.models import OrganizationLecturer, User
from accounts.testing import make_org, make_user

ACME = {'id': 1, 'slug': 'acme', 'name': 'Acme'}
GLOBEX = {'id': 2, 'slug': 'globex', 'name': 'Globex'}


class RecordSignInTests(TestCase):

    def setUp(self):
        self.ann = make_user('ann')

    def rows(self, user=None):
        return sorted(OrganizationLecturer.objects.filter(user=user or self.ann)
                      .values_list('organization_id', 'organization_name'))

    def test_a_sign_in_records_each_organization_the_person_may_run_it_in(self):
        record_sign_in(self.ann, [ACME, GLOBEX])
        self.assertEqual(self.rows(), [(1, 'Acme'), (2, 'Globex')])

    def test_signing_in_again_refreshes_rather_than_duplicates(self):
        record_sign_in(self.ann, [ACME])
        first = OrganizationLecturer.objects.get().last_seen
        record_sign_in(self.ann, [{**ACME, 'name': 'Acme Ltd'}])
        self.assertEqual(self.rows(), [(1, 'Acme Ltd')])
        self.assertGreater(OrganizationLecturer.objects.get().last_seen, first)

    def test_an_organization_they_may_no_longer_use_is_forgotten(self):
        record_sign_in(self.ann, [ACME, GLOBEX])
        record_sign_in(self.ann, [GLOBEX])
        self.assertEqual(self.rows(), [(2, 'Globex')])

    def test_losing_every_organization_leaves_no_rows(self):
        record_sign_in(self.ann, [ACME])
        record_sign_in(self.ann, [])
        self.assertEqual(self.rows(), [])

    def test_one_persons_sign_in_never_touches_anothers(self):
        ben = make_user('ben')
        record_sign_in(ben, [ACME])
        record_sign_in(self.ann, [GLOBEX])
        self.assertEqual(self.rows(ben), [(1, 'Acme')])

    def test_a_missing_name_is_stored_as_empty_text(self):
        record_sign_in(self.ann, [{'id': 5}])
        self.assertEqual(self.rows(), [(5, '')])

    def test_the_row_prints_who_and_where(self):
        record_sign_in(self.ann, [ACME])
        self.assertEqual(str(OrganizationLecturer.objects.get()), f'{self.ann.pk} in 1')


class ColleaguesTests(TestCase):

    def setUp(self):
        self.zed = make_user('zed', first_name='Zed', last_name='Last')
        self.amy = make_user('amy', first_name='Amy', last_name='First')
        self.bob = make_user('bob', first_name='Bob', last_name='Mid')
        self.elsewhere = make_user('elsewhere')
        for person in (self.zed, self.amy, self.bob):
            record_sign_in(person, [ACME])
        record_sign_in(self.elsewhere, [GLOBEX])

    def test_it_lists_the_people_known_in_that_organization_by_name(self):
        self.assertEqual(list(colleagues(1)), [self.amy, self.bob, self.zed])

    def test_it_leaves_out_the_person_asking(self):
        self.assertEqual(list(colleagues(1, excluding=self.amy)), [self.bob, self.zed])

    def test_other_organizations_people_are_not_listed(self):
        self.assertNotIn(self.elsewhere, colleagues(1))
        self.assertEqual(list(colleagues(2)), [self.elsewhere])

    def test_an_organization_nobody_has_signed_in_to_has_no_colleagues(self):
        self.assertEqual(list(colleagues(99)), [])

    def test_somebody_who_never_signed_in_is_not_listed(self):
        stranger = User.objects.create(sub='stranger', username='devperf-stranger')
        self.assertNotIn(stranger, colleagues(1))
