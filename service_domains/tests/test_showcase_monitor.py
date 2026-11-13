from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from service_domains.models import (
    ShowcaseCategory, ShowcaseEvent, ShowcaseExhibit, ShowcaseJudgeCriterion,
    ShowcaseRating,
)
from service_domains.operations import (
    issue_showcase_judge_invitation, replace_showcase_invited_judge_assignments,
    replace_showcase_judge_criteria, submit_showcase_judge_evaluation,
)
from service_domains.showcase_monitor import showcase_live_activity

from service_domains.tests.stage_helpers import put_on_stage

class ShowcaseLiveMonitorTests(TestCase):
    def setUp(self):
        self.org = make_org('Showcase monitor org')
        self.owner = make_user('showcase-monitor-owner')
        self.other = make_user('showcase-monitor-other')
        self.event = ShowcaseEvent.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            name='Open day', status=ShowcaseEvent.Status.DRAFT,
        )
        self.category = ShowcaseCategory.objects.create(
            event=self.event, organization_id=self.org.id, created_by=self.owner,
            name='Engineering',
        )
        self.exhibit = ShowcaseExhibit.objects.create(
            event=self.event, category=self.category, organization_id=self.org.id,
            created_by=self.owner, name='Solar pump',
        )

    def test_monitor_returns_distinct_visitor_and_judge_activity_without_identity(self):
        for digest, stars in (('a' * 64, 3), ('b' * 64, 5)):
            ShowcaseRating.objects.create(event=self.event, exhibit=self.exhibit,
                organization_id=self.org.id, created_by=self.owner,
                device_digest=digest, stars=stars)
        invitation, token = issue_showcase_judge_invitation(
            self.event, 'private-judge@example.org', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [self.exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        replace_showcase_judge_criteria(self.event, [{
            'name': 'Evidence', 'description': '', 'weight': Decimal('100'),
            'scale': 'points', 'max_points': Decimal('100'), 'checklist_items': [],
        }], organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        put_on_stage(self.event, self.exhibit)
        evaluation = submit_showcase_judge_evaluation(token, self.exhibit.public_id,
            {str(ShowcaseJudgeCriterion.objects.get().pk): '86'})
        self.assertEqual(evaluation.score, Decimal('86.00'))

        activity = showcase_live_activity(self.event)
        row = activity['rows'][0]
        self.assertEqual(row['audience_count'], 2)
        self.assertEqual(row['audience_mean'], '4.00')
        self.assertEqual(row['judge_assigned_count'], 1)
        self.assertEqual(row['judge_submitted_count'], 1)
        self.assertEqual(row['judge_mean'], '86.00')
        self.assertNotIn('private-judge@example.org', str(activity))
        self.assertNotIn('winner', activity)

    def test_monitor_page_and_json_are_owner_scoped_and_uncacheable(self):
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        self.client.defaults['REMOTE_ADDR'] = '127.0.0.1'
        sign_in(self.client, self.owner, self.org)
        page_url = reverse('showcase_live_monitor', args=(self.event.pk,))
        data_url = reverse('showcase_live_activity', args=(self.event.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            page = self.client.get(page_url)
            data = self.client.get(data_url)
        self.assertRedirects(page, reverse('showcase_live_control', args=(self.event.pk,)),
                             fetch_redirect_response=False)
        self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(data.status_code, 200)
        self.assertEqual(data.json()['rows'][0]['exhibit'], 'Solar pump')
        self.assertIn('no-store', data['Cache-Control'])

        sign_in(self.client, self.other, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            self.assertEqual(self.client.get(page_url).status_code, 404)
            self.assertEqual(self.client.get(data_url).status_code, 404)

    def test_monitor_is_not_available_for_draft_or_another_organization(self):
        sign_in(self.client, self.owner, self.org)
        self.event.status = ShowcaseEvent.Status.DRAFT
        self.event.save(update_fields=('status', 'updated_at'))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            self.assertEqual(self.client.get(reverse(
                'showcase_live_monitor', args=(self.event.pk,))).status_code, 404)

        other_org = make_org('Other Showcase monitor org')
        sign_in(self.client, self.owner, other_org)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            self.assertEqual(self.client.get(reverse(
                'showcase_live_activity', args=(self.event.pk,))).status_code, 404)
