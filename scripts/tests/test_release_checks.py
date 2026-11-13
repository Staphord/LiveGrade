import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('release_checks', Path(__file__).resolve().parents[1] / 'release_checks.py')
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class SecurityGate(unittest.TestCase):
    def run_record(self, **kwargs):
        return dict(id=1, head_sha='abc', run_number=1, status='completed', conclusion='success', **kwargs)

    def test_missing_or_other_commit_refused(self):
        for payload in ({'workflow_runs': []}, {'workflow_runs': [self.run_record()]}):
            with self.assertRaises(ValueError):
                checks.require_security('other', payload)

    def test_success_accepted(self):
        self.assertEqual(checks.require_security('abc', {'workflow_runs': [self.run_record()]}), 1)

    def test_new_failure_pending_cancelled_or_skipped_overrides_old_success(self):
        for status, conclusion in [('completed', 'failure'), ('in_progress', None),
                                   ('completed', 'cancelled'), ('completed', 'skipped')]:
            run = self.run_record()
            run.update(id=2, run_number=2, status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(ValueError):
                checks.require_security('abc', {'workflow_runs': [self.run_record(), run]})

    def test_latest_attempt_must_pass(self):
        run = self.run_record(run_attempt=2)
        run.update(status='queued', conclusion=None)
        with self.assertRaises(ValueError):
            checks.require_security('abc', {'workflow_runs': [self.run_record(run_attempt=1), run]})


class EnvironmentGate(unittest.TestCase):
    def good(self):
        return {'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': True,
                                      'reviewers': [{'type': 'User', 'reviewer': {'id': 1}}]}],
                'deployment_branch_policy': {'protected_branches': True, 'custom_branch_policies': False}}

    def test_protected_environment_accepted(self):
        checks.require_environment(self.good())

    def test_current_unprotected_production_environment_refused(self):
        with self.assertRaises(ValueError):
            checks.require_environment({'protection_rules': [], 'deployment_branch_policy': None})

    def test_missing_reviewers_or_self_review_refused(self):
        for change in ({'reviewers': []}, {'prevent_self_review': False}, {'prevent_self_review': None}):
            payload = self.good()
            payload['protection_rules'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                checks.require_environment(payload)

    def test_unrestricted_or_custom_branch_policy_refused(self):
        for policy in (None, {}, {'protected_branches': False, 'custom_branch_policies': True},
                       {'protected_branches': True, 'custom_branch_policies': True}):
            payload = self.good()
            payload['deployment_branch_policy'] = policy
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                checks.require_environment(payload)
