"""Failure paths of shared plumbing: throttles, broadcasts, mail task, workbook limits, settings."""
import importlib
import io
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from accounts.testing import make_org, make_user
from assessments import sheets
from assessments.models import AssessmentSession, ClassroomPoll
from service_domains import realtime, throttling
from service_domains.form_mixins import apply_form_field_styles
from service_domains.tasks import send_coassessment_examiner_invitation_email


def fake_cache(add=True, incr=None, error=None):
    cache = MagicMock()
    if error:
        cache.add.side_effect = error
    elif isinstance(incr, Exception):
        cache.add.side_effect = [False, True]
    else:
        cache.add.return_value = add
    if isinstance(incr, Exception):
        cache.incr.side_effect = incr
    else:
        cache.incr.return_value = incr
    return cache


class ThrottleFailurePathTests(SimpleTestCase):
    def examiner_state(self, cache):
        invitation = SimpleNamespace(token_digest='abc')
        with patch('service_domains.throttling.cache', cache):
            return throttling.allow_panel_examiner_state_request(invitation)

    def event(self, cache, addr='127.0.0.1'):
        request = RequestFactory().get('/', REMOTE_ADDR=addr)
        with patch('service_domains.throttling.cache', cache):
            return throttling.allow_public_event_request(request, service='x', event_id=1, limit=3, window=60)

    def test_state_requests_count_then_stop_at_the_limit(self):
        self.assertTrue(self.examiner_state(fake_cache(add=True)))
        self.assertTrue(self.examiner_state(fake_cache(add=False, incr=2)))
        self.assertFalse(self.examiner_state(fake_cache(add=False, incr=10 ** 6)))

    def test_state_requests_restart_an_expired_window_and_fail_closed_on_errors(self):
        self.assertTrue(self.examiner_state(fake_cache(add=False, incr=ValueError())))
        self.assertIsNone(self.examiner_state(fake_cache(error=RuntimeError('down'))))

    def test_public_event_requests_count_expire_and_fail_closed(self):
        self.assertTrue(self.event(fake_cache(add=True)))
        self.assertTrue(self.event(fake_cache(add=False, incr=3)))
        self.assertFalse(self.event(fake_cache(add=False, incr=4)))
        self.assertTrue(self.event(fake_cache(add=False, incr=ValueError())))
        self.assertIsNone(self.event(fake_cache(error=RuntimeError('down'))))

    def test_public_event_requests_without_a_valid_address_are_refused(self):
        self.assertIsNone(self.event(fake_cache(), addr='not-an-ip'))


class BroadcastFailurePathTests(SimpleTestCase):
    def test_leaderboard_broadcast_is_best_effort(self):
        with patch.object(realtime, 'get_channel_layer', return_value=None):
            self.assertIsNone(realtime.broadcast_competition_leaderboard_state('p', True))
        layer = MagicMock()
        with patch.object(realtime, 'get_channel_layer', return_value=layer), \
                patch.object(realtime, 'async_to_sync') as bridge:
            realtime.broadcast_competition_leaderboard_state('p', 1)
            self.assertEqual(bridge.return_value.call_args.args[0], 'competition_leaderboard_p')
            bridge.return_value.side_effect = RuntimeError('down')
            with self.assertLogs(realtime.logger, 'WARNING'):
                realtime.broadcast_competition_leaderboard_state('p', 1)

    def test_panel_broadcast_reaches_each_group_and_survives_failure(self):
        with patch.object(realtime, 'get_channel_layer', return_value=None):
            self.assertIsNone(realtime.broadcast_panel_hearing_event(5, 'x'))
        with patch.object(realtime, 'get_channel_layer', return_value=MagicMock()), \
                patch.object(realtime, 'async_to_sync') as bridge:
            realtime.broadcast_panel_hearing_event(SimpleNamespace(pk=5), 'x', {'a': 1})
            self.assertEqual(bridge.return_value.call_count, 3)
            bridge.return_value.side_effect = RuntimeError('down')
            with self.assertLogs(realtime.logger, 'WARNING'):
                realtime.broadcast_panel_hearing_event(5, 'x')

    def test_scheduled_leaderboard_update_waits_for_commit(self):
        with patch.object(realtime.transaction, 'on_commit') as on_commit, \
                patch.object(realtime, 'broadcast_competition_leaderboard_state') as send:
            realtime.schedule_competition_leaderboard_update(7, True)
            on_commit.call_args.args[0]()
        send.assert_called_once_with('7', True)


@override_settings(LIVEGRADE_PUBLIC_BASE_URL='https://grade.example.org',
                   DEFAULT_FROM_EMAIL='LiveGrade <noreply@example.org>')
