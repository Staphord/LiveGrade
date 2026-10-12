"""Links built from a variable must never be steerable by the visitor.

A template that writes ``href="{{ something }}"`` is an open redirect, or a ``javascript:``
link, the moment ``something`` can be influenced from the query string. These pages do that
for the "back" and "reload" links, so what feeds them is pinned here.
"""
import re

from django.test import Client, TestCase
from django.urls import reverse

from accounts.directory import record_sign_in
from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession

HOSTILE = ['https://evil.example/', '//evil.example/', 'javascript:alert(1)']


class BackLinkTests(TestCase):

    def setUp(self):
        self.org = make_org('Uni')
        self.ann = make_user('ann')
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Demo day', created_by=self.ann)
        sign_in(self.client, self.ann, self.org)
        self.url = reverse('assessment_transfer', args=[self.session.pk])

    def links(self, response):
        return re.findall(r'href="([^"]*)"', response.content.decode())

    def test_the_back_links_go_to_the_session_list_whatever_the_query_says(self):
        expected = reverse('assessment_session_list')
        for hostile in HOSTILE:
            for name in ('next', 'back', 'return_to', 'redirect'):
                with self.subTest(name=name, value=hostile):
                    response = self.client.get(self.url, {name: hostile})
                    links = self.links(response)
                    self.assertIn(expected, links)
                    self.assertNotIn('evil.example', ' '.join(links))
                    self.assertNotIn('javascript:', ' '.join(links))

    def test_a_finished_transfer_returns_to_the_list_whatever_the_form_says(self):
        bob = make_user('bob', first_name='Bob')
        record_sign_in(bob, [self.org.as_claim()])
        response = self.client.post(self.url, {'new_owner': str(bob.pk), 'next': HOSTILE[0]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('assessment_session_list'))


class ReloadLinkTests(TestCase):

    def test_the_expired_form_page_links_back_to_a_path_on_this_site(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/switch-organization/?next=https://evil.example/', {'organization': '1'})
        self.assertEqual(response.status_code, 403)
        target = re.search(r'href="([^"]*)" class="btn btn-primary mt-2"', response.content.decode()).group(1)
        self.assertTrue(target.startswith('/') and not target.startswith('//'))
        # The address that was asked for, query string included, but never an outside site.
        self.assertEqual(target, '/switch-organization/?next=https://evil.example/'.replace('&', '&amp;'))
