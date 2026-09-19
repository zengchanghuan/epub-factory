#!/usr/bin/env bash
# Also usable directly from Tencent Cloud OrcaTerm after uploading the package.
set -euo pipefail
ACTION=${1:?Usage: deploy-server.sh ARCHIVE_OR_--check [PROJECT_DIR]}
PROJECT_DIR=${2:-/home/ubuntu/epub-factory}
LEGACY_REPAIR_ID=${3:-}
[[ -z "$LEGACY_REPAIR_ID" || "$LEGACY_REPAIR_ID" =~ ^[0-9a-f]{32}$ ]] || { echo 'Invalid legacy repair job ID.' >&2; exit 2; }
SERVICES=(epub-factory epub-factory-worker epub-factory-beat)
for cmd in python3 systemctl curl java flock; do
  command -v "$cmd" >/dev/null || { echo "Missing server prerequisite: $cmd" >&2; exit 1; }
done
[[ -d "$PROJECT_DIR/backend/.venv" && -f "$PROJECT_DIR/backend/.env" ]] || {
  echo 'Existing production virtualenv and backend/.env are required; this is not a first-install script.' >&2; exit 1;
}
[[ -f "$PROJECT_DIR/tools/epubcheck-5.1.0/epubcheck.jar" ]] || { echo 'EPUBCheck is missing' >&2; exit 1; }
sudo -n true || { echo 'Run sudo -v in the server terminal first, or configure the deployment account sudo permission.' >&2; exit 1; }
# One server-side lock covers both Macs, including backups and service restarts.
# Never remove the lock file: removing it would let a new process lock another inode.
if [[ "$ACTION" != --check ]]; then
  lock_umask=$(umask)
  umask 077
  exec 9>"$PROJECT_DIR/.deploy.lock"
  umask "$lock_umask"
  flock -n 9 || { echo 'Another deployment is in progress; retry after it finishes.' >&2; exit 1; }
fi
if [[ $(systemctl show nginx -p LoadState --value 2>/dev/null) == loaded ]]; then
  sudo -n /usr/sbin/nginx -t
fi
for service in "${SERVICES[@]}"; do
  [[ $(systemctl show "$service" -p LoadState --value) == loaded ]] || { echo "Service not installed: $service" >&2; exit 1; }
done
# Refuse to interrupt queued/running books or persisted active repairs; read-only.
check_jobs() {
"$PROJECT_DIR/backend/.venv/bin/python" - "$PROJECT_DIR" <<'PY'
import json, os, stat, sys
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
repair_directory = os.environ.get('REPAIR_UPLOAD_DIR')
if repair_directory is None:
    repair_directory = config.get('REPAIR_UPLOAD_DIR')
if repair_directory is None:
    repair_directory = '/tmp/epub-repair'
if not repair_directory.strip():
    raise SystemExit('Cannot inspect repair directory: empty configuration; refusing deployment.')
try:
    entries = os.scandir(repair_directory)
except FileNotFoundError:
    entries = None
except OSError:
    raise SystemExit('Cannot inspect repair directory; refusing deployment.') from None
active_repairs = 0
if entries is not None:
    try:
        with entries:
            for entry in entries:
                if entry.is_symlink():
                    raise SystemExit('Cannot inspect repair directory safely; refusing deployment.')
                if not entry.is_dir(follow_symlinks=False):
                    continue
                metadata = Path(entry.path) / 'order.json'
                try:
                    mode = metadata.lstat().st_mode
                except FileNotFoundError:
                    # Legacy memory-only orders cannot be inferred from source files.
                    continue
                if not stat.S_ISREG(mode):
                    raise ValueError('Invalid metadata file')
                saved = json.loads(metadata.read_text(encoding='utf-8'))
                status = saved.get('status') if isinstance(saved, dict) else None
                if not isinstance(status, str) or status not in {
                    'paid', 'pending', 'running', 'pending_payment', 'repaired', 'failed'
                }:
                    raise ValueError('Invalid metadata status')
                if status in {'paid', 'pending', 'running'}:
                    active_repairs += 1
    except (OSError, ValueError):
        raise SystemExit('Cannot verify repair metadata; refusing deployment.') from None
if active_repairs:
    raise SystemExit(f'{active_repairs} active repair jobs: wait for completion before deploying.')
print('Preflight passed: no queued/running jobs or persisted active repairs.')
PY
}
check_jobs
[[ "$ACTION" != --check ]] || exit 0
ARCHIVE=$(realpath "$ACTION")
BACKUP="$PROJECT_DIR/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
# Validate the release before stopping services or changing production defaults.
python3 - "$ARCHIVE" "$PROJECT_DIR" <<'PY'
import hashlib, json, sys, zipfile
from pathlib import Path
archive, root = map(Path, sys.argv[1:])
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
            raise SystemExit('Release checksum mismatch')
