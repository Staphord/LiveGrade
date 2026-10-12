#!/usr/bin/env python3
"""Markdown summaries of the security scans, for the GitHub Actions run page.

    summary.py section TOOL FILE [FILE...]   one scan's result as markdown (see TOOLS)
    summary.py overview DIR                  the whole run: a status table, then each section

`section` reads what the scanner wrote and prints a short markdown block that starts with a
``<!--headline: ...-->`` comment. A job writes it to ``out/<name>.md`` and uploads it; the
last job of the workflow runs ``overview`` over all of them and appends the result to
``$GITHUB_STEP_SUMMARY``, so the run page shows one table instead of five separate jobs.

Standard library only, and it never fails the job: a missing or unreadable report is
reported as such in the summary. A finding's secret is never printed (gitleaks output is
already redacted, and only the rule, file and line are used).
"""
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ICON = {'success': '✅', 'failure': '❌', 'warning': '⚠️', 'cancelled': '⚪', 'skipped': '⚪'}
LABEL = {'success': 'Passed', 'failure': 'Failed', 'warning': 'Review', 'cancelled': 'Cancelled', 'skipped': 'Skipped'}
LEVELS = {'error': 'High', 'warning': 'Medium', 'note': 'Low', 'none': 'Info'}
LEVEL_ORDER = ['High', 'Medium', 'Low', 'Info']
LIMIT = 15


def read(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def load(path):
    """Parsed JSON, or None when the file is missing, empty or not JSON."""
    try:
        return json.loads(read(path))
    except ValueError:
        return None


def headline(text):
    return f'<!--headline: {text}-->'


def table(header, rows):
    if not rows:
        return ''
    lines = ['| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
    lines += ['| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ') for cell in row) + ' |'
              for row in rows]
    return '\n'.join(lines)


def more(total, shown=LIMIT):
    return f'\n_…and {total - shown} more in the uploaded report._' if total > shown else ''


def missing(name, why):
    return '\n'.join([headline('no report'), f'⚠️ {why}', '',
                      f'The {name} step stopped before it wrote a report. Open that step in the job log.'])


def plural(count, word):
    return f'{count} {word}' if count == 1 else f'{count} {word}s'


def count_text(counter, order):
    parts = [f'{counter[key]} {key.lower()}' for key in order if counter.get(key)]
    return ', '.join(parts)


# ---- one section per scanner ------------------------------------------------------------

def semgrep(path):
    data = load(path)
    if not data or not data.get('runs'):
        return missing('Semgrep', 'No Semgrep report.')
    run = data['runs'][0]
    # Semgrep puts the severity on the rule, not on each result.
    default = {r['id']: r.get('defaultConfiguration', {}).get('level', 'warning')
               for r in run.get('tool', {}).get('driver', {}).get('rules', [])}
    names = {'error': 'Error', 'warning': 'Warning', 'note': 'Info'}
    # Findings a developer silenced in the source (nosemgrep) are in the SARIF, marked suppressed.
    everything = run.get('results', [])
    results = [r for r in everything if not r.get('suppressions')]
    silenced = len(everything) - len(results)
    note = f' ({silenced} silenced in the code with nosemgrep)' if silenced else ''
    if not results:
        return '\n'.join([headline('no findings'), f'✅ No findings{note}.'])
    rows = []
    for r in results:
        level = names.get(r.get('level') or default.get(r['ruleId'], 'warning'), 'Warning')
        place = r['locations'][0]['physicalLocation']
        rows.append((level, f"`{r['ruleId'].split('.')[-1]}`",
                     f"`{place['artifactLocation']['uri']}:{place.get('region', {}).get('startLine', '?')}`"))
    order = ['Error', 'Warning', 'Info']
    rows.sort(key=lambda row: order.index(row[0]))
    levels = Counter(row[0] for row in rows)
    return '\n'.join([headline(f'{plural(len(rows), "finding")} ({count_text(levels, order)})'),
                      f'❌ **{plural(len(rows), "finding")}**: {count_text(levels, order)}{note}', '',
                      table(['Severity', 'Rule', 'Where'], rows[:LIMIT]) + more(len(rows))])


