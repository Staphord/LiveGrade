#!/usr/bin/env python3
"""Measure Competition judge invitation scorecard submission at controlled load.

The input file contains one single-use invitation token per line. Treat it as
secret material; the command never prints token values or scorecard URLs.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
import json
import math
import os
import re
import stat
import sys
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (HTTPCookieProcessor, HTTPRedirectHandler, Request,
                            build_opener)


TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{20,256}$')
ENTRY_SCORE_RE = re.compile(r'^([1-9][0-9]*)=(100(?:\.0{1,2})?|[0-9]{1,2}(?:\.[0-9]{1,2})?)$')


class NoRedirectHandler(HTTPRedirectHandler):
    """Do not follow redirects that could carry a bearer token elsewhere."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def percentile(values, percentage):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentage) - 1)]


def read_invitation_tokens(path, expected_count):
    """Read a private token file and validate exact count, format and uniqueness."""
    if path == '-':
        lines = sys.stdin.read().splitlines()
    else:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise ValueError('invitation token file must not be accessible by group or other users')
        with open(path, encoding='utf-8') as source:
            lines = source.read().splitlines()
    tokens = [line.strip() for line in lines if line.strip()]
    if len(tokens) != expected_count:
        raise ValueError(f'expected {expected_count} invitation tokens')
    if any(not TOKEN_RE.fullmatch(token) for token in tokens):
        raise ValueError('invitation token file contains an invalid token')
    if len(set(tokens)) != len(tokens):
        raise ValueError('invitation token file contains duplicate tokens')
    return tokens


def parse_entry_scores(values):
    scores = []
    seen = set()
    for value in values:
        match = ENTRY_SCORE_RE.fullmatch(value)
        if not match:
            raise ValueError('each score must use ENTRY_ID=SCORE with a score from 0 to 100')
        entry_id, score = match.groups()
        if entry_id in seen:
            raise ValueError('entry score ids must be unique')
        seen.add(entry_id)
        scores.append((f'entry_{entry_id}', score))
    if not scores:
        raise ValueError('provide at least one --entry-score value')
    return scores


def _open(opener, request, timeout):
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        exc.read()
        return exc.code, b''


def submit_one(base_url, token, entry_scores, timeout):
    """Fetch CSRF state and submit one scorecard using an isolated cookie jar."""
    url = base_url + token + '/'
    opener = build_opener(HTTPCookieProcessor(CookieJar()), NoRedirectHandler())
    started = monotonic()
    try:
        get_request = Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': 'LiveGrade-Competition-load-check/1.0',
        })
        get_status, page = _open(opener, get_request, timeout)
        if get_status != 200:
            elapsed = monotonic() - started
            return {'ok': False, 'status': get_status, 'flow_seconds': elapsed}
        try:
            state = json.loads(page)
        except (TypeError, ValueError):
            elapsed = monotonic() - started
            return {'ok': False, 'status': 'invalid_state', 'flow_seconds': elapsed}
        if state.get('status') != 'open' or not isinstance(state.get('csrf_token'), str):
            elapsed = monotonic() - started
            return {'ok': False, 'status': state.get('status', 'invalid_state'),
                    'flow_seconds': elapsed}

        fields = [('csrfmiddlewaretoken', state['csrf_token']), *entry_scores]
        post_started = monotonic()
        post_request = Request(url, data=urlencode(fields).encode(), headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': urlsplit(base_url).scheme + '://' + urlsplit(base_url).netloc,
            'Referer': url,
            'User-Agent': 'LiveGrade-Competition-load-check/1.0',
        })
        post_status, body = _open(opener, post_request, timeout)
        post_seconds = monotonic() - post_started
        flow_seconds = monotonic() - started
        try:
            receipt = json.loads(body)
        except (TypeError, ValueError):
            receipt = {}
        accepted = post_status == 200 and receipt.get('status') == 'accepted'
        return {
            'ok': accepted,
            'status': 200 if accepted else post_status,
            'flow_seconds': flow_seconds,
            'post_seconds': post_seconds,
        }
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        # Exception strings may contain a URL; retain only its type.
        return {'ok': False, 'status': type(exc).__name__,
                'flow_seconds': monotonic() - started}


