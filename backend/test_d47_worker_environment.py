"""R9 startup config from a real, isolated .env before any task imports.

Only infrastructure source files are copied. No developer credentials, books,
database or Broker are read; worker_main is replaced before the real launcher.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BACKEND = Path(__file__).resolve().parent
ENVIRONMENT = """REDIS_URL=memory://
CELERY_RESULT_BACKEND=cache+memory://
CELERY_WORKER_CONCURRENCY=2
CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT=60
CELERY_HOUSEKEEPING_TIME_LIMIT=90
RECONCILE_CRON_HOUR=5
RECONCILE_CRON_MINUTE=17
BALANCE_CHECK_HOUR=9
"""
PROBE = """
import json, sys
from unittest.mock import patch
from celery import Celery

def snapshot(app, argv=None):
    c = app.conf
    schedule = c.beat_schedule['reconcile-payments-daily']['schedule']
    print(json.dumps(dict(broker=c.broker_url, backend=c.result_backend,
        concurrency=c.worker_concurrency,
        soft=c.epub_housekeeping_soft_time_limit, hard=c.epub_housekeeping_time_limit,
        hours=sorted(schedule.hour), minutes=sorted(schedule.minute), argv=argv,
        task_modules_loaded=any(name.startswith('app.tasks.') for name in sys.modules))))
    return 0

with patch('socket.socket.connect', side_effect=AssertionError('No network')), \\
     patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')):
    if sys.argv[1] == 'import':
        from app.infra.celery_app import celery_app
        snapshot(celery_app)
    else:
        with patch.object(Celery, 'worker_main', snapshot):
            from app.infra.worker import main
            main(['--help'] if sys.argv[1] == 'help' else [sys.argv[1]])
"""


class WorkerEnvironmentTests(unittest.TestCase):
    def probe(self, mode='import', *, dotenv=ENVIRONMENT, overrides=None):
        with tempfile.TemporaryDirectory(prefix='r9-env-') as temporary:
            root = Path(temporary)
            infra = root / 'app' / 'infra'
            infra.mkdir(parents=True)
            (root / 'app' / '__init__.py').touch()
            (infra / '__init__.py').touch()
            for name in ('celery_app.py', 'worker.py', 'worker_db_lifecycle.py', 'worker_control.py'):
                source = BACKEND / 'app' / 'infra' / name
                if source.exists():
                    shutil.copyfile(source, infra / name)
            if dotenv is not None:
                (root / '.env').write_text(dotenv)
            unrelated = root / 'unrelated-cwd'
            unrelated.mkdir()
            (unrelated / '.env').write_text('CELERY_HOUSEKEEPING_TIME_LIMIT=1\n')
            env = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'LANG', 'TMPDIR', 'SYSTEMROOT', 'OFFLINE_NETWORK_LOG'}}
            env.update(HOME=temporary, PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=str(root) + os.pathsep + os.environ.get('PYTHONPATH', ''))
            env.update(overrides or {})
            return subprocess.run([sys.executable, '-c', PROBE, mode], cwd=unrelated,
                                  env=env, capture_output=True, text=True, timeout=15)

    def value(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_direct_app_and_beat_config_load_fixed_backend_file_before_tasks(self):
        result = self.value(self.probe())
        self.assertEqual(result['broker'], 'memory://')
        self.assertEqual(result['backend'], 'cache+memory://')
        self.assertEqual((result['soft'], result['hard']), (60, 90))
        self.assertEqual((result['hours'], result['minutes']), ([5], [17]))
        self.assertFalse(result['task_modules_loaded'])

    def test_both_real_launchers_use_file_config_before_worker_main(self):
        for role in ('book', 'housekeeping'):
            with self.subTest(role=role):
                result = self.value(self.probe(role))
                self.assertFalse(result['task_modules_loaded'])
                self.assertEqual(result['broker'], 'memory://')
                self.assertIn('--concurrency=' + ('2' if role == 'book' else '1'), result['argv'])
                if role == 'housekeeping':
                    self.assertIn('--soft-time-limit=60', result['argv'])
                    self.assertIn('--time-limit=90', result['argv'])

    def test_exported_systemd_or_container_environment_wins_over_dotenv(self):
        result = self.value(self.probe('housekeeping', overrides={
            'CELERY_BROKER_URL': 'memory://explicit',
            'CELERY_RESULT_BACKEND': 'cache+memory://explicit',
            'CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT': '70',
            'CELERY_HOUSEKEEPING_TIME_LIMIT': '100', 'RECONCILE_CRON_HOUR': '6',
        }))
        self.assertEqual(result['broker'], 'memory://explicit')
        self.assertEqual(result['backend'], 'cache+memory://explicit')
        self.assertEqual((result['soft'], result['hard'], result['hours']), (70, 100, [6]))

    def test_missing_backend_dotenv_uses_defaults_not_unrelated_cwd_file(self):
        result = self.value(self.probe(dotenv=None))
        self.assertEqual(result['broker'], 'redis://127.0.0.1:6379/0')
        self.assertEqual((result['soft'], result['hard']), (1500, 1800))

    def test_invalid_file_limits_fail_before_worker_main(self):
        result = self.probe('housekeeping', dotenv=ENVIRONMENT.replace('TIME_LIMIT=90', 'TIME_LIMIT=30'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('must exceed', result.stderr)

    def test_help_does_not_load_invalid_config_or_start_worker(self):
        result = self.probe('help', dotenv='CELERY_HOUSEKEEPING_TIME_LIMIT=invalid\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('housekeeping', result.stdout)
        self.assertNotIn('"broker"', result.stdout)


if __name__ == '__main__':
    unittest.main()
