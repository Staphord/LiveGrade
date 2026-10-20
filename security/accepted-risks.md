# Accepted risks in the security scan

Everything the scan is told to ignore is written here with the reason and a date to look again.
A test (`security/tests/test_summary.py`) fails if the scan ignores something that is not listed.

| What | Where it is ignored | Why it is safe to ignore | Look again |
|---|---|---|---|
| `CVE-2026-106454` (Twisted, IMAP wildcard pattern) | `pip-audit --ignore-vuln` in `security/scan.sh` and `.github/workflows/security.yml` | The flaw is in `twisted.mail.imap4`. LiveGrade pulls Twisted in through Daphne for HTTP and WebSocket only and never imports the mail code, so the vulnerable function cannot be reached. No fixed Twisted release existed on 2026-10-19. | When a fixed Twisted is released, or by 2026-11-19 |
| ZAP rule `10106` "HTTP Only Site" | `security/zap/*.yaml` (`alertFilter`) | The scan runs against a throwaway copy on plain http. The live site serves https only; that is checked by the production workflow (redirect, HSTS, TLS). | Never, unless the scan target becomes https |
| The developer's own `.env` | `security/gitleaks-local.toml` | Git-ignored, so it cannot be committed without the history and CI scans (which do not allow it) catching it. | Never |
