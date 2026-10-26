#!/usr/bin/env python3
"""Fail closed before deployment unless the exact clean main revision passed CI.

Uses the operator's existing gh login; never loads application credentials.
Package verification is local and compares every shipped byte against Git HEAD.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

REPOSITORY = 'zengchanghuan/epub-factory'
REQUIRED_STEPS = {
    'frontend': {'Run frontend regression', 'Verify deployment CI gate', 'Check static tool pages cannot regenerate checkout copies',
                 'Exercise unified tool entries in isolated Chrome'},
    'regression': {'Build image (与生产同 Python/依赖/epubcheck)',
                   'Run offline regression in container', 'Verify quota Lua against actual Redis 6 and 7'},
}


def command(args, root):
    try:
        return subprocess.check_output(args, cwd=root, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'{args[0]} check failed; verify installation, authentication and network access') from exc


def git(root, *args):
    return command(['git', *args], root).decode().strip()


def clean_revision(root):
    if git(root, 'branch', '--show-current') != 'main':
        raise ValueError('Deploy only from main')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Commit intended changes and keep the working tree clean before deployment')
    return git(root, 'rev-parse', 'HEAD')


def github(root, endpoint):
    return json.loads(command(['gh', 'api', f'repos/{REPOSITORY}/{endpoint}'], root))


def approved_run(runs, revision):
    matching = [run for run in runs if run.get('head_sha') == revision
                and run.get('head_branch') == 'main' and run.get('event') == 'push'
                and run.get('path') == '.github/workflows/ci.yml']
    if not matching:
        raise ValueError('No main push CI exists for this exact commit')
    run = max(matching, key=lambda item: (item['run_number'], item.get('run_attempt', 1), item['id']))
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        raise ValueError('Latest CI for this commit is not successful: ' + run['html_url'])
    return run


def require_jobs(jobs):
    names = [job['name'] for job in jobs]
    if not REQUIRED_STEPS.keys() <= set(names) or len(names) != len(set(names)):
        raise ValueError('CI job evidence is missing or ambiguous')
    for job in jobs:
        if job.get('status') != 'completed' or job.get('conclusion') != 'success':
            raise ValueError('CI job did not pass: ' + job['name'])
        steps = {step['name']: step.get('conclusion') for step in job.get('steps', [])}
        if any(steps.get(name) != 'success' for name in REQUIRED_STEPS.get(job['name'], ())):
            raise ValueError('Required CI step failed, was skipped or is missing: ' + job['name'])


def check_ci(root):
    revision = clean_revision(root)
    if github(root, 'git/ref/heads/main')['object']['sha'] != revision:
        raise ValueError('Local HEAD must match GitHub main before deployment')
    runs = github(root, f'actions/workflows/ci.yml/runs?head_sha={revision}&branch=main&event=push&per_page=100')
    run = approved_run(runs['workflow_runs'], revision)
    # Attempt-specific jobs prevent a previous successful attempt masking a rerun.
    jobs = []
    page = 1
    while True:
        result = github(root, f'actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs?per_page=100&page={page}')
        jobs.extend(result['jobs'])
        if len(jobs) >= result['total_count']:
            break
        if not result['jobs']:
            raise ValueError('Incomplete CI job response')
        page += 1
    require_jobs(jobs)
    if clean_revision(root) != revision:
        raise ValueError('HEAD changed during the CI check')
    print('Full CI verified: ' + run['html_url'], file=sys.stderr)
    return revision


def verify_package(root, archive_path, revision):
    if clean_revision(root) != revision:
        raise ValueError('Source changed after CI verification')
    spec = importlib.util.spec_from_file_location('deploy_package', Path(__file__).with_name('deploy-package.py'))
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    tree = command(['git', 'ls-tree', '-rz', revision], root).decode().split('\0')
    files = {}
    for entry in filter(None, tree):
        metadata, name = entry.split('\t', 1)
        mode, kind, blob = metadata.split()
        if package.included(name):
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise ValueError('Nonregular release source: ' + name)
            files[name] = hashlib.sha256(command(['git', 'cat-file', 'blob', blob], root)).hexdigest()
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(files) | {'deploy-manifest.json'}:
            raise ValueError('Release contents do not match the tested commit')
        if json.loads(archive.read('deploy-manifest.json')) != files:
            raise ValueError('Release manifest does not match the tested commit')
        if any(hashlib.sha256(archive.read(name)).hexdigest() != digest for name, digest in files.items()):
            raise ValueError('Release bytes do not match the tested commit')
    print(f'Release package matches CI commit {revision}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--package', type=Path)
    parser.add_argument('--revision')
    args = parser.parse_args()
    try:
        if args.package:
            if not args.revision:
                raise ValueError('--package requires the CI-verified --revision')
            verify_package(args.root, args.package, args.revision)
        else:
            if args.revision:
                raise ValueError('--revision requires --package')
            print(check_ci(args.root))
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print('Deployment blocked: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
