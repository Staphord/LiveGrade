#!/usr/bin/env python3
"""Markdown summaries of the security scans, for the GitHub Actions run page.

    summary.py section TOOL FILE [FILE...]   one scan's result as markdown (see TOOLS)
    summary.py overview DIR                  the whole run: verdict, status table, then each section

The run page cannot open downloaded files, so everything a reviewer needs is written into the
summary itself: every finding is a click-to-open block with what it means in plain words, how
to fix it, and where it is (with links to the code on GitHub). Nothing has to be downloaded to
read the result; the raw reports are still uploaded as artifacts for anyone who wants them.

A job writes ``section`` output to ``out/<name>.md`` and uploads it; the last job of the
workflow runs ``overview`` over all of them. Standard library only, and it never fails the job:
a missing or unreadable report is reported as such. A finding's secret is never printed.
"""
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import urlparse

ICON = {'success': '✅', 'failure': '❌', 'warning': '⚠️', 'cancelled': '⚪', 'skipped': '⚪'}
LABEL = {'success': 'Passed', 'failure': 'Failed', 'warning': 'Review', 'cancelled': 'Cancelled', 'skipped': 'Skipped'}
LEVELS = {'error': 'High', 'warning': 'Medium', 'note': 'Low', 'none': 'Info'}
LEVEL_ORDER = ['High', 'Medium', 'Low', 'Info']
DOT = {'High': '🔴', 'Medium': '🟠', 'Low': '🟡', 'Info': '⚪', 'Critical': '🔴', 'Error': '🔴', 'Warning': '🟠', 'Note': '🟡'}
LIMIT = 15            # rows in a summary table
INSTANCES = 10        # pages / places listed under one finding
MAX_BYTES = 900_000   # GitHub rejects a step summary over 1 MiB


# ---- small helpers ---------------------------------------------------------------------------

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


HEADLINE_OPEN, HEADLINE_CLOSE = '<!--headline: ', '-->'


def split_headline(markdown):
    """(headline text, the rest). A section always starts with its headline on the first line."""
    first, _, rest = markdown.partition('\n')
    if first.startswith(HEADLINE_OPEN) and first.endswith(HEADLINE_CLOSE):
        return first[len(HEADLINE_OPEN):-len(HEADLINE_CLOSE)], rest
    return '', markdown


def find_headline(markdown):
    return split_headline(markdown)[0]


def plural(count, word):
    return f'{count} {word}' if count == 1 else f'{count} {word}s'


