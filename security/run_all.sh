#!/usr/bin/env bash
# One command, whole LiveGrade security run, all LOCAL.
#
#   bash security/run_all.sh [static|dynamic|all] [-q|--quiet] [-d|--debug]
#
#   static    static scans only (no Docker needed)
#   dynamic   local app + probe + ZAP only
#   all       everything (default)
#   -q        quiet: terminal shows only one line per stage; full output goes to the log
#   -d        debug: also trace every shell command (set -x) and unbuffer Python output
#
# Default: full tool output live, plus a banner when each stage starts/ends and a
# "still running" line every 30s so slow stages never look frozen. Everything is saved to
# security/reports/run-<time>.log. A failing stage never stops the next one.
cd "$(dirname "$0")/.." || exit 1

MODE=all QUIET=0 DEBUG_RUN=0
for arg in "$@"; do
  case "$arg" in
    static|dynamic|all) MODE=$arg ;;
    -q|--quiet) QUIET=1 ;;
    -d|--debug) DEBUG_RUN=1 ;;
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

mkdir -p security/reports
LOG="security/reports/run-$(date +%Y%m%d-%H%M%S).log"
exec 3>&1                                   # fd 3 = the real terminal, always
if [ "$QUIET" = 1 ]; then exec >>"$LOG" 2>&1; else exec > >(tee -a "$LOG") 2>&1; fi
[ "$DEBUG_RUN" = 1 ] && export PYTHONUNBUFFERED=1   # python tools flush as they go

# In normal mode stdout already reaches the terminal through tee, so only send to fd 3 in
# quiet mode; in normal mode a plain echo is enough and avoids double lines.
say() { if [ "$QUIET" = 1 ]; then echo "$@" >&3; echo "$@"; else echo "$@"; fi; }

declare -a SUMMARY
TOTAL_START=$(date +%s)

stage() {  # stage <name> <command...>
  local name=$1; shift
  local start; start=$(date +%s)
  say ""; say "▶ [$(date +%H:%M:%S)] $name"
  # heartbeat, so a quiet tool does not look frozen
  ( while sleep 30; do say "  … still running: $name ($(( $(date +%s) - start ))s)"; done ) &
  local hb=$!
  [ "$DEBUG_RUN" = 1 ] && set -x
  "$@"; local rc=$?
  { set +x; } 2>/dev/null
  kill "$hb" 2>/dev/null; wait "$hb" 2>/dev/null
  local secs=$(( $(date +%s) - start )) mark="✔"; [ "$rc" != 0 ] && mark="✖"
  say "$mark [$(date +%H:%M:%S)] $name  exit $rc  (${secs}s)"
  SUMMARY+=("$(printf '%s %-30s exit %-3s %ss' "$mark" "$name" "$rc" "$secs")")
}

say "LiveGrade security run: mode=$MODE quiet=$QUIET debug=$DEBUG_RUN  $(date)"
say "Log: $LOG"

# ---- preflight: install the scanners once ------------------------------------------
if [ ! -x security/.tools-venv/bin/semgrep ] || ! command -v gitleaks >/dev/null; then
  stage "install scanners" bash security/install.sh
fi

# ---- static ------------------------------------------------------------------------
if [ "$MODE" = all ] || [ "$MODE" = static ]; then
  for t in sast bandit deps secrets history iac templates fs; do
    stage "static: $t" bash security/scan.sh "$t"
  done
  stage "django check --deploy" env DEBUG=False \
    SECRET_KEY=local-check-only-secret-key-0123456789-abcdefghij \
    ALLOWED_HOSTS=example.com OIDC_RP_CLIENT_ID=x OIDC_RP_CLIENT_SECRET=x \
    SECURE_SSL_REDIRECT=True venv/bin/python manage.py check --deploy
fi

# ---- dynamic -----------------------------------------------------------------------
if [ "$MODE" = all ] || [ "$MODE" = dynamic ]; then
  if ! docker info >/dev/null 2>&1; then
    say ""; say "!! Docker is not running: start Docker Desktop, then: bash security/run_all.sh dynamic"
    SUMMARY+=("$(printf '– %-30s SKIPPED (docker down)' 'dynamic stages')")
  else
    trap 'bash security/local_dast.sh down' EXIT
    stage "local app up + seed" bash security/local_dast.sh up
    stage "probe (XSS/IDOR/anon/ws)" bash security/local_dast.sh probe
    stage "ZAP auth + anonymous" bash security/local_dast.sh zap
  fi
fi

say ""; say "================ SUMMARY ($(( $(date +%s) - TOTAL_START ))s total) ================"
for line in "${SUMMARY[@]}"; do say "$line"; done
say "Full log: $LOG"
say "Reports:  security/reports/   (open zap-*.html in a browser)"
say "Exit 0 = clean; non-zero = findings or a failed stage - read that stage's output."
