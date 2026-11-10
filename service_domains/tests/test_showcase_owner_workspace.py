from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.core import signing
from django.utils import timezone
from datetime import timedelta

from accounts.testing import make_org, make_user, sign_in
from assessments.sheets import workbook_bytes
from service_domains.forms import ShowcaseEventForm, ShowcaseImportForm, ShowcaseJudgeAssignmentsForm
from service_domains.models import (
    ShowcaseCategory, ShowcaseConsentAudit, ShowcaseEvent, ShowcaseEventAudit,
    ShowcaseExhibit, ShowcaseJudgeAssignment, ShowcaseJudgeInvitation,
    ShowcaseJudgeExhibitAssignment,
)
from service_domains.showcase_import import MAX_UPLOAD_BYTES
from service_domains.operations import (confirm_showcase_minor_consent,
    issue_showcase_judge_invitation, replace_showcase_invited_judge_assignments,
    declare_showcase_judge_conflict,
    replace_showcase_judge_criteria, replace_showcase_judge_assignments,
    transition_showcase_event, submit_showcase_judge_evaluation)
from service_domains.views import SHOWCASE_IMPORT_TOKEN_SALT


class ShowcaseOwnerWorkspaceTests(TestCase):
    def setUp(self):
        self.org = make_org('Owner workspace org')
        self.other_org = make_org('Another workspace org')
        self.owner = make_user('showcase-workspace-owner')
        self.other_owner = make_user('showcase-workspace-other')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Draft expo')
        self.event_url = reverse('showcase_event_manage', args=(self.event.pk,))

    def request(self, method, url, *args, **kwargs):
        """Exercise workspace views with server-derived Showcase request scope."""
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'showcase'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def sign_in(self, user=None, org=None):
        sign_in(self.client, user or self.owner, org or self.org)

    def upload(self, filename='exhibits.xlsx', rows=None):
        workbook = workbook_bytes(
            ['Category', 'Exhibit', 'Description'],
            rows if rows is not None else [['Engineering', 'Solar pump', 'A water irrigation prototype']],
        )
        return SimpleUploadedFile(filename, workbook.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_workspace_routes_fail_closed_while_service_is_unreleased(self):
        self.sign_in()
        self.assertEqual(self.client.get(reverse('showcase_event_list')).status_code, 404)
        self.assertEqual(self.client.get(self.event_url).status_code, 404)

    def test_owner_workspace_surfaces_conflict_and_reassignment_needed(self):
        exhibit = ShowcaseExhibit.objects.create(event=self.event,
            organization_id=self.org.id, created_by=self.owner, name='Solar prototype')
        invitation, token = issue_showcase_judge_invitation(self.event,
            'judge@example.org', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        declare_showcase_judge_conflict(token, exhibit.public_id,
            'I worked with this exhibitor.', at=timezone.now())
        self.sign_in()
        workspace = self.request('get', self.event_url)
        self.assertContains(workspace, 'Conflict declarations needing reassignment')
        self.assertContains(workspace, 'judge@example.org')
        self.assertContains(workspace, 'I worked with this exhibitor.')
        self.assertContains(workspace, 'Assign another judge')

    def test_owner_can_create_and_manage_only_their_organization_events(self):
        self.sign_in()
        listing = self.request('get', reverse('showcase_event_list'))
        self.assertContains(listing, 'Event overview')
        self.assertContains(listing, 'Showcase dashboard')
        self.assertNotContains(listing, 'Set up your first Showcase event')
        self.assertContains(listing, reverse('showcase_event_create'))
        self.assertContains(listing, 'Create event')
        created = self.request('post', reverse('showcase_event_list'), {
            'name': 'Community fair', 'includes_minors': 'on',
            'minimum_judges_per_exhibit': '1',
            'branding_tagline': 'A greener future', 'branding_accent': 'green',
        })
        new_event = ShowcaseEvent.objects.get(name='Community fair')
        self.assertEqual(created.status_code, 302)
        self.assertEqual(created['Location'], reverse('showcase_event_manage', args=(new_event.pk,)))
        self.assertTrue(new_event.includes_minors)
        self.assertIsNone(new_event.consent_confirmed_at)
        self.assertEqual(new_event.branding, {'tagline': 'A greener future', 'accent': 'green'})
        self.assertEqual(new_event.brand_accent_color, '#17683a')
        self.assertContains(self.request('get', reverse('showcase_event_list')), 'Draft expo')
        event_workspace = self.request('get', self.event_url)
        self.assertContains(event_workspace, 'Exhibits')
        self.assertContains(event_workspace, 'Prepare this event')
        self.assertContains(event_workspace, 'assign judges to each exhibit')

        self.sign_in(self.other_owner, self.org)
        self.assertEqual(self.request('get', self.event_url).status_code, 404)
        self.sign_in(self.owner, self.other_org)
        self.assertEqual(self.request('get', self.event_url).status_code, 404)

    def test_first_time_owner_sees_getting_started_guidance(self):
        self.event.delete()
        self.sign_in()
        listing = self.request('get', reverse('showcase_event_list'))
        self.assertContains(listing, 'Start event setup')
        self.assertContains(listing, 'Set up your first Showcase event')

    def test_event_label_sheet_prints_every_owned_exhibit_and_no_other_event_data(self):
        first = ShowcaseExhibit.objects.create(event=self.event, name='Solar pump',
            organization_id=self.org.id, created_by=self.owner, order=1)
        second = ShowcaseExhibit.objects.create(event=self.event, name='Water filter',
            organization_id=self.org.id, created_by=self.owner, order=2)
        another_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Private side event')
        ShowcaseExhibit.objects.create(event=another_event, name='Private exhibit',
            organization_id=self.org.id, created_by=self.owner)
        url = reverse('showcase_event_labels', args=(self.event.pk,))
        self.sign_in()
        response = self.request('get', url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Print labels')
        self.assertContains(response, 'Solar pump')
        self.assertContains(response, 'Water filter')
        self.assertNotContains(response, 'Private exhibit')
        self.assertContains(response, f'alt="QR code for rating Solar pump"')
        self.assertContains(response, f'alt="QR code for rating Water filter"')
        self.assertContains(response, reverse('showcase_exhibit_qr', args=(first.public_id,)))
        self.assertContains(response, reverse('showcase_exhibit_qr', args=(second.public_id,)))
        self.assertIn('no-store', response['Cache-Control'])
        manage = self.request('get', self.event_url)
        self.assertContains(manage, reverse('showcase_event_labels', args=(self.event.pk,)))

        self.sign_in(self.other_owner, self.org)
        self.assertEqual(self.request('get', url).status_code, 404)
        self.sign_in(self.owner, self.other_org)
        self.assertEqual(self.request('get', url).status_code, 404)

    def test_event_form_validation_and_model_validation_errors_are_shown(self):
        self.sign_in()
        unbound_form = ShowcaseEventForm()
        unbound_form.full_clean()
        self.assertFalse(unbound_form.is_bound)
        list_url = reverse('showcase_event_list')
        invalid_form = self.request('post', list_url, {'name': ''})
        self.assertEqual(invalid_form.status_code, 200)
        self.assertContains(invalid_form, 'This field is required')
        invalid_dates = ShowcaseEventForm(data={
            'name': 'Invalid dates', 'starts_at': '2027-01-02T10:00',
            'ends_at': '2027-01-01T10:00', 'includes_minors': '',
        })
        self.assertFalse(invalid_dates.is_valid())
        self.assertIn('ends_at', invalid_dates.errors)
        original_full_clean = ShowcaseEvent.full_clean
        call_count = 0
        def fail_after_form_validation(event, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise ValidationError('model refused')
            return original_full_clean(event, *args, **kwargs)
        with patch.object(ShowcaseEvent, 'full_clean', new=fail_after_form_validation):
            model_error = self.request('post', list_url, {
                'name': 'Model error', 'minimum_judges_per_exhibit': '1',
            })
        self.assertContains(model_error, 'model refused')

    def test_branding_validation_rejects_unknown_accents_and_unsafe_model_data(self):
        invalid = ShowcaseEventForm(data={
            'name': 'Invalid brand', 'branding_tagline': 'Tagline',
            'branding_accent': '#fff; color:red', 'minimum_judges_per_exhibit': '1',
        })
        self.assertFalse(invalid.is_valid())
        self.assertIn('branding_accent', invalid.errors)

        self.event.branding = {'accent': 'blue; color:red', 'tagline': 'Safe?'}
        with self.assertRaises(ValidationError):
            self.event.full_clean()
        self.assertEqual(self.event.brand_accent_color, '#174c7b')
        invalid_branding = [
            None,
            {'unsupported': 'value'},
            {'accent': []},
            {'tagline': 42, 'accent': 'blue'},
            {'tagline': 'x' * 161, 'accent': 'blue'},
        ]
        for branding in invalid_branding:
            with self.subTest(branding=branding), self.assertRaises(ValidationError):
                self.event.branding = branding
                self.event.full_clean()
        self.event.branding = {'tagline': '<script>alert(1)</script>', 'accent': 'blue'}
        self.event.full_clean()

    def test_showcase_event_form_saves_branding_on_commit(self):
        form = ShowcaseEventForm(data={
            'name': 'Updated expo', 'includes_minors': '',
            'minimum_judges_per_exhibit': '1',
            'branding_tagline': 'Meet the makers', 'branding_accent': 'red',
        }, instance=self.event)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.pk, self.event.pk)
        self.event.refresh_from_db()
        self.assertEqual(self.event.branding, {'tagline': 'Meet the makers', 'accent': 'red'})

    def test_import_preview_is_read_only_and_signed_apply_creates_expected_rows(self):
        self.sign_in()
        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))
        self.assertEqual(self.request('get', import_url).status_code, 200)
        before = (ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count())
        preview = self.request('post', import_url, {'file': self.upload(), 'action': 'preview'})
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Nothing has been saved yet.')
        self.assertEqual((ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count()), before)
        token = preview.context['preview_token']
        self.assertTrue(token)

        applied = self.request('post', import_url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(applied.status_code, 302)
        exhibit = ShowcaseExhibit.objects.get(event=self.event, name='Solar pump')
        self.assertEqual(exhibit.description, 'A water irrigation prototype')
        self.assertEqual(exhibit.category.name, 'Engineering')

    def test_tampered_preview_and_invalid_file_do_not_write(self):
        self.sign_in()
        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))
        preview = self.request('post', import_url, {'file': self.upload(), 'action': 'preview'})
        token = preview.context['preview_token']
        before = (ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count())
        refused = self.request('post', import_url, {
            'action': 'apply', 'preview_token': token + 'tampered',
        })
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, 'expired or is invalid', status_code=400)
        invalid = self.request('post', import_url, {
            'file': SimpleUploadedFile('exhibits.csv', b'not excel'), 'action': 'preview',
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'Excel .xlsx', status_code=400)
        self.assertEqual((ShowcaseCategory.objects.count(), ShowcaseExhibit.objects.count()), before)

    def test_apply_rejects_wrong_owner_event_stale_preview_and_oversized_signed_payload(self):
        self.sign_in()
        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))
        preview = self.request('post', import_url, {'file': self.upload(), 'action': 'preview'})
        token = preview.context['preview_token']
        mismatch = signing.dumps({
            'event': self.event.pk + 1, 'organization': self.org.id,
            'user': self.owner.pk, 'parsed': {'fatal': None, 'rows': [], 'errors': []},
        }, salt=SHOWCASE_IMPORT_TOKEN_SALT)
        self.assertEqual(self.request('post', import_url, {
            'action': 'apply', 'preview_token': mismatch,
        }).status_code, 404)

        too_large = 'x' * 150_001
        self.assertEqual(self.request('post', import_url, {
            'action': 'apply', 'preview_token': too_large,
        }).status_code, 404)

        stale = self.request('post', import_url, {'file': self.upload(), 'action': 'preview'})
        stale_token = stale.context['preview_token']
        category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Engineering')
        ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, category=category, name='Solar pump')
        stale_response = self.request('post', import_url, {
            'action': 'apply', 'preview_token': stale_token,
        })
        self.assertEqual(stale_response.status_code, 400)
        self.assertContains(stale_response, 'import has errors', status_code=400)

        with patch('service_domains.views.apply_import', side_effect=PermissionDenied):
            denied = self.request('post', import_url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(denied.status_code, 404)

    def test_import_view_shows_row_errors_rejects_apply_without_token_and_large_upload(self):
        self.sign_in()
        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))
        errors = self.request('post', import_url, {
            'file': self.upload(rows=[['Engineering', '', 'Missing name']]), 'action': 'preview',
        })
        self.assertContains(errors, 'Exhibit name is required')
        self.assertFalse(errors.context['preview_token'])
        missing_token = self.request('post', import_url, {'action': 'apply'})
        self.assertEqual(missing_token.status_code, 400)
        large = SimpleUploadedFile('large.xlsx', b'x' * (MAX_UPLOAD_BYTES + 1))
        oversized = self.request('post', import_url, {'file': large, 'action': 'preview'})
        self.assertEqual(oversized.status_code, 400)
        self.assertContains(oversized, 'no larger than 10 MB', status_code=400)

    def test_import_and_sample_routes_are_scoped_to_the_event_owner(self):
        self.sign_in(self.other_owner, self.org)
        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))
        sample_url = reverse('showcase_exhibit_import_sample', args=(self.event.pk,))
        self.assertEqual(self.request('get', import_url).status_code, 404)
        self.assertEqual(self.request('get', sample_url).status_code, 404)

        self.sign_in()
        sample = self.request('get', sample_url)
        self.assertEqual(sample.status_code, 200)
        self.assertEqual(sample['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertTrue(sample.content.startswith(b'PK'))

    def test_draft_event_can_be_edited_but_live_event_is_immutable(self):
        self.sign_in()
        response = self.request('post', self.event_url, {
            'action': 'edit', 'name': 'Renamed Expo', 'starts_at': '', 'ends_at': '',
            'includes_minors': '', 'minimum_judges_per_exhibit': '1',
            'branding_tagline': 'Keep the community growing', 'branding_accent': 'purple',
        })
        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.name, 'Renamed Expo')
        self.assertEqual(self.event.branding, {
            'tagline': 'Keep the community growing', 'accent': 'purple'})

        response = self.request('post', self.event_url, {
            'action': 'edit', 'name': 'Bad dates', 'starts_at': '2027-04-02T10:00',
            'ends_at': '2027-04-01T10:00', 'includes_minors': '',
            'minimum_judges_per_exhibit': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'must be after the start')
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status',))
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'edit', 'name': 'Not editable',
        }).status_code, 404)

    def test_judge_coverage_is_visible_and_owner_can_replace_assignments(self):
        self.sign_in()
        first = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        second = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Wind turbine', order=1)
        self.event.minimum_judges_per_exhibit = 2
        self.event.save(update_fields=('minimum_judges_per_exhibit',))
        initial = self.request('get', self.event_url)
        self.assertContains(initial, 'Invited judge assignment coverage')
        self.assertContains(initial, '2 exhibits have coverage gaps')

        first_invite = self.request('post', self.event_url, {
            'action': 'invite_judge', 'email': 'judge-a@example.org',
            'expires_at': '2030-05-01T10:00', 'exhibits': [str(first.pk)],
        })
        self.assertEqual(first_invite.status_code, 302)
        second_invite = self.request('post', self.event_url, {
            'action': 'invite_judge', 'email': 'judge-b@example.org',
            'expires_at': '2030-05-01T10:00', 'exhibits': [str(first.pk), str(second.pk)],
        })
        self.assertEqual(second_invite.status_code, 302)
        self.assertEqual(ShowcaseJudgeInvitation.objects.filter(event=self.event).count(), 2)
        self.assertEqual(ShowcaseJudgeExhibitAssignment.objects.filter(
            event=self.event, active=True).count(), 3)
        updated = self.request('get', self.event_url)
        self.assertContains(updated, '1 of 2 exhibits meet the minimum')
        self.assertContains(updated, 'Solar pump')
        self.assertContains(updated, 'judge-a@example.org')

    def test_invalid_or_unauthorized_judge_invitation_writes_nothing(self):
        self.sign_in()
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        invalid = self.request('post', self.event_url, {
            'action': 'invite_judge', 'email': 'bad-email',
            'expires_at': '2020-01-01T10:00', 'exhibits': [str(exhibit.pk)],
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'Enter a valid email address', status_code=400)
        self.assertEqual(ShowcaseJudgeInvitation.objects.filter(event=self.event).count(), 0)

        self.sign_in(self.other_owner, self.org)
        denied = self.request('post', self.event_url, {
            'action': 'invite_judge', 'email': 'intruder@example.org',
            'expires_at': '2030-05-01T10:00', 'exhibits': [str(exhibit.pk)],
        })
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(ShowcaseJudgeInvitation.objects.filter(event=self.event).count(), 0)

    def test_judge_assignment_operation_rejects_invalid_threshold_without_partial_change(self):
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        for threshold in (0, 51):
            with self.subTest(threshold=threshold), self.assertRaises(ValidationError):
                replace_showcase_judge_assignments(self.event, {exhibit.pk: ['Judge A']},
                    organization=self.org.as_claim(), user=self.owner, minimum_judges=threshold)
        self.event.refresh_from_db()
        self.assertEqual(self.event.minimum_judges_per_exhibit, 1)
        self.assertEqual(ShowcaseJudgeAssignment.objects.filter(event=self.event, active=True).count(), 0)

    def test_assignment_form_map_requires_validated_form(self):
        form = ShowcaseJudgeAssignmentsForm({'minimum_judges': 'bad'}, event=self.event)
        with self.assertRaisesMessage(ValueError, 'valid form'):
            form.assignment_map()

    def test_published_event_judge_invitation_refuses_post(self):
        self.sign_in()
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save(update_fields=('status', 'public_results'))
        response = self.request('post', self.event_url, {
            'action': 'invite_judge', 'email': 'judge@example.org',
            'expires_at': '2030-05-01T10:00',
        })
        self.assertEqual(response.status_code, 404)

    def test_judge_invitation_displays_assignment_validation_error_atomically(self):
        self.sign_in()
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        with patch('service_domains.views.replace_showcase_invited_judge_assignments',
                   side_effect=ValidationError('assignment plan unavailable')):
            response = self.request('post', self.event_url, {
                'action': 'invite_judge', 'email': 'judge@example.org',
                'expires_at': '2030-05-01T10:00', 'exhibits': [str(exhibit.pk)],
            })
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'assignment plan unavailable', status_code=400)
        self.assertEqual(ShowcaseJudgeInvitation.objects.filter(event=self.event).count(), 0)

    def test_judge_criteria_view_saves_valid_form_and_renders_validation_errors(self):
        self.sign_in()
        payload = {'action': 'judge_criteria', 'criteria-TOTAL_FORMS': '1',
            'criteria-INITIAL_FORMS': '0', 'criteria-MIN_NUM_FORMS': '0',
            'criteria-MAX_NUM_FORMS': '50', 'criteria-0-name': 'Quality',
            'criteria-0-description': 'Evidence quality', 'criteria-0-weight': '100',
            'criteria-0-scale': 'points', 'criteria-0-max_points': '100',
            'criteria-0-checklist_items_text': ''}
        saved = self.request('post', self.event_url, payload)
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(self.event.judge_criteria.get().name, 'Quality')

        with patch('service_domains.views.replace_showcase_judge_criteria',
                   side_effect=ValidationError('criterion update rejected')):
            rejected = self.request('post', self.event_url, payload)
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, 'criterion update rejected', status_code=400)

        with patch('service_domains.views.replace_showcase_judge_criteria',
                   side_effect=PermissionDenied):
            denied = self.request('post', self.event_url, payload)
        self.assertEqual(denied.status_code, 404)

        payload['criteria-0-weight'] = 'not-a-weight'
        invalid = self.request('post', self.event_url, payload)
        self.assertEqual(invalid.status_code, 400)

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        locked = self.request('post', self.event_url, payload)
        self.assertEqual(locked.status_code, 400)
        self.assertIn('Judge criteria are locked after the event opens.',
            str(locked.context['criteria_formset'].non_form_errors()))

    def test_judge_invitation_mail_exception_fallback_and_revoke_responses(self):
        self.sign_in()
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        with patch('service_domains.views.send_mail', side_effect=RuntimeError('mail offline')):
            response = self.request('post', self.event_url, {
                'action': 'invite_judge', 'email': 'judge@example.org',
                'expires_at': '2030-05-01T10:00', 'exhibits': [str(exhibit.pk)],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        invitation = ShowcaseJudgeInvitation.objects.get(event=self.event)

        with patch('service_domains.views.revoke_showcase_judge_invitation',
                   side_effect=ValidationError('revocation reason required')):
            rejected = self.request('post', self.event_url, {
                'action': 'revoke_judge', 'invitation_id': str(invitation.pk),
            })
        self.assertEqual(rejected.status_code, 302)

        revoked = self.request('post', self.event_url, {
            'action': 'revoke_judge', 'invitation_id': str(invitation.pk),
        })
        self.assertEqual(revoked.status_code, 302)
        with patch('service_domains.views.revoke_showcase_judge_invitation',
                   side_effect=PermissionDenied):
            denied = self.request('post', self.event_url, {
                'action': 'revoke_judge', 'invitation_id': str(invitation.pk),
            })
        self.assertEqual(denied.status_code, 404)

    def test_judge_invitation_permission_race_rolls_back_token_and_assignment(self):
        self.sign_in()
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        with patch('service_domains.views.replace_showcase_invited_judge_assignments',
                   side_effect=PermissionDenied):
            denied = self.request('post', self.event_url, {
                'action': 'invite_judge', 'email': 'judge@example.org',
                'expires_at': '2030-05-01T10:00', 'exhibits': [str(exhibit.pk)],
            })
        self.assertEqual(denied.status_code, 404)
        self.assertFalse(ShowcaseJudgeInvitation.objects.filter(event=self.event).exists())

    def test_failed_judge_email_shows_private_link_once_without_referrer_or_cache(self):
        self.sign_in()
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Solar pump')
        with patch('service_domains.views.send_mail', return_value=0):
            response = self.request('post', self.event_url, {
                'action': 'invite_judge', 'email': 'judge@example.org',
                'expires_at': '2030-05-01T10:00', 'exhibits': [str(exhibit.pk)],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        self.assertContains(response, 'Copy private judge link')
        private_url = response.context['manual_judge_url']
        self.assertTrue(private_url)
        invitation = ShowcaseJudgeInvitation.objects.get(event=self.event)
        self.assertNotIn(private_url.rsplit('/', 2)[-2], invitation.token_digest)

        refreshed = self.request('get', self.event_url)
        self.assertEqual(refreshed.status_code, 200)
        self.assertNotContains(refreshed, private_url)

    def test_owner_lifecycle_is_audited_and_public_results_can_be_revoked(self):
        self.sign_in()
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, name='Exhibit for event')
        criteria = replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100', 'scale': 'points'},
        ], organization=self.org.as_claim(), user=self.owner)
        invitation, judge_token = issue_showcase_judge_invitation(self.event, 'judge@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        transition_url = reverse('showcase_event_transition', args=(self.event.pk,))
        cases = [
            (ShowcaseEvent.Status.OPEN, 'Open the event'),
            (ShowcaseEvent.Status.CLOSED, 'End audience ratings'),
            (ShowcaseEvent.Status.PUBLISHED, 'Publish verified awards'),
        ]
        for target, reason in cases:
            response = self.request('post', transition_url, {
                'target_status': target, 'reason': reason,
            })
            self.assertEqual(response.status_code, 302)
            self.event.refresh_from_db()
            self.assertEqual(self.event.status, target)
            if target == ShowcaseEvent.Status.OPEN:
                submit_showcase_judge_evaluation(judge_token, exhibit.public_id,
                    {str(criteria[0].pk): '90'})
                self.assertEqual(self.request('get', self.event_url).status_code, 200)
        self.assertTrue(self.event.public_results)
        self.assertEqual(ShowcaseEventAudit.objects.filter(event=self.event).count(), 3)
        self.assertEqual(list(ShowcaseEventAudit.objects.filter(event=self.event)
            .values_list('from_status', 'to_status', 'reason')), [
                ('draft', 'open', 'Open the event'),
                ('open', 'closed', 'End audience ratings'),
                ('closed', 'published', 'Publish verified awards'),
            ])
        audit = ShowcaseEventAudit.objects.filter(event=self.event).first()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            audit.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            audit.delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            ShowcaseEventAudit.objects.filter(pk=audit.pk).update(reason='changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            ShowcaseEventAudit.objects.filter(pk=audit.pk).delete()
        results_url = reverse('showcase_public_results', args=(self.event.public_id,))
        self.assertEqual(self.request('get', results_url).status_code, 200)

        revoked = self.request('post', transition_url, {
            'target_status': ShowcaseEvent.Status.CLOSED, 'reason': 'Withdraw publication',
        })
        self.assertEqual(revoked.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.CLOSED)
        self.assertFalse(self.event.public_results)
        self.assertEqual(self.request('get', results_url).status_code, 404)
        self.assertEqual(ShowcaseEventAudit.objects.filter(event=self.event).count(), 4)

    def test_minor_event_cannot_open_and_invalid_or_unauthorized_transitions_write_nothing(self):
        self.sign_in()
        self.event.includes_minors = True
        self.event.save(update_fields=('includes_minors',))
        ShowcaseExhibit.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=self.event, name='Minor event exhibit')
        transition_url = reverse('showcase_event_transition', args=(self.event.pk,))
        blocked = self.request('post', transition_url, {
            'target_status': ShowcaseEvent.Status.OPEN, 'reason': 'Attempt open',
        })
        self.assertEqual(blocked.status_code, 400)
        self.assertContains(blocked, 'consent', status_code=400)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.DRAFT)
        self.assertEqual(ShowcaseEventAudit.objects.filter(event=self.event).count(), 0)
        self.assertContains(self.request('get', self.event_url), 'Record consent confirmation')

        invalid_confirmation = self.request('post', self.event_url, {
            'action': 'confirm_consent', 'reason': 'Collected through school forms',
        })
        self.assertEqual(invalid_confirmation.status_code, 400)
        self.assertEqual(ShowcaseConsentAudit.objects.count(), 0)

        for data in (
            {'target_status': ShowcaseEvent.Status.CLOSED, 'reason': 'Skip open'},
            {'target_status': ShowcaseEvent.Status.OPEN, 'reason': ''},
            {'target_status': 'invalid', 'reason': 'Invalid target'},
        ):
            response = self.request('post', transition_url, data)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(ShowcaseEventAudit.objects.filter(event=self.event).count(), 0)
        self.sign_in(self.other_owner, self.org)
        self.assertEqual(self.request('post', transition_url, {
            'target_status': ShowcaseEvent.Status.OPEN, 'reason': 'Other owner',
        }).status_code, 404)
        self.sign_in(self.owner, self.other_org)
        self.assertEqual(self.request('post', transition_url, {
            'target_status': ShowcaseEvent.Status.OPEN, 'reason': 'Other org',
        }).status_code, 404)
        self.assertEqual(ShowcaseEventAudit.objects.count(), 0)
        self.assertEqual(ShowcaseConsentAudit.objects.count(), 0)

        self.sign_in()
        with patch('service_domains.views.confirm_showcase_minor_consent',
                   side_effect=ValidationError('Consent storage unavailable')):
            failed_confirmation = self.request('post', self.event_url, {
                'action': 'confirm_consent', 'confirmation': 'on',
                'reason': 'Collected through school forms',
            })
        self.assertEqual(failed_confirmation.status_code, 400)
        self.assertContains(failed_confirmation, 'Consent storage unavailable', status_code=400)
        self.assertEqual(ShowcaseConsentAudit.objects.count(), 0)
        with self.assertRaises(PermissionDenied):
            confirm_showcase_minor_consent(self.event, 'Wrong owner',
                organization=self.org.as_claim(), user=self.other_owner)
        with self.assertRaises(PermissionDenied):
            confirm_showcase_minor_consent(self.event, 'Wrong organization',
                organization=self.other_org.as_claim(), user=self.owner)
        non_minor = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='No minors event')
        with self.assertRaisesMessage(ValidationError, 'only to events marked'):
            confirm_showcase_minor_consent(non_minor, 'Not applicable',
                organization=self.org.as_claim(), user=self.owner)
        for invalid_reason in (None, '', 'x' * 501):
            with self.subTest(invalid_reason=invalid_reason), self.assertRaises(ValidationError):
                confirm_showcase_minor_consent(self.event, invalid_reason,
                    organization=self.org.as_claim(), user=self.owner)

        confirmed = self.request('post', self.event_url, {
            'action': 'confirm_consent', 'confirmation': 'on',
            'reason': 'Required guardian permissions collected by event organizers.',
        })
        self.assertEqual(confirmed.status_code, 302)
        self.event.refresh_from_db()
        self.assertIsNotNone(self.event.consent_confirmed_at)
        record = ShowcaseConsentAudit.objects.get(event=self.event)
        self.assertEqual(record.actor_user, self.owner)
        self.assertEqual(record.reason, 'Required guardian permissions collected by event organizers.')
        invalid_record = ShowcaseConsentAudit(event=self.event, actor_user=self.owner,
            reason='', organization_id=self.org.id, created_by=self.owner)
        with self.assertRaisesMessage(ValidationError, 'confirmation statement'):
            invalid_record.full_clean()
        page = self.request('get', self.event_url)
        self.assertContains(page, 'Consent was confirmed')
        self.assertContains(page, 'Required guardian permissions collected')

        with self.assertRaisesMessage(ValidationError, 'already been confirmed'):
            confirm_showcase_minor_consent(self.event, 'Duplicate confirmation',
                organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            ShowcaseConsentAudit.objects.filter(pk=record.pk).update(reason='changed')
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            ShowcaseConsentAudit.objects.filter(pk=record.pk).delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            record.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            record.delete()

        edited_without_minors = self.request('post', self.event_url, {
            'action': 'edit', 'name': self.event.name, 'starts_at': '', 'ends_at': '',
            'includes_minors': '', 'minimum_judges_per_exhibit': '1',
            'branding_tagline': '', 'branding_accent': 'blue',
        })
        self.assertEqual(edited_without_minors.status_code, 302)
        self.event.refresh_from_db()
        self.assertIsNone(self.event.consent_confirmed_at)
        self.assertEqual(ShowcaseConsentAudit.objects.filter(event=self.event).count(), 1)
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'confirm_consent', 'confirmation': 'on', 'reason': 'Not a minors event',
        }).status_code, 404)

        edit_back_to_minors = self.request('post', self.event_url, {
            'action': 'edit', 'name': self.event.name, 'starts_at': '', 'ends_at': '',
            'includes_minors': 'on', 'minimum_judges_per_exhibit': '1',
            'branding_tagline': '', 'branding_accent': 'blue',
        })
        self.assertEqual(edit_back_to_minors.status_code, 302)
        self.event.refresh_from_db()
        self.assertTrue(self.event.includes_minors)
        self.assertIsNone(self.event.consent_confirmed_at)
        self.assertEqual(self.request('get', self.event_url).status_code, 200)
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'confirm_consent', 'confirmation': 'on',
            'reason': 'Consent reconfirmed after event update',
        }).status_code, 302)

        exhibit = self.event.exhibits.first()
        replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100', 'scale': 'points'},
        ], organization=self.org.as_claim(), user=self.owner)
        invitation, _ = issue_showcase_judge_invitation(self.event, 'judge@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)

        opened = self.request('post', transition_url, {
            'target_status': ShowcaseEvent.Status.OPEN, 'reason': 'Start the rated event',
        })
        self.assertEqual(opened.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.OPEN)
        self.assertEqual(ShowcaseConsentAudit.objects.filter(event=self.event).count(), 2)
        with self.assertRaises(PermissionDenied):
            confirm_showcase_minor_consent(self.event, 'Late confirmation',
                organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'confirm_consent', 'confirmation': 'on', 'reason': 'Too late',
        }).status_code, 404)

    def test_empty_showcase_open_error_is_rendered_once(self):
        self.sign_in()
        response = self.request('post', reverse('showcase_event_transition', args=(self.event.pk,)), {
            'target_status': ShowcaseEvent.Status.OPEN,
            'reason': 'Check the empty exhibit guard',
        })
        message = 'Add at least one exhibit before opening this Showcase event.'

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode().count(message), 1)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.DRAFT)
        self.assertEqual(ShowcaseEventAudit.objects.filter(event=self.event).count(), 0)

    def test_consent_confirmation_requires_a_minors_event(self):
        self.sign_in()
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'confirm_consent', 'confirmation': 'on', 'reason': 'Not applicable',
        }).status_code, 404)
        self.assertEqual(ShowcaseConsentAudit.objects.count(), 0)

    def test_empty_showcase_cannot_open(self):
        self.sign_in()
        with self.assertRaisesMessage(ValidationError, 'at least one exhibit'):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.org.as_claim(), user=self.owner, reason='Open empty event')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.DRAFT)
        self.assertEqual(ShowcaseEventAudit.objects.count(), 0)

        for target, reason in (([], 'Bad target'), (ShowcaseEvent.Status.OPEN, ''),
                               (ShowcaseEvent.Status.OPEN, 'x' * 501),
                               ('unknown-status', 'Bad status'),
                               (ShowcaseEvent.Status.CLOSED, 'Skip open')):
            with self.assertRaises(ValidationError):
                transition_showcase_event(self.event, target,
                    organization=self.org.as_claim(), user=self.owner, reason=reason)
        with self.assertRaises(PermissionDenied):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.other_org.as_claim(), user=self.owner, reason='Wrong org')
        with self.assertRaises(PermissionDenied):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.org.as_claim(), user=self.other_owner, reason='Wrong owner')

    def test_showcase_with_minors_requires_consent_before_opening(self):
        ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Student exhibit')
        self.event.includes_minors = True
        self.event.minimum_judges_per_exhibit = 0
        self.event.save(update_fields=('includes_minors', 'minimum_judges_per_exhibit'))

        with self.assertRaisesMessage(ValidationError, 'Confirm participant consent'):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.org.as_claim(), user=self.owner, reason='Open event')

        confirm_showcase_minor_consent(self.event, 'Consent records checked',
            organization=self.org.as_claim(), user=self.owner)
        opened = transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
            organization=self.org.as_claim(), user=self.owner, reason='Open event')
        self.assertEqual(opened.status, ShowcaseEvent.Status.OPEN)

    def test_showcase_setup_uses_five_stages_and_combines_judges_with_consent(self):
        self.sign_in()
        self.event.includes_minors = True
        self.event.save(update_fields=('includes_minors',))

        response = self.request('get', self.event_url)

        self.assertEqual(len(response.context['setup_steps']), 5)
        self.assertEqual([step['label'] for step in response.context['setup_steps']], [
            'Event details', 'Exhibits', 'Rubric',
            'Judge assignments and consent', 'Open Showcase',
        ])
        combined = response.context['setup_steps'][3]
        self.assertFalse(combined['complete'])
        self.assertEqual(combined['href'], '#minor-consent-heading')

    def test_event_cannot_open_without_rubric_or_full_active_invited_judge_coverage(self):
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Judged exhibit')
        with self.assertRaisesMessage(ValidationError, 'Configure judge criteria'):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.org.as_claim(), user=self.owner, reason='Open event')

        replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100', 'scale': 'points'},
        ], organization=self.org.as_claim(), user=self.owner)
        with self.assertRaisesMessage(ValidationError, 'Assign enough active invited judges'):
            transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
                organization=self.org.as_claim(), user=self.owner, reason='Open event')

        invitation, _ = issue_showcase_judge_invitation(self.event, 'judge@example.org',
            timezone.now() + timedelta(hours=1), organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        opened = transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
            organization=self.org.as_claim(), user=self.owner, reason='Open event')
        self.assertEqual(opened.status, ShowcaseEvent.Status.OPEN)

    def test_judged_results_cannot_publish_until_required_scorecards_are_submitted(self):
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Reviewed exhibit')
        criteria = replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100', 'scale': 'points'},
        ], organization=self.org.as_claim(), user=self.owner)
        invitation, token = issue_showcase_judge_invitation(self.event,
            'judge@example.org', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        transition_showcase_event(self.event, ShowcaseEvent.Status.OPEN,
            organization=self.org.as_claim(), user=self.owner, reason='Open judging.')
        transition_showcase_event(self.event, ShowcaseEvent.Status.CLOSED,
            organization=self.org.as_claim(), user=self.owner, reason='Close judging.')
        with self.assertRaisesMessage(ValidationError, 'submitted judge scorecards'):
            transition_showcase_event(self.event, ShowcaseEvent.Status.PUBLISHED,
                organization=self.org.as_claim(), user=self.owner, reason='Publish results.')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, ShowcaseEvent.Status.CLOSED)
        self.assertFalse(self.event.public_results)

        submit_showcase_judge_evaluation(token, exhibit.public_id,
            {str(criteria[0].pk): '85'})
        transition_showcase_event(self.event, ShowcaseEvent.Status.PUBLISHED,
            organization=self.org.as_claim(), user=self.owner, reason='Publish reviewed results.')
        self.event.refresh_from_db()
        self.assertTrue(self.event.public_results)

    def test_lifecycle_audit_model_rejects_empty_reason_and_noop_transition(self):
        self.sign_in()
        for reason, target in (('', ShowcaseEvent.Status.OPEN),
                               ('No change', ShowcaseEvent.Status.DRAFT)):
            audit = ShowcaseEventAudit(event=self.event, actor_user=self.owner,
                from_status=ShowcaseEvent.Status.DRAFT, to_status=target, reason=reason,
                organization_id=self.org.id, created_by=self.owner)
            with self.assertRaises(ValidationError):
                audit.full_clean()

    def test_showcase_event_create_validation_and_success(self):
        self.sign_in()
        url = reverse('showcase_event_create')
        res = self.request('get', url)
        self.assertEqual(res.status_code, 200)

        res = self.request('post', url, {'name': ''})
        self.assertEqual(res.status_code, 400)

        res = self.request('post', url, {
            'name': 'Innovation Fair 2026',
            'audience_voting': '1',
            'voting_mode': 'single',
            'allow_guest_judges': '1',
            'includes_minors': '0',
            'minimum_judges_per_exhibit': '1',
        })
        self.assertEqual(res.status_code, 302)
        created = ShowcaseEvent.objects.get(name='Innovation Fair 2026')
        self.assertEqual(created.organization_id, self.org.id)
        self.assertEqual(created.created_by, self.owner)

    def test_showcase_event_delete_draft_safeguards(self):
        self.sign_in()
        res = self.request('post', self.event_url, {'action': 'delete_event'})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(ShowcaseEvent.objects.filter(pk=self.event.pk).exists())

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save()
        res = self.request('post', self.event_url, {'action': 'delete_event', 'confirm_delete': 'yes'})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(ShowcaseEvent.objects.filter(pk=self.event.pk).exists())
        self.event.status = ShowcaseEvent.Status.DRAFT
        self.event.save()

        ShowcaseEventAudit.objects.create(event=self.event, actor_user=self.owner,
            from_status=ShowcaseEvent.Status.DRAFT, to_status=ShowcaseEvent.Status.OPEN,
            reason='Initial audit', organization_id=self.org.id, created_by=self.owner)
        res = self.request('post', self.event_url, {'action': 'delete_event', 'confirm_delete': 'yes'})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(ShowcaseEvent.objects.filter(pk=self.event.pk).exists())

        clean_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Clean draft')
        res = self.request('post', reverse('showcase_event_manage', args=(clean_event.pk,)),
            {'action': 'delete_event', 'confirm_delete': 'yes'})
        self.assertEqual(res.status_code, 302)
        self.assertFalse(ShowcaseEvent.objects.filter(pk=clean_event.pk).exists())

    def test_showcase_live_activity_view_status_gate(self):
        self.sign_in()
        url = reverse('showcase_live_activity', args=(self.event.pk,))
        self.assertEqual(self.request('get', url).status_code, 404)

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save()
        res = self.request('get', url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/json')

    def test_showcase_exhibit_import_cancel_and_embedded_flow(self):
        self.sign_in()
        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))

        res = self.request('post', import_url, {'action': 'cancel'})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], self.event_url)

        wb_file = self.upload()
        res = self.request('post', f'{import_url}?embedded=1', {'file': wb_file})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], self.event_url)

        res = self.request('post', f'{import_url}?embedded=1', {'action': 'apply', 'preview_token': 'bad'})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], self.event_url)

        res = self.request('post', f'{import_url}?embedded=1', {'file': ''})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], self.event_url)

    def test_showcase_judge_workspace_conflict_branches(self):
        exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Exhibit conflict')
        replace_showcase_judge_criteria(self.event, [
            {'name': 'Quality', 'weight': '100', 'scale': 'points'},
        ], organization=self.org.as_claim(), user=self.owner)
        invitation, token = issue_showcase_judge_invitation(self.event,
            'conflictjudge@example.org', timezone.now() + timedelta(hours=1),
            organization=self.org.as_claim(), user=self.owner)
        replace_showcase_invited_judge_assignments(self.event,
            {invitation.pk: [exhibit.pk]}, organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save()

        ws_url = reverse('showcase_judge_workspace', args=(token,))

        declare_showcase_judge_conflict(token, exhibit.public_id, 'First reason')
        res = self.request('post', ws_url, {
            'exhibit_id': str(exhibit.public_id),
            'action': 'declare_conflict',
            'reason': 'Second reason',
        })
        self.assertEqual(res.status_code, 400)
        self.assertContains(res, 'already been declared', status_code=400)

        res = self.request('post', ws_url, {
            'exhibit_id': str(exhibit.public_id),
            'action': 'score',
        })
        self.assertEqual(res.status_code, 400)
        self.assertContains(res, 'marked as a conflict', status_code=400)

    def test_showcase_step_navigation_save_advance_inline_preview_and_removals(self):
        from django.core import signing
        from service_domains.views import SHOWCASE_IMPORT_SESSION_KEY, SHOWCASE_IMPORT_TOKEN_SALT

        self.sign_in()
        saved_res = self.request('post', self.event_url, {
            'action': 'edit',
            'name': 'Spring Showcase Updated',
            'starts_at': '',
            'ends_at': '',
            'includes_minors': '',
            'minimum_judges_per_exhibit': '1',
            'branding_tagline': 'Great projects',
            'branding_accent': 'blue',
        })
        self.assertEqual(saved_res.status_code, 302)
        self.assertEqual(saved_res['Location'], f'{self.event_url}?step=details&saved=1')
        details_page = self.request('get', f'{self.event_url}?step=details&saved=1')
        self.assertContains(details_page, 'Event details saved successfully.')
        self.assertContains(details_page, 'id="showcase-unsaved-dialog"')
        self.assertContains(details_page, 'data-panel-details-form')

        advance_res = self.request('post', self.event_url, {
            'action': 'edit',
            'name': 'Spring Showcase Advanced',
            'starts_at': '',
            'ends_at': '',
            'includes_minors': '',
            'minimum_judges_per_exhibit': '1',
            'branding_tagline': 'Great projects',
            'branding_accent': 'blue',
            'advance': '1',
        })
        self.assertEqual(advance_res.status_code, 302)
        self.assertEqual(advance_res['Location'], f'{self.event_url}?step=exhibits')

        exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event, name='Removable Exhibit'
        )
        criterion = replace_showcase_judge_criteria(self.event, [
            {'name': 'Clarity', 'weight': '100', 'scale': 'points'},
        ], organization=self.org.as_claim(), user=self.owner)[0]

        rm_exhibit = self.request('post', self.event_url, {
            'action': 'remove_exhibit', 'exhibit_id': str(exhibit.pk), 'setup_step': 'exhibits',
        })
        self.assertEqual(rm_exhibit.status_code, 302)
        self.assertEqual(rm_exhibit['Location'], f'{self.event_url}?step=exhibits')
        self.assertFalse(ShowcaseExhibit.objects.filter(pk=exhibit.pk).exists())

        rm_crit = self.request('post', self.event_url, {
            'action': 'remove_criterion', 'criterion_id': str(criterion.pk), 'setup_step': 'criteria',
        })
        self.assertEqual(rm_crit.status_code, 302)
        self.assertEqual(rm_crit['Location'], f'{self.event_url}?step=criteria')
        self.assertFalse(self.event.judge_criteria.filter(pk=criterion.pk).exists())

        self.assertEqual(self.request('post', self.event_url, {
            'action': 'remove_exhibit', 'exhibit_id': '999999',
        }).status_code, 404)
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'remove_criterion', 'criterion_id': '999999',
        }).status_code, 404)

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status',))
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'remove_exhibit', 'exhibit_id': '1',
        }).status_code, 404)
        self.assertEqual(self.request('post', self.event_url, {
            'action': 'remove_criterion', 'criterion_id': '1',
        }).status_code, 404)
        self.event.status = ShowcaseEvent.Status.DRAFT
        self.event.save(update_fields=('status',))

        session = self.client.session
        session[SHOWCASE_IMPORT_SESSION_KEY] = 'invalid-signed-token'
        session.save()
        bad_sig_page = self.request('get', self.event_url)
        self.assertEqual(bad_sig_page.status_code, 200)
        self.assertNotIn(SHOWCASE_IMPORT_SESSION_KEY, self.client.session)

        wrong_owner_token = signing.dumps(
            {'event': self.event.pk, 'organization': self.org.id, 'user': self.other_owner.pk, 'parsed': {}},
            salt=SHOWCASE_IMPORT_TOKEN_SALT,
        )
        session = self.client.session
        session[SHOWCASE_IMPORT_SESSION_KEY] = wrong_owner_token
        session.save()
        wrong_owner_page = self.request('get', self.event_url)
        self.assertEqual(wrong_owner_page.status_code, 200)
        self.assertNotIn(SHOWCASE_IMPORT_SESSION_KEY, self.client.session)

        import_url = reverse('showcase_exhibit_import', args=(self.event.pk,))
        self.request('post', f'{import_url}?embedded=1', {'file': self.upload()})
        preview_page = self.request('get', self.event_url)
        self.assertEqual(preview_page.context['active_step'], 'exhibits')
        self.assertContains(preview_page, 'id="exhibit-inline-preview"')
        self.assertContains(preview_page, 'Solar pump')

        step_preserve = self.request('post', self.event_url, {
            'action': 'edit', 'name': '', 'setup_step': 'details',
        })
        self.assertEqual(step_preserve.status_code, 200)
        self.assertEqual(step_preserve.context['active_step'], 'details')

    def test_enterprise_service_dashboard_states_across_services(self):
        from django.test import override_settings
        from assessments.models import AssessmentSession
        from service_domains.models import Competition, PanelHearing, TrainingSeries

        self.sign_in()

        # 1. Showcase dashboard: between/scheduled, live/open, and empty states
        between_sc = self.request('get', reverse('showcase_event_list'))
        self.assertEqual(between_sc.status_code, 200)
        self.assertContains(between_sc, 'hero-banner between-banner')
        self.assertContains(between_sc, 'bento-grid')
        self.assertContains(between_sc, 'Exhibition Hall Rating &amp; Scorecard Telemetry')
        self.assertContains(between_sc, 'data-event-search')
        self.assertContains(between_sc, 'data-filter-trigger')

        with override_settings(LIVEGRADE_LIVE_SERVICES=['showcase']):
            create_sc = self.client.get(reverse('showcase_event_create'))
            self.assertContains(create_sc, f'href="{reverse("showcase_event_create")}" class="nav-link active" aria-current="page"')

        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status',))
        live_sc = self.request('get', reverse('showcase_event_list'))
        self.assertContains(live_sc, 'EXHIBITION FLOOR OPEN')
        self.assertContains(live_sc, 'live-dot')

        self.event.delete()
        empty_sc = self.request('get', reverse('showcase_event_list'))
        self.assertContains(empty_sc, 'empty-hero')
        self.assertContains(empty_sc, 'empty-steps')
        self.assertNotContains(empty_sc, 'sc-kpis')

        # 2. Classroom dashboard: empty, between/scheduled, and live states
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'classroom'):
            empty_cls = self.client.get(reverse('assessment_dashboard'))
            self.assertEqual(empty_cls.status_code, 200)
            self.assertContains(empty_cls, 'You haven’t created a classroom session yet')
            self.assertContains(empty_cls, 'Session stages')

            cls_session = AssessmentSession.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                service='classroom', name='Capstone Defense',
                status=AssessmentSession.Status.DRAFT,
            )
            between_cls = self.client.get(reverse('assessment_dashboard'))
            self.assertContains(between_cls, 'Total sessions')
            self.assertContains(between_cls, 'In setup')
            self.assertContains(between_cls, 'Capstone Defense')
            session_list = self.client.get(reverse('assessment_session_list'))
            self.assertContains(session_list, 'data-session-auto-filter')
            self.assertContains(session_list, 'id="session-search"')

            cls_session.status = AssessmentSession.Status.LIVE
            cls_session.save(update_fields=('status',))
            live_cls = self.client.get(reverse('assessment_dashboard'))
            self.assertContains(live_cls, 'Live in the classroom')
            self.assertContains(live_cls, '1 active session')
            self.assertContains(live_cls, 'Capstone Defense')

        # 3. Panel dashboard: empty, draft attention, and live states
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'panel'):
            empty_pnl = self.client.get(reverse('panel_hearing_list'))
            self.assertEqual(empty_pnl.status_code, 200)
            self.assertContains(empty_pnl, 'empty-hero')

            hearing = PanelHearing.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                name='Doctoral Defense Chamber', status=PanelHearing.Status.OPEN,
            )
            live_pnl = self.client.get(reverse('panel_hearing_list'))
            self.assertContains(live_pnl, 'LIVE HEARING IN PROGRESS')
            self.assertContains(live_pnl, 'attention-card')
            self.assertContains(live_pnl, 'Examiner Consensus &amp; Score Convergence')
            self.assertContains(live_pnl, 'data-hearing-filter')
            self.assertContains(live_pnl, 'data-hearing-search')
            self.assertEqual(live_pnl.context['dashboard_stats']['total'], 1)

        # 4. Team and Training dashboard: empty and active cohort states
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            empty_tm = self.client.get(reverse('training_series_list'))
            self.assertEqual(empty_tm.status_code, 200)
            self.assertContains(empty_tm, 'empty-hero')

            TrainingSeries.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                name='Engineering Residency Cohort',
            )
            active_tm = self.client.get(reverse('training_series_list'))
            self.assertContains(active_tm, 'ACTIVE TRAINING WORKSPACE')
            self.assertContains(active_tm, 'Longitudinal Competency Growth')
            self.assertContains(active_tm, 'data-series-search')
            self.assertEqual(active_tm.context['dashboard_stats']['total_series'], 1)

        # 5. Competition dashboard: empty, scheduled, and active attention states
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'competition'):
            empty_cmp = self.client.get(reverse('competition_list'))
            self.assertEqual(empty_cmp.status_code, 200)
            self.assertContains(empty_cmp, 'empty-hero')

            comp = Competition.objects.create(
                organization_id=self.org.id, created_by=self.owner,
                name='Global Pitch Finals', status=Competition.Status.ACTIVE,
                audience_weight=35,
            )
            active_cmp = self.client.get(reverse('competition_list'))
            self.assertContains(active_cmp, 'ACTIVE TOURNAMENT ROUND')
            self.assertContains(active_cmp, 'attention-card')
            self.assertContains(active_cmp, 'Advancement Differential &amp; Composite Scoring')
            self.assertContains(active_cmp, 'Scoring Formula Architecture')
            self.assertContains(active_cmp, '65 / 35')
            self.assertContains(active_cmp, 'data-comp-filter')
            self.assertContains(active_cmp, 'data-comp-search')
            self.assertEqual(active_cmp.context['dashboard_stats']['active'], 1)
            self.assertEqual(active_cmp.context['dashboard_stats']['judge_pct'], 65)
            self.assertEqual(active_cmp.context['dashboard_stats']['audience_pct'], 35)