print('Release contents verified before maintenance.')
PY
NGINX_PAUSED=0
LEGACY_WORK=''
legacy_order_available() {
"$PROJECT_DIR/backend/.venv/bin/python" - "$LEGACY_REPAIR_ID" <<'PY'
import json, sys
from urllib.request import urlopen
try:
    with urlopen('http://127.0.0.1:8000/api/v2/repair/' + sys.argv[1] + '/status', timeout=5) as response:
        value = json.load(response)
    assert value.get('job_id') == sys.argv[1]
    assert value.get('status') in {'pending_payment', 'paid', 'repaired', 'failed'}
except Exception:
    raise SystemExit(1) from None
PY
}
resume_nginx() {
  if [[ "$NGINX_PAUSED" == 1 ]]; then
    if ! legacy_order_available; then
      echo 'Legacy repair is not readable; keep nginx stopped until recovery. Source and migration metadata are retained.' >&2
      return 1
    fi
    sudo -n systemctl start nginx || return 1
    systemctl is-active --quiet nginx || return 1
    NGINX_PAUSED=0
  fi
}
restore_ingress() {
  result=$?
  if [[ $result != 0 ]]; then
    echo "Deployment failed. Backup (if created): $BACKUP. See docs/DEPLOY.md." >&2
    sudo -n systemctl start "${SERVICES[@]}" || true
  fi
  if ! resume_nginx; then
    result=1
  fi
  [[ -z "$LEGACY_WORK" ]] || rm -rf -- "$LEGACY_WORK"
  exit "$result"
}
trap restore_ingress EXIT
# An explicitly confirmed legacy normal-price repair can be preserved before
# stopping its memory-only API. The helper rejects other live legacy orders,
# changed legacy code/configuration, active work, or ambiguous source files.
if [[ -n "$LEGACY_REPAIR_ID" ]]; then
  LEGACY_WORK=$(mktemp -d /tmp/epub-legacy-release.XXXXXXXX)
  python3 - "$ARCHIVE" "$LEGACY_WORK/migrate.py" <<'PY'
import sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as archive:
    source = archive.read('scripts/migrate-legacy-repair.py')
Path(sys.argv[2]).write_bytes(source)
PY
  # Set the flag first so even a partially successful stop is recovered safely.
  NGINX_PAUSED=1
  sudo -n systemctl stop nginx
  "$PROJECT_DIR/backend/.venv/bin/python" "$LEGACY_WORK/migrate.py" \
    --project-dir "$PROJECT_DIR" --job-id "$LEGACY_REPAIR_ID" \
    --confirmed-nontest --confirmed-price-unchanged-since-start --apply
fi
# Close the submission window, then recheck jobs before replacing code.
sudo -n systemctl stop epub-factory epub-factory-beat
check_jobs
sudo -n systemctl stop epub-factory-worker
# Keep a consistent server-local runtime backup before schema migrations or config edits.
"$PROJECT_DIR/backend/.venv/bin/python" - "$PROJECT_DIR" "$BACKUP" <<'PY'
import os, shutil, sqlite3, sys
from pathlib import Path
from dotenv import dotenv_values, set_key
from sqlalchemy.engine import make_url

root, backup = map(Path, sys.argv[1:])
backup.mkdir(parents=True, mode=0o700)
backup.chmod(0o700)
env = root / 'backend/.env'
config = dotenv_values(env)
shutil.copy2(env, backup / 'production.env')
(backup / 'production.env').chmod(0o600)
url = make_url(os.environ.get('DATABASE_URL') or config.get('DATABASE_URL') or 'sqlite:///./epub_jobs.db')
if url.get_backend_name() != 'sqlite':
    raise SystemExit('A verified external database backup is required before this deployment.')
database = Path(url.database).expanduser()
if not database.is_absolute():
    database = root / 'backend' / database
paths = [('jobs.sqlite3', database), ('translation-cache.sqlite3', root / 'backend/translation_cache.db')]
for name, path in paths:
    if not path.is_file():
        if name == 'jobs.sqlite3':
            raise SystemExit('Production database missing; refusing deployment.')
        continue
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / name) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise SystemExit('Runtime backup validation failed.')
    (backup / name).chmod(0o600)
# Preserve credentials, custom pricing and AI pricing. After backup, migrate
# approved conversion defaults to 1.99 and standalone repair defaults to 0.99.
for key, legacy_values, price in (
    ('CONVERSION_PRICE_CNY', ('', '5.99'), '1.99'),
    ('REPAIR_PRICE_CNY', ('', '5.99', '1.99'), '0.99'),
):
    configured = str(config.get(key) or '').strip()
    if configured in legacy_values:
        set_key(str(env), key, price)
# Update the approved model/queue defaults.
for key, value in {
    'OPENAI_MODEL': 'deepseek-flash',
    'EPUB_DEFAULT_TRANSLATION_MODEL': 'deepseek-flash',
    'EPUB_TRANSLATION_PROACTIVE_QUALITY_MODEL_ENABLED': '0',
    'CELERY_VISIBILITY_TIMEOUT': str(max(
        10800,
        int(config.get('EPUB_BOOK_TIME_LIMIT') or int(config.get('EPUB_BOOK_SOFT_TIME_LIMIT') or 7200) + 300) + 1800,
        int(config.get('CELERY_TASK_TIME_LIMIT') or 1800) + 1800,
    )),
}.items():
    set_key(str(env), key, value)
print('Server-local database/cache/config backups verified; production defaults updated.')
PY
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
    backup.mkdir(parents=True, exist_ok=True)
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
    resume_nginx
    echo "Deployment healthy. Backup: $BACKUP"
    exit 0
  fi
  sleep 2
done
echo 'Health check failed; inspect journalctl -u epub-factory.' >&2
exit 1
