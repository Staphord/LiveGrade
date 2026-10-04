#!/usr/bin/env bash
# Run LiveGrade for local development: checks what it depends on, migrates, then
# starts the web server (8001) and the Celery worker (turn timers) together.
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=venv/bin/python
[ -x "$PY" ] || { echo "No venv. Run: python3.12 -m venv venv && venv/bin/pip install -r requirements-dev.txt"; exit 1; }
[ -f .env ] || { echo "No .env. Copy .env.example to .env and fill in the OIDC client id/secret"; echo "(from: python manage.py register_livegrade_client --base-url http://localhost:8001, run in ../devperf)."; exit 1; }

set -a; . ./.env; set +a
: "${OIDC_RP_CLIENT_ID:?OIDC_RP_CLIENT_ID is empty in .env}"
: "${DEVPERF_URL:=http://localhost:8000}"
: "${REDIS_URL:=redis://127.0.0.1:6379/1}"

command -v redis-cli >/dev/null && redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1 \
  || { echo "Redis is not answering at $REDIS_URL (live updates and timers need it)."; exit 1; }
curl -fsS -o /dev/null "$DEVPERF_URL/o/.well-known/openid-configuration" \
  || { echo "DevPerf is not answering at $DEVPERF_URL (sign-in needs it). Start it: cd ../devperf && python manage.py runserver 8000"; exit 1; }

$PY manage.py migrate --noinput

# solo pool: the default forking pool is unreliable on macOS, and one process is plenty here.
$PY -m celery -A config worker -P solo -l info &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT INT TERM

echo
echo "LiveGrade: ${LIVEGRADE_BASE_URL:-http://localhost:8001}   (sign in with a DevPerf account that may run LiveGrade)"
$PY manage.py runserver 8001
