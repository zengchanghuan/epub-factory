#!/usr/bin/env bash
# One-time setup. Only the public key is sent to the authorized server.
set -euo pipefail
HOST=${DEPLOY_HOST:-ubuntu@81.71.22.79}
PORT=${DEPLOY_PORT:-22}
KEY=${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_fixepub}
MODE=${1:-install}
case "$MODE" in
  install|--prepare) ;;
  *) echo 'Usage: bash scripts/setup-deploy-ssh.sh [--prepare]' >&2; exit 2 ;;
esac
[[ "$HOST" != -* && "$HOST" != *[[:space:]]* && -n "$HOST" ]] || exit 2
[[ "$PORT" =~ ^[0-9]+$ ]] || exit 2
umask 077
mkdir -p "$(dirname "$KEY")"
if [[ ! -e "$KEY" ]]; then
  [[ ! -e "$KEY.pub" ]] || { echo 'Public key exists without private key; refusing to overwrite.' >&2; exit 1; }
  ssh-keygen -t ed25519 -N '' -C fixepub-deploy -f "$KEY"
fi
[[ -r "$KEY.pub" ]] || { echo "Public key missing: $KEY.pub" >&2; exit 1; }
ssh-keygen -lf "$KEY.pub"
if [[ "$MODE" == --prepare ]]; then
  echo "Public key ready: $KEY.pub (private key stays on this Mac)."
  exit 0
fi
# SSH handles the one-time password through the terminal, never a script argument.
# The first connection asks for host fingerprint verification; never auto-accept it.
ssh -p "$PORT" -o ConnectTimeout=15 -o StrictHostKeyChecking=ask "$HOST" \
  'umask 077; mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; touch "$HOME/.ssh/authorized_keys"; chmod 600 "$HOME/.ssh/authorized_keys"; IFS= read -r key; case "$key" in "ssh-ed25519 "*) ;; *) exit 2 ;; esac; grep -qxF "$key" "$HOME/.ssh/authorized_keys" || printf "%s\n" "$key" >> "$HOME/.ssh/authorized_keys"' < "$KEY.pub"
ssh -p "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "$HOST" \
  'sudo -n true && echo "Passwordless SSH and sudo verified."'
echo 'Ready: bash deploy.sh'
