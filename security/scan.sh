#!/usr/bin/env bash
# Usage: bash security/scan.sh <target|all>     Reports go to security/reports/<target>.*
# Targets: sast bandit deps secrets history iac django-deploy templates fs dast
# All static targets are read-only; only `dast` sends traffic (to a local dev server).
set -uo pipefail
cd "$(dirname "$0")/.."
T=security/.tools-venv/bin
R=security/reports; mkdir -p "$R"
EXCL=(venv security/.tools-venv security/reports staticfiles htmlcov .git)

run() { # sast: Semgrep rules for Django, Python, OWASP Top 10, secrets, JS
  case "$1" in
  sast)
    $T/semgrep scan --config p/django --config p/python --config p/owasp-top-ten \
      --config p/security-audit --config p/javascript --config p/secrets \
      --exclude venv --exclude security --exclude '*/migrations/*' \
      --metrics=off --sarif-output=$R/semgrep.sarif --text-output=$R/semgrep.txt . ; cat $R/semgrep.txt ;;
  bandit)  # Python-specific security linter
    $T/bandit -r accounts assessments config -x '*/tests/*,*/migrations/*' \
      -f txt -o $R/bandit.txt; cat $R/bandit.txt ;;
  deps)    # known-vulnerable dependencies (PyPI advisory DB + OSV)
    # The one ignored advisory is explained in security/accepted-risks.md (and a test enforces that).
    $T/pip-audit -r requirements.txt --desc --ignore-vuln CVE-2026-106454 | tee $R/pip-audit.txt
    # OSV-Scanner v2 syntax is `osv-scanner scan source -r <dir>`; pointing it at the
    # repo root would also walk venv/, so it is left out of the default run.
    ;;
  secrets) # working tree
    gitleaks dir . -c security/gitleaks-local.toml -r $R/gitleaks-tree.json --redact -v ;;
  history) # every commit ever pushed (a deleted secret is still a leaked secret)
    gitleaks git . -c security/gitleaks.toml -r $R/gitleaks-history.json --redact -v
    # --no-update: the updater needs to replace its own binary, which fails when it is installed
    # somewhere the current user cannot write, and that failure used to leave an empty report.
    # 2>&1 keeps the "finished scanning ... verified_secrets" line in the report, so an empty
    # result reads as "scanned, nothing found" and not as "never ran". --fail: exit 183 on a verified secret.
    trufflehog git file://"$PWD" --only-verified --no-update --fail 2>&1 | tee $R/trufflehog.txt
    grep -q "finished scanning" $R/trufflehog.txt || { echo "trufflehog did not finish"; exit 1; } ;;
  iac)     # GitHub Actions workflow + any Docker/Terraform
    $T/checkov -d .github --framework github_actions | tee $R/checkov-actions.txt ;;
  django-deploy) # Django's own production-config audit, using production-like settings
    echo "Run with a real prod-style env, e.g.:"
    echo "  DEBUG=False SECRET_KEY=\$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))') \\"
    echo "  ALLOWED_HOSTS=example.com OIDC_RP_CLIENT_ID=x OIDC_RP_CLIENT_SECRET=x \\"
    echo "  venv/bin/python manage.py check --deploy" ;;
  templates) # XSS-prone patterns in Django templates / JS
    $T/djlint templates --lint | tee $R/djlint.txt
    grep -rnE "\|safe|mark_safe|autoescape off|innerHTML|document\.write|eval\(" templates static assessments accounts \
      --include=*.html --include=*.js --include=*.py | grep -v tests | tee $R/xss-grep.txt ;;
  fs)      # trivy: vulns + misconfig + secrets in one pass
    # Trivy only reads requirements.txt when every version is pinned with ==, and ours are ranges, so it
    # would report "not scanned". The versions actually installed are written out and scanned instead.
    mkdir -p security/.tmp/frozen && venv/bin/pip freeze --exclude-editable | grep -E '^[A-Za-z0-9_.-]+==' > security/.tmp/frozen/requirements.txt
    { trivy fs --scanners vuln --exit-code 1 --severity HIGH,CRITICAL --quiet security/.tmp/frozen
      trivy fs --scanners secret,misconfig --exit-code 1 --skip-dirs venv,security --quiet .; } | tee $R/trivy.txt
    [ "${PIPESTATUS[0]}" = 0 ] ;;
  dast)    # dynamic: start the app first (python manage.py runserver 8001), then:
    echo "ZAP:    zap-baseline.py -t http://localhost:8001 -r $R/zap.html   (docker: ghcr.io/zaproxy/zaproxy:stable)"
    echo "nuclei: nuclei -u http://localhost:8001 -o $R/nuclei.txt"
    echo "WebSocket: test manually / with ZAP WebSocket add-on (see README)" ;;
  *) echo "unknown target: $1"; exit 2 ;;
  esac
}

if [ "${1:-}" = all ]; then
  for t in sast bandit deps secrets history iac templates fs; do echo "=== $t ==="; run $t; done
else run "${1:?target required}"; fi
