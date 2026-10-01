"""R9 deployment contracts: temporary files and fake system commands only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPTS = Path(__file__).resolve().parent
SERVER = SCRIPTS / 'deploy-server.sh'
spec = importlib.util.spec_from_file_location('worker_services', SCRIPTS / 'prepare-worker-services.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
PREFLIGHT = SERVER.read_text().split('# R9_WORKER_PREFLIGHT_BEGIN\n', 1)[1].split('# R9_WORKER_PREFLIGHT_END', 1)[0]
policy = {'__name__': 'offline_policy'}
exec(compile(PREFLIGHT, str(SERVER), 'exec'), policy)


class WorkerUnitTests(unittest.TestCase):
    def units(self, **overrides):
        params = dict(project_dir='/srv/epub', user='epub', group='books',
                      environment_files=['/srv/epub/backend/.env', '/etc/epub/worker.env'])
        return generator.render_units(**dict(params, **overrides))

    def test_book_dropin_preserves_existing_non_exec_settings(self):
        text = self.units()['epub-factory-worker.service.d/90-explicit-role.conf']
        self.assertIn('ExecStart=\nExecStart=/srv/epub/backend/.venv/bin/python -m app.infra.worker book', text)
        self.assertIn('Environment=NOSETPS=1', text)
        for directive in ('User=', 'Group=', 'WorkingDirectory=', 'EnvironmentFile=', 'LimitNOFILE='):
            self.assertNotIn(directive, text)

    def test_housekeeping_uses_explicit_identity_workdir_and_env_paths(self):
        text = self.units()['epub-factory-housekeeping.service']
        for value in ('User=epub', 'Group=books', 'WorkingDirectory=/srv/epub/backend',
                      'EnvironmentFile=/srv/epub/backend/.env', 'EnvironmentFile=/etc/epub/worker.env',
                      'Environment=NOSETPS=1', '-m app.infra.worker housekeeping',
                      'KillMode=mixed', 'TimeoutStopSec=1900'):
            self.assertIn(value, text)
        self.assertLess(text.index('/backend/.env'), text.index('/etc/epub/worker.env'))

    def test_generator_does_not_read_environment_files(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('No credential reads')):
            self.units()

    def test_preserves_explicit_workdir_and_optional_env_file(self):
        text = self.units(working_directory='/srv/custom-backend', environment_files=['-/etc/epub/optional.env'])['epub-factory-housekeeping.service']
        self.assertIn('WorkingDirectory=/srv/custom-backend', text)
        self.assertIn('EnvironmentFile=-/etc/epub/optional.env', text)

    def test_rejects_ambiguous_paths_and_accounts(self):
        for params in ({'project_dir': '/srv/my book'}, {'project_dir': '/srv/%n'},
                       {'project_dir': '/srv/../etc'}, {'user': 'epub\nExecStart=bad'},
                       {'environment_files': []}, {'user': ''}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.units(**params)

    def test_preview_and_explicit_output_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'drafts'
            args = [sys.executable, str(SCRIPTS / 'prepare-worker-services.py'),
                    '--project-dir', '/srv/epub', '--user', 'epub',
                    '--working-directory', '/srv/epub/backend',
                    '--environment-file', '/not-read/production.env']
            preview = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertFalse(target.exists())
            result = subprocess.run(args + ['--output-dir', str(target)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()}
            repeated = subprocess.run(args + ['--output-dir', str(target)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(before, {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()})


class WorkerRolePolicyTests(unittest.TestCase):
    def test_accepts_launcher_and_explicit_single_queue(self):
        for role, queue in [('book', 'celery'), ('housekeeping', 'housekeeping')]:
            self.assertTrue(policy['correct_role'](['/venv/bin/python', '-m', 'app.infra.worker', role], role))
            self.assertTrue(policy['correct_role'](['/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app',
                                                 'worker', '-Q', queue, '-c', '1'], role))

    def test_rejects_unrestricted_wrong_and_multiple_queues(self):
        base = ['/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app', 'worker']
        for tail in ([], ['-Q', 'celery,housekeeping'], ['-Q', 'housekeeping'],
                     ['-Q', 'celery', '--queues=housekeeping']):
            self.assertFalse(policy['correct_role'](base + tail, 'book'))
        self.assertFalse(policy['correct_role'](base + ['-Q', 'housekeeping', '-c', '2'], 'housekeeping'))
        self.assertFalse(policy['correct_role'](['/bin/bash', '-c', 'python -m app.infra.worker book'], 'book'))

    def test_accepts_only_unambiguous_explicit_prefork_pool(self):
        base = ['/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app',
                'worker', '-Q', 'housekeeping', '-c', '1']
        for pool in ([], ['-P', 'prefork'], ['-Pprefork'], ['--pool', 'prefork'], ['--pool=prefork']):
            with self.subTest(pool=pool):
                self.assertTrue(policy['correct_role'](base + pool, 'housekeeping'))
        for pool in (['-P', 'solo'], ['-Psolo'], ['--pool', 'threads'], ['--pool=eventlet'],
                     ['-P', 'prefork', '--pool=prefork'], ['--pool'], ['--pool='], ['--poolprefork']):
            with self.subTest(pool=pool):
                self.assertFalse(policy['correct_role'](base + pool, 'housekeeping'))

    def test_rejects_autoscale_exclusions_and_ambiguous_concurrency(self):
        for role, queue in [('book', 'celery'), ('housekeeping', 'housekeeping')]:
            base = ['/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app',
                    'worker', '-Q', queue, '-c', '1']
            for tail in (['--autoscale=3,1'], ['--autoscale', '3,1'], ['--autoscale'],
                         ['-X', queue], ['-X' + queue], ['--exclude-queues', queue],
                         ['--exclude-queues=' + queue], ['-c1'], ['--concurrency=1'],
                         ['--concurrency'], ['--concurrency1'], ['--']):
                with self.subTest(role=role, tail=tail):
                    self.assertFalse(policy['correct_role'](base + tail, role))

    def test_configured_role_does_not_authorize_old_live_process(self):
        def value(service, name):
            role = 'book' if service.endswith('worker') else 'housekeeping'
            return '345' if name == 'MainPID' else '{ path=/venv/bin/python ; argv[]=/venv/bin/python -m app.infra.worker ' + role + ' ; ignore_errors=no ; }'
        with patch.dict(policy, property_value=value), patch.object(Path, 'read_bytes', return_value=b'/venv/bin/celery\0-A\0app.infra.celery_app:celery_app\0worker\0'):
            with self.assertRaisesRegex(SystemExit, 'running process'):
                policy['verify_workers']()

    def test_accepts_both_verified_live_launchers_and_stopped_units(self):
        def value(service, name):
            role = 'book' if service.endswith('worker') else 'housekeeping'
            if name == 'MainPID':
                return '345' if role == 'book' else '0'
            return '{ path=/venv/bin/python ; argv[]=/venv/bin/python -m app.infra.worker ' + role + ' ; ignore_errors=no ; }'
        with patch.dict(policy, property_value=value), patch.object(Path, 'read_bytes', return_value=b'/venv/bin/python\0-m\0app.infra.worker\0book\0'):
            policy['verify_workers']()

    @unittest.skipUnless(hasattr(sys, 'orig_argv'), 'Real interpreter argv requires Python 3.10+')
    def test_real_shebang_argv_passes_configured_and_live_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            celery = Path(tmp) / 'celery'
            celery.write_text('#!' + sys.executable + '\nimport json, sys\nprint(json.dumps(sys.orig_argv))\n')
            celery.chmod(0o700)
            snapshots = {}
            for service, role, queue in [('epub-factory-worker', 'book', 'celery'),
                                         ('epub-factory-housekeeping', 'housekeeping', 'housekeeping')]:
                configured = [str(celery), '-A', 'app.infra.celery_app:celery_app', 'worker', '-Q', queue, '-c', '1']
                result = subprocess.run(configured, text=True, capture_output=True, check=True, timeout=10)
                live = json.loads(result.stdout)
                self.assertEqual(live[1:], configured)
                self.assertTrue(policy['correct_role'](live, role))
                snapshots[service] = (configured, b'\0'.join(value.encode() for value in live) + b'\0')
            def value(service, name):
                if name == 'MainPID':
                    return '501' if service.endswith('worker') else '502'
                return '{ path=' + str(celery) + ' ; argv[]=' + shlex.join(snapshots[service][0]) + ' ; ignore_errors=no ; }'
            def proc(path):
                service = 'epub-factory-worker' if str(path) == '/proc/501/cmdline' else 'epub-factory-housekeeping'
                return snapshots[service][1]
            with patch.dict(policy, property_value=value), patch.object(Path, 'read_bytes', proc):
                policy['verify_workers']()

    def test_shebang_form_still_rejects_unknown_entrypoints_and_unsafe_options(self):
        flags = ['-A', 'app.infra.celery_app:celery_app', 'worker', '-Q', 'housekeeping', '-c', '1']
        for script in ('celery', '/venv/bin/unknown', '/venv/bin/celery.py'):
            self.assertFalse(policy['correct_role'](['/venv/bin/python', script] + flags, 'housekeeping'))
        for tail in (['--autoscale=3,1'], ['--pool=solo'], ['-Xhousekeeping'], ['-Qcelery'], ['-c2']):
            self.assertFalse(policy['correct_role'](['/venv/bin/python', '/venv/bin/celery'] + flags + tail, 'housekeeping'))
        self.assertFalse(policy['correct_role'](['/bin/bash', '/venv/bin/celery'] + flags, 'housekeeping'))

    def test_live_shebang_mismatch_does_not_inherit_configured_role(self):
        def value(service, name):
            if name == 'MainPID':
                return '501'
            return '{ path=/venv/bin/celery ; argv[]=/venv/bin/celery -A app.infra.celery_app:celery_app worker -Q celery ; ignore_errors=no ; }'
        live = b'\0'.join(arg.encode() for arg in ['/venv/bin/python', '/venv/bin/celery', '-A',
                          'app.infra.celery_app:celery_app', 'worker', '-Q', 'housekeeping', '-c', '1']) + b'\0'
        with patch.dict(policy, property_value=value), patch.object(Path, 'read_bytes', return_value=live):
            with self.assertRaisesRegex(SystemExit, 'running process does not prove'):
                policy['verify_workers']()


class DeployWorkerFlowTests(unittest.TestCase):
    def run_script(self, *, missing_housekeeping=False, old_worker=False, live_pid=0, fail_pip=False,
                   check=False, direct_tail=None, direct_role='housekeeping', post_live_argv=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, commands = root / 'project', root / 'commands'
            commands.mkdir()
            backend = project / 'backend'
            (backend / '.venv/bin').mkdir(parents=True)
            (backend / '.env').write_text('DATABASE_URL=sqlite:///./epub_jobs.db\nREPAIR_UPLOAD_DIR=' + str(root / 'repairs') + '\n')
            (backend / 'requirements.txt').write_text('')
            with sqlite3.connect(backend / 'epub_jobs.db') as db:
                db.execute('CREATE TABLE epub_jobs(status TEXT)')
            jar = project / 'tools/epubcheck-5.1.0/epubcheck.jar'
            jar.parent.mkdir(parents=True)
            jar.write_text('offline fixture')
            log = root / 'actions.jsonl'
            driver = '''#!PYTHON
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['FAKE_ACTIONS'], 'a') as f:
    f.write(json.dumps([name] + args) + '\\n')
if name == 'sudo':
    os.execvp(args[1], args[1:])
elif name == 'systemctl' and args[0] == 'show':
    service, prop = args[1], args[3]
    if prop == 'LoadState':
        print('not-found' if service == 'nginx' or (service == 'epub-factory-housekeeping' and os.environ['FAKE_MISSING'] == '1') else 'loaded')
    elif prop == 'MainPID':
        role = 'book' if service.endswith('worker') else 'housekeeping'
        if os.environ['FAKE_POST_LIVE'] and role == os.environ['FAKE_DIRECT_ROLE'] and pathlib.Path(os.environ['FAKE_RESTARTED']).exists():
            print('501')
        else: print(os.environ['FAKE_PID'] if service.endswith('worker') else '0')
    elif prop == 'ExecStart':
        role = 'book' if service.endswith('worker') else 'housekeeping'
        argv = '/venv/bin/python -m app.infra.worker ' + role
        if role == 'book' and os.environ['FAKE_OLD'] == '1': argv = '/venv/bin/celery -A app.infra.celery_app:celery_app worker'
        if os.environ['FAKE_DIRECT'] and role == os.environ['FAKE_DIRECT_ROLE']:
            argv = os.environ['FAKE_DIRECT']
        print('{ path=/venv/bin/python ; argv[]=' + argv + ' ; ignore_errors=no ; }')
elif name == 'systemctl' and args[0] == 'restart': pathlib.Path(os.environ['FAKE_RESTARTED']).touch()
elif name == 'curl': print('{"status":"ok"}')
elif name == 'pip' and args[0] == 'install' and os.environ['FAKE_FAIL_PIP'] == '1': sys.exit(7)
'''.replace('PYTHON', sys.executable)
            for name in ('systemctl', 'sudo', 'curl', 'java', 'flock', 'sleep'):
                path = commands / name
                path.write_text(driver)
                path.chmod(0o700)
            pip = backend / '.venv/bin/pip'
            pip.write_text(driver)
            pip.chmod(0o700)
            python = backend / '.venv/bin/python'
            python.write_text('#!/bin/bash\nif [[ "$1" == -m && "$2" == pip ]]; then shift 2; exec '
                              + shlex.quote(str(pip)) + ' "$@"; fi\nexec '
                              + shlex.quote(sys.executable) + ' "$@"\n')
            python.chmod(0o700)
            if post_live_argv is None:
                (commands / 'python3').symlink_to(sys.executable)
            else:
                # Execute the actual deployment heredoc unchanged; only /proc
                # bytes are replaced, since macOS has no Linux proc filesystem.
                proc_python = commands / 'python3'
                proc_python.write_text('''#!PYTHON
import json, os, pathlib, sys
read_bytes = pathlib.Path.read_bytes
def fixture_proc(path):
    if str(path) == '/proc/501/cmdline':
        return b'\\0'.join(arg.encode() for arg in json.loads(os.environ['FAKE_POST_LIVE'])) + b'\\0'
    return read_bytes(path)
pathlib.Path.read_bytes = fixture_proc
if sys.argv[1] == '-':
    code = sys.stdin.read()
    sys.argv = sys.argv[1:]
elif sys.argv[1] == '-c':
    code = sys.argv[2]
    sys.argv = sys.argv[2:]
else: raise AssertionError('Unexpected fixture Python invocation')
exec(compile(code, '<actual-deployment-script>', 'exec'), {'__name__': '__main__'})
'''.replace('PYTHON', sys.executable))
                proc_python.chmod(0o700)
            archive = root / 'release.zip'
            sources = {'backend/requirements.txt': b'', 'scripts/prepare-worker-services.py': b'# offline source fixture\n'}
            with zipfile.ZipFile(archive, 'w') as z:
                for name, data in sources.items():
                    z.writestr(name, data)
                z.writestr('deploy-manifest.json', json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}))
            env = {key: value for key, value in os.environ.items() if not key.startswith(('DATABASE_', 'REPAIR_', 'CELERY_', 'OPENAI_', 'EPUB_', 'PYTHON')) and key != 'BASH_ENV'}
            env.update(PATH=str(commands) + os.pathsep + os.environ['PATH'], FAKE_ACTIONS=str(log),
                       FAKE_MISSING=str(int(missing_housekeeping)), FAKE_OLD=str(int(old_worker)),
                       FAKE_PID=str(live_pid), FAKE_FAIL_PIP=str(int(fail_pip)),
                       FAKE_DIRECT=(' '.join(['/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app',
                                              'worker', '-Q', 'celery' if direct_role == 'book' else 'housekeeping',
                                              '-c', '1'] + direct_tail) if direct_tail is not None else ''),
                       FAKE_DIRECT_ROLE=direct_role,
                       FAKE_POST_LIVE=json.dumps(post_live_argv) if post_live_argv is not None else '',
                       FAKE_RESTARTED=str(root / 'restarted'),
                       PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
            result = subprocess.run(['bash', str(SERVER), '--check' if check else str(archive), str(project)],
                                    capture_output=True, text=True, env=env, timeout=30)
            actions = [json.loads(line) for line in log.read_text().splitlines()]
            return result, actions

    def test_missing_fourth_unit_refuses_before_any_stop(self):
        result, actions = self.run_script(missing_housekeeping=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Service not installed: epub-factory-housekeeping', result.stderr)
        self.assertFalse(any('stop' in row or 'restart' in row for row in actions))

    def test_unrestricted_worker_refuses_before_any_stop(self):
        result, actions = self.run_script(old_worker=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('explicit book', result.stderr)
        self.assertFalse(any('stop' in row or 'restart' in row for row in actions))

    def test_check_requires_roles_but_does_not_mutate_services(self):
        result, actions = self.run_script(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any('stop' in row or 'restart' in row or 'start' in row for row in actions))

    def test_unsafe_direct_worker_options_refuse_before_any_stop(self):
        for role, tail in [('housekeeping', ['--autoscale=3,1']), ('housekeeping', ['--pool=solo']),
                           ('book', ['-Xcelery']), ('housekeeping', ['--pool=prefork', '-Pprefork'])]:
            with self.subTest(role=role, tail=tail):
                result, actions = self.run_script(direct_tail=tail, direct_role=role)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('explicit ' + role, result.stderr)
                self.assertFalse(any('stop' in row or 'restart' in row for row in actions))

    def test_explicit_direct_prefork_worker_passes_read_only_preflight(self):
        result, actions = self.run_script(check=True, direct_tail=['--pool=prefork'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any('stop' in row or 'restart' in row or 'start' in row for row in actions))

    def test_new_configuration_with_unverified_live_process_refuses_before_stop(self):
        result, actions = self.run_script(live_pid=os.getpid())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue('cannot verify running worker' in result.stderr or 'running process does not prove' in result.stderr)
        self.assertFalse(any('stop' in row or 'restart' in row for row in actions))

    def test_release_stops_and_restarts_all_four_services(self):
        result, actions = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(['systemctl', 'stop', 'epub-factory', 'epub-factory-beat', 'epub-factory-housekeeping'], actions)
        self.assertIn(['systemctl', 'stop', 'epub-factory-worker'], actions)
        self.assertIn(['systemctl', 'restart', 'epub-factory', 'epub-factory-worker', 'epub-factory-housekeeping', 'epub-factory-beat'], actions)
        self.assertIn(['systemctl', 'is-active', '--quiet', 'epub-factory-housekeeping'], actions)

    def test_release_post_restart_accepts_both_direct_shebang_roles(self):
        for role, queue in [('book', 'celery'), ('housekeeping', 'housekeeping')]:
            with self.subTest(role=role):
                live = ['/venv/bin/python', '/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app',
                        'worker', '-Q', queue, '-c', '1', '--pool=prefork']
                result, actions = self.run_script(direct_tail=['--pool=prefork'], direct_role=role, post_live_argv=live)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Deployment healthy', result.stdout)
                self.assertTrue(any(row[:2] == ['systemctl', 'restart'] for row in actions))

    def test_release_post_restart_rejects_shebang_runtime_role_mismatch(self):
        live = ['/venv/bin/python', '/venv/bin/celery', '-A', 'app.infra.celery_app:celery_app',
                'worker', '-Q', 'celery', '-c', '1']
        result, actions = self.run_script(direct_tail=[], direct_role='housekeeping', post_live_argv=live)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('running process does not prove the housekeeping role', result.stderr)
        self.assertNotIn('Deployment healthy', result.stdout)
        self.assertTrue(any(row[:2] == ['systemctl', 'restart'] for row in actions))

    def test_failed_release_attempts_to_restore_all_four_services(self):
        result, actions = self.run_script(fail_pip=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(['systemctl', 'start', 'epub-factory', 'epub-factory-worker', 'epub-factory-housekeeping', 'epub-factory-beat'], actions)


if __name__ == '__main__':
    unittest.main()
