"""Offline release authorization tests; temporary Git repositories, no real API/SSH."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('deploy_ci', ROOT / 'scripts/check-deploy-ci.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def run_fixture(**changes):
    return {**dict(id=100, run_number=10, run_attempt=1, head_sha='a' * 40, head_branch='main',
                event='push', path='.github/workflows/ci.yml', status='completed', conclusion='success',
                html_url='https://github.com/example/actions/runs/100'), **changes}


def job_fixtures():
    return [dict(name=name, status='completed', conclusion='success',
                 steps=[dict(name=step, conclusion='success') for step in sorted(steps)])
            for name, steps in gate.REQUIRED_STEPS.items()]


class CiEvidenceTests(unittest.TestCase):
    def test_exact_main_push_workflow_only(self):
        good = run_fixture()
        self.assertEqual(gate.approved_run([good], 'a' * 40), good)
        for changes in ({'head_sha': 'b' * 40}, {'event': 'pull_request'}, {'head_branch': 'other'},
                        {'path': '.github/workflows/pages.yml'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gate.approved_run([run_fixture(**changes)], 'a' * 40)

    def test_pending_failed_cancelled_or_newer_rerun_blocks_old_success(self):
        for state, conclusion in [('in_progress', None), ('queued', None), ('completed', 'failure'),
                                  ('completed', 'cancelled'), ('completed', 'skipped')]:
            with self.subTest(state=state, conclusion=conclusion), self.assertRaises(ValueError):
                gate.approved_run([run_fixture(), run_fixture(id=101, run_number=11,
                    status=state, conclusion=conclusion)], 'a' * 40)
        with self.assertRaises(ValueError):
            gate.approved_run([run_fixture(), run_fixture(run_attempt=2, status='in_progress')], 'a' * 40)

    def test_skipped_redis_or_missing_job_cannot_be_counted_as_passed(self):
        gate.require_jobs(job_fixtures())
        for name in gate.REQUIRED_STEPS:
            with self.assertRaises(ValueError):
                gate.require_jobs([job for job in job_fixtures() if job['name'] != name])
        for job in job_fixtures():
            for step in job['steps']:
                jobs = job_fixtures()
                target = next(item for item in jobs if item['name'] == job['name'])
                next(item for item in target['steps'] if item['name'] == step['name'])['conclusion'] = 'skipped'
                with self.subTest(step=step['name']), self.assertRaises(ValueError):
                    gate.require_jobs(jobs)
        jobs = job_fixtures()
        jobs[0]['conclusion'] = 'failure'
        with self.assertRaises(ValueError): gate.require_jobs(jobs)

    def test_uses_current_attempt_and_checks_remote_main(self):
        revision = 'a' * 40
        responses = [{'object': {'sha': revision}}, {'workflow_runs': [run_fixture(run_attempt=2)]},
                     {'jobs': job_fixtures(), 'total_count': 2}]
        with patch.object(gate, 'clean_revision', return_value=revision), patch.object(
                gate, 'github', side_effect=responses) as api:
            self.assertEqual(gate.check_ci(ROOT), revision)
            self.assertIn('/attempts/2/jobs', api.call_args_list[-1].args[1])
        with patch.object(gate, 'clean_revision', return_value=revision), patch.object(
                gate, 'github', return_value={'object': {'sha': 'b' * 40}}), self.assertRaises(ValueError):
            gate.check_ci(ROOT)


class ReleaseBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'
        self.root.mkdir()
        self.archive = Path(self.tmp.name) / 'release.zip'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.root)], check=True)
        for name in ['deploy.sh', 'scripts/deploy-server.sh', 'scripts/check-deploy-ci.py',
                     'scripts/deploy-package.py', 'backend/app/main.py', 'frontend/index.html',
                     'backend/requirements.txt']:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name.startswith('scripts/') or name == 'deploy.sh':
                shutil.copyfile(ROOT / name, target)
            else:
                target.write_text('synthetic source\n')
        self.commit()

    def commit(self):
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], cwd=self.root, check=True)
        self.revision = gate.clean_revision(self.root)

    def build(self):
        subprocess.run(['python3', str(self.root / 'scripts/deploy-package.py'), str(self.root), str(self.archive)],
                       check=True, capture_output=True)

    def test_package_binds_all_bytes_and_file_set_to_commit(self):
        self.build()
        gate.verify_package(self.root, self.archive, self.revision)
        with zipfile.ZipFile(self.archive) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        variants = []
        changed = dict(original); changed['frontend/index.html'] = b'changed after CI'; variants.append(changed)
        added = dict(original); added['frontend/unreviewed.js'] = b'new'; variants.append(added)
        missing = dict(original); del missing['frontend/index.html']; variants.append(missing)
        for data in variants:
            with zipfile.ZipFile(self.archive, 'w') as archive:
                for name, content in data.items(): archive.writestr(name, content)
            with self.assertRaises(ValueError): gate.verify_package(self.root, self.archive, self.revision)

    def test_dirty_untracked_and_different_commit_or_branch_are_blocked(self):
        self.build()
        file = self.root / 'frontend/uncommitted.js'
        file.write_text('pending')
        with self.assertRaises(ValueError): gate.verify_package(self.root, self.archive, self.revision)
        file.unlink()
        file = self.root / 'frontend/index.html'
        file.write_text('modified')
        with self.assertRaises(ValueError): gate.verify_package(self.root, self.archive, self.revision)
        old = self.revision
        self.commit()
        with self.assertRaises(ValueError): gate.verify_package(self.root, self.archive, old)
        subprocess.run(['git', 'checkout', '-qb', 'feature'], cwd=self.root, check=True)
        with self.assertRaises(ValueError): gate.clean_revision(self.root)

    def test_failed_ci_stops_deploy_before_packaging_or_ssh(self):
        binaries = Path(self.tmp.name) / 'bin'; binaries.mkdir()
        for tool in ['ssh', 'scp']:
            path = binaries / tool
            path.write_text('#!/bin/sh\ntouch "$SIDE_EFFECT"\nexit 99\n'); path.chmod(0o700)
        gh = binaries / 'gh'
        responses = [json.dumps({'object': {'sha': self.revision}}), json.dumps({'workflow_runs': [
            run_fixture(head_sha=self.revision, conclusion='failure')]})]
        gh.write_text('#!/usr/bin/env python3\nimport sys\nprint(' + repr(responses) +
                      '[0 if sys.argv[2].endswith("git/ref/heads/main") else 1])\n')
        gh.chmod(0o700)
        marker = Path(self.tmp.name) / 'contacted-server'
        result = subprocess.run(['bash', str(self.root / 'deploy.sh')], capture_output=True, text=True,
            env={**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                 'SIDE_EFFECT': str(marker), 'PYTHON': 'python3'}, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Latest CI', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / 'epub-factory-deploy.zip').exists())


if __name__ == '__main__':
    unittest.main()
