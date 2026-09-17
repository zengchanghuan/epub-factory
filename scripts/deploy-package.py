#!/usr/bin/env python3
"""Build a source-only release; include uncommitted fixes, never runtime data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT_FILES = {'deploy.sh', 'Dockerfile', 'docker-compose.yml', 'README.md', 'index.html'}
SOURCE_SUFFIXES = {'.py', '.html', '.css', '.js', '.json', '.md', '.txt', '.yml', '.yaml', '.toml', '.lock', '.sh'}


def included(name):
    p = Path(name)
    if any(part.startswith('.') or 'secret' in part.lower() for part in p.parts):
        return False
    if name in ROOT_FILES:
        return True
    if p.suffix not in SOURCE_SUFFIXES:
        return False
    return (name.startswith(('frontend/', 'docs/', 'scripts/', 'backend/app/', 'backend/data/'))
            or (len(p.parts) == 2 and p.parts[0] == 'backend'
                and (p.name.startswith('test_') or p.name in {'requirements.txt', 'requirements.lock', 'run_regression.py'})))


def build(root, output):
    names = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root
    ).decode().split('\0')
    files = sorted({n for n in names if n and included(n) and (root / n).is_file()})
    required = {'deploy.sh', 'scripts/deploy-server.sh', 'backend/app/main.py', 'frontend/index.html', 'backend/requirements.txt'}
    if not required.issubset(files):
        raise RuntimeError(f'Missing deployment files: {required - set(files)}')
    manifest = {}
    temporary = output.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                path = root / name
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise RuntimeError(f'Refusing symlink or external file: {name}')
                data = path.read_bytes()
                manifest[name] = hashlib.sha256(data).hexdigest()
                archive.writestr(name, data)
            archive.writestr('deploy-manifest.json', json.dumps(manifest, indent=2))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip():
                raise RuntimeError('Archive integrity check failed')
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Packaged {len(files)} source files ({output.stat().st_size:,} bytes). Runtime data and credentials excluded.')


if __name__ == '__main__':
    build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
