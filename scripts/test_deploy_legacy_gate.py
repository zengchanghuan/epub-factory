"""Exercise deployment recovery without network access or real service commands."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LegacyIngressRecoveryTests(unittest.TestCase):
    def run_recovery(self, *, initial_exit=0, paused=True, readable=True, nginx_start_ok=True):
        source = (Path(__file__).parent / 'deploy-server.sh').read_text()
        recovery = 'NGINX_PAUSED=0\n' + source.split('NGINX_PAUSED=0\n', 1)[1].split(
            'trap restore_ingress EXIT', 1)[0] + 'trap restore_ingress EXIT\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'backend/.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/bash\ncat >/dev/null\nexit "$FAKE_CHECK_EXIT"\n')
            python.chmod(0o700)
            log = root / 'service-actions'
            script = '''
set -euo pipefail
SERVICES=(epub-factory epub-factory-worker epub-factory-beat)
BACKUP=offline-backup
LEGACY_REPAIR_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
sudo() {
  printf '%s\n' "$*" >> "$FAKE_ACTIONS"
  if [[ "$*" == '-n systemctl start nginx' ]]; then return "$FAKE_NGINX_EXIT"; fi
}
systemctl() { return 0; }
''' + recovery + '\nNGINX_PAUSED="$FAKE_PAUSED"\nexit "$FAKE_INITIAL_EXIT"\n'
            env = dict(os.environ, PROJECT_DIR=str(root), FAKE_ACTIONS=str(log),
                       FAKE_CHECK_EXIT='0' if readable else '1',
                       FAKE_NGINX_EXIT='0' if nginx_start_ok else '1',
                       FAKE_PAUSED='1' if paused else '0',
                       FAKE_INITIAL_EXIT=str(initial_exit))
            env.pop('BASH_ENV', None)
            result = subprocess.run(['bash'], input=script, text=True,
                                    capture_output=True, env=env, timeout=10)
            return result, log.read_text() if log.exists() else ''

    def test_success_reopens_only_when_legacy_order_is_readable(self):
        result, actions = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl start nginx', actions)
        self.assertNotIn('systemctl start epub-factory', actions)

    def test_missing_legacy_order_keeps_ingress_closed_even_after_success(self):
        result, actions = self.run_recovery(readable=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('systemctl start nginx', actions)
        self.assertIn('keep nginx stopped', result.stderr)

    def test_failed_release_restores_services_but_not_an_unreadable_order(self):
        result, actions = self.run_recovery(initial_exit=7, readable=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('systemctl start epub-factory', actions)
        self.assertNotIn('systemctl start nginx', actions)

    def test_failed_release_reopens_existing_api_without_claiming_success(self):
        result, actions = self.run_recovery(initial_exit=7)
        self.assertEqual(result.returncode, 7)
        self.assertIn('systemctl start epub-factory', actions)
        self.assertIn('systemctl start nginx', actions)

    def test_ordinary_release_does_not_touch_nginx(self):
        result, actions = self.run_recovery(paused=False, readable=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, '')

    def test_nginx_start_failure_is_not_reported_as_success(self):
        result, _ = self.run_recovery(nginx_start_ok=False)
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
