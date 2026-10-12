"""Names typed by whoever sets a session up, or read from their spreadsheet, are hostile input.

The live pages are refreshed from JSON endpoints. These prove two things the page's own
scripts rely on: the pieces of HTML those endpoints return are already escaped, and the plain
strings they return are never mistaken for markup by a browser (a JSON content type plus
``nosniff``), because the scripts that use them put them in with ``textContent``.
"""
import json

from django.urls import reverse

from accounts.testing import make_user, sign_in
from assessments.models import AssessmentSession, PresentationGroup, Student
from assessments.tests.test_edges_core import SessionFixture

PAYLOAD = '<script>alert(9001)</script>'
IMG = '<img src=x onerror=alert(9002)>'
ESCAPED = '&lt;script&gt;alert(9001)&lt;/script&gt;'


class HostileNamesTests(SessionFixture):

    def setUp(self):
        super().setUp()
        self.lecturer = make_user('lect')
        sign_in(self.client, self.lecturer, self.org)
        AssessmentSession.objects.filter(pk=self.session.pk).update(
            created_by=self.lecturer, name=PAYLOAD)
        PresentationGroup.objects.filter(pk=self.red.pk).update(name=PAYLOAD, topic=IMG)
        Student.objects.filter(pk=self.ann.pk).update(full_name=IMG)
        self.session.refresh_from_db()

    def lecturer_url(self, name):
        return reverse(name, args=[self.session.pk])

    def student_url(self, name):
        return reverse(name, args=[self.session.uuid])

    def assertJson(self, response):
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def assertNoMarkup(self, html):
        for raw in ('<script>alert', '<img src=x'):
            self.assertNotIn(raw, html)

    def test_the_results_rows_html_is_escaped(self):
        response = self.client.get(self.lecturer_url('assessment_results_refresh'))
        self.assertJson(response)
        html = response.json()['rows_html']
        self.assertNoMarkup(html)
        self.assertIn(ESCAPED, html)

    def test_the_participation_cards_html_is_escaped(self):
        response = self.client.get(self.lecturer_url('assessment_participation_refresh'))
        self.assertJson(response)
        html = response.json()['cards_html']
        self.assertNoMarkup(html)
        self.assertIn('&lt;img src=x', html)

    def test_chart_labels_are_data_for_the_chart_not_markup(self):
        response = self.client.get(self.lecturer_url('assessment_results_refresh'))
        # Plain strings in JSON: Chart.js draws them as text. They must come back unchanged
        # (escaping them here would show "&lt;" on the chart) with a type browsers will not sniff.
        self.assertEqual(response.json()['group_chart']['labels'], [PAYLOAD, 'Blue'])
        self.assertJson(response)

    def test_the_public_join_screen_state_is_json_the_browser_will_not_sniff(self):
        PresentationGroup.objects.filter(pk=self.red.pk).update(name=PAYLOAD)
        self.active_turn(self.red)
        response = self.client.get(self.student_url('assessment_join_screen_state'))
        self.assertJson(response)
        self.assertEqual(json.loads(response.content)['public_group_name'], PAYLOAD)

    def test_the_join_screen_page_escapes_the_group_it_names(self):
        self.active_turn(self.red)
        response = self.client.get(self.student_url('assessment_join_screen'))
        html = response.content.decode()
        self.assertNoMarkup(html)
        self.assertIn(ESCAPED, html)
