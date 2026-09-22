#!/usr/bin/env bash
# Correct only the verification public key; never restart, order, pay, or retry.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HOST=${DEPLOY_HOST:-ubuntu@81.71.22.79}
PORT=${DEPLOY_PORT:-22}
KEY=${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_fixepub}
REMOTE_DIR=${DEPLOY_REMOTE_DIR:-/home/ubuntu/epub-factory}
MODE=${1:-configure}
case "$MODE" in
  --help|-h)
    echo 'Usage: bash scripts/configure-alipay-server.sh [--check]'
    echo 'Default: hidden-input Alipay public key, validate and save with a private backup.'
    echo '--check: read-only key-format check; does not contact Alipay.'
    echo 'No private keys on the command line. No orders, charges, restart, or deployment.'
    exit 0 ;;
  configure|--check) ;;
  *) echo 'Unknown option; use --help.' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Never pass key contents as command-line arguments.' >&2; exit 2; }
[[ -n "$HOST" && "$HOST" != -* && "$HOST" != *[[:space:]]* ]] || exit 2
[[ "$PORT" =~ ^[0-9]+$ ]] || exit 2
[[ "$REMOTE_DIR" =~ ^/[a-zA-Z0-9_./-]+$ && "$REMOTE_DIR" != / ]] || exit 2
[[ -r "$KEY" ]] || { echo 'Deployment SSH key is unavailable.' >&2; exit 2; }
if [[ "$MODE" == configure && ! -t 0 ]]; then
  echo 'Use an interactive terminal for hidden public-key input; no configuration changed.' >&2
  exit 2
fi
SSH_OPTS=(-p "$PORT" -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15)
REMOTE_SCRIPT=$(ssh "${SSH_OPTS[@]}" "$HOST" 'umask 077; mktemp /tmp/fixepub-alipay-XXXXXXXX.py')
[[ "$REMOTE_SCRIPT" =~ ^/tmp/fixepub-alipay-[a-zA-Z0-9]+\.py$ ]] || exit 2
cleanup() { ssh "${SSH_OPTS[@]}" "$HOST" "rm -f -- '$REMOTE_SCRIPT'" >/dev/null 2>&1 || true; }
trap cleanup EXIT
ssh "${SSH_OPTS[@]}" "$HOST" "cat > '$REMOTE_SCRIPT'" < "$ROOT/scripts/configure-alipay-public-key.py"
if [[ "$MODE" == --check ]]; then
  ssh "${SSH_OPTS[@]}" "$HOST" "'$REMOTE_DIR/backend/.venv/bin/python' '$REMOTE_SCRIPT' --env '$REMOTE_DIR/backend/.env' --check"
else
  ssh -tt "${SSH_OPTS[@]}" "$HOST" "'$REMOTE_DIR/backend/.venv/bin/python' '$REMOTE_SCRIPT' --env '$REMOTE_DIR/backend/.env'"
fi
