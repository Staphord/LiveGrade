"""Competition setup: always-visible sections, background saves, honest steps."""
import uuid

from django.test import TestCase, override_settings

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import Competition, CompetitionCriterion, CompetitionTrack


@override_settings(LIVEGRADE_LIVE_SERVICES=['classroom', 'panel', 'polls', 'team', 'showcase', 'competition'])
class CompetitionSetupCleanTests(TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.org = make_org(f'Setup clean {suffix}')
        self.owner = make_user(f'setup-clean-{suffix}')
        self.competition = Competition.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Clean comp')
        sign_in(self.client, self.owner, self.org)
        self.url = f'/competition/app/{self.competition.pk}/'

    def add_track(self):
        return CompetitionTrack.objects.create(
            organization_id=self.org.id, created_by=self.owner, competition=self.competition, name='Open')

    def test_draft_setup_has_no_dropdown_sections_and_saves_in_the_background(self):
        self.add_track()
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('<summary', html)
        self.assertNotIn('tc-disclosure', html.replace('.tc-disclosure', ''))
        for region in ('counts', 'tracks', 'entries', 'criteria'):
            self.assertIn(f'data-inline-region="{region}"', html)
        self.assertEqual(html.count('Tracks and advancement'), 1)
        self.assertIn('Add a track', html)
        self.assertIn('Import entries', html)
        self.assertIn('Import judging criteria', html)
        self.assertNotIn('—', html)

    def test_step_label_does_not_promise_judges_before_the_event_starts(self):
        steps = self.client.get(self.url).context['stepper_steps']
        self.assertEqual([step['label'] for step in steps][3], 'Scoring criteria')

    def test_criteria_step_lists_criteria_with_per_track_totals(self):
        track = self.add_track()
        CompetitionCriterion.objects.create(
            organization_id=self.org.id, created_by=self.owner, track=track, name='Impact', weight=60)
        html = self.client.get(self.url).content.decode()
        self.assertIn('Remove Impact', html)
        self.assertIn('Open: 60', html)