class ExaminerInvitationTaskTests(SimpleTestCase):
    def examiner(self, token='tok'):
        return SimpleNamespace(
            pk=1, invite_token=token, display_name='Dr Cho', email='cho@example.org', is_chair=False,
            session=SimpleNamespace(name='Defense Day'))

    def run_task(self, examiner, mail):
        with patch('service_domains.tasks.CoAssessmentExaminer') as model, \
                patch('service_domains.tasks.send_mail', mail):
            model.objects.select_related.return_value.filter.return_value.first.return_value = examiner
            return send_coassessment_examiner_invitation_email.run(1)

    def test_missing_examiner_or_token_is_ignored(self):
        self.assertEqual(self.run_task(None, MagicMock()), 'ignored')
        self.assertEqual(self.run_task(self.examiner(token=''), MagicMock()), 'ignored')

    def test_mail_result_is_reported(self):
        self.assertEqual(self.run_task(self.examiner(), MagicMock(return_value=1)), 'sent')
        self.assertEqual(self.run_task(self.examiner(), MagicMock(return_value=0)), 'mail_not_accepted')

    def test_mail_errors_are_retried(self):
        with self.assertRaises(Exception):
            self.run_task(self.examiner(), MagicMock(side_effect=OSError('smtp down')))


class FormStyleTests(SimpleTestCase):
    def test_multi_checkbox_is_styled_like_other_choice_inputs(self):
        fields = {
            'many': forms.MultipleChoiceField(choices=[('a', 'A')], widget=forms.CheckboxSelectMultiple),
            'one': forms.CharField(help_text='hint'),
        }
        apply_form_field_styles(fields)
        self.assertEqual(fields['many'].widget.attrs['class'], 'form-check-input')
        self.assertIn('form-control', fields['one'].widget.attrs['class'])
        self.assertEqual(fields['one'].widget.attrs['aria-describedby'], 'id_one_helptext')


class WorkbookLimitTests(SimpleTestCase):
    def test_archive_with_too_many_entries_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for number in range(1001):
                archive.writestr(f'f{number}', '')
        buffer.seek(0)
        self.assertIsNone(sheets.read_rows(buffer))

    def test_oversized_file_is_refused(self):
        class Huge(io.BytesIO):
            def tell(self):
                return 11 * 1024 * 1024
        self.assertIsNone(sheets.read_rows(Huge(b'x')))

    def test_rows_wider_than_the_limit_are_refused_while_reading(self):
        sheet = MagicMock(max_row=2, max_column=5)
        sheet.iter_rows.return_value = [[SimpleNamespace(value=1)] * 101]
        workbook = MagicMock(worksheets=[sheet])
        with patch.object(sheets.zipfile, 'ZipFile'), patch.object(sheets.openpyxl, 'load_workbook', return_value=workbook):
            self.assertIsNone(sheets.read_rows(io.BytesIO(b'x')))
        workbook.close.assert_called_once()

    def test_sheets_larger_than_the_limit_are_refused(self):
        sheet = MagicMock(max_row=10001, max_column=5)
        workbook = MagicMock(worksheets=[sheet])
        with patch.object(sheets.zipfile, 'ZipFile'), patch.object(sheets.openpyxl, 'load_workbook', return_value=workbook):
            self.assertIsNone(sheets.read_rows(io.BytesIO(b'x')))


class SettingsGuardTests(SimpleTestCase):
    def reload_with(self, **env):
        from config import settings as project_settings
        try:
            with patch.dict(os.environ, env):
                with self.assertRaises(ImproperlyConfigured):
                    importlib.reload(project_settings)
        finally:
            importlib.reload(project_settings)

    def test_public_base_url_with_a_bad_port_is_rejected(self):
        self.reload_with(LIVEGRADE_PUBLIC_BASE_URL='https://grade.example.org:99999')

    def test_request_budgets_must_be_positive(self):
        self.reload_with(COMPETITION_JOIN_RATE_LIMIT='0')
        self.reload_with(POLL_PUBLIC_READ_RATE_LIMIT='0')


class PollModelRuleTests(TestCase):
    def setUp(self):
        self.org = make_org('Poll rules')
        self.owner = make_user('poll-rules-owner')

    def poll(self, **fields):
        base = dict(question='Q?', standalone=False)
        base.update(fields)
        return ClassroomPoll(**base)

    def test_standalone_polls_cannot_belong_to_a_session(self):
        session = AssessmentSession.objects.create(organization_id=self.org.pk, created_by=self.owner, name='S')
        with self.assertRaises(ValidationError):
            self.poll(standalone=True, assessment_session=session, organization_id=self.org.pk,
                      created_by=self.owner).clean()

    def test_standalone_polls_need_an_owner_and_organization(self):
        with self.assertRaises(ValidationError):
            self.poll(standalone=True).clean()

    def test_polls_belong_to_classroom_sessions(self):
        session = AssessmentSession.objects.create(
            organization_id=self.org.pk, created_by=self.owner, name='T', service='team')
        with self.assertRaises(ValidationError):
            self.poll(assessment_session=session).clean()

    def test_confirmed_option_must_belong_to_the_poll(self):
        session = AssessmentSession.objects.create(organization_id=self.org.pk, created_by=self.owner, name='C')
        first = ClassroomPoll.objects.create(assessment_session=session, question='A?')
        second = ClassroomPoll.objects.create(assessment_session=session, question='B?')
        option = second.options.create(label='x', order=1)
        first.confirmed_option = option
        with self.assertRaises(ValidationError):
            first.clean()
