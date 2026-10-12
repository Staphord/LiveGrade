#!/usr/bin/env bash
# Installs the scanners into an isolated venv (Python tools) and Homebrew (binaries).
# Run once, by hand:  bash security/install.sh
# Python tools live in security/.tools-venv so they never touch the app's venv/requirements.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv security/.tools-venv
security/.tools-venv/bin/pip install --quiet --upgrade pip
security/.tools-venv/bin/pip install --quiet semgrep bandit pip-audit checkov detect-secrets djlint websockets

# Binaries (macOS). Skips any that are already present.
for pkg in gitleaks trufflehog trivy osv-scanner hadolint; do
  command -v "$pkg" >/dev/null || brew install "$pkg"
done
echo "Done. DAST tools (ZAP, nuclei) are optional: brew install --cask zap ; brew install nuclei"
