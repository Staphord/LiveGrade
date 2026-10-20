"""The scan's own reporting must not tear: these tests run in the security workflow.

A summary that shows nothing, or shows a secret, or crashes on an empty report, is a hole in the
shield, and nothing else would notice. Run with:  python3 -m unittest discover -s security/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import summary  # noqa: E402

SCRIPT = HERE.parent / 'summary.py'
SECRET = 'AKIA-SUPER-SECRET-VALUE-0123456789'


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, content):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
        return str(path)


class SectionTests(Files):

    def test_every_tool_survives_a_missing_report(self):
        for tool in summary.TOOLS:
            with self.subTest(tool=tool):
                text = summary.section(tool, [str(self.dir / 'nothing.json')])
                first = text.splitlines()[0]
                self.assertIn(first, ('<!--headline: no report-->', '<!--headline: could not read alerts-->'))
                self.assertEqual(summary.verdict(text), 'warning')

    def test_every_tool_survives_garbage(self):
        for tool in summary.TOOLS:
            with self.subTest(tool=tool):
                text = summary.section(tool, [self.write('garbage.json', '{{{ not json')])
                self.assertTrue(text.startswith('<!--headline:'))

    def test_bandit_lists_where_what_and_how_to_fix(self):
        path = self.write('bandit.json', {'metrics': {'_totals': {'loc': 10}, 'a.py': {}}, 'results': [{
            'test_id': 'B608', 'test_name': 'hardcoded_sql_expressions', 'issue_severity': 'MEDIUM', 'issue_text': 'sql',
            'filename': './app/db.py', 'line_number': 7, 'code': '7 query = f"SELECT {x}"\n', 'issue_cwe': {'id': 89, 'link': 'https://cwe'},
            'more_info': 'https://bandit/B608'}]})
        text = summary.section('bandit', [path])
        self.assertIn('headline: 1 issue (1 medium)', text)
        for expected in ('app/db.py:7', 'In plain words', 'What to do', 'SELECT', 'CWE-89'):
            self.assertIn(expected, text)
        self.assertEqual(summary.verdict(text), 'failure')

    def test_bandit_with_no_results_is_clean(self):
        text = summary.section('bandit', [self.write('b.json', {'metrics': {'_totals': {'loc': 5}}, 'results': []})])
        self.assertEqual(summary.verdict(text), 'success')

    def test_pip_audit_names_the_fix(self):
        path = self.write('p.json', {'dependencies': [
            {'name': 'django', 'version': '4.2', 'vulns': [{'id': 'PYSEC-1', 'fix_versions': ['5.2.1'], 'aliases': ['CVE-1'], 'description': 'bad'}]},
            {'name': 'ok', 'version': '1', 'vulns': []}]})
        text = summary.section('pip-audit', [path])
        self.assertIn('1 advisory in 1 component', text)
        self.assertIn('5.2.1', text)
        self.assertIn('osv.dev/vulnerability/PYSEC-1', text)

    def test_gitleaks_never_prints_the_secret(self):
        path = self.write('g.json', [{'RuleID': 'generic-api-key', 'File': '.env', 'StartLine': 3, 'Commit': 'abcdef1234567',
                                      'Secret': SECRET, 'Match': f'KEY={SECRET}', 'Author': 'a'}])
        text = summary.section('gitleaks', [path])
        self.assertNotIn(SECRET, text)
        self.assertIn('1 possible secret', text)
        self.assertEqual(summary.verdict(text), 'failure')

    def test_gitleaks_empty_means_clean(self):
        self.assertEqual(summary.verdict(summary.section('gitleaks', [self.write('g.json', [])])), 'success')

    def test_semgrep_ignores_what_was_silenced_in_the_code(self):
        def result(rule, suppressed):
            r = {'ruleId': rule, 'message': {'text': 'm'}, 'locations': [{'physicalLocation': {
                'artifactLocation': {'uri': 'a.py'}, 'region': {'startLine': 1}}}]}
            if suppressed:
                r['suppressions'] = [{'kind': 'inSource'}]
            return r
        sarif = {'runs': [{'tool': {'driver': {'rules': [{'id': 'x.y.real', 'defaultConfiguration': {'level': 'error'}},
                                                         {'id': 'x.y.quiet', 'defaultConfiguration': {'level': 'error'}}]}},
                           'results': [result('x.y.real', False), result('x.y.quiet', True)]}]}
        text = summary.section('semgrep', [self.write('s.sarif', sarif)])
        self.assertIn('1 finding', text)
        self.assertIn('real', text)
        self.assertNotIn('`quiet`', text)
        clean = summary.section('semgrep', [self.write('s2.sarif', {'runs': [{'results': [result('x.y.quiet', True)]}]})])
        self.assertEqual(summary.verdict(clean), 'success')
        self.assertIn('1 silenced', clean)

    def test_checkov_failures_are_a_review_not_a_failure(self):
        path = self.write('c.json', {'summary': {'passed': 5}, 'results': {'failed_checks': [
            {'check_id': 'CKV2_GHA_1', 'check_name': 'perm', 'file_path': '/workflows/a.yml', 'file_line_range': [3, 4]}]}})
        text = summary.section('checkov', [path])
        self.assertEqual(summary.verdict(text), 'warning')
        self.assertIn('.github/workflows/a.yml', text)

    def test_codeql_accepts_a_paginated_list_an_empty_one_and_an_error(self):
        alert = {'number': 2, 'html_url': 'https://x/2', 'rule': {'id': 'js/xss', 'description': 'XSS', 'severity': 'error',
                                                                  'security_severity_level': 'high', 'help': 'XSS Writing input is bad.'},
                 'most_recent_instance': {'message': {'text': 'user value'}, 'commit_sha': 'abc',
                                          'location': {'path': 'a.js', 'start_line': 4}}}
        text = summary.section('codeql', [self.write('a.json', [[alert]])])
        self.assertIn('1 open alert (1 high)', text)
        self.assertIn('a.js:4', text)
        self.assertNotIn('XSS XSS', text)   # the repeated title is trimmed from the help text
        self.assertEqual(summary.verdict(summary.section('codeql', [self.write('e.json', [[]])])), 'success')
        self.assertEqual(summary.verdict(summary.section('codeql', [self.write('x.json', {'error': 'no'})])), 'warning')

    def test_probe_groups_the_checks_by_the_question_they_answer(self):
        path = self.write('probe.txt', 'ok    stored XSS /a [200]\nFAIL  cross-org B->A POST /x -> 200\nWARN  WebSocket refuses a foreign Origin\n')
        text = summary.section('probe', [path])
        self.assertIn('1 ok, 1 warning, 1 failed', text)
        self.assertIn("another organisation", text)
        self.assertEqual(summary.verdict(text), 'failure')

    def test_testssl_flags_high_findings(self):
        text = summary.section('testssl', [self.write('t.json', [{'id': 'BEAST', 'severity': 'HIGH', 'finding': 'vulnerable'}])])
        self.assertEqual(summary.verdict(text), 'failure')

    def zap_report(self, results, rules):
        return {'runs': [{'tool': {'driver': {'rules': rules}}, 'results': results}]}

    def zap_result(self, rule, level, uri):
        return {'ruleId': rule, 'level': level, 'locations': [{'physicalLocation': {'artifactLocation': {'uri': uri}}}],
                'webRequest': {'method': 'GET'}, 'webResponse': {'statusCode': 200}}

    def test_zap_alerts_open_on_the_page_with_meaning_fix_and_pages_but_no_host(self):
        rules = [{'id': '10038', 'name': 'CSP Header Not Set', 'defaultConfiguration': {'level': 'warning'}, 'properties': {'references': []}},
                 {'id': '10109', 'name': 'Modern Web Application', 'defaultConfiguration': {'level': 'none'}, 'properties': {}}]
        data = self.zap_report([self.zap_result('10038', 'warning', 'http://localhost:8001/assessments/'),
                                self.zap_result('10109', 'none', 'http://localhost:8001/')], rules)
        text = summary.section('zap', [self.write('zap-authenticated.json', data), str(self.dir / 'zap-anonymous.json')])
        self.assertIn('<details open>', text)
        self.assertIn('In plain words', text)
        self.assertIn('What to do', text)
        self.assertIn('`GET /assessments/`', text)
        self.assertNotIn('localhost:8001', text)
        self.assertIn('Signed-in scan', text)
        self.assertIn('No report was produced for this scan', text)   # the missing second file
        self.assertEqual(summary.verdict(text), 'warning')

    def test_zap_high_alerts_fail(self):
        rules = [{'id': '40012', 'name': 'XSS', 'defaultConfiguration': {'level': 'error'}, 'properties': {}}]
        data = self.zap_report([self.zap_result('40012', 'error', 'http://x/a')], rules)
        self.assertEqual(summary.verdict(summary.section('zap', [self.write('zap-authenticated.json', data)])), 'failure')


class OverviewTests(Files):

    def run_overview(self, needs, gate=False):
        env = {**os.environ, 'NEEDS_JSON': json.dumps(needs), 'SUMMARY_TITLE': 'T'}
        env.pop('SUMMARY_GATE', None)
        if gate:
            env['SUMMARY_GATE'] = '1'
        return subprocess.run([sys.executable, '-I', str(SCRIPT), 'overview', str(self.dir)], env=env, capture_output=True, text=True)

    def test_sections_are_found_in_the_folders_artifacts_unpack_into(self):
        # download-artifact puts each artifact's files under out/ inside the folder: the first version of the
        # overview only looked one level down and showed a table with no details at all.
        self.write('result-bandit/out/bandit.md', summary.section('bandit', [self.write('b.json', {'results': [], 'metrics': {}})]))
        out = self.run_overview({'bandit': {'result': 'success'}}).stdout
        self.assertIn('Bandit', out)
        self.assertIn('No issues', out)

    def test_a_failed_job_without_a_report_is_shown_as_failed_not_hidden(self):
        out = self.run_overview({'bandit': {'result': 'failure'}}).stdout
        self.assertIn('❌', out)
        self.assertIn('stopped before reporting', out)

    def test_the_gate_fails_only_when_asked_to_and_only_for_failures(self):
        self.write('x/out/bandit.md', summary.section('bandit', [self.write('b.json', {'results': [], 'metrics': {}})]))
        self.assertEqual(self.run_overview({'bandit': {'result': 'success'}}, gate=True).returncode, 0)
        self.assertEqual(self.run_overview({'bandit': {'result': 'failure'}}, gate=False).returncode, 0)
        self.assertEqual(self.run_overview({'bandit': {'result': 'failure'}}, gate=True).returncode, 1)

    def test_a_review_result_does_not_trip_the_gate(self):
        self.write('x/out/checkov.md', summary.section('checkov', [self.write('c.json', {'summary': {'passed': 1}, 'results': {'failed_checks': [
            {'check_id': 'K', 'check_name': 'n', 'file_path': '/a.yml'}]}})]))
        self.assertEqual(self.run_overview({'checkov': {'result': 'success'}}, gate=True).returncode, 0)

    def test_the_summary_is_cut_before_github_would_reject_it(self):
        self.write('x/out/zap.md', '<!--headline: big-->\n✅ ok\n' + ('padding line\n' * 120_000))
        text = summary.build_overview(str(self.dir), 'T')[0]
        self.assertLess(len(text.encode()), 1_048_576)
        self.assertIn('cut to fit', text)


class AcceptedRisksTests(unittest.TestCase):
    """Nothing may be ignored by the scan without being written down, with its reason, in accepted-risks.md."""

    ROOT = HERE.parent.parent

    def listed(self):
        return (self.ROOT / 'security' / 'accepted-risks.md').read_text()

    def test_every_ignored_advisory_is_explained(self):
        import re
        flagged = set()
        for path in [self.ROOT / 'security' / 'scan.sh', *(self.ROOT / '.github' / 'workflows').glob('*.yml')]:
            flagged |= set(re.findall(r'--ignore-vuln\s+([\w.-]+)', path.read_text()))
        self.assertTrue(flagged, 'expected at least the Twisted advisory to be ignored')
        for advisory in flagged:
            self.assertIn(advisory, self.listed())

    def test_every_zap_alert_filter_is_explained(self):
        import re
        for path in (self.ROOT / 'security' / 'zap').glob('*.yaml'):
            for rule in re.findall(r'ruleId:\s*(\d+)', path.read_text()):
                self.assertIn(rule, self.listed(), f'{path.name} filters ZAP rule {rule} without saying why')

    def test_the_env_file_the_local_scan_allows_is_git_ignored(self):
        self.assertRegex((self.ROOT / '.gitignore').read_text(), r'(?m)^\.env$')

    def test_the_strict_gitleaks_config_does_not_allow_env_files(self):
        self.assertNotIn('.env', (self.ROOT / 'security' / 'gitleaks.toml').read_text())


class ToolingFilesTests(unittest.TestCase):

    def test_the_scripts_compile(self):
        for name in ('probe.py', 'check_headers.py', 'seed_session.py', 'summary.py'):
            with self.subTest(script=name):
                compile((HERE.parent / name).read_text(), name, 'exec')

    def test_the_security_tools_are_pinned(self):
        for name in ('bandit', 'pip-audit', 'checkov'):
            text = (HERE.parent / f'requirements-{name}.txt').read_text()
            self.assertRegex(text, rf'(?m)^{name}==\d+\.\d+\.\d+$')

    def test_every_workflow_action_is_pinned_to_a_commit(self):
        import re
        for path in (HERE.parent.parent / '.github' / 'workflows').glob('*.yml'):
            for line in path.read_text().splitlines():
                found = re.search(r'uses:\s*(\S+)', line)
                if found and not found.group(1).startswith('./'):
                    self.assertRegex(found.group(1), r'@[0-9a-f]{40}$', f'{path.name}: {line.strip()}')

    def test_every_container_image_in_the_workflows_is_pinned_by_digest(self):
        import re
        seen = []
        for path in (HERE.parent.parent / '.github' / 'workflows').glob('*.yml'):
            text = path.read_text()
            for image in re.findall(r'(?:image:\s*|docker run [^\n]*?)((?:ghcr\.io|semgrep|redis)[\w./-]*[:@][\w.:-]+)', text):
                seen.append(image)
                self.assertIn('@sha256:', image, f'{path.name}: {image}')
        self.assertGreaterEqual(len(seen), 4, 'the pattern no longer finds the images it is meant to check')


if __name__ == '__main__':
    unittest.main()
