"""Offline Mac deployment-entry checks with isolated homes and fake SSH."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PortableDeploymentTests(unittest.TestCase):
    def test_each_mac_uses_its_own_home_key_from_any_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binaries = base / 'bin'
            binaries.mkdir()
            ssh = binaries / 'ssh'
            ssh.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            ssh.chmod(0o700)
            for name in ['Mac A user', 'Mac B user']:
                home = base / name
                key = home / '.ssh/id_ed25519_fixepub'
                key.parent.mkdir(parents=True)
                key.write_text('offline-key-fixture')
                env = dict(os.environ, HOME=str(home), PATH=str(binaries) + os.pathsep + os.environ['PATH'])
                for field in ['DEPLOY_KEY', 'DEPLOY_HOST', 'DEPLOY_PORT', 'DEPLOY_REMOTE_DIR']:
                    env.pop(field, None)
                result = subprocess.run(['bash', str(ROOT / 'deploy.sh'), '--check'],
                                        cwd=base, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(result.stdout.splitlines()[0])
                self.assertEqual(args[args.index('-i') + 1], str(key))
                self.assertIn('StrictHostKeyChecking=yes', args)
                self.assertIn('BatchMode=yes', args)
                self.assertIn('ubuntu@81.71.22.79', args)

    def test_server_has_one_shared_lock_before_any_service_stop(self):
        source = (ROOT / 'scripts/deploy-server.sh').read_text()
        self.assertIn('exec 9>"$PROJECT_DIR/.deploy.lock"', source)
        self.assertIn('flock -n 9', source)
        self.assertIn('umask "$lock_umask"', source)
        self.assertLess(source.index('flock -n 9'), source.index('systemctl stop'))
        self.assertIn('sudo -n /usr/sbin/nginx -t', source)


if __name__ == '__main__':
    unittest.main()
