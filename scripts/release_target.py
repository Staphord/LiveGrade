#!/usr/bin/env python3
"""Resolve a manual deployment request to one full commit, without server secrets."""
import argparse
import re
import subprocess


REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/#=-]{0,511}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
TAG = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+\Z')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def resolve(operation, environment, target='', approval='', backup='', migration_plan=''):
    if environment not in {'testing', 'production'}:
        raise ValueError('Unknown environment')
    if operation not in {'deploy-testing', 'deploy-production', 'rollback', 'status'}:
        raise ValueError('Unknown operation')
    if operation == 'status':
        if target or approval or backup or migration_plan:
            raise ValueError('Status accepts only an environment')
        return ''
    if operation == 'deploy-testing' and environment != 'testing':
        raise ValueError('Testing operation requires testing')
    if operation == 'deploy-production' and environment != 'production':
        raise ValueError('Production operation requires production')
    if environment == 'production' or operation == 'rollback':
        if not TAG.fullmatch(target):
            raise ValueError('A version tag is required')
        if not all(REFERENCE.fullmatch(value) for value in (approval, backup, migration_plan)):
            raise ValueError('Approval, backup and migration-plan references are required')
        ref = f'refs/tags/{target}'
        # Fetch the exact remote tag without allowing replacement of an existing tag.
        git('fetch', '--no-tags', 'origin', f'{ref}:{ref}')
    else:
        if not SHA.fullmatch(target):
            raise ValueError('Testing requires a full commit SHA')
        ref = target
    branch = 'main' if environment == 'production' else 'staging'
    git('fetch', '--no-tags', 'origin', f'refs/heads/{branch}:refs/remotes/origin/{branch}')
    commit = git('rev-parse', '--verify', f'{ref}^{{commit}}')
    if not SHA.fullmatch(commit):
        raise ValueError('Git did not resolve a full commit SHA')
    git('merge-base', '--is-ancestor', commit, f'refs/remotes/origin/{branch}')
    return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('operation', 'environment', 'target', 'approval', 'backup', 'migration-plan'):
        parser.add_argument('--' + argument, default='')
    args = parser.parse_args()
    try:
        print(resolve(**vars(args)))
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Release validation failed: {error}\n')


if __name__ == '__main__':
    main()
