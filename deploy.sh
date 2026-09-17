#!/usr/bin/env bash
# Run from any directory using SSH public-key authentication only.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HOST=${DEPLOY_HOST:-ubuntu@81.71.22.79}
PORT=${DEPLOY_PORT:-22}
REMOTE_DIR=${DEPLOY_REMOTE_DIR:-/home/ubuntu/epub-factory}
PYTHON=${PYTHON:-python3}
PACKAGE="$ROOT/epub-factory-deploy.zip"
MODE=${1:-deploy}
case "$MODE" in
  --help|-h)
    cat <<'HELP'
Usage: bash deploy.sh [--package-only | --check | --help]
  default         Package, upload, back up server code, deploy and verify services.
  --package-only  Build epub-factory-deploy.zip without connecting to the server.
  --check         Check SSH, server prerequisites and active jobs; do not deploy.
Environment:
  DEPLOY_HOST        ubuntu@81.71.22.79 (or an SSH config alias)
  DEPLOY_PORT        22
  DEPLOY_REMOTE_DIR  /home/ubuntu/epub-factory
  DEPLOY_KEY         SSH key path (default: ~/.ssh/id_ed25519_fixepub).
  DEPLOY_PUBLIC_URL  https://fixepub.com (public health check after deployment)
  PYTHON             Local Python 3 executable
See docs/DEPLOY.md for browser-terminal deployment and rollback instructions.
HELP
    exit 0 ;;
  deploy|--package-only|--check) ;;
  *) echo "Unknown option: $MODE" >&2; exit 2 ;;
esac
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo 'Invalid DEPLOY_PORT' >&2; exit 2; }
[[ "$HOST" != -* && "$HOST" != *[[:space:]]* ]] || { echo 'Invalid DEPLOY_HOST' >&2; exit 2; }
[[ "$REMOTE_DIR" =~ ^/[a-zA-Z0-9_./-]+$ && "$REMOTE_DIR" != / ]] || { echo 'Invalid DEPLOY_REMOTE_DIR' >&2; exit 2; }
if [[ "$MODE" != --check ]]; then
  "$PYTHON" "$ROOT/scripts/deploy-package.py" "$ROOT" "$PACKAGE"
  if [[ "$MODE" == --package-only ]]; then
    echo "Package ready: $PACKAGE"
    echo 'See docs/DEPLOY.md for deployment through the Tencent Cloud terminal.'
    exit 0
  fi
fi
command -v ssh >/dev/null
command -v scp >/dev/null
WORK=$(mktemp -d /tmp/epub-deploy.XXXXXX)
SSH_OPTS=(-p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o ControlMaster=auto -o ControlPersist=120 -o "ControlPath=$WORK/ssh")
KEY=${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_fixepub}
if [[ -n "$KEY" ]]; then
  [[ -r "$KEY" ]] || { rm -rf "$WORK"; echo "Missing deployment key. Run: bash $ROOT/scripts/setup-deploy-ssh.sh" >&2; exit 2; }
  SSH_OPTS+=(-i "$KEY" -o IdentitiesOnly=yes)
fi
cleanup() {
  ssh "${SSH_OPTS[@]}" -O exit "$HOST" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
# Fail promptly if the key or verified server host key is unavailable.
ssh "${SSH_OPTS[@]}" "$HOST" "bash -s -- --check '$REMOTE_DIR'" < "$ROOT/scripts/deploy-server.sh"
[[ "$MODE" != --check ]] || exit 0
STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$$
REMOTE_PACKAGE="/tmp/epub-factory-$STAMP.zip"
SCP_OPTS=(-P "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o "ControlPath=$WORK/ssh")
[[ -z "$KEY" ]] || SCP_OPTS+=(-i "$KEY")
scp "${SCP_OPTS[@]}" "$PACKAGE" "$HOST:$REMOTE_PACKAGE"
ssh "${SSH_OPTS[@]}" "$HOST" "bash -s -- '$REMOTE_PACKAGE' '$REMOTE_DIR'" < "$ROOT/scripts/deploy-server.sh"
PUBLIC_URL=${DEPLOY_PUBLIC_URL:-https://fixepub.com}
curl --fail --silent --show-error --max-time 20 "${PUBLIC_URL%/}/api/healthz" | "$PYTHON" -c 'import json,sys; result=json.load(sys.stdin); assert result.get("status") == "ok", result; print("Public API health: ok")'
echo
echo "Deployment verified: $PUBLIC_URL"
