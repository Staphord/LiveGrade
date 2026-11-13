#!/usr/bin/env python3
"""Exercise Showcase public rating with independent browser-like clients.

Run only against a dedicated, non-production Showcase event. The script writes
one anonymous rating per simulated visitor and does not retry submissions.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from html.parser import HTMLParser
import json
import math
import re
import sys
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import UUID


class CsrfInputParser(HTMLParser):
    """Read only the Django CSRF form value from the rating page."""

    def __init__(self):
        super().__init__()
        self.value = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'csrfmiddlewaretoken':
            self.value = attrs.get('value')


def percentile(values, percentage):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentage) - 1)]


def _read_response(opener, request, timeout):
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        exc.read()
        return exc.code, b''


def submit_one(rating_url, timeout):
    """GET the page and submit one 5-star rating with a fresh cookie jar."""
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    started = monotonic()
    try:
        get_request = Request(rating_url, headers={'User-Agent': 'LiveGrade-Showcase-load-check/1.0'})
        get_status, page = _read_response(opener, get_request, timeout)
        if get_status != 200:
            elapsed = monotonic() - started
            return {'ok': False, 'status': get_status, 'seconds': elapsed,
                    'flow_seconds': elapsed}

        parser = CsrfInputParser()
        parser.feed(page.decode('utf-8', errors='replace'))
        if not parser.value:
            elapsed = monotonic() - started
            return {'ok': False, 'status': 'csrf_missing', 'seconds': elapsed,
                    'flow_seconds': elapsed}

        post_request = Request(rating_url,
            data=urlencode({'csrfmiddlewaretoken': parser.value, 'stars': '5', 'comment': ''}).encode(),
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': rating_url,
                'User-Agent': 'LiveGrade-Showcase-load-check/1.0',
            })
        post_started = monotonic()
        post_status, response_body = _read_response(opener, post_request, timeout)
        post_seconds = monotonic() - post_started
        flow_seconds = monotonic() - started
        success = (post_status == 200 and
                   b'Your rating has been recorded' in response_body)
        return {
            'ok': success,
            'status': post_status if not success else 200,
            'seconds': flow_seconds,
            'flow_seconds': flow_seconds,
            'post_seconds': post_seconds,
        }
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        # Exception text may contain the full request URL; do not include it.
        return {'ok': False, 'status': type(exc).__name__, 'seconds': monotonic() - started}


def run_load(rating_url, visitors, workers, timeout):
    results = []
    wall_started = monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(submit_one, rating_url, timeout) for _ in range(visitors)]
        for future in as_completed(futures):
            results.append(future.result())
    wall_seconds = monotonic() - wall_started
    request_times = [row['post_seconds'] for row in results if 'post_seconds' in row]
    flow_times = [row['flow_seconds'] for row in results if 'flow_seconds' in row]
    successful = sum(row['ok'] for row in results)
    return {
        'visitors': visitors,
        'workers': workers,
        'successful_ratings': successful,
        'failed_ratings': visitors - successful,
        'wall_seconds': wall_seconds,
        'ratings_per_second': round(successful / wall_seconds, 3) if wall_seconds else 0,
        'post_latency_seconds': {
            'p50': percentile(request_times, .50) if request_times else None,
            'p95': percentile(request_times, .95) if request_times else None,
            'max': max(request_times) if request_times else None,
        },
        'visitor_flow_latency_seconds': {
            'p50': percentile(flow_times, .50) if flow_times else None,
            'p95': percentile(flow_times, .95) if flow_times else None,
            'max': max(flow_times) if flow_times else None,
        },
        'failures_by_status': {
            str(code): sum(row['status'] == code for row in results)
            for code in sorted({row['status'] for row in results if not row['ok']}, key=str)
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rating-url', required=True,
        help='Full public rating URL for an exhibit in the dedicated test event.')
    parser.add_argument('--visitors', type=int, default=600)
    parser.add_argument('--workers', type=int, default=100)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--event-window-seconds', type=float, default=3600,
        help='Maximum time allowed for all simulated visitors to rate (default: one hour).')
    parser.add_argument('--approved-host', required=True,
        help='Exact hostname approved for this non-production write test.')
    parser.add_argument('--confirm-dedicated-test-event', action='store_true',
        help='Confirm this event contains no real participant data and its results may be discarded.')
    parser.add_argument('--max-p95', type=float, default=1.0)
    args = parser.parse_args(argv)
    parts = urlsplit(args.rating_url)
    exhibit_path = re.fullmatch(
        r'/showcase/exhibits/([0-9a-fA-F-]{36})/rate/?', parts.path)
    valid_exhibit_path = False
    if exhibit_path:
        try:
            valid_exhibit_path = str(UUID(exhibit_path.group(1))) == exhibit_path.group(1).lower()
        except ValueError:
            pass
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.hostname.casefold() != args.approved_host.casefold()
            or not valid_exhibit_path or parts.username or parts.password
            or parts.query or parts.fragment):
        parser.error('rating URL must be a Showcase exhibit rating path on the exact approved host, without credentials, query, or fragment')
    if not args.confirm_dedicated_test_event:
        parser.error('confirm the URL is for a dedicated, non-production test event')
    if not 1 <= args.visitors <= 10000 or not 1 <= args.workers <= 1000:
        parser.error('visitors must be 1..10000 and workers must be 1..1000')
    if (not 0 < args.timeout <= 120 or not 0 < args.max_p95 <= 60
            or not 0 < args.event_window_seconds <= 86400):
        parser.error('timeout, max-p95, and event-window-seconds must be positive bounded values')

    summary = run_load(args.rating_url, args.visitors, args.workers, args.timeout)
    summary['target_p95_seconds'] = args.max_p95
    summary['event_window_seconds'] = args.event_window_seconds
    summary['target_passed'] = (
        summary['successful_ratings'] == args.visitors
        and summary['wall_seconds'] <= args.event_window_seconds
        and summary['visitor_flow_latency_seconds']['p95'] is not None
        and summary['visitor_flow_latency_seconds']['p95'] < args.max_p95)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary['target_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
