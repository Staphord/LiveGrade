#!/usr/bin/env bash
# Full LOCAL dynamic security run against a throwaway copy of LiveGrade. Your dev database
# (db.sqlite3) and DevPerf are never touched: everything lives in security/.local/.
#
#   bash security/local_dast.sh up       start Redis + LiveGrade on :8001, seed payload data
#   bash security/local_dast.sh probe    XSS / cross-org / anonymous / student / WebSocket checks
#   bash security/local_dast.sh zap      ZAP: authenticated scan, then anonymous scan (slow)
#   bash security/local_dast.sh down     stop everything and delete security/.local
#
# Needs Docker (Redis + ZAP) and `bash security/install.sh` first. Reports: security/reports/
set -euo pipefail
cd "$(dirname "$0")/.."
L=security/.local; R=security/reports; PORT=8001
PY=venv/bin/python
TOOLS_PY=security/.tools-venv/bin/python   # has `websockets` for the probe
[ -x "$TOOLS_PY" ] || TOOLS_PY=python3

app_env() {  # production-like settings: DEBUG off so the real security settings are active
  export DEBUG=False SECRET_KEY=local-dast-only-secret-key-0123456789-abcdefghijkl
  export ALLOWED_HOSTS=localhost,127.0.0.1,host.docker.internal SECURE_SSL_REDIRECT=False
  export OIDC_RP_CLIENT_ID=local OIDC_RP_CLIENT_SECRET=local DEVPERF_URL=http://localhost:9
  export DB_NAME="$PWD/$L/db.sqlite3" REDIS_URL=redis://127.0.0.1:6390/1
}
load_seed() { set -a; . "$L/seed.env"; set +a; }

case "${1:-}" in
up)
  mkdir -p "$L" "$R"
  docker rm -f livegrade-dast-redis >/dev/null 2>&1 || true
  docker run -d --name livegrade-dast-redis -p 6390:6379 redis:7 >/dev/null
  app_env; rm -f "$DB_NAME"
  $PY manage.py migrate --noinput >/dev/null
  $PY security/seed_session.py > "$L/seed.env"
  nohup $PY -m daphne -b 127.0.0.1 -p $PORT config.asgi:application > "$L/server.log" 2>&1 &
  echo $! > "$L/server.pid"
  for _ in $(seq 1 30); do curl -fs "http://localhost:$PORT/healthz" >/dev/null && break; sleep 1; done
  curl -fs "http://localhost:$PORT/healthz" >/dev/null && echo "LiveGrade is up on :$PORT (log: $L/server.log)" ;;
probe)
  load_seed; $TOOLS_PY security/probe.py "http://localhost:$PORT" | tee "$R/probe.txt" ;;
zap)
  load_seed; chmod -R a+rwx security
  run_zap() {  # $1 plan, remaining args: extra docker -e flags. ZAP reaches the host via host.docker.internal.
    plan=$1; shift
    # --memory: without a limit the signed-in scan was killed (exit 137) in earlier local runs; with one it completes
    docker run --rm --memory=6g -v "$PWD:/zap/wrk/:rw" -e ZAP_TARGET=http://host.docker.internal:$PORT \
      -e ORG_A_SESSION_PK -e ORG_A_SESSION_UUID "$@" ghcr.io/zaproxy/zaproxy@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6 \
      zap.sh -cmd -autorun "/zap/wrk/security/zap/$plan"
  }
  # ZAP_AUTH_HEADER* must be real environment variables before ZAP starts (docker run -e).
  run_zap authenticated.yaml -e ZAP_AUTH_HEADER=Cookie \
    -e "ZAP_AUTH_HEADER_VALUE=livegrade_sessionid=${ORG_A_COOKIE}" || echo "authenticated scan exit $? (1=High, 2=Medium alerts)"
  run_zap anonymous.yaml || echo "anonymous scan exit $? (1=High, 2=Medium alerts)" ;;
down)
  [ -f "$L/server.pid" ] && kill "$(cat "$L/server.pid")" 2>/dev/null || true
  docker rm -f livegrade-dast-redis >/dev/null 2>&1 || true
  rm -rf "$L"; echo "stopped and cleaned" ;;
*) echo "usage: $0 up|probe|zap|down"; exit 2 ;;
esac
