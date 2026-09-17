#!/usr/bin/env bash
# Push the committed main checkout, then deploy the same clean source tree.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  echo 'Usage: bash push.sh'
  echo 'Push committed main to zengchanghuan/epub-factory, then run deploy.sh.'
  echo 'Commit changes first. Deployment fails safely if production jobs are active.'
  exit 0
fi
[[ $# == 0 ]] || { echo 'Unexpected arguments; use --help.' >&2; exit 2; }
[[ $(git branch --show-current) == main ]] || { echo 'Switch to main before pushing and deploying.' >&2; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo 'Commit all intended changes before pushing and deploying.' >&2; exit 1; }
EXPECTED_REMOTE='git@github.com:zengchanghuan/epub-factory.git'
[[ $(git remote get-url --push --all origin) == "$EXPECTED_REMOTE" ]] || {
  echo "Unexpected push destination; expected $EXPECTED_REMOTE" >&2; exit 1;
}
REVISION=$(git rev-parse HEAD)
git push origin main
echo "Pushed $REVISION. Starting deployment."
# Deployment failure does not roll back the Git push. After resolving it,
# run bash deploy.sh again; no force push or history rewrite is needed.
bash "$ROOT/deploy.sh"
echo "Pushed and deployed $REVISION."
