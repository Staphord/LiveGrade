# LiveGrade

A separate service from DevPerf (`../devperf`). Lecturers sign in with their DevPerf
account (OpenID Connect); this service keeps no passwords. See `README.md`.

## Running tests

Always with `--parallel`:

```
python manage.py test --parallel
```

## Coverage

Held at **100% line and branch coverage**, like DevPerf:

```
pip install -r requirements-dev.txt
coverage run manage.py test --parallel
coverage combine && coverage report
```

A line that cannot be reached is marked `# pragma: no cover` with the reason on the
same line. Do not use a pragma to skip a path that merely lacks a test.

## Rules that matter here

- Never decide who may do what in this service. DevPerf's token says which
  organizations a lecturer may run LiveGrade for (`orgs` claim); trust that and
  scope every query to the active organization.
- Every owner query is scoped by service as well as organization and owner: chain
  `.for_service(request.service)` (set by `portal.middleware.ServiceMiddleware` from the address,
  never from input). A session of one service must never be reachable from another.
- Cookie names are LiveGrade's own (`livegrade_sessionid`, `livegrade_csrftoken`)
  so `localhost:8000` and `localhost:8001` do not log each other out. Never set
  `SESSION_COOKIE_DOMAIN`.
- Never use em dashes (`—` or `&mdash;`) in user-facing copy, page titles, templates, UI elements, or documentation. Use hyphens (`-`), colons (`:`), commas, or concise sentence structures instead.
- Implementation plans go in `plans/` (gitignored). The migration plan lives in
  `../devperf/plans/livegrade-microservice.md`.

## Mandatory Implementation Skills

The following domain skills are defined in `.claude/skills/` (and `.gemini/skills/` / `.codex/skills/`) and must be strictly adhered to:
1. `systematic-debugging`: 4-phase root-cause debugging protocol; eliminates guessing and enforces reproduction tests.
2. `django-query-optimization`: N+1 query elimination, `select_related`/`prefetch_related`, and bulk operations.
3. `tdd-test-driven-development`: Red-Green-Refactor discipline with mandatory boundary and edge-case testing.
4. `git-safe-workflows`: Working-tree preservation, secret protection, and atomic commits.
5. `ui-ux-design-system`: Token hierarchy (`_design_system.html`), responsive breakpoints, user interaction states, and layout consistency.
6. `web-accessibility-a11y`: WCAG 2.1 AA standards, semantic landmarks, ARIA labeling, keyboard navigation, and visible focus management.
7. `security-best-practices`: Injection prevention, CSRF enforcement, token protections, and rate-limiting.
8. `clean-code-refactoring`: SRP, low cyclomatic complexity, early returns, and neutral documentation.
9. `livegrade-domain-architecture`: Layered separation of concerns. Domain algorithms reside in service modules (`operations.py`), never in view controllers.
10. `livegrade-scoring-aggregation`: Exact formulas, weights, trimmed means, abstention handling, and non-self-assessment invariants with independent test oracles.
11. `livegrade-realtime-websockets`: Django Channels consumer security, state-projection data minimization, and connection error handling.
12. `livegrade-spreadsheet-transports`: Two-phase preview/commit workflows and CSV formula injection neutralization.
13. `livegrade-tenancy-security`: Multi-tenant organization boundaries, capability link token scrubbing, and access matrix test synchronization.
14. `livegrade-test-and-coverage-gate`: Mandatory 100% line/branch coverage gate and parallel test suite execution (`manage.py test --parallel`).



