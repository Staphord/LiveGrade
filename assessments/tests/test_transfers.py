"""Handing a session to another lecturer: the rule, the page, and the operator command."""
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse

from accounts.directory import record_sign_in
from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession, SessionTransfer
from assessments.transfers import TransferError, eligible_recipients, transfer_session


class TransferTestCase(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.other_org = make_org('Elsewhere')
        self.ann = make_user('ann', first_name='Ann', last_name='Owner', email='ann@uni.test')
        self.ben = make_user('ben', first_name='Ben', last_name='Colleague', email='ben@uni.test')
        self.cat = make_user('cat')            # no name, no email
        self.dan = make_user('dan')            # signed in, but to another organization
        self.eve = make_user('eve')            # exists, never signed in
        record_sign_in(self.ann, [self.org.as_claim()])
        record_sign_in(self.ben, [self.org.as_claim()])
        record_sign_in(self.cat, [self.org.as_claim()])
        record_sign_in(self.dan, [self.other_org.as_claim()])
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Demo day', created_by=self.ann)


class TransferServiceTests(TransferTestCase):

    def test_the_session_changes_hands_and_the_hand_over_is_logged(self):
        transfer_session(self.session, self.ben, by_user=self.ann, note='going on leave')
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ben)
        log = SessionTransfer.objects.get()
        self.assertEqual((log.session, log.from_user, log.to_user, log.by_user, log.note),
                         (self.session, self.ann, self.ben, self.ann, 'going on leave'))
        self.assertEqual(str(log), f'{self.session.pk}: {self.ann.pk} -> {self.ben.pk}')

    def test_the_new_owner_owns_it_and_the_old_one_no_longer_does(self):
        transfer_session(self.session, self.ben)
        self.assertIn(self.session, AssessmentSession.objects.owned_by(self.ben))
        self.assertNotIn(self.session, AssessmentSession.objects.owned_by(self.ann))

    def test_it_cannot_be_handed_to_its_own_owner(self):
        with self.assertRaisesMessage(TransferError, 'already owns'):
            transfer_session(self.session, self.ann)
        self.assertEqual(SessionTransfer.objects.count(), 0)

    def test_it_cannot_be_handed_to_somebody_in_another_organization(self):
        with self.assertRaisesMessage(TransferError, 'not known to run LiveGrade in this organization'):
            transfer_session(self.session, self.dan)
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ann)

    def test_it_cannot_be_handed_to_somebody_who_never_signed_in(self):
        with self.assertRaisesMessage(TransferError, 'sign in to LiveGrade once'):
            transfer_session(self.session, self.eve)

    def test_an_operator_may_skip_the_known_person_check(self):
        transfer_session(self.session, self.eve, require_known=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.eve)
        self.assertIsNone(SessionTransfer.objects.get().by_user)

    def test_a_live_session_can_be_handed_over_too(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        self.session.refresh_from_db()
        transfer_session(self.session, self.ben)
        self.session.refresh_from_db()
        self.assertEqual((self.session.created_by, self.session.status), (self.ben, 'live'))

    def test_the_recipients_are_the_other_known_people_in_its_organization(self):
        self.assertEqual(list(eligible_recipients(self.session)), [self.ben, self.cat])

    def test_a_session_with_no_owner_lists_everybody_known(self):
        orphan = AssessmentSession.objects.create(organization_id=self.org.pk, name='Orphan')
        self.assertEqual(list(eligible_recipients(orphan)), [self.ann, self.ben, self.cat])

    def test_the_log_survives_the_people_it_names_being_deleted(self):
        transfer_session(self.session, self.ben)
        self.ann.delete()
        self.assertIsNone(SessionTransfer.objects.get().from_user)


class TransferPageTests(TransferTestCase):

    def setUp(self):
        super().setUp()
        sign_in(self.client, self.ann, self.org)
        self.url = reverse('assessment_transfer', args=[self.session.pk])

    def test_the_page_warns_and_lists_only_the_colleagues_known_in_this_organization(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'you will no longer see or control it')
        self.assertContains(response, 'Ben Colleague (ben@uni.test)')
        self.assertContains(response, '>cat<')                    # no name or email: the username
        for hidden in ('Ann Owner (ann@uni.test)', 'dan', 'eve'):
            self.assertNotContains(response, f'>{hidden}</option>')

    def test_with_nobody_to_hand_it_to_the_page_says_so(self):
        record_sign_in(self.ben, [])
        record_sign_in(self.cat, [])
        response = self.client.get(self.url)
        self.assertContains(response, 'No other colleague has signed in')
        self.assertNotContains(response, 'name="new_owner"')

    def test_choosing_a_colleague_hands_it_over_and_returns_to_the_list(self):
        response = self.client.post(self.url, {'new_owner': self.ben.pk})
        self.assertRedirects(response, reverse('assessment_session_list'))
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ben)
        self.assertEqual(SessionTransfer.objects.get().by_user, self.ann)

    def test_the_success_message_names_the_session_and_the_new_owner(self):
        response = self.client.post(self.url, {'new_owner': self.ben.pk}, follow=True)
        self.assertContains(response, '&quot;Demo day&quot; now belongs to Ben Colleague')

    def test_the_message_falls_back_to_the_username(self):
        response = self.client.post(self.url, {'new_owner': self.cat.pk}, follow=True)
        self.assertContains(response, 'now belongs to cat')

    def test_the_previous_owner_loses_the_session_at_once(self):
        self.client.post(self.url, {'new_owner': self.ben.pk})
        self.assertEqual(self.client.get(reverse('assessment_session_detail', args=[self.session.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        sign_in(self.client, self.ben, self.org)
        self.assertEqual(self.client.get(reverse('assessment_session_detail', args=[self.session.pk])).status_code, 200)

    def test_nothing_chosen_or_nonsense_changes_nothing(self):
        for value in ('', 'abc', '999999', str(self.ann.pk)):
            with self.subTest(value=value):
                response = self.client.post(self.url, {'new_owner': value})
                self.assertContains(response, 'Choose one of the lecturers in the list.')
        self.client.post(self.url, {})
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ann)
        self.assertEqual(SessionTransfer.objects.count(), 0)

    def test_somebody_from_another_organization_or_never_signed_in_cannot_be_chosen(self):
        for outsider in (self.dan, self.eve):
            with self.subTest(person=outsider.username):
                self.client.post(self.url, {'new_owner': outsider.pk})
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ann)

    def test_if_the_recipient_stops_being_eligible_in_between_the_owner_is_told(self):
        with mock.patch('assessments.views.lecturer_views.transfer_session',
                        side_effect=TransferError('They were removed.')):
            response = self.client.post(self.url, {'new_owner': self.ben.pk})
        self.assertContains(response, 'They were removed.')
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ann)

    def test_a_colleague_cannot_open_or_use_the_page(self):
        sign_in(self.client, self.ben, self.org)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {'new_owner': self.cat.pk}).status_code, 404)
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ann)

    def test_a_visitor_who_is_not_signed_in_is_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_session_pages_link_to_it(self):
        self.assertContains(self.client.get(reverse('assessment_session_detail', args=[self.session.pk])), self.url)
        self.assertContains(self.client.get(reverse('assessment_session_list')), self.url)

    def test_the_live_control_page_links_to_it_too(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(status='live')
        self.assertContains(self.client.get(reverse('assessment_session_detail', args=[self.session.pk])), self.url)

    def test_the_new_session_page_has_no_transfer_link(self):
        self.assertNotContains(self.client.get(reverse('assessment_session_create')), '/transfer/')


class TransferCommandTests(TransferTestCase):

    def run_command(self, *args):
        out = StringIO()
        call_command('transfer_session', *args, stdout=out)
        return out.getvalue()

    def test_it_hands_the_session_over_and_says_so(self):
        text = self.run_command('--session', str(self.session.pk), '--to-sub', self.ben.sub)
        self.assertIn('now belongs to Ben Colleague', text)
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.ben)
        log = SessionTransfer.objects.get()
        self.assertEqual((log.by_user, log.note), (None, 'operator command'))

    def test_a_person_with_no_name_is_shown_by_username(self):
        self.assertIn('now belongs to cat', self.run_command('--session', str(self.session.pk), '--to-sub', self.cat.sub))

    def test_an_unknown_session_is_reported(self):
        with self.assertRaisesMessage(CommandError, 'There is no session 999'):
            self.run_command('--session', '999', '--to-sub', self.ben.sub)

    def test_an_unknown_lecturer_is_reported_with_what_to_do(self):
        with self.assertRaisesMessage(CommandError, 'must sign in to LiveGrade once'):
            self.run_command('--session', str(self.session.pk), '--to-sub', 'nobody')

    def test_somebody_not_in_the_organization_is_refused_unless_forced(self):
        with self.assertRaisesMessage(CommandError, 'not known to run LiveGrade'):
            self.run_command('--session', str(self.session.pk), '--to-sub', self.eve.sub)
        self.run_command('--session', str(self.session.pk), '--to-sub', self.eve.sub, '--force')
        self.session.refresh_from_db()
        self.assertEqual(self.session.created_by, self.eve)

    def test_handing_it_to_its_owner_is_refused(self):
        with self.assertRaisesMessage(CommandError, 'already owns'):
            self.run_command('--session', str(self.session.pk), '--to-sub', self.ann.sub)