def run_load(base_url, tokens, entry_scores, workers, timeout):
    results = []
    wall_started = monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(submit_one, base_url, token, entry_scores, timeout)
                   for token in tokens]
        for future in as_completed(futures):
            results.append(future.result())
    wall_seconds = monotonic() - wall_started
    successful = sum(row['ok'] for row in results)
    flow_times = [row['flow_seconds'] for row in results if 'flow_seconds' in row]
    post_times = [row['post_seconds'] for row in results if 'post_seconds' in row]
    return {
        'invitations': len(tokens),
        'workers': workers,
        'successful_scorecards': successful,
        'failed_scorecards': len(tokens) - successful,
        'wall_seconds': wall_seconds,
        'scorecards_per_second': round(successful / wall_seconds, 3) if wall_seconds else 0,
        'visitor_flow_latency_seconds': {
            'p50': percentile(flow_times, .50) if flow_times else None,
            'p95': percentile(flow_times, .95) if flow_times else None,
            'max': max(flow_times) if flow_times else None,
        },
        'post_latency_seconds': {
            'p50': percentile(post_times, .50) if post_times else None,
            'p95': percentile(post_times, .95) if post_times else None,
            'max': max(post_times) if post_times else None,
        },
        'failures_by_status': {
            str(code): sum(row['status'] == code for row in results if not row['ok'])
            for code in sorted({row['status'] for row in results if not row['ok']}, key=str)
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scorecard-base-url', required=True,
        help='Exact public base URL ending in /competition/judge/.')
    parser.add_argument('--approved-host', required=True,
        help='Exact hostname approved for this non-production write test.')
    parser.add_argument('--invitation-tokens-file', required=True,
        help='Private file with one single-use test invitation token per line; use - for stdin.')
    parser.add_argument('--visitors', type=int, default=2000)
    parser.add_argument('--workers', type=int, default=2000)
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--max-wall-seconds', type=float, default=60)
    parser.add_argument('--max-p95', type=float, default=1.0)
    parser.add_argument('--entry-score', action='append', default=[], metavar='ENTRY_ID=SCORE',
        help='Score every entry on this track; repeat once per round entry.')
    parser.add_argument('--confirm-dedicated-test-competition', action='store_true',
        help='Confirm all invitations belong to an isolated test competition and scores may be discarded.')
    args = parser.parse_args(argv)
    parts = urlsplit(args.scorecard_base_url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.hostname.casefold() != args.approved_host.casefold()
            or parts.path != '/competition/judge/' or parts.query or parts.fragment
            or parts.username or parts.password):
        parser.error('scorecard base URL must be /competition/judge/ on the exact approved host')
    if not args.confirm_dedicated_test_competition:
        parser.error('confirm the tokens belong to a dedicated non-production test competition')
    if (not 1 <= args.visitors <= 10000 or not 1 <= args.workers <= 2000
            or not 0 < args.timeout <= 120 or not 0 < args.max_wall_seconds <= 3600
            or not 0 < args.max_p95 <= 60):
        parser.error('visitors, workers, timeout and target limits must be positive bounded values')
    try:
        tokens = read_invitation_tokens(args.invitation_tokens_file, args.visitors)
        entry_scores = parse_entry_scores(args.entry_score)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    summary = run_load(args.scorecard_base_url, tokens, entry_scores,
        args.workers, args.timeout)
    summary['target_wall_seconds'] = args.max_wall_seconds
    summary['target_p95_seconds'] = args.max_p95
    summary['target_passed'] = (
        summary['successful_scorecards'] == args.visitors
        and summary['wall_seconds'] <= args.max_wall_seconds
        and summary['visitor_flow_latency_seconds']['p95'] is not None
        and summary['visitor_flow_latency_seconds']['p95'] < args.max_p95)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary['target_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