def count_text(counter, order):
    return ', '.join(f'{counter[key]} {key.lower()}' for key in order if counter.get(key))


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def table(header, rows):
    if not rows:
        return ''
    lines = ['| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
    lines += ['| ' + ' | '.join(cell(c) for c in row) + ' |' for row in rows]
    return '\n'.join(lines)


def shorten(text, size=300):
    text = re.sub(r'\s+', ' ', (text or '').strip())
    if len(text) <= size:
        return text
    cut = text[:size].rsplit(' ', 1)[0]
    sentence = cut.rfind('.')
    return cut[:sentence + 1] if sentence > size // 2 else cut + '…'


def details(title, body, open_=False):
    return '\n'.join([f'<details{" open" if open_ else ""}>', f'<summary>{title}</summary>', '', body.strip(), '', '</details>', ''])


def repo_link(path, line=None, ref=None):
    """A link to a file in this repository at the commit that was scanned (plain text outside CI)."""
    repo, sha = os.getenv('GITHUB_REPOSITORY', ''), ref or os.getenv('GITHUB_SHA', '')
    label = f'{path}:{line}' if line else path
    if not repo or not sha:
        return f'`{label}`'
    server = os.getenv('GITHUB_SERVER_URL', 'https://github.com')
    anchor = f'#L{line}' if line else ''
    return f'[`{label}`]({server}/{repo}/blob/{sha}/{path}{anchor})'


def fence(code, lang=''):
    code = (code or '').rstrip()
    return f'```{lang}\n{code}\n```' if code else ''


def no_report(name, why='No report was produced.'):
    return '\n'.join([headline('no report'), f'⚠️ **{name}: {why}**', '',
                      'The step stopped before it wrote a report. Open that step in the job log to see why; '
                      'this is a problem with the run, not a finding.'])


def clean(headline_text, message):
    return '\n'.join([headline(headline_text), f'✅ {message}'])


# ---- plain-language explanations (ZAP) -----------------------------------------------------------

# rule id -> (what it means, how to fix it / what to do). Anything not listed falls back to ZAP's own text.
ZAP_PLAIN = {
    '10038': ('The site does not send a browser rule (a "Content Security Policy") that lists which scripts and files may run on a page. '
              'If harmful code ever reached a page, the browser would have no rule telling it to refuse.',
              'Send a Content-Security-Policy header. LiveGrade now sends one but in "watch only" mode, which ZAP does not count. '
              'If this still shows, set CSP_ENFORCE=True once the pages have been checked in a browser.'),
    '90003': ('A page loads a script or style sheet from another website without a "fingerprint" (integrity hash), so the browser cannot tell '
              'whether the file was swapped for a different one.',
              'Add an integrity attribute to each tag. The Bootstrap and chart files already have one. Google Fonts style sheets cannot carry one, '
              'so a remaining alert for those is expected.'),
    '10010': ('A cookie can be read by scripts running on the page. Cookies that keep someone signed in should be hidden from scripts.',
              'LiveGrade\'s login (session) cookie is hidden. The anti-forgery cookie is deliberately readable because the student pages read it '
              'to submit grades, so an alert for that one is expected.'),
    '10017': ('The pages load scripts from another website (a public delivery network). If that website were ever compromised, its script would run on our pages.',
              'Keep the integrity fingerprints on those tags, or host the files ourselves.'),
    '10106': ('The site was reached over plain http, which anyone on the network can read.',
              'Expected for the scan itself, which runs against a local test copy. The live site must use https (checked separately in the production checks).'),
    '10027': ('Notes left inside the page source (comments) contain words such as TODO, admin or debug. They are visible to anyone who views the source.',
              'Check that none of them reveals a secret, a password or an internal address. Informational.'),
    '10031': ('A value taken from the address or a form is written into an HTML attribute on the page. By itself this is not a flaw; it matters only if the value is not escaped.',
              'Check that the value is escaped (Django does this automatically) and, where it is a choice, validated against a short list.'),
    '10109': ('The pages use a lot of scripts, so ZAP switched to a slower crawler. Nothing is wrong.', 'No action needed.'),
    '10112': ('ZAP noticed the cookie that keeps a visitor signed in. This is how it follows a session during the scan.', 'No action needed.'),
    '10104': ('ZAP asked for the same page pretending to be different browsers and saw no difference.', 'No action needed.'),
    '2': ('A private network address (such as 192.168.x.x) appears in a page. It could help an attacker learn about an internal network.',
          'Find where it appears. A likely cause is the QR join link, which the app builds from its own network address when it runs on a private network; '
          'check that the live site shows its public address instead.'),
    '10020': ('Pages can be shown inside another site\'s frame, which enables "clickjacking" (tricking someone into clicking a hidden button).',
              'Send X-Frame-Options: DENY or a frame-ancestors rule. LiveGrade sets both in production.'),
    '10021': ('Browsers may "guess" a file\'s type instead of trusting the server, which can turn data into code.', 'Send X-Content-Type-Options: nosniff (LiveGrade does).'),
}


# ---- one section per scanner -------------------------------------------------------------------------

def zap(*paths):
    scans, totals, any_report = [], Counter(), False
    for path in paths:
        stem = Path(path).stem.replace('zap-', '')
        title = {'authenticated': 'Signed-in scan (as a lecturer)', 'anonymous': 'Signed-out scan (students and public pages)'}.get(stem, stem.capitalize())
        data = load(path)
        if not data or not data.get('runs'):
            scans.append(f'### {title}\n\n⚠️ No report was produced for this scan.\n')
            continue
        any_report = True
        run = data['runs'][0]
        rules = {r['id']: r for r in run.get('tool', {}).get('driver', {}).get('rules', [])}
        groups = OrderedDict()
        for result in run.get('results', []):
            rule = rules.get(result['ruleId'], {})
            level = LEVELS.get(result.get('level') or rule.get('defaultConfiguration', {}).get('level', 'none'), 'Info')
            group = groups.setdefault(result['ruleId'], {'level': level, 'rule': rule, 'where': OrderedDict()})
            uri = result['locations'][0]['physicalLocation']['artifactLocation']['uri']
            parsed = urlparse(uri)
            page = (parsed.path or '/') + (f'?{parsed.query}' if parsed.query else '')
            method = result.get('webRequest', {}).get('method', 'GET')
            status = result.get('webResponse', {}).get('statusCode', '')
            group['where'][(method, page)] = status
        levels = Counter()
        for g in groups.values():
            levels[g['level']] += 1
            totals[g['level']] += 1
        pages = len({p for g in groups.values() for (_, p) in g['where']})
        body = [f'### {title}', '',
                f'{count_text(levels, LEVEL_ORDER) or "No alerts"} (kinds of alert), found on {plural(pages, "page")}.', '']
        rows = [(f"{DOT[g['level']]} {g['level']}", g['rule'].get('name', rid), len(g['where']))
                for rid, g in sorted(groups.items(), key=lambda kv: (LEVEL_ORDER.index(kv[1]['level']), kv[1]['rule'].get('name', '')))]
        body += [table(['Risk', 'Alert', 'Pages'], rows), '']
        for band, heading, opened in (('attention', 'Needs attention (Medium and above)', True), ('low', 'Low risk', False)):
            chosen = [(rid, g) for rid, g in groups.items()
                      if (g['level'] in ('High', 'Medium')) == (band == 'attention') and g['level'] != 'Info']
            if not chosen:
                continue
            body += [f'#### {heading}', '']
            for rid, g in sorted(chosen, key=lambda kv: (LEVEL_ORDER.index(kv[1]['level']), kv[1]['rule'].get('name', ''))):
                rule = g['rule']
                plain, fix = ZAP_PLAIN.get(rid, (shorten(rule.get('fullDescription', {}).get('text', ''), 420),
                                                 shorten(rule.get('properties', {}).get('solution', {}).get('text', ''), 420)))
                pages_list = [f'- `{m} {p}`' + (f' (answered {s})' if s else '') for (m, p), s in list(g['where'].items())[:INSTANCES]]
                extra = len(g['where']) - INSTANCES
                refs = rule.get('properties', {}).get('references', [])[:2]
                cwe = next((r['target']['id'] for r in rule.get('relationships', []) if r.get('target', {}).get('toolComponent', {}).get('name') == 'CWE'), '')
                more = ' · '.join(([f'[CWE-{cwe}](https://cwe.mitre.org/data/definitions/{cwe}.html)'] if cwe else []) + [f'[reference]({u})' for u in refs])
                block = [f'**In plain words:** {plain}', '', f'**What to do:** {fix}', '',
                         f'**Found on {plural(len(g["where"]), "page")}:**', *pages_list]
                if extra > 0:
                    block.append(f'- …and {extra} more')
                if more:
                    block += ['', more]
                body += [details(f"{DOT[g['level']]} <b>{g['level']}</b> · {rule.get('name', rid)} · {plural(len(g['where']), 'page')}", '\n'.join(block), open_=opened)]
        info = [(rid, g) for rid, g in groups.items() if g['level'] == 'Info']
        if info:
            lines = [f"- **{g['rule'].get('name', rid)}** ({len(g['where'])}): {ZAP_PLAIN.get(rid, ('', ''))[0] or shorten(g['rule'].get('fullDescription', {}).get('text', ''), 160)}"
                     for rid, g in sorted(info, key=lambda kv: kv[1]['rule'].get('name', ''))]
            body += [details('⚪ <b>For information only</b> (no action needed unless noted)', '\n'.join(lines)), '']
        scans.append('\n'.join(body))
    if not any_report:
        return no_report('ZAP', 'No ZAP report was produced.')
    attention = totals['High'] + totals['Medium']
    mark = '❌' if totals['High'] else ('⚠️' if attention else '✅')
    summary = ('No high-risk alerts.' if not totals['High'] else f"{plural(totals['High'], 'high-risk alert')}.") + \
              (f" {totals['Medium']} kind{'' if totals['Medium'] == 1 else 's'} of medium alert to review." if totals['Medium'] else '')
    return '\n'.join([headline(count_text(totals, LEVEL_ORDER) or 'no alerts'),
                      f'{mark} **{summary}** ZAP clicked through the running app and tried it as an attacker would. '
                      'Open a block below to read what an alert means, how to fix it and which pages are affected.', '',
                      '\n\n'.join(scans)])


def semgrep(path):
    data = load(path)
    if not data or not data.get('runs'):
        return no_report('Semgrep', 'No Semgrep report was produced.')
    run = data['runs'][0]
    rules = {r['id']: r for r in run.get('tool', {}).get('driver', {}).get('rules', [])}
    names = {'error': 'Error', 'warning': 'Warning', 'note': 'Info'}
    everything = run.get('results', [])
    results = [r for r in everything if not r.get('suppressions')]
    silenced = len(everything) - len(results)
    note = f' ({silenced} silenced in the code with `nosemgrep`)' if silenced else ''
    if not results:
        return clean('no findings', f'**No findings.** Semgrep searched the code for known risky patterns{note}.')
    groups = OrderedDict()
    for r in results:
        rule = rules.get(r['ruleId'], {})
        level = names.get(r.get('level') or rule.get('defaultConfiguration', {}).get('level', 'warning'), 'Warning')
        g = groups.setdefault(r['ruleId'], {'level': level, 'rule': rule, 'where': [], 'text': r.get('message', {}).get('text', '')})
        place = r['locations'][0]['physicalLocation']
        g['where'].append((place['artifactLocation']['uri'], place.get('region', {}).get('startLine'), place.get('region', {}).get('snippet', {}).get('text', '')))
    order = ['Error', 'Warning', 'Info']
    levels = Counter()
    for g in groups.values():
        levels[g['level']] += len(g['where'])
    rows = [(f"{DOT.get(g['level'], '')} {g['level']}", f"`{rid.split('.')[-1]}`", len(g['where']))
            for rid, g in sorted(groups.items(), key=lambda kv: order.index(kv[1]['level']))]
    out = [headline(f'{plural(sum(levels.values()), "finding")} ({count_text(levels, order)})'),
           f'❌ **{plural(sum(levels.values()), "finding")}** in {plural(len(groups), "rule")}{note}. Semgrep searches the code for known risky patterns.', '',
           table(['Severity', 'Rule', 'Places'], rows), '']
    for rid, g in sorted(groups.items(), key=lambda kv: order.index(kv[1]['level'])):
        short = rid.split('.')[-1]
        block = [f'**What Semgrep says:** {shorten(g["text"] or g["rule"].get("fullDescription", {}).get("text", ""), 500)}', '', '**Where:**']
        for uri, line, _snippet in g['where'][:INSTANCES]:
            block.append(f'- {repo_link(uri, line)}')
        if len(g['where']) > INSTANCES:
            block.append(f'- …and {len(g["where"]) - INSTANCES} more')
        first = next((s for _, _, s in g['where'] if s), '')
        if first:
            block += ['', '**First match:**', fence(first.strip()[:600])]
        block += ['', f'**If it is a false alarm:** add a `# nosemgrep: {rid}` comment on that line with the reason.']
        out.append(details(f"{DOT.get(g['level'], '')} <b>{g['level']}</b> · {short} · {plural(len(g['where']), 'place')}", '\n'.join(block), open_=True))
    return '\n'.join(out)


BANDIT_PLAIN = {
    'B608': ('SQL text is assembled by joining strings. If any piece came from a visitor, they could change the database query.',
             'Use query parameters for values. Table or column names that come from the program itself are fine; quote them and add a `# nosec B608` note saying why.'),
    'B104': ('The code mentions "0.0.0.0", which means "every network address". Listening there would expose the service to the whole network.',
             'If it is only in a list of addresses to replace (not a listening address), add `# nosec B104` with the reason.'),
    'B105': ('A password-like value is written directly in the code.', 'Keep real secrets in the environment. Test-only fakes can carry `# nosec B105`.'),
    'B106': ('A password-like value is passed directly in a function call.', 'Keep real secrets in the environment. Test-only fakes can carry `# nosec B106`.'),
    'B101': ('An `assert` is used; it is removed when Python runs in optimised mode, so it must not guard security.', 'Use a normal `if` and raise an error.'),
    'B303': ('A weak hash (MD5 or SHA-1) is used.', 'Use SHA-256 or better for anything security related.'),
    'B324': ('A weak hash (MD5 or SHA-1) is used.', 'Use SHA-256 or better, or pass `usedforsecurity=False` if it is not for security.'),
    'B602': ('A shell command is run with input that could be controlled by someone.', 'Avoid `shell=True`; pass a list of arguments.'),
}


def bandit(path):
    data = load(path)
    if not data or 'results' not in data:
        return no_report('Bandit', 'No Bandit report was produced.')
    results = data['results']
    metrics = data.get('metrics', {})
    scanned = metrics.get('_totals', {}).get('loc')
    files = max(len(metrics) - 1, 0)
    if not results:
        return clean('no issues', f"**No issues at medium severity or above.** Bandit read the Python code ({files} files{f', {scanned} lines' if scanned else ''}).")
    levels = Counter(r['issue_severity'].capitalize() for r in results)
    groups = OrderedDict()
    for r in sorted(results, key=lambda r: (LEVEL_ORDER.index(r['issue_severity'].capitalize()) if r['issue_severity'].capitalize() in LEVEL_ORDER else 9, r['test_id'])):
        groups.setdefault((r['test_id'], r['issue_severity'].capitalize()), []).append(r)
    out = [headline(f'{plural(len(results), "issue")} ({count_text(levels, LEVEL_ORDER)})'),
           f'❌ **{plural(len(results), "issue")}** in {plural(len(groups), "kind")}: {count_text(levels, LEVEL_ORDER)}. Bandit reads the Python code for risky habits.', '',
           table(['Severity', 'Test', 'What', 'Places'], [(f'{DOT.get(sev, "")} {sev}', f'`{tid}`', items[0]['test_name'].replace('_', ' '), len(items))
                                                         for (tid, sev), items in groups.items()]), '']
    for (tid, sev), items in groups.items():
        plain, fix = BANDIT_PLAIN.get(tid, (items[0]['issue_text'], 'See the Bandit documentation linked below.'))
        block = [f'**In plain words:** {plain}', '', f'**What to do:** {fix}', '', '**Where:**']
        for r in items[:INSTANCES]:
            block.append(f"- {repo_link(r['filename'].lstrip('./'), r['line_number'])}")
        if len(items) > INSTANCES:
            block.append(f'- …and {len(items) - INSTANCES} more')
        code = items[0].get('code', '')
        if code:
            block += ['', '**First match:**', fence(code.strip()[:700], 'python')]
        links = []
        cwe = items[0].get('issue_cwe') or {}
        if cwe.get('link'):
            links.append(f"[CWE-{cwe.get('id')}]({cwe['link']})")
        if items[0].get('more_info'):
            links.append(f"[Bandit {tid}]({items[0]['more_info']})")
        if links:
            block += ['', ' · '.join(links)]
        out.append(details(f"{DOT.get(sev, '')} <b>{sev}</b> · {tid} {items[0]['test_name'].replace('_', ' ')} · {plural(len(items), 'place')}", '\n'.join(block), open_=True))
    return '\n'.join(out)


def advisories(count):
    return f'{count} advisory' if count == 1 else f'{count} advisories'


def pip_audit(path):
    data = load(path)
    if not data or 'dependencies' not in data:
        return no_report('pip-audit', 'No pip-audit report was produced.')
    packages = [d for d in data['dependencies'] if d.get('name')]
    vulnerable = [d for d in packages if d.get('vulns')]
    if not vulnerable:
        return clean('no known vulnerabilities',
                     f'**No known vulnerabilities** in the {len(packages)} software components LiveGrade uses (checked against the public advisory lists).')
    total = sum(len(d['vulns']) for d in vulnerable)
    out = [headline(f'{advisories(total)} in {plural(len(vulnerable), "component")}'),
           f'❌ **{advisories(total)}** in {len(vulnerable)} of {len(packages)} software components. '
           'Each is on a public list, so attackers know about it too.', '',
           table(['Component', 'Version', 'Advisories', 'Update to'],
                 [(f"`{d['name']}`", d['version'], len(d['vulns']),
                   ', '.join(sorted({f for v in d['vulns'] for f in v.get('fix_versions', [])})) or 'no fix yet') for d in vulnerable]), '']
    for d in vulnerable:
        block = [f"`{d['name']}` {d['version']} is affected by:", '']
        for v in d['vulns']:
            aliases = ', '.join(v.get('aliases', [])[:3])
            fixed = ', '.join(v.get('fix_versions') or []) or 'no fix yet'
            block.append(f"- [{v['id']}](https://osv.dev/vulnerability/{v['id']}){f' ({aliases})' if aliases else ''}. Fixed in **{fixed}**."
                         + (f" {shorten(v.get('description', ''), 220)}" if v.get('description') else ''))
        block += ['', '**What to do:** raise the version in `requirements.txt` to one of the fixed versions, run the tests, and re-run this check.']
        out.append(details(f"🔴 <b>{d['name']}</b> {d['version']} · {advisories(len(d['vulns']))}", '\n'.join(block), open_=True))
    return '\n'.join(out)


def gitleaks(path):
    data = load(path)
    if data is None:
        return no_report('gitleaks', 'No gitleaks report was produced.')
    if not data:
        return clean('no leaks', '**No secrets found** in the project files or in any past commit.')
    server, repo = os.getenv('GITHUB_SERVER_URL', 'https://github.com'), os.getenv('GITHUB_REPOSITORY', '')
    rows = []
    for f in data[:LIMIT]:
        commit = f.get('Commit') or ''
        where = repo_link(f.get('File', '?'), f.get('StartLine'), ref=commit or None)
        link = f'[`{commit[:8]}`]({server}/{repo}/commit/{commit})' if commit and repo else (f'`{commit[:8]}`' if commit else 'working tree')
        rows.append((f"`{f.get('RuleID', '?')}`", where, link, f.get('Author', '') or '-'))
    return '\n'.join([headline(plural(len(data), 'possible secret')),
                      f'❌ **{plural(len(data), "possible secret")}** found (values are hidden here and in the log).', '',
                      table(['Kind', 'Where', 'Commit', 'Author'], rows) + (f'\n_…and {len(data) - LIMIT} more._' if len(data) > LIMIT else ''), '',
                      '**What to do:** treat anything real as exposed: replace it with a new value at the provider first. '
                      'Deleting the file is not enough, because it stays in the project history.'])


CHECKOV_PLAIN = {
    'CKV2_GHA_1': ('The automation file does not limit what its access token may do, so by default it may write to the repository.',
                   'Add `permissions: contents: read` at the top of the workflow, and grant extra rights only to the job that needs them.'),
    'CKV_GHA_7': ('A workflow lets someone type inputs that change what is built.', 'Remove the manual inputs, or make sure they cannot influence the build.'),
}


def checkov(path):
    data = load(path)
    if isinstance(data, list):
        data = data[0] if data else None
    if not data or 'summary' not in data:
        return no_report('Checkov', 'No Checkov report was produced.')
    summary = data['summary']
    failed = data.get('results', {}).get('failed_checks', [])
    passed = summary.get('passed', 0)
    if not failed:
        return clean(f'{passed} checks passed', f'**All {passed} automation checks passed.** Checkov reviewed the settings of the build and deploy workflows.')
    out = [headline(f'{len(failed)} failed, {passed} passed (advisory)'),
           f'⚠️ **{plural(len(failed), "check")} failed** and {passed} passed. Checkov reviews the settings of the automation files; '
           'this one is advisory and does not fail the run.', '']
    for c in failed:
        plain, fix = CHECKOV_PLAIN.get(c['check_id'], (c['check_name'], 'Follow the guideline linked below.'))
        file_path = c['file_path'].lstrip('/')
        file_path = file_path if file_path.startswith('.github') else f'.github/{file_path}'
        lines = c.get('file_line_range') or [None]
        block = [f'**In plain words:** {plain}', '', f'**What to do:** {fix}', '', f'**Where:** {repo_link(file_path, lines[0])}']
        if c.get('guideline'):
            block += ['', f"[Guideline]({c['guideline']})"]
        out.append(details(f"🟡 <b>{c['check_id']}</b> · {shorten(c['check_name'], 90)}", '\n'.join(block), open_=True))
    return '\n'.join(out)


SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'error', 'warning', 'note']


