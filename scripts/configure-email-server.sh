#!/usr/bin/env bash
# Configure the production QQ sender directly; credentials never leave its TTY.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HOST=${DEPLOY_HOST:-ubuntu@81.71.22.79}
PORT=${DEPLOY_PORT:-22}
KEY=${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_fixepub}
REMOTE_DIR=${DEPLOY_REMOTE_DIR:-/home/ubuntu/epub-factory}
MODE=${1:-configure}
case "$MODE" in
  --help|-h)
    cat <<'HELP'
Usage: bash scripts/configure-email-server.sh [--check | --test]
  default  Hidden-input QQ SMTP configuration directly on the server, then TLS/auth check.
  --check  Check existing server SMTP configuration and authentication; do not send mail.
  --test   Send one clearly labelled test email to 249998620@qq.com; do not change orders.
Does not deploy, restart services, run translation, or copy local .env files.
Uses the same DEPLOY_HOST/PORT/KEY/REMOTE_DIR settings as deploy.sh.
HELP
    exit 0 ;;
  configure|--check|--test) ;;
  *) echo 'Unknown option. Use --help.' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Unexpected arguments; never pass an authorization code on the command line.' >&2; exit 2; }
[[ -n "$HOST" && "$HOST" != -* && "$HOST" != *[[:space:]]* ]] || exit 2
[[ "$PORT" =~ ^[0-9]+$ ]] || exit 2
[[ "$REMOTE_DIR" =~ ^/[a-zA-Z0-9_./-]+$ && "$REMOTE_DIR" != / ]] || exit 2
[[ -r "$KEY" ]] || { echo 'Deployment SSH key is unavailable.' >&2; exit 2; }
if [[ "$MODE" == configure && ! -t 0 ]]; then
  echo 'Open an interactive terminal for hidden authorization-code input; no configuration changed.' >&2
  exit 2
fi
SSH_OPTS=(-p "$PORT" -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15)
REMOTE_SCRIPT=$(ssh "${SSH_OPTS[@]}" "$HOST" 'umask 077; mktemp /tmp/fixepub-email-XXXXXXXX.py')
[[ "$REMOTE_SCRIPT" =~ ^/tmp/fixepub-email-[a-zA-Z0-9]+\.py$ ]] || { echo 'Unexpected remote temporary path.' >&2; exit 2; }
cleanup() { ssh "${SSH_OPTS[@]}" "$HOST" "rm -f -- '$REMOTE_SCRIPT'" >/dev/null 2>&1 || true; }
trap cleanup EXIT
ssh "${SSH_OPTS[@]}" "$HOST" "cat > '$REMOTE_SCRIPT'" < "$ROOT/scripts/configure-email.py"
if [[ "$MODE" == configure ]]; then
  # The authorization code is read by getpass on the remote encrypted SSH TTY.
  ssh -tt "${SSH_OPTS[@]}" "$HOST" "python3 '$REMOTE_SCRIPT' --env '$REMOTE_DIR/backend/.env' --qq --enable-owner-notifications --owner-to 249998620@qq.com"
fi
if [[ "$MODE" == --test ]]; then
  ssh "${SSH_OPTS[@]}" "$HOST" "python3 '$REMOTE_SCRIPT' --env '$REMOTE_DIR/backend/.env' --test-to 249998620@qq.com"
else
  ssh "${SSH_OPTS[@]}" "$HOST" "python3 '$REMOTE_SCRIPT' --env '$REMOTE_DIR/backend/.env' --check"
fi