def bandit(path):
    data = load(path)
    if not data or 'results' not in data:
        return missing('Bandit', 'No Bandit report.')
    results = data['results']
    scanned = data.get('metrics', {}).get('_totals', {}).get('loc')
    files = len(data.get('metrics', {})) - 1
    if not results:
        return '\n'.join([headline('no issues'),
                          f"✅ No issues at medium severity or above ({files} files{f', {scanned} lines' if scanned else ''})."])
    levels = Counter(r['issue_severity'].capitalize() for r in results)
    rows = [(r['issue_severity'].capitalize(), f"`{r['test_id']}`", r['issue_text'][:90],
             f"`{r['filename'].lstrip('./')}:{r['line_number']}`") for r in results]
    return '\n'.join([headline(f'{plural(len(results), "issue")} ({count_text(levels, LEVEL_ORDER)})'),
                      f'❌ **{plural(len(results), "issue")}**: {count_text(levels, LEVEL_ORDER)}', '',
                      table(['Severity', 'Test', 'Issue', 'Where'], rows[:LIMIT]) + more(len(rows))])


def pip_audit(path):
    data = load(path)
    if not data or 'dependencies' not in data:
        return missing('pip-audit', 'No pip-audit report.')
    packages = [d for d in data['dependencies'] if d.get('name')]
    vulnerable = [d for d in packages if d.get('vulns')]
    if not vulnerable:
        return '\n'.join([headline('no known vulnerabilities'),
                          f'✅ No known vulnerabilities in {len(packages)} packages.'])
    rows = [(f"`{d['name']}`", d['version'], f"`{v['id']}`", ', '.join(v.get('fix_versions') or []) or 'no fix yet')
            for d in vulnerable for v in d['vulns']]
    return '\n'.join([headline(f'{plural(len(rows), "advisory").replace("advisorys", "advisories")} in {plural(len(vulnerable), "package")}'),
                      f'❌ **{len(rows)} known vulnerabilities** in {len(vulnerable)} of {len(packages)} packages', '',
                      table(['Package', 'Version', 'Advisory', 'Fixed in'], rows[:LIMIT]) + more(len(rows))])


def gitleaks(path):
    data = load(path)
    if data is None:
        return missing('gitleaks', 'No gitleaks report.')
    if not data:
        return '\n'.join([headline('no leaks'), '✅ No secrets found in the working tree or the history.'])
    rows = [(f"`{f.get('RuleID', '?')}`", f"`{f.get('File', '?')}:{f.get('StartLine', '?')}`",
             f"`{(f.get('Commit') or '')[:8] or 'working tree'}`") for f in data]
    return '\n'.join([headline(plural(len(data), 'possible secret')),
                      f'❌ **{plural(len(data), "possible secret")}** (values are redacted)', '',
                      table(['Rule', 'Where', 'Commit'], rows[:LIMIT]) + more(len(rows)), '',
                      'Rotate anything real, then remove it from history; deleting the file is not enough.'])


def checkov(path):
    data = load(path)
    if isinstance(data, list):
        data = data[0] if data else None
    if not data or 'summary' not in data:
        return missing('Checkov', 'No Checkov report.')
    summary = data['summary']
    failed = data.get('results', {}).get('failed_checks', [])
    passed = summary.get('passed', 0)
    if not failed:
        return '\n'.join([headline(f'{passed} checks passed'), f'✅ All {passed} workflow checks passed.'])
    rows = [(f"`{c['check_id']}`", c['check_name'][:80], f"`{c['file_path'].lstrip('/')}`") for c in failed]
    return '\n'.join([headline(f'{len(failed)} failed, {passed} passed (advisory)'),
                      f'⚠️ **{len(failed)} failed** and {passed} passed. This check is advisory and does not fail the run.', '',
                      table(['Check', 'What', 'File'], rows[:LIMIT]) + more(len(rows))])


def codeql(path):
    return '\n'.join([headline('see the Security tab'),
                      'CodeQL results are not in the log. Open **Security → Code scanning** to see alerts.'])