def codeql(path):
    data = load(path)
    if isinstance(data, list) and data and isinstance(data[0], list):   # `gh api --paginate --slurp`: one list per page
        data = [alert for page in data for alert in page]
    if isinstance(data, dict) or data is None:
        return '\n'.join([headline('could not read alerts'),
                          '⚠️ **CodeQL ran, but its alert list could not be read here.** Open **Security → Code scanning** to see the alerts.'])
    if not data:
        return clean('no open alerts', '**No open alerts for this branch.** CodeQL followed how data moves through the Python and JavaScript code and found nothing to report.')

    def level_of(alert):
        return (alert['rule'].get('security_severity_level') or alert['rule'].get('severity') or 'note').lower()

    ordered = sorted(data, key=lambda a: (SEVERITY_ORDER.index(level_of(a)) if level_of(a) in SEVERITY_ORDER else 9, a['number']))
    levels = Counter(level_of(a).capitalize() for a in ordered)
    rows, blocks = [], []
    for a in ordered:
        level = level_of(a).capitalize()
        inst = a['most_recent_instance']
        loc = inst['location']
        where = repo_link(loc['path'], loc.get('start_line'), ref=inst.get('commit_sha'))
        rows.append((f"{DOT.get(level, '')} {level}", f"[#{a['number']}]({a['html_url']})", a['rule']['description'], where))
        blocks.append((a, level, where))
    order = [s.capitalize() for s in SEVERITY_ORDER]
    out = [headline(f'{plural(len(data), "open alert")} ({count_text(levels, order)})'),
           f'⚠️ **{plural(len(data), "open alert")}** on this branch ({count_text(levels, order)}). Each links to its page in GitHub, where it can be fixed, dismissed or reopened.', '',
           table(['Severity', 'Alert', 'What', 'Where'], rows), '']
    for a, level, where in blocks:
        text = shorten(a['most_recent_instance']['message']['text'], 300)
        help_text = re.sub(r'[#*`]|\[([^\]]*)\]\([^)]*\)', r'\1', a['rule'].get('help') or a['rule'].get('full_description') or '').strip()
        if help_text.startswith(a['rule']['description']):
            help_text = help_text[len(a['rule']['description']):].strip()
        help_text = shorten(help_text, 380)
        block = [f'**Where:** {where}', '', f'**CodeQL says:** {text}']
        if help_text:
            block += ['', f'**About this kind of problem:** {help_text}']
        block += ['', f"[Open alert #{a['number']} in GitHub]({a['html_url']}) · rule `{a['rule']['id']}`"]
        out.append(details(f"{DOT.get(level, '')} <b>{level}</b> · {a['rule']['description']} · #{a['number']}", '\n'.join(block)))
    return '\n'.join(out)


