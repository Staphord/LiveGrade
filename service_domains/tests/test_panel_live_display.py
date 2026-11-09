from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    PanelAbstention, PanelAuditEvent, PanelCandidate, PanelConflict,
    PanelCriterion, PanelHearing, PanelInvitation, PanelLiveDisplayLink,
    PanelScore, PanelScoringPanel, PanelVote, PanelWrittenReview,
)
from service_domains.operations import (
    issue_panel_invitation, issue_panel_live_display_link,
    revoke_panel_live_display_link,
)
from service_domains.panel_reporting import panel_live_activity


class PanelLiveDisplayTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel live display org')
        self.owner = make_user('panel-live-owner')
        sign_in(self.client, self.owner, self.org)
        self.hearing = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Live review', status=PanelHearing.Status.OPEN,
            workflow=PanelHearing.Workflow.SCORED)
        self.candidate = PanelCandidate.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, display_name='Candidate One')
        self.criterion = PanelCriterion.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, name='Evidence')
        self.panel = PanelScoringPanel.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, name='Main panel', share=Decimal('100.00'))
        self.panel.criteria.add(self.criterion)

    def invitation(self, email):
        return issue_panel_invitation(self.hearing, email,
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)[0]

    def test_live_activity_uses_aggregate_scores_and_excludes_recused_examiner(self):
        first = self.invitation('first@example.test')
        second = self.invitation('second@example.test')
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=first, candidate=self.candidate, criterion=self.criterion,
            score=Decimal('80'), comment='Private note')
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=second, candidate=self.candidate, criterion=self.criterion,
            score=Decimal('100'), comment='Another private note')
        conflict = PanelConflict(organization_id=self.org.id, created_by=self.owner,
            invitation=second, candidate=self.candidate, reason='Known relationship')
        conflict.full_clean()
        conflict.save()

        activity = panel_live_activity(self.hearing)
        row = activity['candidates'][0]

        self.assertEqual(row['score'], '80.00')
        self.assertTrue(row['complete'])
        self.assertEqual(row['responses'], 1)
        self.assertNotIn('email', row)
        self.assertNotIn('comment', row)

    def test_public_live_link_shows_only_aggregate_progress_and_can_be_revoked(self):
        invitation = self.invitation('private@example.test')
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=invitation, candidate=self.candidate, criterion=self.criterion,
            score=Decimal('76'), comment='Do not expose this')
        link, token = issue_panel_live_display_link(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        url = reverse('panel_live_display_updates', kwargs={'token': token})
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('private, no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.json()['candidates'][0]['score'], '76.00')
        self.assertNotIn('private@example.test', response.content.decode())
        self.assertNotIn('Do not expose this', response.content.decode())

        revoke_panel_live_display_link(self.hearing, link,
            organization=self.org.as_claim(), user=self.owner)
        link.refresh_from_db()
        self.assertIsNotNone(link.revoked_at)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            revoked = self.client.get(url)
        self.assertEqual(revoked.status_code, 404)
        self.assertEqual(PanelAuditEvent.objects.filter(
            action=PanelAuditEvent.Action.LIVE_DISPLAY_REVOKED).count(), 1)

    def test_issuing_a_new_link_revokes_the_previous_one(self):
        old, old_token = issue_panel_live_display_link(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        new, new_token = issue_panel_live_display_link(self.hearing,
            organization=self.org.as_claim(), user=self.owner)
        old.refresh_from_db()
        self.assertIsNotNone(old.revoked_at)
        self.assertIsNone(new.revoked_at)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(PanelLiveDisplayLink.objects.filter(
            hearing=self.hearing, revoked_at__isnull=True).count(), 1)

    def test_live_score_respects_relative_criterion_weights(self):
        invitation = self.invitation('weighted@example.test')
        self.criterion.weight = Decimal('40')
        self.criterion.save(update_fields=['weight'])
        for criterion, score in [(self.criterion, 80)]:
            PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
                invitation=invitation, candidate=self.candidate, criterion=criterion, score=score)
        for name, weight, score in [('Delivery', 35, 60), ('Questions', 25, 100)]:
            criterion = PanelCriterion.objects.create(organization_id=self.org.id, created_by=self.owner,
                hearing=self.hearing, name=name, weight=weight)
            self.panel.criteria.add(criterion)
            PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
                invitation=invitation, candidate=self.candidate, criterion=criterion, score=score)
        self.assertEqual(panel_live_activity(self.hearing)['candidates'][0]['score'], '78.00')

    def test_live_activity_tied_ranks_and_partial_panel_share(self):
        # Two panels: Panel 1 has 50% share, Panel 2 has 50% share
        panel2 = PanelScoringPanel.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, name='Second panel', share=Decimal('50.00'))
        self.panel.share = Decimal('50.00')
        self.panel.save()
        self.criterion.panels.add(panel2)

        # Second candidate with identical score to test tied rank
        cand2 = PanelCandidate.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=self.hearing, display_name='Candidate Two')

        invitation1 = self.invitation('tie1@example.test')
        # Only panel 1 scores submitted: observed_share is 0.5 != 1
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=invitation1, candidate=self.candidate, criterion=self.criterion, score=Decimal('85.00'))
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=invitation1, candidate=cand2, criterion=self.criterion, score=Decimal('85.00'))

        # Exercise observed_share != 1 when partial panels respond
        partial = panel_live_activity(self.hearing)
        self.assertEqual(partial['candidates'][0]['score'], '85.00')

        # Add panel 2 scores so completion condition is met and ranks are evaluated
        invitation2 = issue_panel_invitation(self.hearing, 'panel2@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=panel2)[0]
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=invitation2, candidate=self.candidate, criterion=self.criterion, score=Decimal('85.00'))
        PanelScore.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=invitation2, candidate=cand2, criterion=self.criterion, score=Decimal('85.00'))

        activity = panel_live_activity(self.hearing)
        candidates = activity['candidates']
        self.assertEqual(candidates[0]['score'], '85.00')
        self.assertEqual(candidates[1]['score'], '85.00')
        # Both tied candidates receive rank 1
        self.assertEqual(candidates[0]['rank'], 1)
        self.assertEqual(candidates[1]['rank'], 1)

    def test_live_activity_voting_and_review_workflows(self):
        voting_hearing = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Review and vote hearing', status=PanelHearing.Status.OPEN,
            workflow=PanelHearing.Workflow.REVIEW_VOTE)
        candidate = PanelCandidate.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, display_name='Voting candidate')
        panel = PanelScoringPanel.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, name='Committee', minimum_examiners=2)
        criterion = PanelCriterion.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, name='Evaluation')
        panel.criteria.add(criterion)

        examiner1 = issue_panel_invitation(voting_hearing, 'examiner1@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=panel)[0]
        examiner2 = issue_panel_invitation(voting_hearing, 'examiner2@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=panel)[0]
        conflicted = issue_panel_invitation(voting_hearing, 'conflict@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=panel)[0]
        PanelConflict.objects.create(organization_id=self.org.id, created_by=self.owner,
            invitation=conflicted, candidate=candidate, reason='Conflict')

        # Examiner 1 votes and writes review
        PanelVote.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, candidate=candidate, invitation=examiner1,
            choice=PanelVote.Choice.SUPPORT)
        PanelWrittenReview.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, candidate=candidate, invitation=examiner1,
            report='Strong proposal')

        # Examiner 2 abstains
        PanelAbstention.objects.create(organization_id=self.org.id, created_by=self.owner,
            candidate=candidate, invitation=examiner2, criterion=criterion, reason='Recused')

        # Conflicted examiner vote and review are ignored
        PanelVote.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, candidate=candidate, invitation=conflicted,
            choice=PanelVote.Choice.OPPOSE)
        PanelWrittenReview.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=voting_hearing, candidate=candidate, invitation=conflicted,
            report='Biased review')

        activity = panel_live_activity(voting_hearing)
        self.assertFalse(activity['scoring'])
        self.assertEqual(len(activity['candidates']), 1)
        row = activity['candidates'][0]
        self.assertIsNone(row['rank'])
        self.assertEqual(row['progress_unit'], 'response items')
        self.assertGreater(row['responses'], 0)

        # Vote-only workflow branch
        vote_only = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Vote only', status=PanelHearing.Status.OPEN,
            workflow=PanelHearing.Workflow.VOTE)
        cand_v = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=vote_only, display_name='Vote candidate')
        p_v = PanelScoringPanel.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=vote_only, name='VPanel', minimum_examiners=1)
        inv_v = issue_panel_invitation(vote_only, 'v@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=p_v)[0]
        PanelVote.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=vote_only, candidate=cand_v, invitation=inv_v, choice=PanelVote.Choice.SUPPORT)
        act_v = panel_live_activity(vote_only)
        self.assertFalse(act_v['scoring'])

        # Review-only workflow branch
        review_only = PanelHearing.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Review only', status=PanelHearing.Status.OPEN,
            workflow=PanelHearing.Workflow.REVIEW)
        cand_r = PanelCandidate.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=review_only, display_name='Review candidate')
        p_r = PanelScoringPanel.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=review_only, name='RPanel', minimum_examiners=1)
        inv_r = issue_panel_invitation(review_only, 'r@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=p_r)[0]
        PanelWrittenReview.objects.create(organization_id=self.org.id, created_by=self.owner,
            hearing=review_only, candidate=cand_r, invitation=inv_r, report='Notes')
        act_r = panel_live_activity(review_only)
        self.assertFalse(act_r['scoring'])
