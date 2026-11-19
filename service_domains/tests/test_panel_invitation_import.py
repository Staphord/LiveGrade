from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_org, make_user
from assessments.sheets import workbook_bytes
from service_domains.models import PanelAuditEvent, PanelHearing, PanelInvitation, PanelScoringPanel
from service_domains.operations import apply_panel_invitation_import
from service_domains.operations import (mark_panel_invitation_enqueue_failed,
    queue_panel_invitation, request_panel_invitation_resend, panel_invitation_resend_available)
from service_domains.panel_invitation_import import (TOO_MANY_PHYSICAL_ROWS,
    parse_examiner_emails, plan_examiner_import, sample_workbook)


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