PROBE_GROUPS = [
    ('stored XSS', 'Hostile text typed into names, topics and locations: does it come back as harmless text?'),
    ('reflected XSS', 'Hostile text placed in the web address: does it come back as harmless text?'),
    ('cross-org', 'Can a lecturer from one organisation reach another organisation\'s sessions?'),
    ('anonymous', 'Can someone who is not signed in open a lecturer page?'),
    ('student', 'Does the student flow (joining, grading) behave, and does a student pass stay inside its own session?'),
    ('WebSocket', 'Does the live-update connection refuse other websites?'),
]


def checks(path, name='Checks'):
    """probe.py and check_headers.py print ``ok``/``WARN``/``FAIL`` lines."""
    lines = re.findall(r'^(ok|WARN|FAIL)\s+(.*)$', read(path), re.M)
    if not lines:
        return no_report(name, f'No {name} output was produced.')
    counts = Counter(kind for kind, _ in lines)
    mark = '❌' if counts['FAIL'] else ('⚠️' if counts['WARN'] else '✅')
    out = [headline(f"{counts['ok']} ok, {plural(counts['WARN'], 'warning')}, {counts['FAIL']} failed"),
           f"{mark} **{counts['ok']} of {len(lines)} checks passed**, {plural(counts['WARN'], 'warning')}, {counts['FAIL']} failed.", '']
    if name == 'probe':
        grouped = OrderedDict((key, [0, 0, 0]) for key, _ in PROBE_GROUPS)
        for kind, detail in lines:
            slot = next((grouped[key] for key, _ in PROBE_GROUPS if key.lower() in detail.lower()), None)
            if slot is not None:
                slot[['ok', 'WARN', 'FAIL'].index(kind)] += 1
        rows = []
        for key, question in PROBE_GROUPS:
            ok, warn, fail = grouped[key]
            if ok + warn + fail:
                rows.append(('❌' if fail else ('⚠️' if warn else '✅'), question,
                             f'{ok} ok' + (f', {warn} warning' if warn else '') + (f', {fail} failed' if fail else '')))
        out += [table(['', 'What was tried', 'Result'], rows), '']
    bad = [(kind, detail) for kind, detail in lines if kind != 'ok']
    if bad:
        out += [details(f'🔎 <b>Checks that need a look</b> ({len(bad)})',
                        table(['Result', 'Check'], [(('❌ Failed' if k == 'FAIL' else '⚠️ Warning'), d[:200]) for k, d in bad]), open_=True)]
    out += [details('✅ <b>All checks</b>', table(['Result', 'Check'], [(('✅' if k == 'ok' else ('⚠️' if k == 'WARN' else '❌')), d[:200]) for k, d in lines]))]
    return '\n'.join(out)