def zap(*paths):
    blocks, totals, any_report = [], Counter(), False
    for path in paths:
        scan = Path(path).stem.replace('zap-', '')
        data = load(path)
        if not data or not data.get('runs'):
            blocks.append(f'**{scan.capitalize()} scan**: ⚠️ no report was produced.')
            continue
        any_report = True
        run = data['runs'][0]
        names = {r['id']: r.get('name', r['id']) for r in run.get('tool', {}).get('driver', {}).get('rules', [])}
        found = {}
        for r in run.get('results', []):
            key = (LEVELS.get(r.get('level', 'none'), 'Info'), names.get(r['ruleId'], r['ruleId']))
            found[key] = found.get(key, 0) + 1
        levels = Counter()
        for (level, _), n in found.items():
            levels[level] += n
            totals[level] += n
        rows = [(level, name, n) for (level, name), n in
                sorted(found.items(), key=lambda kv: (LEVEL_ORDER.index(kv[0][0]), kv[0][1]))]
        urls = len({r['locations'][0]['physicalLocation']['artifactLocation']['uri'] for r in run.get('results', [])})
        blocks.append('\n'.join([
            f"**{scan.capitalize()} scan**: {count_text(levels, LEVEL_ORDER) or 'no alerts'} "
            f'(alerts seen on {urls} URLs)', '',
            table(['Risk', 'Alert', 'Instances'], rows[:LIMIT]) + more(len(rows))]))
    if not any_report:
        return missing('ZAP', 'No ZAP report.')
    bad = totals['High'] + totals['Medium']
    mark = '❌' if totals['High'] else ('⚠️' if bad else '✅')
    return '\n'.join([headline(count_text(totals, LEVEL_ORDER) or 'no alerts'),
                      f"{mark} {count_text(totals, LEVEL_ORDER) or 'No alerts'}", '', '\n\n'.join(blocks)])


def checks(path, name='Checks'):
    """probe.py and check_headers.py print ``ok``/``WARN``/``FAIL`` lines."""
    text = read(path)
    lines = re.findall(r'^(ok|WARN|FAIL)\s+(.*)$', text, re.M)
    if not lines:
        return missing(name, f'No {name} output.')
    counts = Counter(kind for kind, _ in lines)
    bad = [(kind, detail) for kind, detail in lines if kind != 'ok']
    mark = '❌' if counts['FAIL'] else ('⚠️' if counts['WARN'] else '✅')
    out = [headline(f"{counts['ok']} ok, {plural(counts['WARN'], 'warning')}, {counts['FAIL']} failed"),
           f"{mark} **{counts['ok']} passed**, {plural(counts['WARN'], 'warning')}, {counts['FAIL']} failed"]
    if bad:
        out += ['', table(['Result', 'Check'], [(kind, detail[:160]) for kind, detail in bad[:LIMIT]]) + more(len(bad))]
    return '\n'.join(out)


def testssl(path):
    data = load(path)
    if not isinstance(data, list):
        return missing('testssl.sh', 'No TLS report.')
    levels = Counter(f.get('severity', 'INFO') for f in data)
    serious = [f for f in data if f.get('severity') in ('CRITICAL', 'HIGH', 'MEDIUM')]
    mark = '❌' if levels['CRITICAL'] or levels['HIGH'] else ('⚠️' if levels['MEDIUM'] else '✅')
    order = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'WARN']
    text = count_text(levels, order) or 'no problems'
    out = [headline(text), f'{mark} TLS findings: {text} ({len(data)} checks)']
    if serious:
        out += ['', table(['Severity', 'Check', 'Finding'], [(f['severity'], f"`{f['id']}`", f['finding'][:100]) for f in serious[:LIMIT]])]
    return '\n'.join(out)


TOOLS = {
    'semgrep': (semgrep, 'Semgrep (code patterns)'),
    'bandit': (bandit, 'Bandit (Python security)'),
    'pip-audit': (pip_audit, 'pip-audit (dependencies)'),
    'gitleaks': (gitleaks, 'gitleaks (secrets)'),
    'checkov': (checkov, 'Checkov (workflow config)'),
    'codeql': (codeql, 'CodeQL'),
    'zap': (zap, 'ZAP (web scan)'),
    'probe': (lambda path: checks(path, 'probe'), 'Targeted checks (XSS, access, WebSocket)'),
    'headers': (lambda path: checks(path, 'header check'), 'Production headers and cookies'),
    'testssl': (testssl, 'Production TLS'),
}


