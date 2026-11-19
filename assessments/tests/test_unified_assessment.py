"""Tests for the unified Assessments engine (Peer, Panel, Both, Polls, & Async V2)."""

from decimal import Decimal
import hashlib
import json
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (
    AssessmentSession,
    Evaluation,
    EvaluationScore,
    Evaluator,
    GroupMembership,
    PanelExaminer,
    PanelScoreValue,
    PanelScorecard,
    PresentationGroup,
    PresentationTurn,
    ResultPublication,
    ReviewAssignment,
    RubricCategory,
    SessionAuditEvent,
    Student,
)
from assessments.unified_operations import (
    aggregate_panel_scores,
    commit_allocations,
    compute_session_readiness,
    freeze_session_submissions,
    preview_allocations,
    publish_session_snapshot,
    record_turn_moderation,
    recuse_assignment,
    submit_assignment_review,
    submit_entry_work,
    unified_group_outcome,
)
from portal.services import SLUGS


class UnifiedAssessmentEngineTests(TestCase):
    def setUp(self):
        self.org = make_org('Unified Org')
        self.user = make_user('organizer')
        sign_in(self.client, self.user, self.org)

    def test_single_assessment_entry_and_polls_accessible(self):
        self.assertIn('assessment', SLUGS)
        self.assertNotIn('classroom', SLUGS)
        self.assertNotIn('panel', SLUGS)
        self.assertNotIn('coassessment', SLUGS)

        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Unified Live Session',
            status=AssessmentSession.Status.LIVE,
            enable_peer=True,
            enable_panel=True,
            peer_weight=Decimal('30.00'),
            panel_weight=Decimal('70.00'),
        )
        # Dashboard & detail & polls are all reachable under /assessment/app/
        self.assertEqual(self.client.get(reverse('assessment_dashboard')).status_code, 200)
        detail = self.client.get(reverse('assessment_session_detail', args=[s.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, reverse('assessment_polls', args=[s.pk]))
        self.assertEqual(self.client.get(reverse('assessment_polls', args=[s.pk])).status_code, 200)

    def test_peer_only_mode_skips_panel_examiners(self):
        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Peer Only',
            enable_peer=True,
            enable_panel=False,
            peer_weight=Decimal('100.00'),
            panel_weight=Decimal('0.00'),
        )
        self.assertEqual(s.assessment_mode_label, 'Peer only')
        resp = self.client.get(reverse('assessment_panel_examiners', args=[s.pk]))
        self.assertRedirects(resp, reverse('assessment_session_detail', args=[s.pk]), fetch_redirect_response=False)

    def test_panel_aggregations_and_live_peer_plus_panel_blending_with_moderation(self):
        self.assertEqual(aggregate_panel_scores([70, 80, 90], 'mean'), Decimal('80.0000'))
        self.assertEqual(aggregate_panel_scores([70, 80, 95], 'median'), Decimal('80.0000'))
        self.assertEqual(
            aggregate_panel_scores([10, 80, 82, 84, 100], 'trimmed_mean', Decimal('0.200')),
            Decimal('82.0000'),
        )

        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Combined Live',
            status=AssessmentSession.Status.LIVE,
            enable_peer=True,
            enable_panel=True,
            peer_weight=Decimal('30.00'),
            panel_weight=Decimal('70.00'),
            moderation_threshold=Decimal('10.00'),
            panel_enable_vote=True,
        )
        cat = RubricCategory.objects.create(
            assessment_session=s, name='Delivery', weight=Decimal('100.00'), order=1,
        )
        g1 = PresentationGroup.objects.create(assessment_session=s, name='Team A', order=1)
        g2 = PresentationGroup.objects.create(assessment_session=s, name='Team B', order=2)
        p_a = Student.objects.create(assessment_session=s, full_name='Alice')
        p_b = Student.objects.create(assessment_session=s, full_name='Bob')
        GroupMembership.objects.create(group=g1, student=p_a)
        GroupMembership.objects.create(group=g2, student=p_b)

        turn = PresentationTurn.objects.create(assessment_session=s, group=g1, status=PresentationTurn.Status.ACTIVE)
        ev_b = Evaluator.objects.get(assessment_session=s, student=p_b)
        peer_eval = Evaluation.objects.create(presentation_turn=turn, evaluator=p_b, evaluator_record=ev_b)
        EvaluationScore.objects.create(evaluation=peer_eval, rubric_category=cat, value=Decimal('60.00'), max_value=Decimal('100.00'))

        # Add panelist via view and submit scorecard + vote via examiner_portal
        self.client.post(reverse('assessment_panel_examiners', args=[s.pk]), {
            'display_name': 'Dr. Chair',
            'email': 'chair@example.com',
            'is_chair': 'on',
        })
        examiner = PanelExaminer.objects.get(session=s)
        portal_url = reverse('assessment_examiner_portal', args=[examiner.invite_token])
        self.assertEqual(self.client.get(portal_url).status_code, 200)
        self.client.post(portal_url, {
            f'category_{cat.pk}': '90.00',
            'qualitative_feedback': 'Strong architecture.',
            'choice': 'pass',
        })
        self.assertTrue(PanelScorecard.objects.filter(turn=turn, examiner=examiner).exists())

        outcome = unified_group_outcome(g1, s)
        self.assertEqual(outcome['peer_score'], Decimal('60.00'))
        self.assertEqual(outcome['panel_score'], Decimal('90.00'))
        self.assertEqual(outcome['discrepancy'], Decimal('30.00'))
        self.assertTrue(outcome['moderation_required'])
        # 30% * 60 + 70% * 90 = 18 + 63 = 81.00
        self.assertEqual(outcome['combined_score'], Decimal('81.00'))

        # Chair moderation override + immutable snapshot
        record_turn_moderation(turn, 'adjusted', Decimal('85.00'), 'Panel calibrated.', self.user)
        moderated_outcome = unified_group_outcome(g1, s)
        self.assertEqual(moderated_outcome['effective_score'], Decimal('85.00'))

        pub = publish_session_snapshot(s, self.user, [moderated_outcome])
        self.assertEqual(pub.revision, 1)
        self.assertEqual(ResultPublication.objects.filter(session=s).count(), 1)

    def test_async_submission_allocation_draft_conflict_review_and_publication(self):
        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Async Capstone',
            status=AssessmentSession.Status.SUBMISSIONS_OPEN,
            enable_peer=True,
            enable_panel=True,
            peer_weight=Decimal('30.00'),
            panel_weight=Decimal('70.00'),
            delivery_mode=AssessmentSession.DeliveryMode.ASYNC_SUBMISSION,
            minimum_peer_evaluators=1,
            minimum_panel_evaluators=1,
        )
        cat = RubricCategory.objects.create(
            assessment_session=s, name='Design', weight=Decimal('100.00'), order=1,
        )
        g1 = PresentationGroup.objects.create(assessment_session=s, name='Team One', order=1)
        g2 = PresentationGroup.objects.create(assessment_session=s, name='Team Two', order=2)
        p1 = Student.objects.create(assessment_session=s, full_name='P1')
        p2 = Student.objects.create(assessment_session=s, full_name='P2')
        GroupMembership.objects.create(group=g1, student=p1)
        GroupMembership.objects.create(group=g2, student=p2)
        PanelExaminer.objects.create(
            session=s, display_name='Dr. K', email='k@example.com', token_digest='tok_k_digest',
        )

        readiness = compute_session_readiness(s)
        self.assertTrue(readiness['ready'])

        v1 = submit_entry_work(s, g1, p1, 'Project One', 'Full report text')
        v2 = submit_entry_work(s, g2, p2, 'Project Two', '', 'https://example.com/deck')
        self.assertEqual(freeze_session_submissions(s, self.user), 2)

        preview = preview_allocations(s, seed=7)
        self.assertTrue(preview['feasible'])
        self.assertEqual(commit_allocations(s, self.user, seed=7), 4)

        peer_g1 = ReviewAssignment.objects.get(session=s, track='peer', submission_version=v1)
        self.assertEqual(peer_g1.peer_evaluator_id, p2.pk)

        draft_url = reverse('assessment_assignment_draft', args=[peer_g1.pk])
        ok = self.client.post(
            draft_url,
            data=json.dumps({'row_version': 1, 'payload': {'cat': '80'}}),
            content_type='application/json',
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()['row_version'], 2)

        stale = self.client.post(
            draft_url,
            data=json.dumps({'row_version': 1, 'payload': {'cat': '90'}}),
            content_type='application/json',
        )
        self.assertEqual(stale.status_code, 409)

        submitted = submit_assignment_review(peer_g1, {cat.pk: '88.00'}, 'Solid design.')
        self.assertEqual(submitted.status, ReviewAssignment.Status.SUBMITTED)
        self.assertEqual(submitted.total_score, Decimal('88.0000'))

        panel_g2 = ReviewAssignment.objects.get(session=s, track='panel', submission_version=v2)
        recused = recuse_assignment(panel_g2, 'Co-author conflict', actor=self.user)
        self.assertEqual(recused.status, ReviewAssignment.Status.RECUSED)
        self.assertTrue(SessionAuditEvent.objects.filter(session=s, action='assignment.recused').exists())

    def test_create_form_renders_mode_toggles_and_go_live_branches_for_async(self):
        new_resp = self.client.get(reverse('assessment_session_create'))
        self.assertEqual(new_resp.status_code, 200)
        self.assertContains(new_resp, 'name="enable_peer"')
        self.assertContains(new_resp, 'name="enable_panel"')
        self.assertContains(new_resp, 'name="delivery_mode"')
        self.assertNotContains(new_resp, 'Identify students by')

        # Create a Both (Peer + Panel) Async session via the form
        create_resp = self.client.post(reverse('assessment_session_create'), {
            'name': 'Unified Wizard Test',
            'enable_peer': 'on',
            'enable_panel': 'on',
            'delivery_mode': 'async_submission',
            'entry_mode': 'group',
            'peer_weight': '30.00',
            'panel_weight': '70.00',
            'minimum_peer_evaluators': '1',
            'minimum_panel_evaluators': '1',
            'moderation_threshold': '15.00',
            'panel_aggregation': 'mean',
            'identify_by': 'full_name',
            'ungraded_penalty': '0.00',
        })
        created = AssessmentSession.objects.get(name='Unified Wizard Test')
        self.assertEqual(create_resp.status_code, 302)
        self.assertTrue(created.enable_peer)
        self.assertTrue(created.enable_panel)
        self.assertEqual(created.delivery_mode, AssessmentSession.DeliveryMode.ASYNC_SUBMISSION)

        setup_resp = self.client.get(reverse('assessment_setup', args=[created.pk]))
        self.assertEqual(setup_resp.status_code, 200)
        self.assertContains(setup_resp, 'data-step="panel"')
        self.assertContains(setup_resp, 'data-step="go-live"')

        # Add rubric, group, participant, and panelist so readiness passes
        RubricCategory.objects.create(
            assessment_session=created, name='Quality', weight=Decimal('100.00'), order=1,
        )
        g1 = PresentationGroup.objects.create(assessment_session=created, name='Entry 1', order=1)
        g2 = PresentationGroup.objects.create(assessment_session=created, name='Entry 2', order=2)
        p1 = Student.objects.create(assessment_session=created, full_name='Participant 1')
        p2 = Student.objects.create(assessment_session=created, full_name='Participant 2')
        GroupMembership.objects.create(group=g1, student=p1)
        GroupMembership.objects.create(group=g2, student=p2)
        PanelExaminer.objects.create(
            session=created, display_name='Dr. Panel', email='p@example.com', token_digest='tok_p',
        )

        launch_resp = self.client.post(reverse('assessment_go_live', args=[created.pk]))
        self.assertRedirects(launch_resp, reverse('assessment_async_console', args=[created.pk]))
        created.refresh_from_db()
        self.assertEqual(created.status, AssessmentSession.Status.SUBMISSIONS_OPEN)

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        CELERY_TASK_ALWAYS_EAGER=True,
    )
    def test_panel_examiner_invitation_sends_email_using_panel_template_and_supports_resend(self):
        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Panel Email Session',
            enable_peer=False,
            enable_panel=True,
            peer_weight=Decimal('0.00'),
            panel_weight=Decimal('100.00'),
        )
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('assessment_panel_examiners', args=[s.pk]), {
                'next': 'setup',
                'display_name': 'Dr. Grace',
                'email': 'grace@example.com',
                'is_chair': 'on',
            })
        self.assertRedirects(resp, f"{reverse('assessment_setup', args=[s.pk])}?step=panel")
        examiner = PanelExaminer.objects.get(session=s, email='grace@example.com')
        self.assertEqual(examiner.delivery_status, PanelExaminer.DeliveryStatus.SENT)
        self.assertIsNotNone(examiner.sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('LiveGrade Panel invitation: Panel Email Session', mail.outbox[0].subject)
        self.assertIn(reverse('assessment_examiner_portal', args=[examiner.invite_token]), mail.outbox[0].body)

        # Resend
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('assessment_panel_examiners', args=[s.pk]), {
                'next': 'setup',
                'action': 'resend_examiner',
                'examiner_id': examiner.pk,
            })
        self.assertEqual(len(mail.outbox), 2)

        # Sanitized error message on failure (never raw backend exception)
        examiner.delivery_status = PanelExaminer.DeliveryStatus.FAILED
        examiner.delivery_error = 'mail_backend_error'
        examiner.save(update_fields=['delivery_status', 'delivery_error'])
        self.assertEqual(
            examiner.delivery_error_message,
            'Email delivery failed. Please check mail settings and click Resend.',
        )
        panel_page = self.client.get(reverse('assessment_panel_examiners', args=[s.pk]))
        self.assertContains(panel_page, 'Email delivery failed. Please check mail settings and click Resend.')

        # Celery app discovers the task
        from config.celery import app as celery_app
        celery_app.loader.import_default_modules()
        self.assertIn('assessments.tasks.send_assessment_panel_invitation_email', celery_app.tasks)

    def test_examiner_portal_websocket_and_state_updates(self):
        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Live Panel Session',
            enable_peer=False,
            enable_panel=True,
            peer_weight=Decimal('0.00'),
            panel_weight=Decimal('100.00'),
        )
        RubricCategory.objects.create(
            assessment_session=s, name='Clarity', weight=Decimal('100.00'), order=1,
        )
        g1 = PresentationGroup.objects.create(assessment_session=s, name='Team Alpha', order=1)
        raw_token = 'tok_live_panel_1'
        examiner = PanelExaminer.objects.create(
            session=s,
            display_name='Panel 1',
            email='panel1@example.com',
            invite_token=raw_token,
            token_digest=hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
        )

        portal_url = reverse('assessment_examiner_portal', args=[examiner.invite_token])
        state_url = reverse('assessment_examiner_portal_state', args=[examiner.invite_token])

        # Portal renders WebSocket endpoint, status marker, and fallback state URL
        portal_resp = self.client.get(portal_url)
        self.assertEqual(portal_resp.status_code, 200)
        self.assertContains(portal_resp, f"const sessionUuid = '{s.uuid}';")
        self.assertContains(portal_resp, "'/ws/assessments/' + sessionUuid + '/'")
        self.assertContains(portal_resp, state_url)
        self.assertContains(portal_resp, 'data-session-status')

        # Initial state is draft with no active turn
        state_resp = self.client.get(state_url)
        self.assertEqual(state_resp.status_code, 200)
        self.assertEqual(state_resp.json(), {
            'status': 'draft',
            'status_display': 'Draft',
            'active_turn_id': None,
            'active_group_name': None,
        })

        # Going live broadcasts session.live and updates state
        with patch('assessments.views.lecturer_views.broadcast_session_event') as mock_broadcast:
            go_live_resp = self.client.post(reverse('assessment_go_live', args=[s.pk]))
        self.assertEqual(go_live_resp.status_code, 302)
        mock_broadcast.assert_called_with(s, 'session.live', {'status': AssessmentSession.Status.LIVE})

        state_resp = self.client.get(state_url)
        self.assertEqual(state_resp.json()['status'], 'live')
        self.assertIsNone(state_resp.json()['active_turn_id'])

        # Activating a group updates active_turn_id and active_group_name
        activate_resp = self.client.post(reverse('assessment_activate_group', args=[s.pk, g1.pk]))
        self.assertEqual(activate_resp.status_code, 302)
        active_turn = PresentationTurn.objects.get(assessment_session=s, group=g1)
        state_resp = self.client.get(state_url)
        self.assertEqual(state_resp.json()['status'], 'live')
        self.assertEqual(state_resp.json()['active_turn_id'], active_turn.pk)
        self.assertEqual(state_resp.json()['active_group_name'], 'Team Alpha')

    def test_examiner_portal_sliders_individual_rubric_empty_and_duplicate_guards(self):
        s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Panel Rubric Session',
            status=AssessmentSession.Status.LIVE,
            enable_peer=False,
            enable_panel=True,
            peer_weight=Decimal('0.00'),
            panel_weight=Decimal('100.00'),
        )
        g_cat = RubricCategory.objects.create(
            assessment_session=s,
            name='Technical depth',
            scope=RubricCategory.Scope.GROUP,
            weight=Decimal('60.00'),
            order=1,
        )
        i_cat = RubricCategory.objects.create(
            assessment_session=s,
            name='Teamwork',
            scope=RubricCategory.Scope.INDIVIDUAL,
            weight=Decimal('40.00'),
            order=2,
        )
        g1 = PresentationGroup.objects.create(assessment_session=s, name='Aurora', order=1)
        m1 = Student.objects.create(assessment_session=s, full_name='Alice')
        m2 = Student.objects.create(assessment_session=s, full_name='Bob')
        GroupMembership.objects.create(group=g1, student=m1)
        GroupMembership.objects.create(group=g1, student=m2)
        turn = PresentationTurn.objects.create(
            assessment_session=s,
            group=g1,
            status=PresentationTurn.Status.ACTIVE,
        )
        raw_token = 'tok_rubric_panel_1'
        examiner = PanelExaminer.objects.create(
            session=s,
            display_name='Panel 1',
            email='panel1@example.com',
            invite_token=raw_token,
            token_digest=hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
        )

        portal_url = reverse('assessment_examiner_portal', args=[raw_token])
        page = self.client.get(portal_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-score-slider')
        self.assertContains(page, 'Entry-level rubric')
        self.assertContains(page, 'Individual member rubric')
        self.assertContains(page, f'name="category_{g_cat.pk}"')
        self.assertContains(page, f'name="ind_{m1.pk}_{i_cat.pk}"')
        self.assertContains(page, f'name="ind_{m2.pk}_{i_cat.pk}"')

        # Empty submission is rejected
        empty_resp = self.client.post(portal_url, {
            f'category_{g_cat.pk}': '',
            f'ind_{m1.pk}_{i_cat.pk}': '80',
            f'ind_{m2.pk}_{i_cat.pk}': '85',
        })
        self.assertEqual(empty_resp.status_code, 302)
        self.assertFalse(PanelScorecard.objects.filter(turn=turn, examiner=examiner).exists())

        # Valid submission saves both entry-level and individual scores
        ok_resp = self.client.post(portal_url, {
            f'category_{g_cat.pk}': '88.50',
            f'ind_{m1.pk}_{i_cat.pk}': '90.00',
            f'ind_{m2.pk}_{i_cat.pk}': '82.00',
            'qualitative_feedback': 'Solid demo.',
        })
        self.assertEqual(ok_resp.status_code, 302)
        card = PanelScorecard.objects.get(turn=turn, examiner=examiner)
        self.assertEqual(
            PanelScoreValue.objects.get(scorecard=card, category=g_cat, target_student__isnull=True).score,
            Decimal('88.50'),
        )
        self.assertEqual(
            PanelScoreValue.objects.get(scorecard=card, category=i_cat, target_student=m1).score,
            Decimal('90.00'),
        )
        self.assertEqual(
            PanelScoreValue.objects.get(scorecard=card, category=i_cat, target_student=m2).score,
            Decimal('82.00'),
        )

        # GET after submission shows already-evaluated state without the form
        done_page = self.client.get(portal_url)
        self.assertContains(done_page, 'You have already evaluated Aurora')
        self.assertNotContains(done_page, 'id="panel-score-form"')

        # Duplicate POST is rejected and does not overwrite
        dup_resp = self.client.post(portal_url, {
            f'category_{g_cat.pk}': '10.00',
            f'ind_{m1.pk}_{i_cat.pk}': '10.00',
            f'ind_{m2.pk}_{i_cat.pk}': '10.00',
        })
        self.assertEqual(dup_resp.status_code, 302)
        self.assertEqual(PanelScorecard.objects.filter(turn=turn, examiner=examiner).count(), 1)
        self.assertEqual(
            PanelScoreValue.objects.get(scorecard=card, category=g_cat, target_student__isnull=True).score,
            Decimal('88.50'),
        )

    def test_live_control_and_tabs_respond_to_session_modes(self):
        # 1. Panel-only live session hides peer-only cards/tabs and shows Panelists card
        panel_only = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Panel Only Live',
            status=AssessmentSession.Status.LIVE,
            enable_peer=False,
            enable_panel=True,
            peer_weight=Decimal('0.00'),
            panel_weight=Decimal('100.00'),
        )
        PresentationGroup.objects.create(assessment_session=panel_only, name='Entry A', order=1)
        PanelExaminer.objects.create(
            session=panel_only, display_name='Dr. P', email='p@example.com', token_digest='tok_mode_p',
        )
        resp = self.client.get(reverse('assessment_session_detail', args=[panel_only.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="live-panelists"')
        self.assertContains(resp, 'Manage panelists')
        self.assertNotContains(resp, 'Invite participants')
        self.assertNotContains(resp, 'Joined students')
        self.assertNotContains(resp, 'id="joining-control"')
        self.assertNotContains(resp, reverse('assessment_polls', args=[panel_only.pk]))
        self.assertNotContains(resp, reverse('assessment_participation', args=[panel_only.pk]))
        self.assertNotContains(resp, 'Moderation &amp; Publish')

        # 2. Both (Peer + Panel) live session shows both peer and panel controls + public scores
        both = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Both Live',
            status=AssessmentSession.Status.LIVE,
            enable_peer=True,
            enable_panel=True,
            peer_weight=Decimal('40.00'),
            panel_weight=Decimal('60.00'),
        )
        g_both = PresentationGroup.objects.create(assessment_session=both, name='Entry B', order=1)
        t_both = PresentationTurn.objects.create(
            assessment_session=both, group=g_both, status=PresentationTurn.Status.CLOSED,
        )
        resp_both = self.client.get(reverse('assessment_session_detail', args=[both.pk]))
        self.assertEqual(resp_both.status_code, 200)
        self.assertContains(resp_both, 'Invite participants')
        self.assertContains(resp_both, 'id="live-panelists"')
        self.assertContains(resp_both, 'Show public scores')
        self.assertContains(resp_both, reverse('assessment_polls', args=[both.pk]))
        self.assertContains(resp_both, reverse('assessment_participation', args=[both.pk]))

        # 3. Async session redirects session_detail to async_console and hides Live control tab
        async_s = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Async Session',
            delivery_mode=AssessmentSession.DeliveryMode.ASYNC_SUBMISSION,
            status=AssessmentSession.Status.LIVE,
            enable_peer=True,
            enable_panel=True,
            peer_weight=Decimal('50.00'),
            panel_weight=Decimal('50.00'),
        )
        redir_async = self.client.get(reverse('assessment_session_detail', args=[async_s.pk]))
        self.assertRedirects(redir_async, reverse('assessment_async_console', args=[async_s.pk]))
        resp_async = self.client.get(reverse('assessment_async_console', args=[async_s.pk]))
        self.assertEqual(resp_async.status_code, 200)
        self.assertNotContains(resp_async, 'Live control')
        self.assertContains(resp_async, 'Close session')
        self.assertContains(resp_async, reverse('assessment_setup', args=[async_s.pk]))

        # 4. Results hosts Moderation & Publish directly (and no Moderation & Publish tab)
        results_url = reverse('assessment_results', args=[both.pk])
        results_resp = self.client.get(results_url)
        self.assertEqual(results_resp.status_code, 200)
        self.assertNotContains(results_resp, 'Moderation &amp; Publish')
        self.assertContains(results_resp, 'Publish snapshot')
        self.assertContains(results_resp, f'moderateTurnModal-{t_both.pk}')

        mod_resp = self.client.post(results_url, {
            'action': 'moderate_turn',
            'turn_id': t_both.pk,
            'mod_action': 'adjusted',
            'final_score': '88.00',
            'justification': 'Panel consensus adjustment.',
        })
        self.assertRedirects(mod_resp, results_url)
        pub_resp = self.client.post(results_url, {'action': 'publish_snapshot'})
        self.assertRedirects(pub_resp, results_url)
        self.assertEqual(ResultPublication.objects.filter(session=both).count(), 1)

    def test_live_rankings_respond_to_peer_panel_and_both_modes(self):
        # 1. Panel-only rankings show Panelists header and panel score %
        p_sess = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Panel Rankings',
            status=AssessmentSession.Status.LIVE,
            enable_peer=False,
            enable_panel=True,
            peer_weight=Decimal('0.00'),
            panel_weight=Decimal('100.00'),
        )
        p_cat = RubricCategory.objects.create(
            assessment_session=p_sess, name='Depth', scope=RubricCategory.Scope.GROUP, weight=Decimal('100.00'),
        )
        g_p = PresentationGroup.objects.create(assessment_session=p_sess, name='Aurora', order=1)
        g_p2 = PresentationGroup.objects.create(assessment_session=p_sess, name='Meridian', order=2)
        t_p = PresentationTurn.objects.create(
            assessment_session=p_sess, group=g_p, status=PresentationTurn.Status.ACTIVE,
        )
        ex = PanelExaminer.objects.create(
            session=p_sess, display_name='Dr. P', email='p@example.com', token_digest='tok_rank_p',
        )
        card = PanelScorecard.objects.create(turn=t_p, examiner=ex)
        PanelScoreValue.objects.create(scorecard=card, category=p_cat, score=Decimal('90.00'))

        p_resp = self.client.get(reverse('assessment_session_detail', args=[p_sess.pk]))
        self.assertContains(p_resp, 'Panelists')
        self.assertContains(p_resp, '90%')
        self.assertContains(p_resp, 'no scores yet')
        self.assertContains(p_resp, 'Dr. P scored Aurora')
        self.assertContains(p_resp, 'Panel evaluation')

        # 2. Both (Peer + Panel) rankings show Graders header and Peer + Panel breakdown
        b_sess = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Both Rankings',
            status=AssessmentSession.Status.LIVE,
            enable_peer=True,
            enable_panel=True,
            peer_weight=Decimal('40.00'),
            panel_weight=Decimal('60.00'),
        )
        b_cat = RubricCategory.objects.create(
            assessment_session=b_sess, name='Quality', scope=RubricCategory.Scope.GROUP, weight=Decimal('100.00'),
        )
        g_b = PresentationGroup.objects.create(assessment_session=b_sess, name='Horizon', order=1)
        s_b = Student.objects.create(assessment_session=b_sess, full_name='Peer 1')
        t_b = PresentationTurn.objects.create(
            assessment_session=b_sess, group=g_b, status=PresentationTurn.Status.ACTIVE,
        )
        ev_rec = Evaluator.objects.get(assessment_session=b_sess, student=s_b)
        ev = Evaluation.objects.create(
            presentation_turn=t_b, evaluator=s_b, evaluator_record=ev_rec,
        )
        EvaluationScore.objects.create(
            evaluation=ev, rubric_category=b_cat, value=Decimal('50.00'), max_value=Decimal('100.00'),
        )
        ex_b = PanelExaminer.objects.create(
            session=b_sess, display_name='Dr. B', email='b@example.com', token_digest='tok_rank_b',
        )
        card_b = PanelScorecard.objects.create(turn=t_b, examiner=ex_b)
        PanelScoreValue.objects.create(scorecard=card_b, category=b_cat, score=Decimal('100.00'))

        b_resp = self.client.get(reverse('assessment_session_detail', args=[b_sess.pk]))
        self.assertContains(b_resp, 'Graders')
        # 40% * 50 + 60% * 100 = 80%
        self.assertContains(b_resp, '80%')
        self.assertContains(b_resp, 'Peer 50.00%')
        self.assertContains(b_resp, 'Panel 100.00%')
        self.assertContains(b_resp, 'Dr. B scored Horizon')

        # Closing turn in Both mode logs peer + panel counts in Live activity
        from django.utils import timezone
        t_b.status = PresentationTurn.Status.CLOSED
        t_b.closed_at = timezone.now()
        t_b.save(update_fields=['status', 'closed_at'])
        b_closed_resp = self.client.get(reverse('assessment_session_detail', args=[b_sess.pk]))
        self.assertContains(b_closed_resp, 'Horizon finished presenting')
        self.assertContains(b_closed_resp, '1 peer vote')
        self.assertContains(b_closed_resp, '1 panelist')

        # 3. Peer-only keeps Graded by and no votes yet
        peer_sess = AssessmentSession.objects.create(
            organization_id=self.org.pk,
            created_by=self.user,
            service='assessment',
            name='Peer Only Rankings',
            status=AssessmentSession.Status.LIVE,
            enable_peer=True,
            enable_panel=False,
            peer_weight=Decimal('100.00'),
            panel_weight=Decimal('0.00'),
        )
        PresentationGroup.objects.create(assessment_session=peer_sess, name='Solo', order=1)
        peer_resp = self.client.get(reverse('assessment_session_detail', args=[peer_sess.pk]))
        self.assertContains(peer_resp, 'Graded by')
        self.assertContains(peer_resp, 'no votes yet')

    def test_setup_return_link_respects_source_tab_and_session_mode(self):
        # Draft session falls back to All sessions
        draft_s = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.user, service='assessment',
            name='Draft Setup', status=AssessmentSession.Status.DRAFT,
        )
        draft_resp = self.client.get(reverse('assessment_setup', args=[draft_s.pk]))
        self.assertContains(draft_resp, 'All sessions')
        self.assertNotContains(draft_resp, 'Back to live control')

        # Live session defaults to Back to live control, or honours ?from=results
        live_s = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.user, service='assessment',
            name='Live Setup', status=AssessmentSession.Status.LIVE,
        )
        live_resp = self.client.get(reverse('assessment_setup', args=[live_s.pk]))
        self.assertContains(live_resp, 'Back to live control')
        self.assertContains(live_resp, reverse('assessment_session_detail', args=[live_s.pk]))

        from_results = self.client.get(f"{reverse('assessment_setup', args=[live_s.pk])}?from=results")
        self.assertContains(from_results, 'Back to results')
        self.assertContains(from_results, reverse('assessment_results', args=[live_s.pk]))

        # Async active session defaults to Back to Submissions & Allocation
        async_s = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.user, service='assessment',
            name='Async Setup', delivery_mode=AssessmentSession.DeliveryMode.ASYNC_SUBMISSION,
            status=AssessmentSession.Status.SUBMISSIONS_OPEN,
        )
        async_resp = self.client.get(reverse('assessment_setup', args=[async_s.pk]))
        self.assertContains(async_resp, 'Back to Submissions &amp; Allocation')
        self.assertContains(async_resp, reverse('assessment_async_console', args=[async_s.pk]))