def testssl(path):
    data = load(path)
    if not isinstance(data, list):
        return no_report('testssl.sh', 'No TLS report was produced.')
    levels = Counter(f.get('severity', 'INFO') for f in data)
    serious = [f for f in data if f.get('severity') in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'WARN')]
    mark = '❌' if levels['CRITICAL'] or levels['HIGH'] else ('⚠️' if levels['MEDIUM'] else '✅')
    order = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'WARN']
    text = count_text(levels, order) or 'no problems'
    out = [headline(text), f'{mark} **TLS (the https encryption of the live site): {text}.** {len(data)} checks were made on the certificate, protocols and known attacks.']
    if serious:
        out += ['', table(['Severity', 'Check', 'Finding'], [(f"{DOT.get(f['severity'].capitalize(), '')} {f['severity']}", f"`{f['id']}`", f['finding'][:140]) for f in serious[:LIMIT]])]
    return '\n'.join(out)


TOOLS = {
    'codeql': (codeql, 'CodeQL (code flow analysis)'),
    'semgrep': (semgrep, 'Semgrep (risky code patterns)'),
    'bandit': (bandit, 'Bandit (Python security)'),
    'pip-audit': (pip_audit, 'pip-audit (software components)'),
    'gitleaks': (gitleaks, 'gitleaks (leaked secrets)'),
    'checkov': (checkov, 'Checkov (automation settings)'),
    'probe': (lambda path: checks(path, 'probe'), 'Targeted attack checks'),
    'zap': (zap, 'ZAP (web attack scan)'),
    'headers': (lambda path: checks(path, 'header check'), 'Live site headers and cookies'),
    'testssl': (testssl, 'Live site TLS'),
}


