#!/usr/bin/env bash
# Executed on the server through SSH. See DEPLOYMENT.md for the trusted config.
# usage: deploy.sh OPERATION ENVIRONMENT [TARGET APPROVAL EXPECTED_SHA BACKUP MIGRATION_PLAN]
set -euo pipefail
umask 077

fail() { echo "!! $*" >&2; exit 1; }
OPERATION="${1:-}"
DEPLOY_ENVIRONMENT="${2:-}"
TARGET="${3:-}"
APPROVAL_REFERENCE="${4:-}"
EXPECTED_SHA="${5:-}"
BACKUP_REFERENCE="${6:-}"
MIGRATION_PLAN_REFERENCE="${7:-}"
[[ $# -le 7 ]] || fail 'Too many arguments'
case "$OPERATION" in deploy-testing|deploy-production|rollback|status) ;; *) fail 'Unknown operation' ;; esac
case "$DEPLOY_ENVIRONMENT" in testing|production) ;; *) fail 'Unknown environment' ;; esac
[[ "$OPERATION" != deploy-testing || "$DEPLOY_ENVIRONMENT" == testing ]] || fail 'Testing operation requires testing'
[[ "$OPERATION" != deploy-production || "$DEPLOY_ENVIRONMENT" == production ]] || fail 'Production operation requires production'
if [[ "$OPERATION" == status ]]; then
  [[ $# == 2 ]] || fail 'Status accepts only an environment'
else
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'Expected commit must be a full SHA'
  if [[ "$DEPLOY_ENVIRONMENT" == production || "$OPERATION" == rollback ]]; then
    [[ "$TARGET" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'A version tag is required'
    for reference in "$APPROVAL_REFERENCE" "$BACKUP_REFERENCE" "$MIGRATION_PLAN_REFERENCE"; do
      [[ ${#reference} -le 512 && "$reference" =~ ^[A-Za-z0-9][A-Za-z0-9_.:/#=-]*$ ]] || fail 'Approval, backup and migration-plan references are required'
    done
  else
    [[ "$TARGET" == "$EXPECTED_SHA" ]] || fail 'Testing target must be the expected full SHA'
  fi
fi

# Only the server operator manages these files. No config path is accepted over SSH.
CONFIG_DIR="${LIVEGRADE_DEPLOY_CONFIG_DIR:-$HOME/.config/livegrade}"
CONFIG_FILE="$CONFIG_DIR/$DEPLOY_ENVIRONMENT.env"
[[ -f "$CONFIG_FILE" ]] || fail 'Server environment configuration is missing'
# shellcheck disable=SC1090
source "$CONFIG_FILE"
: "${APP_DIR:?APP_DIR must be configured}" "${SERVICES:?SERVICES must be configured}"
: "${STATE_DIR:?STATE_DIR must be configured}" "${HEALTHCHECK_URL:?HEALTHCHECK_URL must be configured}"
[[ "$APP_DIR" == /* && "$STATE_DIR" == /* ]] || fail 'App and state directories must be absolute'
APP_DIR="$(cd "$APP_DIR" && pwd -P)"
STATE_DIR="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$STATE_DIR")"
[[ "$STATE_DIR/" != "$APP_DIR/"* ]] || fail 'State directory must be outside the checkout'
read -r -a UNITS <<< "$SERVICES"
for unit in "${UNITS[@]}"; do
  [[ "$unit" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$ ]] || fail 'Invalid systemd unit'
done
cd "$APP_DIR"
[[ "$(git rev-parse --show-toplevel)" == "$APP_DIR" ]] || fail 'APP_DIR must be the checkout root'
health() {
  local unit
  for unit in "${UNITS[@]}"; do systemctl is-active --quiet "$unit" || return 1; done
  curl --fail --silent --show-error --max-time 10 --retry 2 --output /dev/null "$HEALTHCHECK_URL"
}
if [[ "$OPERATION" == status ]]; then
  echo "==> Environment $DEPLOY_ENVIRONMENT"
  echo "==> Commit $(git rev-parse HEAD)"
  [[ -z "$(git status --porcelain)" ]] || fail 'Checkout has local changes'
  health
  echo '==> Healthy'
  exit 0
fi

mkdir -p "$STATE_DIR"
LOCK_DIR="$STATE_DIR/operation.lock"
mkdir "$LOCK_DIR" 2>/dev/null || fail 'Another operation holds the lock; investigate before removing a stale lock'
PREVIOUS_SHA="$(git rev-parse HEAD)"
PHASE=validation
RECEIPT="$STATE_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
export OPERATION DEPLOY_ENVIRONMENT TARGET APPROVAL_REFERENCE EXPECTED_SHA BACKUP_REFERENCE MIGRATION_PLAN_REFERENCE PREVIOUS_SHA RECEIPT
finish() {
  local result=$?
  trap - EXIT
  set +e
  export PHASE
  DEPLOY_EXIT_CODE="$result" python3 - <<'PY'
import datetime
import json
import os
from pathlib import Path
keys = ('operation', 'deploy_environment', 'target', 'approval_reference', 'expected_sha',
        'backup_reference', 'migration_plan_reference', 'previous_sha', 'phase')
record = {key: os.environ[key.upper()] for key in keys}
record.update(completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
              exit_code=int(os.environ['DEPLOY_EXIT_CODE']))
record['result'] = 'success' if record['exit_code'] == 0 else 'failed'
path = Path(os.environ['RECEIPT'])
temporary = path.with_suffix('.tmp')
temporary.write_text(json.dumps(record, indent=2) + '\n')
temporary.replace(path)
PY
  local record_result=$?
  rmdir "$LOCK_DIR"
  if [[ "$result" == 0 && "$record_result" != 0 ]]; then result=$record_result; fi
  exit "$result"
}
trap finish EXIT
[[ -z "$(git status --porcelain)" ]] || fail 'Refusing to overwrite local changes'

echo '==> Verifying remote release'
if [[ "$DEPLOY_ENVIRONMENT" == production ]]; then RELEASE_BRANCH=main; else RELEASE_BRANCH=staging; fi
git fetch --no-tags origin "refs/heads/$RELEASE_BRANCH:refs/remotes/origin/$RELEASE_BRANCH"
if [[ "$OPERATION" == rollback || "$DEPLOY_ENVIRONMENT" == production ]]; then
  git fetch --no-tags origin "refs/tags/$TARGET:refs/tags/$TARGET"
  RESOLVED_SHA="$(git rev-parse --verify "refs/tags/$TARGET^{commit}")"
else
  RESOLVED_SHA="$(git rev-parse --verify "$TARGET^{commit}")"
fi
[[ "$RESOLVED_SHA" == "$EXPECTED_SHA" ]] || fail 'Release tag/commit changed since validation'
git merge-base --is-ancestor "$RESOLVED_SHA" "refs/remotes/origin/$RELEASE_BRANCH" || fail 'Target is not on the required release branch'
PHASE=checkout
git checkout --detach "$RESOLVED_SHA"
PHASE=dependencies
"$APP_DIR/venv/bin/python" -m pip install --quiet -r requirements.txt
PHASE=configuration
"$APP_DIR/venv/bin/python" manage.py check --deploy --fail-level ERROR
if [[ "$OPERATION" != rollback ]]; then
  PHASE=migrations
  "$APP_DIR/venv/bin/python" manage.py migrate --noinput
fi
# Rollback is code-only: approval must attest that the current schema is compatible.
PHASE=static
"$APP_DIR/venv/bin/python" manage.py collectstatic --noinput
PHASE=restart
for unit in "${UNITS[@]}"; do sudo -n systemctl restart "$unit"; done
PHASE=health
health
PHASE=complete
echo "==> Deployed $RESOLVED_SHA"
