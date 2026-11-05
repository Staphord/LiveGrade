"""Local deployment rehearsals: real disposable Git history, stubbed OS/app actions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_target', SCRIPT_DIR / 'release_target.py')
release_target = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_target)


class TargetValidation(unittest.TestCase):
    def test_rejects_invalid_inputs_before_git(self):
        cases = [
            ('bad', 'production', ''), ('status', 'bad', ''), ('status', 'testing', 'unused'),
            ('deploy-production', 'testing', 'v1.0.0'), ('deploy-testing', 'production', 'a' * 40),
            ('deploy-testing', 'testing', 'main'), ('deploy-production', 'production', 'main'),
            ('deploy-production', 'production', 'v1.0.0'),
            ('rollback', 'testing', 'v1.0.0', 'APP-1', 'BACKUP-1', '$(bad)'),
        ]
        with patch.object(release_target, 'git') as git:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    release_target.resolve(*case)
            git.assert_not_called()

    def test_status_needs_no_git(self):
        with patch.object(release_target, 'git') as git:
            self.assertEqual(release_target.resolve('status', 'production'), '')
            git.assert_not_called()

    def test_tag_is_fetched_peeled_and_checked_against_main(self):
        sha = 'a' * 40
        with patch.object(release_target, 'git', side_effect=['', '', sha, '']) as git:
            self.assertEqual(release_target.resolve('deploy-production', 'production', 'v1.2.3',
                                                   'APP-1', 'BACKUP-1', 'MIGRATION-1'), sha)
            self.assertIn(('fetch', '--no-tags', 'origin', 'refs/tags/v1.2.3:refs/tags/v1.2.3'),
                          [call.args for call in git.call_args_list])
            self.assertEqual(git.call_args.args,
                             ('merge-base', '--is-ancestor', sha, 'refs/remotes/origin/main'))

    def test_bad_git_output_refused(self):
        with patch.object(release_target, 'git', side_effect=['', 'invalid']):
            with self.assertRaises(ValueError):
                release_target.resolve('deploy-testing', 'testing', 'a' * 40)


class DeployRehearsal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='livegrade-ops-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'checkout'
        self.repo.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                        LIVEGRADE_DEPLOY_CONFIG_DIR=str(self.root / 'config'))
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'synthetic@example.test')
        self.git('config', 'user.name', 'Synthetic rehearsal')
        (self.repo / '.gitignore').write_text('venv/\n')
        (self.repo / 'requirements.txt').write_text('# synthetic\n')
        self.git('add', '.')
        self.git('commit', '-m', 'synthetic baseline')
        self.old = self.git('rev-parse', 'HEAD')
        self.git('tag', '-a', 'v1.0.0', '-m', 'synthetic baseline')
        (self.repo / 'version.txt').write_text('second release')
        self.git('add', '.')
        self.git('commit', '-m', 'synthetic second release')
        self.new = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v1.1.0')
        self.git('branch', 'staging')
        remote = self.root / 'remote.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(remote)], check=True,
                       capture_output=True, env=self.env)
        self.git('remote', 'add', 'origin', str(remote))
        self.git('checkout', '--detach', self.old)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.effects = self.root / 'effects'
        for name in ('systemctl', 'sudo', 'curl'):
            self.executable(self.bin / name, f'echo "{name} $*" >> "$EFFECTS"\n'
                            + ('exit "${HEALTH_FAILURE:-0}"\n' if name == 'curl' else ''))
        (self.repo / 'venv/bin').mkdir(parents=True)
        self.executable(self.repo / 'venv/bin/python', 'echo "python $*" >> "$EFFECTS"\n'
                        'if [[ "$*" == *"manage.py migrate"* ]]; then exit "${MIGRATE_FAILURE:-0}"; fi\n')
        config = self.root / 'config'
        config.mkdir()
        for environment in ('testing', 'production'):
            (config / f'{environment}.env').write_text(
                f'APP_DIR="{self.repo}"\nSERVICES="livegrade-web.service livegrade-worker.service"\n'
                f'STATE_DIR="{self.root / environment}"\nHEALTHCHECK_URL="http://127.0.0.1:9999/healthz"\n')
        self.env.update(PATH=f'{self.bin}:{os.environ["PATH"]}', EFFECTS=str(self.effects))

    @staticmethod
    def executable(path, body):
        path.write_text('#!/usr/bin/env bash\nset -e\n' + body)
        path.chmod(0o700)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, env=self.env,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def run_operation(self, operation='deploy-production', environment='production', target='v1.1.0',
                      approval='APP-1', sha=None, backup='BACKUP-1', migration='MIGRATION-1'):
        args = [operation, environment]
        if operation != 'status':
            args += [target, approval, sha or self.new, backup, migration]
        return subprocess.run(['bash', str(SCRIPT_DIR / 'deploy.sh'), *args], cwd=self.repo,
                              env=self.env, text=True, capture_output=True)

    def receipt(self, environment='production'):
        paths = list((self.root / environment).glob('*.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_cli_resolves_real_annotated_remote_tag(self):
        result = subprocess.run(['python3', str(SCRIPT_DIR / 'release_target.py'),
                                 '--operation', 'deploy-production', '--environment', 'production',
                                 '--target', 'v1.0.0', '--approval', 'APP-1', '--backup', 'BACKUP-1',
                                 '--migration-plan', 'MIGRATION-1'], cwd=self.repo, env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.old)

    def test_success_deploys_exact_commit_and_records_evidence(self):
        result = self.run_operation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.new)
        receipt = self.receipt()
        self.assertEqual(receipt['result'], 'success')
        self.assertEqual(receipt['previous_sha'], self.old)
        self.assertEqual(receipt['expected_sha'], self.new)
        self.assertEqual(receipt['approval_reference'], 'APP-1')
        self.assertEqual(receipt['backup_reference'], 'BACKUP-1')
        effects = self.effects.read_text()
        self.assertLess(effects.index('manage.py migrate'), effects.index('systemctl restart'))
        self.assertFalse((self.root / 'production/operation.lock').exists())

    def test_missing_approval_backup_or_migration_ref_never_mutates(self):
        for override in ({'approval': ''}, {'backup': ''}, {'migration': ''}):
            with self.subTest(override=override):
                result = self.run_operation(**override)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.effects.exists())
                self.assertEqual(self.git('rev-parse', 'HEAD'), self.old)

    def test_sha_changed_between_validation_and_server_refused(self):
        result = self.run_operation(sha=self.old)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.old)
        self.assertFalse(self.effects.exists())
        self.assertEqual(self.receipt()['phase'], 'validation')

    def test_unmerged_tag_refused(self):
        self.git('checkout', '--orphan', 'unreviewed')
        self.git('commit', '--allow-empty', '-m', 'unreviewed')
        unmerged = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v9.0.0')
        self.git('push', 'origin', 'refs/tags/v9.0.0')
        self.git('checkout', '--detach', self.old)
        result = self.run_operation(target='v9.0.0', sha=unmerged)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.effects.exists())

    def test_dirty_checkout_refused_without_discard(self):
        (self.repo / 'untracked.txt').write_text('keep this')
        result = self.run_operation()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.repo / 'untracked.txt').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.old)

    def test_lock_refuses_concurrent_operation(self):
        (self.root / 'production/operation.lock').mkdir(parents=True)
        result = self.run_operation()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'production/operation.lock').exists())
        self.assertFalse(self.effects.exists())

    def test_migration_failure_records_phase_and_never_restarts(self):
        self.env['MIGRATE_FAILURE'] = '7'
        result = self.run_operation()
        self.assertEqual(result.returncode, 7, result.stderr)
        receipt = self.receipt()
        self.assertEqual(receipt['phase'], 'migrations')
        self.assertEqual(receipt['result'], 'failed')
        self.assertNotIn('systemctl restart', self.effects.read_text())
        self.assertFalse((self.root / 'production/operation.lock').exists())

    def test_failed_health_is_not_reported_as_success(self):
        self.env['HEALTH_FAILURE'] = '22'
        result = self.run_operation()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('==> Deployed', result.stdout)
        self.assertEqual(self.receipt()['phase'], 'health')

    def test_rollback_changes_code_without_reversing_migrations(self):
        self.git('checkout', '--detach', self.new)
        result = self.run_operation(operation='rollback', target='v1.0.0', sha=self.old)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.old)
        self.assertNotIn('manage.py migrate', self.effects.read_text())
        self.assertEqual(self.receipt()['operation'], 'rollback')

    def test_testing_requires_staging_commit_and_uses_testing_config(self):
        result = self.run_operation(operation='deploy-testing', environment='testing', target=self.new,
                                    approval='', backup='', migration='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipt('testing')['deploy_environment'], 'testing')
        self.assertFalse((self.root / 'production').exists())

    def test_status_is_read_only_and_checks_health(self):
        result = self.run_operation(operation='status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.old, result.stdout)
        self.assertNotIn('restart', self.effects.read_text())
        self.assertFalse((self.root / 'production').exists())

    def test_environment_mismatch_and_shell_injection_refused(self):
        for kwargs in ({'environment': 'testing'}, {'target': 'v1.1.0;touch /tmp/not-run'},
                       {'approval': '$(id)'}, {'target': '--upload-pack=malicious'}):
            with self.subTest(kwargs=kwargs):
                result = self.run_operation(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.effects.exists())


if __name__ == '__main__':
    unittest.main()
