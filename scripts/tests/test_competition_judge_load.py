import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts.competition_judge_load import (
    main, parse_entry_scores, read_invitation_tokens, run_load, submit_one,
)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class CompetitionJudgeLoadTests(unittest.TestCase):
    def private_token_file(self, content):
        handle = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False)
        handle.write(content)
        handle.close()
        os.chmod(handle.name, 0o600)
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_reads_exact_count_of_unique_well_formed_tokens(self):
        path = self.private_token_file('A' * 32 + '\n' + 'b' * 43 + '\n')
        self.assertEqual(read_invitation_tokens(path, 2), ['A' * 32, 'b' * 43])

    def test_refuses_insecure_token_file_duplicate_tokens_and_invalid_tokens(self):
        path = self.private_token_file('a' * 32 + '\n')
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ValueError, 'accessible by group'):
            read_invitation_tokens(path, 1)
        os.chmod(path, 0o600)
        with open(path, 'w', encoding='utf-8') as target:
            target.write('a' * 32 + '\n' + 'a' * 32 + '\n')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            read_invitation_tokens(path, 2)
        with open(path, 'w', encoding='utf-8') as target:
            target.write('short\n')
        with self.assertRaisesRegex(ValueError, 'invalid token'):
            read_invitation_tokens(path, 1)
        with self.assertRaisesRegex(ValueError, 'expected 2'):
            read_invitation_tokens(path, 2)

    def test_parses_valid_scores_and_refuses_bad_or_duplicate_entries(self):
        self.assertEqual(parse_entry_scores(['12=80', '13=99.75', '14=100.00']), [
            ('entry_12', '80'), ('entry_13', '99.75'), ('entry_14', '100.00')])
        for values in ([], ['0=10'], ['1=101'], ['1=10', '1=20'], ['x=10']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_entry_scores(values)

    def test_submits_json_state_and_scorecard_without_retrying_or_exposing_token(self):
        token = 'private-judge-token-12345678901234567890'
        opener = type('Opener', (), {'open': lambda _self, request, timeout: (
            FakeResponse(200, json.dumps({'status': 'open', 'csrf_token': 'csrf'}).encode())
            if request.data is None else FakeResponse(200, b'{"status":"accepted"}'))})()
        with patch('scripts.competition_judge_load.build_opener', return_value=opener):
            result = submit_one('https://test.example/competition/judge/', token,
                [('entry_1', '80')], 1)
        self.assertTrue(result['ok'])
        self.assertIn('flow_seconds', result)
        self.assertIn('post_seconds', result)

    def test_run_load_counts_failures_and_reports_both_latency_summaries(self):
        outcomes = iter((
            {'ok': True, 'status': 200, 'flow_seconds': .3, 'post_seconds': .1},
            {'ok': False, 'status': 429, 'flow_seconds': 1.2, 'post_seconds': .2},
        ))
        with patch('scripts.competition_judge_load.submit_one',
                   side_effect=lambda *_: next(outcomes)):
            summary = run_load('https://test.example/competition/judge/',
                ['a' * 32, 'b' * 32], [('entry_1', '80')], 2, 1)
        self.assertEqual(summary['successful_scorecards'], 1)
        self.assertEqual(summary['failed_scorecards'], 1)
        self.assertEqual(summary['visitor_flow_latency_seconds']['p95'], 1.2)
        self.assertEqual(summary['post_latency_seconds']['p95'], .2)
        self.assertEqual(summary['failures_by_status'], {'429': 1})

    def test_main_requires_exact_approved_host_dedicated_event_and_tokens(self):
        base = ['--scorecard-base-url', 'https://bad.example/competition/judge/',
            '--approved-host', 'testing.example', '--invitation-tokens-file', '-',
            '--entry-score', '1=80', '--confirm-dedicated-test-competition']
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(base)
        self.assertEqual(error.exception.code, 2)
        base[1] = 'https://testing.example/competition/judge/'
        base.remove('--confirm-dedicated-test-competition')
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(base)
        self.assertEqual(error.exception.code, 2)

    def test_main_gate_uses_full_flow_p95_and_never_prints_tokens(self):
        token = 'sensitive-judge-token-12345678901234567890'
        path = self.private_token_file(token + '\n')
        args = ['--scorecard-base-url', 'https://testing.example/competition/judge/',
            '--approved-host', 'testing.example', '--invitation-tokens-file', path,
            '--visitors', '1', '--workers', '1', '--entry-score', '1=80',
            '--confirm-dedicated-test-competition']
        summary = {'invitations': 1, 'workers': 1, 'successful_scorecards': 1,
            'failed_scorecards': 0, 'wall_seconds': .5,
            'visitor_flow_latency_seconds': {'p95': 1.1},
            'post_latency_seconds': {'p95': .2}}
        output = StringIO()
        with patch('scripts.competition_judge_load.run_load', return_value=summary), \
                redirect_stdout(output):
            result = main(args)
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())['target_passed'])
        self.assertNotIn(token, output.getvalue())

    def test_main_uses_unrounded_wall_time_for_the_one_minute_gate(self):
        path = self.private_token_file('a' * 32 + '\n')
        args = ['--scorecard-base-url', 'https://testing.example/competition/judge/',
            '--approved-host', 'testing.example', '--invitation-tokens-file', path,
            '--visitors', '1', '--workers', '1', '--entry-score', '1=80',
            '--confirm-dedicated-test-competition']
        summary = {'invitations': 1, 'workers': 1, 'successful_scorecards': 1,
            'failed_scorecards': 0, 'wall_seconds': 60.0004,
            'visitor_flow_latency_seconds': {'p95': .2}, 'post_latency_seconds': {'p95': .1}}
        output = StringIO()
        with patch('scripts.competition_judge_load.run_load', return_value=summary), \
                redirect_stdout(output):
            result = main(args)
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())['target_passed'])


if __name__ == '__main__':
    unittest.main()
