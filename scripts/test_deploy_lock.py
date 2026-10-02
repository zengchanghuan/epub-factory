"""Deployment lock handoff, with real OS locks and no service/network actions.

Linux uses native /proc and flock. On macOS only /proc's FD stat lookup is
mapped to fstat and the absent flock executable uses the same fcntl.flock API.
The unchanged production shell block is exercised in a separate process.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


_PROBE = r'''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(3)
finally:
    os.close(fd)
'''

_PARENT = r'''
import fcntl, json, os, subprocess, sys
from pathlib import Path
root, mode = Path(sys.argv[1]), sys.argv[2]
script = sys.stdin.read()
fd = None
if mode != 'missing':
    path = root / ('other.lock' if mode == 'wrong' else '.deploy.lock')
    fd = os.open(path, os.O_RDWR)
    if mode == 'locked':
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if fd != 9:
        os.dup2(fd, 9)
        os.close(fd)
    fd = 9
else:
    try:
        os.close(9)
    except OSError:
        pass
result = subprocess.run(['bash'], input=script, text=True, capture_output=True,
                        pass_fds=(9,) if fd is not None else (), timeout=10)
retained = None
if mode == 'locked':
    check = subprocess.run([sys.executable, '-c', os.environ['TEST_LOCK_PROBE'],
                            str(root / '.deploy.lock')], capture_output=True, timeout=10)
    retained = check.returncode == 3
if fd is not None:
    os.close(fd)
print(json.dumps(dict(returncode=result.returncode, stdout=result.stdout,
                     stderr=result.stderr, parent_lock_retained=retained)))
'''

_HOLDER = r'''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('ready', flush=True)
sys.stdin.readline()
os.close(fd)
'''


class DeployLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='epub-deploy-lock-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / '.deploy.lock'
        self.lock.write_bytes(b'keep-the-same-inode')
        (self.root / 'other.lock').write_bytes(b'unrelated')
        self.before_inode = self.lock.stat().st_ino
        source = (Path(__file__).parent / 'deploy-server.sh').read_text()
        self.block = source.split('# DEPLOY_LOCK_BEGIN\n', 1)[1].split('# DEPLOY_LOCK_END', 1)[0]
        self.env = dict(os.environ, PROJECT_DIR=str(self.root), ACTION='release.zip',
                        TEST_LOCK_PROBE=_PROBE)
        self.env.pop('BASH_ENV', None)
        self.env.pop('EPUB_DEPLOY_LOCK_FD', None)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        if not Path('/proc/self/fd').is_dir():
            # Portability alias only: inherited descriptors and all locks are real.
            self.executable(bin_dir / 'python3', '''
import os, sys
original_stat = os.stat
def fd_stat(path, *args, **kwargs):
    if path == '/proc/self/fd/9':
        return os.fstat(9)
    return original_stat(path, *args, **kwargs)
os.stat = fd_stat
sys.argv = sys.argv[1:]
exec(compile(sys.stdin.read(), '<deploy-lock-verifier>', 'exec'))
''')
        if not shutil.which('flock'):
            self.executable(bin_dir / 'flock', '''
import fcntl, sys
assert sys.argv[1:] == ['-n', '9']
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (OSError, ValueError):
    sys.exit(1)
''')
        self.env['PATH'] = str(bin_dir) + os.pathsep + os.environ['PATH']

    @staticmethod
    def executable(path, body):
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o700)

    def run_block(self, *, inherited=None, mode=None, action='release.zip'):
        env = dict(self.env, ACTION=action)
        if inherited is not None:
            env['EPUB_DEPLOY_LOCK_FD'] = inherited
        script = 'set -euo pipefail\n' + self.block + '\necho LOCK_ADMITTED\n'
        if mode is not None:
            result = subprocess.run([sys.executable, '-c', _PARENT, str(self.root), mode],
                                    input=script, text=True, capture_output=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        result = subprocess.run(['bash'], input=script, text=True, capture_output=True,
                                env=env, timeout=10)
        return dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def hold_lock(self):
        holder = subprocess.Popen([sys.executable, '-c', _HOLDER, str(self.lock)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_holder, holder)
        self.assertEqual(holder.stdout.readline().strip(), 'ready')
        return holder

    @staticmethod
    def stop_holder(holder):
        if holder.poll() is None:
            holder.communicate('\n', timeout=5)
        else:
            holder.communicate(timeout=5)

    def assert_rejected(self, result):
        self.assertNotEqual(result['returncode'], 0)
        self.assertNotIn('LOCK_ADMITTED', result['stdout'])

    def test_default_path_creates_private_lock_and_acquires_it(self):
        self.lock.unlink()  # Test fixture setup only, never a running deployment lock.
        result = self.run_block()
        self.assertEqual(result['returncode'], 0, result['stderr'])
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
        check = subprocess.run([sys.executable, '-c', _PROBE, str(self.lock)], timeout=5)
        self.assertEqual(check.returncode, 0)  # Released when deployment exits.

    def test_default_path_rejects_another_process_lock(self):
        self.hold_lock()
        result = self.run_block()
        self.assert_rejected(result)
        self.assertIn('Another deployment', result['stderr'])
        self.assertEqual(self.lock.stat().st_ino, self.before_inode)

    def test_configured_fd_missing_is_rejected_without_reopening(self):
        result = self.run_block(inherited='9', mode='missing')
        self.assert_rejected(result)
        self.assertEqual(self.lock.read_bytes(), b'keep-the-same-inode')

    def test_wrong_inode_is_rejected_without_touching_either_file(self):
        result = self.run_block(inherited='9', mode='wrong')
        self.assert_rejected(result)
        self.assertEqual(self.lock.read_bytes(), b'keep-the-same-inode')
        self.assertEqual((self.root / 'other.lock').read_bytes(), b'unrelated')

    def test_correct_inode_but_another_process_owns_lock_is_rejected(self):
        self.hold_lock()
        result = self.run_block(inherited='9', mode='unlocked')
        self.assert_rejected(result)
        self.assertIn('Another deployment', result['stderr'])
        self.assertEqual(self.lock.read_bytes(), b'keep-the-same-inode')

    def test_valid_parent_handoff_preserves_parent_lock_and_inode(self):
        result = self.run_block(inherited='9', mode='locked')
        self.assertEqual(result['returncode'], 0, result['stderr'])
        self.assertIn('LOCK_ADMITTED', result['stdout'])
        self.assertTrue(result['parent_lock_retained'])
        self.assertEqual(self.lock.stat().st_ino, self.before_inode)
        self.assertEqual(self.lock.read_bytes(), b'keep-the-same-inode')

    def test_correct_unlocked_descriptor_must_acquire_lock(self):
        result = self.run_block(inherited='9', mode='unlocked')
        self.assertEqual(result['returncode'], 0, result['stderr'])

    def test_only_exact_fd_9_is_supported(self):
        for value in ('', '0', '8', '09', '10', '-1', '/proc/self/fd/9', '9;echo unsafe'):
            with self.subTest(value=value):
                result = self.run_block(inherited=value, mode='locked')
                self.assert_rejected(result)
                self.assertIn('must use FD 9', result['stderr'])
                self.assertTrue(result['parent_lock_retained'])

    def test_missing_project_lock_does_not_create_it_for_inherited_mode(self):
        self.lock.unlink()
        result = self.run_block(inherited='9', mode='wrong')
        self.assert_rejected(result)
        self.assertFalse(self.lock.exists())

    def test_check_remains_non_mutating_without_lock_authority(self):
        self.lock.unlink()
        result = self.run_block(inherited='9', mode='missing', action='--check')
        self.assertEqual(result['returncode'], 0, result['stderr'])
        self.assertFalse(self.lock.exists())


if __name__ == '__main__':
    unittest.main()
