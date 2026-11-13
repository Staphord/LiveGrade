"""A Team page never lists, links to or opens another service's records."""
import uuid

from django.test import TestCase, override_settings

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession
from service_domains.models import TrainingParticipant, TrainingSeries, TrainingSeriesSession

SERVICES = ['classroom', 'panel', 'polls', 'team', 'showcase', 'competition']


@override_settings(LIVEGRADE_LIVE_SERVICES=SERVICES)
class TeamServiceIsolationTests(TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.org = make_org(f'Isolation {suffix}')
        self.owner = make_user(f'iso-owner-{suffix}')
        self.classroom = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='classroom',
            name='Private classroom class', status='live')
        self.series = TrainingSeries.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Isolation series')
        TrainingParticipant.objects.create(
            organization_id=self.org.id, created_by=self.owner, series=self.series,
            display_name='Alice', group_name='Pod', order=1)
        self.demo = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='team',
            name='Team demo', status='draft')
        self.link = TrainingSeriesSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, series=self.series,
            assessment_session=self.demo, sequence=1)
        sign_in(self.client, self.owner, self.org)

    def test_team_cannot_list_classroom_sessions(self):
        for path in ('/team/app/sessions/', '/team/app/sessions/?scope=all', '/team/app/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotContains(response, 'Private classroom class', status_code=404)

    def test_classroom_still_lists_its_own_sessions_only(self):
        page = self.client.get('/classroom/app/sessions/')
        self.assertContains(page, 'Private classroom class')
        self.assertNotContains(page, 'Team demo')

    def test_team_cannot_open_a_classroom_session_by_id(self):
        for suffix in ('', 'setup/', 'roster/', 'rubric/', 'groups/', 'results/', 'participation/'):
            response = self.client.get(f'/team/app/{self.classroom.pk}/{suffix}')
            self.assertEqual(response.status_code, 404, suffix)

    def test_classroom_cannot_open_a_team_demo_by_id(self):
        self.assertEqual(self.client.get(f'/classroom/app/{self.demo.pk}/').status_code, 404)

    def test_demo_pages_go_back_to_the_workshop_not_the_classroom_list(self):
        live = f'/team/series/{self.series.pk}/sessions/{self.link.pk}/live/'
        for suffix in ('', 'setup/'):
            html = self.client.get(f'/team/app/{self.demo.pk}/{suffix}').content.decode()
            self.assertIn(f'href="{live}"', html, suffix)
            self.assertIn('Back to live workshop', html, suffix)
            self.assertNotIn('/sessions/"', html.replace('/sessions/' + str(self.link.pk), ''), suffix)
            self.assertNotIn('Classroom sessions', html, suffix)
            self.assertNotIn('Private classroom class', html, suffix)

    def test_unknown_or_foreign_demo_falls_back_to_the_series_list(self):
        other = make_user('iso-other')
        stranger = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=other, service='team', name='Stranger demo', status='draft')
        self.assertEqual(self.client.get(f'/team/app/{stranger.pk}/').status_code, 404)
        orphan = AssessmentSession.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='team', name='Orphan demo', status='draft')
        html = self.client.get(f'/team/app/{orphan.pk}/').content.decode()
        self.assertIn('Training series', html)

    def test_classroom_pages_keep_their_own_back_link(self):
        html = self.client.get(f'/classroom/app/{self.classroom.pk}/').content.decode()
        self.assertIn('/classroom/app/sessions/', html)
        self.assertIn('All sessions', html)

    def test_non_session_team_pages_use_the_series_list_as_home(self):
        from accounts.context import _sessions_home
        request = type('R', (), {'service': 'team', 'resolver_match': None,
                                 'user': self.owner})()
        home = _sessions_home(request, {'id': self.org.id})
        self.assertEqual(home['label'], 'Training series')
