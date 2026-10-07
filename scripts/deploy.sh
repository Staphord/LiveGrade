#!/usr/bin/env bash
# Runs ON THE VPS (piped over ssh by .github/workflows/deploy.yml).
# Pulls main, installs dependencies, migrates, collects static files and
# restarts the services. Stops at the first failure.
#
# Overridable from the environment:
#   APP_DIR   checkout location            (default: $HOME/LiveGrade)
#   BRANCH    branch to deploy             (default: main)
#   SERVICES  systemd units to restart     (REQUIRED, no default: so a missing value
#             can never restart another app's services on a shared VPS)
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/LiveGrade}"
BRANCH="${BRANCH:-main}"
SERVICES="${SERVICES:?set the VPS_SERVICES secret to the systemd units of this app}"

cd "$APP_DIR"
echo "==> Fetching $BRANCH"
git fetch --prune origin "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "==> Installing dependencies"
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet -r requirements.txt

echo "==> Checking configuration"
python manage.py check --deploy --fail-level ERROR

echo "==> Migrating"
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Restarting: $SERVICES"
for unit in $SERVICES; do
  sudo systemctl restart "$unit"
done
for unit in $SERVICES; do
  systemctl is-active --quiet "$unit" || { echo "!! $unit failed to start" >&2; exit 1; }
done

echo "==> Deployed $(git rev-parse --short HEAD)"