def section(tool, paths):
    if tool not in TOOLS:
        sys.exit(f'unknown tool {tool}; one of {", ".join(TOOLS)}')
    try:
        return TOOLS[tool][0](*paths)
    except Exception as error:  # a summary must never be the reason a run fails
        return no_report(tool, f'The report could not be read ({type(error).__name__}).')


# ---- the whole run ------------------------------------------------------------------------------------------

def verdict(markdown):
    """What the section's own first line says: failure, warning or success."""
    body = split_headline(markdown)[1].strip()
    return {'❌': 'failure', '⚠': 'warning', '✅': 'success'}.get(body[:1], 'success')


def outcome(job_result, markdown):
    """The job's result, made stricter by what its report says (a ZAP run can finish green
    and still list Medium alerts, which a reader wants to see as "Review", not "Passed")."""
    if job_result in (None, 'success'):
        return verdict(markdown) if markdown else (job_result or 'success')
    return job_result


def build_overview(directory, title):
    """(markdown, number of checks that failed)."""
    try:
        needs = json.loads(os.getenv('NEEDS_JSON', '{}'))
    except ValueError:
        needs = {}
    # Artifacts unpack into sub-folders (out/out/...), so look everywhere below the directory.
    sections = {path.stem: path.read_text(encoding='utf-8') for path in sorted(Path(directory).rglob('*.md'))}
    results = OrderedDict()
    for name in TOOLS:
        if name in sections or name in needs:
            results[name] = outcome((needs.get(name) or {}).get('result'), sections.get(name, ''))
    rows = []
    for name, result in results.items():
        text = sections.get(name, '')
        rows.append((ICON[result], f'<b>{TOOLS[name][1]}</b>', LABEL.get(result, result.capitalize()),
                     find_headline(text) or ('stopped before reporting' if result == 'failure' else '-')))
    for name, info in needs.items():
        if name not in TOOLS:
            result = info.get('result', 'skipped')
            rows.append((ICON.get(result, '⚪'), name, LABEL.get(result, result.capitalize()), '-'))
    failed = sum(1 for row in rows if row[0] == '❌')
    review = sum(1 for row in rows if row[0] == '⚠️')
    if failed:
        banner = f'❌ **{plural(failed, "check")} failed**' + (f' and {review} to review' if review else '') + f' (of {len(rows)}).'
    elif review:
        banner = f'⚠️ **All {len(rows)} checks ran; {review} to review.**'
    else:
        banner = f'✅ **All {len(rows)} checks passed.**'
    server, repo, run = os.getenv('GITHUB_SERVER_URL', 'https://github.com'), os.getenv('GITHUB_REPOSITORY', ''), os.getenv('GITHUB_RUN_ID', '')
    commit = os.getenv('GITHUB_SHA', '')
    context = [('Branch', f"`{os.getenv('GITHUB_REF_NAME', '-')}`"),
               ('Commit', f"[`{commit[:7]}`]({server}/{repo}/commit/{commit})" if commit and repo else f"`{commit[:7] or '-'}`"),
               ('Trigger', os.getenv('GITHUB_EVENT_NAME', '-')), ('By', os.getenv('GITHUB_ACTOR', '-'))]
    out = [f'# {title}', '', banner, '', ' · '.join(f'{k}: {v}' for k, v in context), '',
           table(['', 'Check', 'Result', 'What it found'], rows), '']
    todo = [(name, find_headline(sections.get(name, ''))) for name, result in results.items() if result in ('failure', 'warning') and sections.get(name)]
    if todo:
        out += ['### What needs attention', ''] + [f'- **{TOOLS[name][1]}**: {text}' for name, text in todo] + ['']
    out += ['> Every finding below can be opened right here: click a line to see what it means in plain words, what to do about it, and where it is. '
            'Nothing needs to be downloaded.', '']
    for name in results:
        text = sections.get(name)
        if not text:
            continue
        out += [f'## {ICON[results[name]]} {TOOLS[name][1]}', '', split_headline(text)[1].strip(), '']
    if run and repo:
        out.append(f'[Open the full job logs]({server}/{repo}/actions/runs/{run}) · [Download the raw reports]({server}/{repo}/actions/runs/{run}#artifacts)')
    text = '\n'.join(out) + '\n'
    if len(text.encode('utf-8')) > MAX_BYTES:
        text = text.encode('utf-8')[:MAX_BYTES].decode('utf-8', 'ignore').rsplit('\n', 1)[0] + \
            '\n\n_The summary was cut to fit GitHub\'s size limit. The raw reports hold the rest._\n'
    return text, failed


def overview(directory, title):
    return build_overview(directory, title)[0]


if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == 'section':
        print(section(args[1], args[2:]))
    elif len(args) == 2 and args[0] == 'overview':
        summary, failed_checks = build_overview(args[1], os.getenv('SUMMARY_TITLE', 'Security run'))
        print(summary)
        # SUMMARY_GATE=1 turns the summary job into one status that is red when any check failed,
        # so a branch rule can require this single check instead of one per tool.
        if os.getenv('SUMMARY_GATE') == '1' and failed_checks:
            sys.exit(1)
    else:
        sys.exit(__doc__)