def section(tool, paths):
    if tool not in TOOLS:
        sys.exit(f'unknown tool {tool}; one of {", ".join(TOOLS)}')
    try:
        return TOOLS[tool][0](*paths)
    except Exception as error:  # a summary must never be the reason a run fails
        return missing(tool, f'Could not read the report ({type(error).__name__}).')


# ---- the whole run ----------------------------------------------------------------------

def find_headline(markdown):
    found = re.search(r'<!--headline: (.*?)-->', markdown)
    return found.group(1) if found else ''


def verdict(markdown):
    """What the section's own first line says: failure, warning or success."""
    body = re.sub(r'<!--.*?-->', '', markdown).strip()
    return {'❌': 'failure', '⚠': 'warning', '✅': 'success'}.get(body[:1], 'success')


def outcome(job_result, markdown):
    """The job's result, made stricter by what its report says (a ZAP run can finish green
    and still list Medium alerts, which a reader wants to see as "Review", not "Passed")."""
    if job_result in (None, 'success'):
        return verdict(markdown) if markdown else (job_result or 'success')
    return job_result


def overview(directory, title):
    try:
        needs = json.loads(os.getenv('NEEDS_JSON', '{}'))
    except ValueError:
        needs = {}
    sections = {path.stem: path.read_text(encoding='utf-8') for path in sorted(Path(directory).glob('*.md'))}
    results = {}
    for name in TOOLS:
        if name in sections or name in needs:
            results[name] = outcome((needs.get(name) or {}).get('result'), sections.get(name, ''))
    rows = []
    for name, result in results.items():
        text = sections.get(name, '')
        rows.append((ICON[result], TOOLS[name][1], LABEL.get(result, result.capitalize()),
                     find_headline(text) or ('stopped before reporting' if result == 'failure' else '-')))
    # jobs that have no section of their own (a build step, say) still show up
    for name, info in needs.items():
        if name not in TOOLS:
            result = info.get('result', 'skipped')
            rows.append((ICON.get(result, '⚪'), name, LABEL.get(result, result.capitalize()), '-'))
    failed = sum(1 for row in rows if row[0] == '❌')
    review = sum(1 for row in rows if row[0] == '⚠️')
    if failed:
        verdict_line = f'❌ **{failed} of {len(rows)} checks failed**' + (f' and {review} to review' if review else '')
    elif review:
        verdict_line = f'⚠️ **All {len(rows)} checks ran; {review} to review**'
    else:
        verdict_line = f'✅ **All {len(rows)} checks passed**'
    context = [
        ('Branch', f"`{os.getenv('GITHUB_REF_NAME', '-')}`"),
        ('Commit', f"`{os.getenv('GITHUB_SHA', '')[:7] or '-'}`"),
        ('Trigger', os.getenv('GITHUB_EVENT_NAME', '-')),
        ('By', os.getenv('GITHUB_ACTOR', '-')),
    ]
    out = [f'# {title}', '', verdict_line, '', ' · '.join(f'{k}: {v}' for k, v in context), '',
           table(['', 'Check', 'Result', 'Details'], rows), '']
    for name in results:
        text = sections.get(name)
        if not text:
            continue
        result = results[name]
        body = re.sub(r'<!--headline:.*?-->\n?', '', text).strip()
        out += [f'<details{" open" if result in ("failure", "warning") else ""}>',
                f'<summary><b>{ICON[result]} {TOOLS[name][1]}</b>: {find_headline(text)}</summary>', '', body, '', '</details>', '']
    run, repo = os.getenv('GITHUB_RUN_ID', ''), os.getenv('GITHUB_REPOSITORY', '')
    if run and repo:
        out.append(f"[Open the full logs]({os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run})")
    return '\n'.join(out) + '\n'


if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == 'section':
        print(section(args[1], args[2:]))
    elif len(args) == 2 and args[0] == 'overview':
        print(overview(args[1], os.getenv('SUMMARY_TITLE', 'Security run')))
    else:
        sys.exit(__doc__)
