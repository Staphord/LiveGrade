"""ServiceAccess: the note that a person chose a service."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.testing import make_user
from portal.models import ServiceAccess, record_choice, services_for


class RecordChoiceTests(TestCase):

    def setUp(self):
        self.ann = make_user('ann')

    def test_the_first_choice_creates_one_row(self):
        access = record_choice(self.ann, 'classroom')
        self.assertEqual(ServiceAccess.objects.count(), 1)
        self.assertEqual((access.user, access.service), (self.ann, 'classroom'))
        self.assertEqual(access.first_chosen, access.last_used)

    def test_choosing_again_only_refreshes_when_it_was_last_used(self):
        first = record_choice(self.ann, 'classroom')
        ServiceAccess.objects.filter(pk=first.pk).update(
            first_chosen=timezone.now() - timedelta(days=3), last_used=timezone.now() - timedelta(days=3))
        again = record_choice(self.ann, 'classroom')
        self.assertEqual(ServiceAccess.objects.count(), 1)
        self.assertEqual(again.pk, first.pk)
        again.refresh_from_db()
        self.assertGreater(again.last_used, again.first_chosen + timedelta(days=2))

    def test_each_person_has_their_own_row(self):
        ben = make_user('ben')
        record_choice(self.ann, 'classroom')
        record_choice(ben, 'classroom')
        self.assertEqual(ServiceAccess.objects.count(), 2)

    def test_a_person_can_use_several_services(self):
        record_choice(self.ann, 'classroom')
        record_choice(self.ann, 'panel')
        self.assertEqual(set(ServiceAccess.objects.values_list('service', flat=True)), {'classroom', 'panel'})

    def test_a_name_that_is_not_a_service_is_refused_and_nothing_is_written(self):
        for slug in ('bogus', '', None, 7, 'Classroom'):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                record_choice(self.ann, slug)
        self.assertEqual(ServiceAccess.objects.count(), 0)

    def test_the_database_refuses_a_second_row_for_the_same_person_and_service(self):
        ServiceAccess.objects.create(user=self.ann, service='classroom')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ServiceAccess.objects.create(user=self.ann, service='classroom')

    def test_the_rows_go_when_the_person_does(self):
        record_choice(self.ann, 'classroom')
        self.ann.delete()
        self.assertEqual(ServiceAccess.objects.count(), 0)

    def test_it_reads_plainly(self):
        self.assertEqual(str(record_choice(self.ann, 'classroom')), f'{self.ann.pk} uses classroom')


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom'])
class ServicesForTests(TestCase):

    def setUp(self):
        self.ann = make_user('ann')

    def test_nothing_before_a_first_choice(self):
        self.assertEqual(services_for(self.ann), [])

    def test_the_services_a_person_uses_in_the_registrys_order(self):
        record_choice(self.ann, 'classroom')
        self.assertEqual([s.slug for s in services_for(self.ann)], ['classroom'])

    def test_a_service_that_is_not_open_is_left_out(self):
        record_choice(self.ann, 'panel')
        self.assertEqual(services_for(self.ann), [])

    def test_a_service_that_has_left_the_registry_is_left_out(self):
        ServiceAccess.objects.create(user=self.ann, service='retired')
        record_choice(self.ann, 'classroom')
        self.assertEqual([s.slug for s in services_for(self.ann)], ['classroom'])

    def test_other_peoples_choices_are_not_shown(self):
        record_choice(make_user('ben'), 'classroom')
        self.assertEqual(services_for(self.ann), [])
