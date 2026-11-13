from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.models import PanelAuditEvent, PanelHearing, PanelInvitation, PanelScoringPanel
from service_domains.operations import apply_panel_invitation_import
from service_domains.operations import (mark_panel_invitation_enqueue_failed,
    queue_panel_invitation, request_panel_invitation_resend, panel_invitation_resend_available)
from service_domains.panel_invitation_import import (TOO_MANY_PHYSICAL_ROWS,
    parse_examiner_emails, plan_examiner_import, sample_workbook)
from service_domains.views import PANEL_INVITATION_IMPORT_TOKEN_SALT
from django.core import signing


class PanelInvitationImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Panel invitation import org')
        self.other_org = make_org('Other invitation import org')
        self.owner = make_user('panel-invitation-import-owner')
        self.other = make_user('panel-invitation-import-other')
        self.hearing = PanelHearing.objects.create(organization_id=self.org.pk,
            created_by=self.owner, name='Draft examiner import')
        self.panel = PanelScoringPanel.objects.create(hearing=self.hearing,
            organization_id=self.org.pk, created_by=self.owner, name='Oral panel', share=100)

    def upload(self, headers, rows, filename='examiners.xlsx'):
        stream = workbook_bytes(headers, rows)
        return SimpleUploadedFile(filename, stream.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_sample_parser_and_preview_detect_invalid_duplicate_and_existing_emails(self):
        self.assertEqual(len(parse_examiner_emails(sample_workbook())['rows']), 1)
        parsed = parse_examiner_emails(self.upload(['Email'], [
            ['good@example.test'], ['GOOD@example.test'], ['bad'], [''],
        ]))
        plan = plan_examiner_import(self.hearing, parsed)
        self.assertFalse(plan['ok'])
        messages = '\n'.join(message for _, message in plan['errors'])
        self.assertIn('repeated in rows', messages)
        self.assertIn('valid examiner email', messages)
        PanelInvitation.objects.create(hearing=self.hearing, panel=self.panel,
            organization_id=self.org.pk, created_by=self.owner, email='active@example.test',
            expires_at=timezone.now() + timedelta(hours=1), delivery_status='sent')
        active = plan_examiner_import(self.hearing, parse_examiner_emails(
            self.upload(['Email'], [['ACTIVE@example.test']])))
        self.assertFalse(active['ok'])
        self.assertIn('active invitation', active['errors'][0][1])

    def test_parser_rejects_bad_sheet_headers_and_oversized_upload(self):
        self.assertIsNotNone(parse_examiner_emails(SimpleUploadedFile('broken.xlsx', b'broken'))['fatal'])
        self.assertIn('Email column', parse_examiner_emails(
            self.upload(['Name'], [['Examiner']]))['fatal'])
        too_big = SimpleUploadedFile('examiners.xlsx', b'x' * (10 * 1024 * 1024 + 1))
        from service_domains.forms import PanelInvitationImportForm
        form = PanelInvitationImportForm({'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()},
            {'file': too_big}, panels=PanelScoringPanel.objects.filter(pk=self.panel.pk))
        self.assertFalse(form.is_valid())
        wrong_extension = PanelInvitationImportForm({
            'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()},
            {'file': SimpleUploadedFile('examiners.csv', b'data')},
            panels=PanelScoringPanel.objects.filter(pk=self.panel.pk))
        self.assertFalse(wrong_extension.is_valid())
        self.assertIn('xlsx', str(wrong_extension.errors))

    def test_parser_covers_empty_limits_blank_rows_long_addresses_and_physical_limit(self):
        with patch('service_domains.panel_invitation_import._read_bounded_rows', return_value=[]):
            self.assertIn('file is empty', parse_examiner_emails(self.upload(['Email'], []))['fatal'])
        too_many = self.upload(['Email'], [[f'e{number}@example.test']
            for number in range(501)])
        self.assertIn('more than 500', parse_examiner_emails(too_many)['fatal'])
        with patch('service_domains.panel_invitation_import._read_bounded_rows',
                   return_value=TOO_MANY_PHYSICAL_ROWS):
            result = parse_examiner_emails(self.upload(['Email'], []))
        self.assertIn('physical row limit', result['fatal'])
        blank = self.upload(['Email'], [['  ']])
        self.assertIn('no examiner email rows', parse_examiner_emails(blank)['fatal'])
        too_long = 'a' * 250 + '@x.test'
        parsed = parse_examiner_emails(self.upload(['Email'], [[''], [too_long]]))
        self.assertIn('longer than 254', parsed['errors'][0][1])
        self.assertEqual(plan_examiner_import(self.hearing, {'fatal': 'bad', 'rows': [], 'errors': []})[
            'ok'], False)
        oversized = SimpleUploadedFile('large.xlsx', b'x' * (10 * 1024 * 1024 + 1))
        self.assertIn('10 MB', parse_examiner_emails(oversized)['fatal'])

    def test_apply_queues_only_digests_and_audits_each_invitation(self):
        parsed = parse_examiner_emails(self.upload(['Email'], [['one@example.test'], ['two@example.test']]))
        created = apply_panel_invitation_import(self.hearing, parsed,
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(row.token_digest is None and row.delivery_status == 'pending'
                            for row in created))
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.INVITATION_ISSUED).count(), 2)
        with self.assertRaises(PermissionDenied):
            apply_panel_invitation_import(self.hearing, parsed,
                timezone.now() + timedelta(hours=2), organization=self.other_org.as_claim(),
                user=self.other, panel=self.panel)
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(self.hearing, parsed,
                timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
                user=self.owner, panel=self.panel)

    def test_single_invite_and_resend_rotate_token_and_record_resend(self):
        invitation = queue_panel_invitation(self.hearing, 'resend@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        invitation.delivery_status = PanelInvitation.DeliveryStatus.FAILED
        invitation.token_digest = PanelInvitation.digest_token('old-bearer')
        invitation.save(update_fields=('delivery_status', 'token_digest', 'updated_at'))
        resent = request_panel_invitation_resend(self.hearing, invitation,
            organization=self.org.as_claim(), user=self.owner)
        self.assertIsNone(resent.token_digest)
        self.assertEqual(resent.delivery_status, PanelInvitation.DeliveryStatus.PENDING)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.INVITATION_RESENT).count(), 1)

    def test_queue_resend_and_failure_helpers_fail_closed_and_cover_all_delivery_states(self):
        with self.assertRaises(PermissionDenied):
            queue_panel_invitation(self.hearing, 'x@example.test', timezone.now() + timedelta(hours=1),
                organization=self.other_org.as_claim(), user=self.other, panel=self.panel)
        foreign_panel_hearing = PanelHearing.objects.create(organization_id=self.org.pk,
            created_by=self.owner, name='Other hearing')
        foreign_panel = PanelScoringPanel.objects.create(hearing=foreign_panel_hearing,
            organization_id=self.org.pk, created_by=self.owner, name='Other panel')
        with self.assertRaises(PermissionDenied):
            queue_panel_invitation(self.hearing, 'x@example.test', timezone.now() + timedelta(hours=1),
                organization=self.org.as_claim(), user=self.owner, panel=foreign_panel)
        invitation = queue_panel_invitation(self.hearing, 'queue@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner,
            panel=self.panel)
        self.assertTrue(panel_invitation_resend_available(invitation) is False)
        self.assertFalse(mark_panel_invitation_enqueue_failed(-1))
        self.assertTrue(mark_panel_invitation_enqueue_failed(invitation.pk))
        self.assertFalse(mark_panel_invitation_enqueue_failed(invitation.pk))
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.FAILED)
        for status in (PanelInvitation.DeliveryStatus.SENT, PanelInvitation.DeliveryStatus.UNKNOWN):
            invitation.delivery_status = status
            invitation.save(update_fields=('delivery_status', 'updated_at'))
            self.assertTrue(panel_invitation_resend_available(invitation))
        invitation.delivery_status = PanelInvitation.DeliveryStatus.SENDING
        invitation.save(update_fields=('delivery_status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'delivery attempt in progress'):
            request_panel_invitation_resend(self.hearing, invitation,
                organization=self.org.as_claim(), user=self.owner)
        invitation.delivery_status = PanelInvitation.DeliveryStatus.PENDING
        invitation.save(update_fields=('delivery_status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'already queued'):
            request_panel_invitation_resend(self.hearing, invitation,
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            request_panel_invitation_resend(self.hearing, invitation,
                organization=self.other_org.as_claim(), user=self.other)
        with self.assertRaises(ValidationError):
            request_panel_invitation_resend(self.hearing, invitation,
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            request_panel_invitation_resend(self.hearing, PanelInvitation(pk=-1),
                organization=self.org.as_claim(), user=self.owner)

    def test_import_operation_revalidates_scope_expiry_duplicate_active_and_panel(self):
        valid = {'fatal': None, 'errors': [], 'rows': [{'email': 'new@example.test'}]}
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(self.hearing,
                {'fatal': 'invalid', 'errors': [], 'rows': []},
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(self.hearing, valid, 'not-a-date',
                organization=self.org.as_claim(), user=self.owner, panel=self.panel)
        naive_created = apply_panel_invitation_import(self.hearing,
            {'fatal': None, 'errors': [], 'rows': [{'email': 'naive@example.test'}]},
            (timezone.now() + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            organization=self.org.as_claim(), user=self.owner,
            panel=self.panel)
        self.assertEqual(len(naive_created), 1)
        bare_hearing = PanelHearing.objects.create(organization_id=self.org.pk,
            created_by=self.owner, name='No panel yet')
        defaulted = apply_panel_invitation_import(bare_hearing,
            {'fatal': None, 'errors': [], 'rows': [{'email': 'default@example.test'}]},
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(defaulted[0].panel.name, 'Default panel')
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(self.hearing, valid, timezone.now().isoformat(),
                organization=self.org.as_claim(), user=self.owner, panel=self.panel)
        self.assertEqual(len(apply_panel_invitation_import(self.hearing, valid,
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)), 1)
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(self.hearing, valid,
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        duplicate = {'fatal': None, 'errors': [], 'rows': [
            {'email': 'same@example.test'}, {'email': 'SAME@example.test'}]}
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(self.hearing, duplicate,
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        other_hearing = PanelHearing.objects.create(organization_id=self.org.pk,
            created_by=self.owner, name='Import panel boundary')
        other_panel = PanelScoringPanel.objects.create(hearing=other_hearing,
            organization_id=self.org.pk, created_by=self.owner, name='Other')
        with self.assertRaises(ValidationError):
            apply_panel_invitation_import(other_hearing, duplicate,
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
                user=self.owner, panel=other_panel)
        with self.assertRaises(PermissionDenied):
            apply_panel_invitation_import(other_hearing, valid,
                timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
                user=self.owner, panel=self.panel)
        with self.assertRaises(PermissionDenied):
            apply_panel_invitation_import(self.hearing, valid,
                timezone.now() + timedelta(hours=1), organization=self.other_org.as_claim(),
                user=self.other, panel=self.panel)
        expired = queue_panel_invitation(self.hearing, 'expired@example.test',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        expired.revoked_at = timezone.now()
        expired.save(update_fields=('revoked_at', 'updated_at'))
        self.assertFalse(panel_invitation_resend_available(expired))
        with self.assertRaises(ValidationError):
            request_panel_invitation_resend(self.hearing, expired,
                organization=self.org.as_claim(), user=self.owner)
        active_on_closed_hearing = queue_panel_invitation(self.hearing,
            'closed@example.test', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner, panel=self.panel)
        self.hearing.status = PanelHearing.Status.REVIEW
        self.hearing.save(update_fields=('status', 'updated_at'))
        self.assertFalse(panel_invitation_resend_available(expired))
        with self.assertRaises(ValidationError):
            request_panel_invitation_resend(self.hearing, active_on_closed_hearing,
                organization=self.org.as_claim(), user=self.owner)
        self.hearing.status = PanelHearing.Status.DRAFT
        self.hearing.save(update_fields=('status', 'updated_at'))

    def test_recent_worker_claim_blocks_resend_but_stale_claim_can_be_recovered(self):
        invitation = queue_panel_invitation(self.hearing, 'stuck@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        invitation.delivery_status = PanelInvitation.DeliveryStatus.SENDING
        invitation.save(update_fields=('delivery_status', 'updated_at'))
        with self.assertRaisesMessage(ValidationError, 'delivery attempt in progress'):
            request_panel_invitation_resend(self.hearing, invitation,
                organization=self.org.as_claim(), user=self.owner)
        PanelInvitation.objects.filter(pk=invitation.pk).update(
            updated_at=timezone.now() - timedelta(minutes=16))
        stale = PanelInvitation.objects.get(pk=invitation.pk)
        recovered = request_panel_invitation_resend(self.hearing, stale,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(recovered.delivery_status, PanelInvitation.DeliveryStatus.PENDING)
        self.assertEqual(PanelAuditEvent.objects.filter(hearing=self.hearing,
            action=PanelAuditEvent.Action.INVITATION_RESENT).count(), 1)

    @override_settings(LIVEGRADE_BASE_URL='http://localhost:8001',
                       LIVEGRADE_PUBLIC_BASE_URL='http://public.example.test')
    def test_worker_sends_only_for_pending_and_records_sent_or_failed_state(self):
        invitation = queue_panel_invitation(self.hearing, 'worker@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        from service_domains.tasks import send_panel_invitation_email
        with patch('service_domains.tasks.secrets.token_urlsafe', return_value='worker-token'), \
             patch('service_domains.tasks.send_mail', return_value=1) as send:
            self.assertEqual(send_panel_invitation_email(invitation.pk), 'sent')
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.SENT)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs['recipient_list'], ['worker@example.test'])
        self.assertIn('http://public.example.test/panel/examiner/worker-token',
            send.call_args.kwargs['message'])
        self.assertEqual(send_panel_invitation_email(invitation.pk), 'ignored')

        failed = queue_panel_invitation(self.hearing, 'failed@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        with patch('service_domains.tasks.send_mail', side_effect=OSError('smtp down')):
            self.assertEqual(send_panel_invitation_email(failed.pk), 'failed')
        failed.refresh_from_db()
        self.assertEqual(failed.delivery_status, PanelInvitation.DeliveryStatus.FAILED)
        self.assertEqual(failed.delivery_error, 'mail_backend_error')

    def test_worker_marks_unusable_and_superseded_attempts_without_leaking_errors(self):
        invitation = queue_panel_invitation(self.hearing, 'unusable@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        self.hearing.status = PanelHearing.Status.REVIEW
        self.hearing.save(update_fields=('status', 'updated_at'))
        from service_domains.tasks import send_panel_invitation_email
        self.assertEqual(send_panel_invitation_email(invitation.pk), 'unusable')
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_error, 'invitation_unusable')

        self.hearing.status = PanelHearing.Status.DRAFT
        self.hearing.save(update_fields=('status', 'updated_at'))
        invitation = queue_panel_invitation(self.hearing, 'superseded@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        def race_delivery(**kwargs):
            PanelInvitation.objects.filter(pk=invitation.pk).update(
                delivery_status=PanelInvitation.DeliveryStatus.SENDING,
                delivery_attempts=2)
            return 1
        with patch('service_domains.tasks.send_mail', side_effect=race_delivery):
            self.assertEqual(send_panel_invitation_email(invitation.pk), 'superseded')
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.SENDING)
        self.assertEqual(invitation.delivery_attempts, 2)
        self.assertEqual(invitation.delivery_error, '')

    def test_owner_preview_apply_and_sample_routes_are_scoped_and_enqueue_without_tokens(self):
        sign_in(self.client, self.owner, self.org)
        preview_url = reverse('panel_invitation_import', args=(self.hearing.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            preview = self.client.post(preview_url, {
                'expires_at': (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
                'panel': str(self.panel.pk),
                'file': self.upload(['Email'], [['one@example.test']]),
            })
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'invitation ready.')
        token = preview.context['preview_token']
        payload = signing.loads(token, salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        self.assertEqual(payload['hearing'], self.hearing.pk)
        with patch('service_domains.views.send_panel_invitation_email.delay') as enqueue, \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'), \
             self.captureOnCommitCallbacks(execute=True):
            applied = self.client.post(preview_url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(applied.status_code, 302)
        invitation = PanelInvitation.objects.get(hearing=self.hearing)
        self.assertEqual(invitation.token_digest, None)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[0], invitation.pk)
        self.assertTrue(enqueue.call_args.args[1].startswith('http'))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            sample = self.client.get(reverse('panel_invitation_import_sample', args=(self.hearing.pk,)))
        self.assertEqual(sample.status_code, 200)
        self.assertTrue(sample['Content-Type'].startswith('application/vnd.openxmlformats'))

    def test_management_exposes_resend_only_when_delivery_can_be_recovered(self):
        invitation = queue_panel_invitation(self.hearing, 'stale@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        PanelInvitation.objects.filter(pk=invitation.pk).update(
            delivery_status=PanelInvitation.DeliveryStatus.SENDING,
            updated_at=timezone.now() - timedelta(minutes=16))
        recent = queue_panel_invitation(self.hearing, 'recent@example.test',
            timezone.now() + timedelta(hours=2), organization=self.org.as_claim(),
            user=self.owner, panel=self.panel)
        sign_in(self.client, self.owner, self.org)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            response = self.client.get(reverse('panel_hearing_manage', args=(self.hearing.pk,)))
        self.assertEqual(response.status_code, 200)
        rows = {row.email: row.can_resend for row in response.context['invitations']}
        self.assertTrue(rows['stale@example.test'])
        self.assertFalse(rows['recent@example.test'])
        self.assertIsNotNone(recent.pk)

    def test_apply_route_rejects_invalid_and_expired_preview_and_queue_error_is_recorded(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('panel_invitation_import', args=(self.hearing.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            invalid = self.client.post(url, {'action': 'apply', 'preview_token': 'x' * 150_001})
        self.assertEqual(invalid.status_code, 404)
        expired = signing.dumps({'hearing': self.hearing.pk, 'organization': self.org.pk,
            'user': self.owner.pk, 'parsed': {'rows': [], 'errors': [], 'fatal': None}},
            salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            bad_signature = self.client.post(url, {'action': 'apply', 'preview_token': 'broken'})
        self.assertEqual(bad_signature.status_code, 400)
        payload = signing.loads(expired, salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        payload['hearing'] = -10
        tampered = signing.dumps(payload, salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            wrong_owner = self.client.post(url, {'action': 'apply', 'preview_token': tampered})
        self.assertEqual(wrong_owner.status_code, 404)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            invalid_form = self.client.post(url, {'action': 'apply'})
        self.assertEqual(invalid_form.status_code, 400)

        preview = signing.dumps({'hearing': self.hearing.pk, 'organization': self.org.pk,
            'user': self.owner.pk, 'parsed': {'rows': [{'email': 'x@example.test'}],
            'errors': [], 'fatal': None}, 'panel': 999999,
            'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()},
            salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            invalid_panel = self.client.post(url, {'action': 'apply', 'preview_token': preview})
        self.assertEqual(invalid_panel.status_code, 404)

        expired_at = signing.dumps({'hearing': self.hearing.pk, 'organization': self.org.pk,
            'user': self.owner.pk, 'parsed': {'rows': [{'email': 'x@example.test'}],
            'errors': [], 'fatal': None}, 'expires_at': (timezone.now() - timedelta(hours=1)).isoformat()},
            salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        with patch('service_domains.views.apply_panel_invitation_import',
                   side_effect=ValidationError('expiry is stale')), \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            stale_apply = self.client.post(url, {'action': 'apply', 'preview_token': expired_at})
        self.assertEqual(stale_apply.status_code, 400)

        valid_apply = signing.dumps({'hearing': self.hearing.pk, 'organization': self.org.pk,
            'user': self.owner.pk, 'parsed': {'rows': [{'email': 'x@example.test'}],
            'errors': [], 'fatal': None}, 'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()},
            salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        with patch('service_domains.views.apply_panel_invitation_import',
                   side_effect=PermissionDenied), \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            denied_apply = self.client.post(url, {'action': 'apply', 'preview_token': valid_apply})
        self.assertEqual(denied_apply.status_code, 404)

    def test_preview_route_rejects_invalid_form_and_row_errors_without_apply_token(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('panel_invitation_import', args=(self.hearing.pk,))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            invalid_form = self.client.post(url, {'expires_at': 'bad',
                'file': self.upload(['Email'], [['x@example.test']])})
        self.assertEqual(invalid_form.status_code, 400)
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            row_errors = self.client.post(url, {
                'expires_at': (timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
                'file': self.upload(['Email'], [['bad-address']]),
            })
        self.assertEqual(row_errors.status_code, 200)
        self.assertContains(row_errors, 'valid examiner email')
        self.assertEqual(row_errors.context['preview_token'], '')

    def test_single_invite_enqueue_failure_sets_failed_status_and_resend_rotates(self):
        sign_in(self.client, self.owner, self.org)
        manage = reverse('panel_hearing_manage', args=(self.hearing.pk,))
        expiry = (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        with patch('service_domains.views.send_panel_invitation_email.delay',
                   side_effect=OSError('broker secret')), \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'), \
             self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(manage, {'action': 'invite',
                'email': 'queue-fail@example.test', 'expires_at': expiry})
        self.assertEqual(response.status_code, 302)
        invitation = PanelInvitation.objects.get(hearing=self.hearing)
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.FAILED)
        self.assertEqual(invitation.delivery_error, 'queue_unavailable')

        with patch('service_domains.views.send_panel_invitation_email.delay') as enqueue, \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'), \
             self.captureOnCommitCallbacks(execute=True):
            resent = self.client.post(manage, {'action': 'resend', 'invitation_id': invitation.pk})
        self.assertEqual(resent.status_code, 302)
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.PENDING)
        self.assertIsNone(invitation.token_digest)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[0], invitation.pk)
        self.assertTrue(enqueue.call_args.args[1].startswith('http'))
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            too_soon = self.client.post(manage, {'action': 'resend', 'invitation_id': invitation.pk})
            missing = self.client.post(manage, {'action': 'resend', 'invitation_id': 999999})
        self.assertEqual(too_soon.status_code, 400)
        self.assertEqual(missing.status_code, 404)
        invitation.delivery_status = PanelInvitation.DeliveryStatus.FAILED
        invitation.save(update_fields=('delivery_status', 'updated_at'))
        with patch('service_domains.views.send_panel_invitation_email.delay',
                   side_effect=OSError('broker unavailable')), \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'), \
             self.captureOnCommitCallbacks(execute=True):
            failed_resend = self.client.post(manage, {
                'action': 'resend', 'invitation_id': invitation.pk})
        self.assertEqual(failed_resend.status_code, 302)
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.FAILED)
        self.assertEqual(invitation.delivery_error, 'queue_unavailable')

    def test_examiner_import_apply_records_each_broker_enqueue_failure(self):
        sign_in(self.client, self.owner, self.org)
        url = reverse('panel_invitation_import', args=(self.hearing.pk,))
        parsed = {'fatal': None, 'errors': [], 'rows': [{'row': 2,
            'email': 'import-queue-fail@example.test', 'errors': []}]}
        token = signing.dumps({'hearing': self.hearing.pk, 'organization': self.org.pk,
            'user': self.owner.pk, 'parsed': parsed, 'panel': self.panel.pk,
            'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()},
            salt=PANEL_INVITATION_IMPORT_TOKEN_SALT)
        with patch('service_domains.views.send_panel_invitation_email.delay',
                   side_effect=OSError('broker unavailable')), \
             patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'), \
             self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(response.status_code, 302)
        invitation = PanelInvitation.objects.get(hearing=self.hearing)
        self.assertEqual(invitation.delivery_status, PanelInvitation.DeliveryStatus.FAILED)
        self.assertEqual(invitation.delivery_error, 'queue_unavailable')

    def test_invitation_form_and_scoring_panel_display_name(self):
        from service_domains.forms import PanelInvitationForm, PanelInvitationImportForm
        self.assertEqual(str(self.panel), 'Oral panel')
        single_form = PanelInvitationForm(panels=PanelScoringPanel.objects.filter(hearing=self.hearing))
        rendered_single = str(single_form['panel'])
        self.assertIn('Oral panel', rendered_single)
        self.assertNotIn('PanelScoringPanel object', rendered_single)

        import_form = PanelInvitationImportForm(panels=PanelScoringPanel.objects.filter(hearing=self.hearing))
        rendered_import = str(import_form['panel'])
        self.assertIn('Oral panel', rendered_import)
        self.assertNotIn('PanelScoringPanel object', rendered_import)
