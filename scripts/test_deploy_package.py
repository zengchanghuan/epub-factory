"""Offline checks for release contents; never connects to a server."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('deploy_package', Path(__file__).with_name('deploy-package.py'))
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class ReleasePackageTests(unittest.TestCase):
    def test_runtime_and_credentials_are_excluded(self):
        for name in ['backend/.env', 'backend/epub_jobs.db', 'backend/outputs/book.epub',
                     'backend/uploads/book.html', 'backend/failed_chunks/private.json',
                     'backend/reduce_work/chapter.xhtml', 'fix_epub.pem', 'deploy.local.env',
                     'scripts/secret-config.json', 'frontend/.env.production', '.git/config',
                     'translation_cache.db', 'backend/app/__pycache__/main.pyc',
                     'backend/scripts/matches.json', 'backend/scripts/original-provider-bill.json']:
            with self.subTest(name=name):
                self.assertFalse(package.included(name))

    def test_package_includes_uncommitted_sources_and_verified_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            sources = ['deploy.sh', 'scripts/deploy-server.sh', 'backend/app/main.py',
                       'frontend/index.html', 'backend/requirements.txt', 'backend/app/new_fix.py',
                       'backend/scripts/import_llm_bill.py']
            for name in sources + ['backend/.env', 'backend/outputs/private.epub']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test content\n')
            output = root / 'release.zip'
            package.build(root, output)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read('deploy-manifest.json'))
                self.assertEqual(set(manifest), set(sources))
                for name, digest in manifest.items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)

    def test_reject_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            for name in ['deploy.sh', 'scripts/deploy-server.sh', 'backend/app/main.py',
                         'frontend/index.html', 'backend/requirements.txt']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test content')
            (root / 'backend/app/copied.py').symlink_to(root / 'backend/app/main.py')
            with self.assertRaises(RuntimeError):
                package.build(root, root / 'release.zip')


if __name__ == '__main__':
    unittest.main()
