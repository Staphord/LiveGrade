"""Signing out asks first, in a centered dialog, then goes to the home page."""
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in


class SignOutDialogTests(TestCase):

    def setUp(self):
        sign_in(self.client, make_user('ann', first_name='Ann', last_name='Owner'), make_org('Uni'))
        self.page = self.client.get(reverse('assessment_session_list'))

    def test_the_sidebar_button_opens_a_dialog_instead_of_signing_out_at_once(self):
        self.assertContains(self.page, 'id="sidebar-logout-link"')
        self.assertContains(self.page, 'data-bs-target="#signout-modal"')
        self.assertNotContains(self.page, 'id="sidebar-logout-link" class="sidebar-signout border-0 bg-transparent" aria-label="Sign out" title="Sign out" type="submit"')

    def test_the_dialog_is_centered_and_asks_a_plain_question(self):
        self.assertContains(self.page, 'modal-dialog-centered')
        self.assertContains(self.page, 'Sign out of LiveGrade?')
        self.assertContains(self.page, 'You will need to sign in again to run your sessions.')

    def test_confirming_posts_to_the_sign_out_address_with_the_security_token(self):
        html = self.page.content.decode()
        form = html.split('id="signout-modal"')[1].split('</form>')[0]
        self.assertIn(f'action="{reverse("oidc_logout")}"', form)
        self.assertIn('csrfmiddlewaretoken', form)
        self.assertIn('data-signout-confirm', form)
        self.assertIn('>Sign out</button>', form)

    def test_cancelling_just_closes_the_dialog_and_signs_nobody_out(self):
        self.assertContains(self.page, 'data-bs-dismiss="modal" data-signout-cancel')
        self.assertIn('_auth_user_id', self.client.session)

    def test_there_is_no_dialog_for_somebody_who_is_not_signed_in(self):
        self.client.logout()
        self.assertNotContains(self.client.get('/'), 'signout-modal')

    def test_the_sign_out_request_still_ends_the_livegrade_session_and_goes_to_devperf(self):
        response = self.client.post(reverse('oidc_logout'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/o/logout/', response['Location'])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_the_destination_after_signing_out_is_livegrades_home_page(self):
        response = self.client.post(reverse('oidc_logout'))
        self.assertIn('post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A8001%2F', response['Location'])
        landing = self.client.get('/')
        self.assertContains(landing, 'Grade presentations')
