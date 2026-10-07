#!/usr/bin/env python3
"""Markdown job summaries for GitHub Actions (appended to $GITHUB_STEP_SUMMARY).

    ci_summary.py test   TEST_LOG COVERAGE_JSON
    ci_summary.py deploy DEPLOY_LOG

Reads what the earlier steps wrote; never fails the job itself.
"""
import json
import os
import re
import sys


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except OSError:
        return ''


def _table(rows):
    return '\n'.join(['| | |', '|---|---|'] + [f'| {a} | {b} |' for a, b in rows])


def test_summary(log_path, cov_path):
    log = _read(log_path)
    ran = re.search(r'^Ran (\d+) tests? in ([\d.]+)s', log, re.M)
    verdict = re.search(r'^(OK|FAILED)(?: \((.*)\))?', log, re.M)
    failed = re.findall(r'^(?:FAIL|ERROR): (\S+) \(([^)]*)\)', log, re.M)
    passed = bool(verdict and verdict.group(1) == 'OK')
    out = ['## Test suite', '']
    if not ran:
        out += ['❌ The test run did not finish (no result in the log).', '']
    else:
        out += [_table([
            ('Result', '✅ Passed' if passed else f'❌ Failed ({verdict.group(2) if verdict and verdict.group(2) else "see log"})'),
            ('Tests run', ran.group(1)),
            ('Duration', f'{float(ran.group(2)):.1f}s'),
        ]), '']
    if failed:
        out += ['### Failing tests', ''] + [f'- `{name}` ({where})' for name, where in failed[:20]]
        if len(failed) > 20:
            out.append(f'- …and {len(failed) - 20} more')
        out.append('')

    out += ['## Coverage', '']
    try:
        data = json.loads(_read(cov_path))
        totals = data['totals']
    except (ValueError, KeyError):
        out += ['No coverage data was produced.', '']
    else:
        percent = totals['percent_covered']
        out += [_table([
            ('Gate (must be 100%)', '✅ Met' if totals['missing_lines'] == 0 and totals.get('missing_branches', 0) == 0 else '❌ Not met'),
            ('Total', f'{percent:.2f}%'),
            ('Lines', f"{totals['covered_lines']} of {totals['num_statements']} covered"),
            ('Branches', f"{totals.get('covered_branches', 0)} of {totals.get('num_branches', 0)} covered"),
        ]), '']
        gaps = [(name, info) for name, info in data['files'].items() if info['summary']['percent_covered'] < 100]
        if gaps:
            out += ['### Files below 100%', '', '| File | Covered | Missing lines |', '|---|---|---|']
            for name, info in sorted(gaps):
                missing = ', '.join(str(n) for n in info['missing_lines'][:15])
                out.append(f"| `{name}` | {info['summary']['percent_covered']:.1f}% | {missing} |")
            out.append('')
    return '\n'.join(out)


def deploy_summary(log_path):
    log = _read(log_path)
    steps = re.findall(r'^==> (.+)$', log, re.M)
    sha = re.search(r'^==> Deployed (\S+)', log, re.M)
    status = os.getenv('JOB_STATUS', 'success' if sha else 'failure')
    ok = status == 'success' and bool(sha)
    ref = os.getenv('GITHUB_SHA', '')[:7]
    out = ['## Deployment', '', _table([
        ('Result', '✅ Deployed' if ok else '❌ Failed'),
        ('Commit', f'`{sha.group(1)}`' if sha else (f'`{ref}` (not deployed)' if ref else '-')),
        ('Branch', f"`{os.getenv('GITHUB_REF_NAME', 'main')}`"),
        ('Triggered by', os.getenv('GITHUB_ACTOR', '-')),
    ]), '', '### Steps', '']
    labels = [re.sub(r'^Restarting: .*', 'Restarting services', re.sub(r'^Deployed .*', 'Deployed', step)) for step in steps]
    for index, label in enumerate(labels):
        last = index == len(labels) - 1
        mark = '✅' if ok or not last else '❌'
        out.append(f'- {mark} {label}')
    if not labels:
        out.append('- ❌ Could not reach the server (no output from the deploy script)')
    if not ok:
        tail = [line for line in log.strip().splitlines() if line.strip()][-15:]
        if tail:
            out += ['', '### Last output', '', '```', *tail, '```']
    return '\n'.join(out) + '\n'


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'test' and len(sys.argv) == 4:
        print(test_summary(sys.argv[2], sys.argv[3]))
    elif mode == 'deploy' and len(sys.argv) == 3:
        print(deploy_summary(sys.argv[2]))
    else:
        sys.exit(__doc__)
