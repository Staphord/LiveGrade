import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from unittest.mock import patch

from scripts.showcase_rating_load import CsrfInputParser, main, percentile, run_load


class ShowcaseLoadHelperTests(unittest.TestCase):
    def test_csrf_parser_reads_only_the_named_input(self):
        parser = CsrfInputParser()
        parser.feed('<input name="other" value="no"><input name="csrfmiddlewaretoken" value="abc">')
        self.assertEqual(parser.value, 'abc')

    def test_percentile_uses_nearest_rank_and_handles_empty_input(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(percentile([4, 1, 3, 2], .50), 2)
        self.assertEqual(percentile([4, 1, 3, 2], .95), 4)

    def test_run_load_summarizes_successes_failures_and_post_latency(self):
        outcomes = iter((
            {'ok': True, 'status': 200, 'seconds': .2, 'flow_seconds': .2,
             'post_seconds': .1},
            {'ok': False, 'status': 429, 'seconds': .3, 'flow_seconds': .3,
             'post_seconds': .2},
        ))
        with patch('scripts.showcase_rating_load.submit_one', side_effect=lambda *_: next(outcomes)):
            summary = run_load('https://test.example/rate/', 2, 2, 1)
        self.assertEqual(summary['successful_ratings'], 1)
        self.assertEqual(summary['failed_ratings'], 1)
        self.assertEqual(summary['post_latency_seconds']['p95'], .2)
        self.assertEqual(summary['visitor_flow_latency_seconds']['p95'], .3)
        self.assertEqual(summary['failures_by_status'], {'429': 1})

    def test_target_gate_uses_full_visitor_flow_latency_not_post_latency(self):
        args = ['--rating-url', 'https://testing.example.org/showcase/exhibits/'
            '123e4567-e89b-12d3-a456-426614174000/rate/',
            '--approved-host', 'testing.example.org', '--confirm-dedicated-test-event',
            '--visitors', '1', '--workers', '1', '--max-p95', '1']
        summary = {
            'visitors': 1, 'workers': 1, 'successful_ratings': 1, 'failed_ratings': 0,
            'wall_seconds': .5, 'post_latency_seconds': {'p95': .2},
            'visitor_flow_latency_seconds': {'p95': 1.1},
        }
        output = StringIO()
        with patch('scripts.showcase_rating_load.run_load', return_value=summary), \
                redirect_stdout(output):
            result = main(args)
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())['target_passed'])

    def test_target_gate_uses_unrounded_wall_time(self):
        args = ['--rating-url', 'https://testing.example.org/showcase/exhibits/'
            '123e4567-e89b-12d3-a456-426614174000/rate/',
            '--approved-host', 'testing.example.org', '--confirm-dedicated-test-event',
            '--visitors', '1', '--workers', '1']
        summary = {
            'visitors': 1, 'workers': 1, 'successful_ratings': 1, 'failed_ratings': 0,
            'wall_seconds': 3600.0004, 'post_latency_seconds': {'p95': .1},
            'visitor_flow_latency_seconds': {'p95': .2},
        }
        output = StringIO()
        with patch('scripts.showcase_rating_load.run_load', return_value=summary), \
                redirect_stdout(output):
            result = main(args)
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())['target_passed'])

    def test_main_refuses_a_rating_url_outside_the_exact_showcase_host(self):
        args = ['--rating-url', 'https://wrong.example/showcase/exhibits/'
            '123e4567-e89b-12d3-a456-426614174000/rate/',
            '--approved-host', 'testing.example.org', '--confirm-dedicated-test-event']
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(args)
        self.assertEqual(error.exception.code, 2)

    def test_main_refuses_non_showcase_paths_even_on_the_approved_host(self):
        args = ['--rating-url', 'https://testing.example.org/admin/',
            '--approved-host', 'testing.example.org', '--confirm-dedicated-test-event']
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(args)
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
