#!/usr/bin/env python3
"""Require the latest static-security workflow run for this exact commit to pass."""
import argparse
import json
import sys


def require_security(sha, payload):
    runs = [run for run in payload['workflow_runs'] if run.get('head_sha') == sha]
    if not runs:
        raise ValueError('No static-security run exists for this commit')
    latest = max(runs, key=lambda run: (run['run_number'], run.get('run_attempt', 1)))
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise ValueError('The latest static-security run must complete successfully')
    return latest['id']


def require_environment(payload):
    reviewers = [rule for rule in payload.get('protection_rules', [])
                 if rule.get('type') == 'required_reviewers']
    if not any(rule.get('reviewers') and rule.get('prevent_self_review') is True for rule in reviewers):
        raise ValueError('Environment requires reviewers and prevention of self-review')
    policy = payload.get('deployment_branch_policy') or {}
    if policy.get('protected_branches') is not True or policy.get('custom_branch_policies') is not False:
        raise ValueError('Environment must allow only protected branches')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('security', 'environment'))
    parser.add_argument('path')
    parser.add_argument('--sha', default='')
    args = parser.parse_args()
    try:
        with open(args.path, encoding='utf-8') as handle:
            payload = json.load(handle)
        if args.mode == 'security':
            print(f'Static-security gate passed: run {require_security(args.sha, payload)}')
        else:
            require_environment(payload)
            print('Environment-protection gate passed')
    except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
        sys.exit(f'Release gate refused: {error}')
