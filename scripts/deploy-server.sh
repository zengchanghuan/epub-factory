#!/usr/bin/env bash
# Also usable directly from Tencent Cloud OrcaTerm after uploading the package.
set -euo pipefail
ACTION=${1:?Usage: deploy-server.sh ARCHIVE_OR_--check [PROJECT_DIR]}
PROJECT_DIR=${2:-/home/ubuntu/epub-factory}
SERVICES=(epub-factory epub-factory-worker epub-factory-beat)
for cmd in python3 systemctl curl java; do
  command -v "$cmd" >/dev/null || { echo "Missing server prerequisite: $cmd" >&2; exit 1; }
done
[[ -d "$PROJECT_DIR/backend/.venv" && -f "$PROJECT_DIR/backend/.env" ]] || {
  echo 'Existing production virtualenv and backend/.env are required; this is not a first-install script.' >&2; exit 1;
}
[[ -f "$PROJECT_DIR/tools/epubcheck-5.1.0/epubcheck.jar" ]] || { echo 'EPUBCheck is missing' >&2; exit 1; }
sudo -n true || { echo 'Run sudo -v in the server terminal first, or configure the deployment account sudo permission.' >&2; exit 1; }
for service in "${SERVICES[@]}"; do
  [[ $(systemctl show "$service" -p LoadState --value) == loaded ]] || { echo "Service not installed: $service" >&2; exit 1; }
done
# Refuse to interrupt queued/running books. Database is opened read-only.
check_jobs() {
"$PROJECT_DIR/backend/.venv/bin/python" - "$PROJECT_DIR" <<'PY'
import os, sys
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
root = Path(sys.argv[1])
config = dotenv_values(root / 'backend/.env')
url = os.environ.get('DATABASE_URL') or config.get('DATABASE_URL') or 'sqlite:///./epub_jobs.db'
os.chdir(root / 'backend')
if url.startswith('sqlite:///'):
    import sqlite3
    db = Path(url[len('sqlite:///'):]).resolve()
    if not db.is_file():
        raise SystemExit(f'Database not found: {db}')
    conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    count = conn.execute("SELECT COUNT(*) FROM epub_jobs WHERE status IN ('pending','running')").fetchone()[0]
    conn.close()
else:
    with create_engine(url).connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM epub_jobs WHERE status IN ('pending','running')")).scalar_one()
if count:
    raise SystemExit(f'{count} queued/running jobs: wait for completion before deploying.')
print('Preflight passed: no queued/running jobs.')
PY
}
check_jobs
[[ "$ACTION" != --check ]] || exit 0
ARCHIVE=$(realpath "$ACTION")
BACKUP="$PROJECT_DIR/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
restore_ingress() {
  result=$?
  if [[ $result != 0 ]]; then
    echo "Deployment failed. Backup (if created): $BACKUP. See docs/DEPLOY.md." >&2
    sudo -n systemctl start epub-factory epub-factory-beat || true
  fi
}
trap restore_ingress EXIT
# Close the submission window, then recheck jobs before replacing code.
sudo -n systemctl stop epub-factory epub-factory-beat
check_jobs
# Validate all paths and hashes before changing any production file.
python3 - "$ARCHIVE" "$PROJECT_DIR" "$BACKUP" <<'PY'
import hashlib, json, sys, zipfile
from pathlib import Path
archive, root, backup = map(Path, sys.argv[1:])
root = root.resolve()
with zipfile.ZipFile(archive) as z:
    manifest = json.loads(z.read('deploy-manifest.json'))
    if set(z.namelist()) != set(manifest) | {'deploy-manifest.json'}:
        raise SystemExit('Unexpected archive entries')
    for name, digest in manifest.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not (root / path).resolve().is_relative_to(root):
            raise SystemExit('Unsafe archive path')
        if hashlib.sha256(z.read(name)).hexdigest() != digest:
            raise SystemExit(f'Checksum mismatch: {name}')
    backup.mkdir(parents=True)
    new_files = []
    with zipfile.ZipFile(backup / 'previous-code.zip', 'w', zipfile.ZIP_DEFLATED) as previous:
        for name in manifest:
            path = root / name
            if path.is_file():
                previous.write(path, name)
            else:
                new_files.append(name)
    (backup / 'new-files.json').write_text(json.dumps(new_files))
    (backup / 'deploy-manifest.json').write_text(json.dumps(manifest, indent=2))
    for name in manifest:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(z.read(name))
print(f'Code backup: {backup}')
PY
cd "$PROJECT_DIR/backend"
.venv/bin/python -m pip freeze > "$BACKUP/pip-freeze.txt"
.venv/bin/python -m pip install -r requirements.txt
# Graceful systemd restart; never kill unrelated uvicorn processes.
sudo -n systemctl restart "${SERVICES[@]}"
for service in "${SERVICES[@]}"; do
  systemctl is-active --quiet "$service" || { echo "$service is not active" >&2; exit 1; }
done
for attempt in {1..30}; do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8000/healthz | python3 -c 'import json,sys; assert json.load(sys.stdin).get("status") == "ok"' 2>/dev/null; then
    for service in "${SERVICES[@]}"; do
      systemctl is-active --quiet "$service" || { echo "$service stopped during startup" >&2; exit 1; }
    done
    echo "Deployment healthy. Backup: $BACKUP"
    exit 0
  fi
  sleep 2
done
echo 'Health check failed; inspect journalctl -u epub-factory.' >&2
exit 1
