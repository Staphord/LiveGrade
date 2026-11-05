# Foundation service-entry threat note

Scope: PF-06 through PF-11, service choice and OIDC return, owner-route migration,
service navigation, first-session samples, and public launch-notification signup.

Status: implementation notes for independent review. This is not release approval.

## Boundaries and controls

- **Redirect and service tampering:** only a registered, released service can be selected
  at its entry address. The sign-in initializer binds that choice to its generated OIDC
  state, alongside the library's nonce and PKCE verifier. The callback relies on the
  library's state verification and consumption, rechecks release state, and derives its
  destination from the registry. `next`, headers, and callback query fields cannot choose
  a destination. A missing or invalid choice returns to `/`.
- **Parallel tabs and session rotation:** service choice is per OIDC state, captured before
  the library consumes the record. Login rotates the session key through Django's standard
  authentication path. Completing one transaction does not consume another tab's choice.
- **Owner data isolation:** `ServiceMiddleware` derives `request.service` from the address.
  Owner queries retain service, organization, and owner/oversight filters. Legacy URLs
  redirect without acquiring Classroom scope. Participant UUID and WebSocket addresses
  are unchanged and retain their existing student-session and Origin checks.
- **Legacy URL manipulation:** old addresses resolve against the assessment route table.
  Their destination is generated with `reverse`; arbitrary suffixes cannot redirect.
  HTTP 308 retains the method and body; the raw query string is preserved as query data.
- **Cross-service navigation:** the landing page owns service selection and shortcuts.
  A service shell names only its own service. Organization changes validate membership
  from DevPerf claims and return to the service dashboard using the URL's service scope.
- **Public signup abuse:** CSRF and POST are required. Valid email and affirmative consent
  are required before inserting an interest row. A database constraint requires consent;
  a unique pair prevents duplicates. The same confirmation is used for first and duplicate
  signups. The service comes from the route and must be unreleased.
- **Rate limit and logging:** Redis counters are shared across web workers. Their keys use
  a keyed digest of `REMOTE_ADDR` and expire with the configured window. Caller-supplied
  forwarded headers are ignored. A Redis connection failure returns 503 and writes no
  interest; a reached limit returns 429. Error messages never contain backend exception
  details. The code does not log email addresses, raw client addresses, or tokens.
- **Samples:** onboarding downloads contain only the existing synthetic import examples,
  require an authorized lecturer in Classroom scope, and expose no session records.

## Evidence and remaining release work

Automated evidence is in `accounts/tests/test_oidc.py`, `portal/tests/`, the full access
matrix, and the existing organization, ownership, participant, and WebSocket tests.
The probe and authenticated ZAP seeds use `/classroom/app/` while sockets retain their
original address. These updated scanner sources are not evidence of a completed live scan.

Independent review must verify the DevPerf return against a running provider, proxy trust
and client-address handling, Redis-backed multi-worker limits, browser/accessibility behavior,
and the new interest migration on an anonymised rehearsal database. Consent retention and
one-time notification delivery require the service-launch/retention decisions. Production
approval, branch protections, backup/restore, rollback, and deployment verification remain
separate release prerequisites.
