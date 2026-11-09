# LiveGrade agent guidance

LiveGrade is a Django service integrated with DevPerf sign-in. Read this file and the applicable project documentation before changing code. Also read `.claude/CLAUDE.md` for the full test, coverage, and service-scoping rules; follow it where it adds detail.

## Work carefully

- Inspect the relevant views, forms, models, templates, tests, and current `git status` before editing. Preserve unrelated user changes.
- For non-trivial work, first map the user-visible flow and its business rules. Reuse existing architecture and make assumptions explicit when documentation or code does not settle a decision.
- Keep changes focused. Apply SOLID as a guide: give each unit a clear responsibility, depend on stable interfaces where they already exist, and avoid speculative abstractions or needless indirection.
- When reviewing, lead with actionable findings. Give severity, file and line, concrete evidence, impact, and a focused correction. Separate confirmed defects from risks or preferences; scope any “no findings” conclusion to what was inspected.

## Boundaries and security

- DevPerf owns authentication and the organizations a lecturer may use. Trust only the validated identity/organization claims and enforce authorization on the server.
- Scope every owner query by the active organization, current service, and owner as required by the model. Service context comes from `portal.middleware.ServiceMiddleware`, never user input. Do not allow a session or record to cross service or organization boundaries.
- Preserve LiveGrade's own session and CSRF cookie names. Never set `SESSION_COOKIE_DOMAIN`.
- Treat public participant pages, uploads, imports, WebSockets, and invitation links as security boundaries. Validate input server-side and avoid exposing identities, scores, or records beyond the intended role.
- For security-specific tasks, use the installed `security-best-practices` and `security-threat-model` skills. For auth, imports, tenancy, or public endpoints, apply the relevant security checks during implementation and review.
- Follow the dedicated LiveGrade domain skills located in `.gemini/skills/`, `.codex/skills/`, and `.claude/skills/`:
  - `systematic-debugging`: 4-phase root-cause debugging protocol; eliminates guessing and enforces reproduction tests.
  - `django-query-optimization`: N+1 query elimination, `select_related`/`prefetch_related`, and bulk operations.
  - `tdd-test-driven-development`: Red-Green-Refactor discipline with mandatory boundary and edge-case testing.
  - `git-safe-workflows`: Working-tree preservation, secret protection, and atomic commits.
  - `ui-ux-design-system`: Token hierarchy, responsive breakpoints, user interaction states, and layout consistency.
  - `web-accessibility-a11y`: WCAG 2.1 AA landmarks, ARIA labeling, keyboard navigation, and focus management.
  - `security-best-practices`: Injection prevention, CSRF enforcement, token protections, and rate-limiting.
  - `clean-code-refactoring`: SRP, low cyclomatic complexity, early returns, and neutral documentation.
  - `livegrade-domain-architecture`: Layered separation of concerns and clean code practices.
  - `livegrade-scoring-aggregation`: Strict rubric, weight, and aggregation invariants with independent test verification.
  - `livegrade-realtime-websockets`: WebSocket authorization, consumer safety, and data minimization.
  - `livegrade-spreadsheet-transports`: Bounded parsing, formula injection prevention, and two-phase preview caching.
  - `livegrade-tenancy-security`: Tenant isolation and access matrix synchronization.
  - `livegrade-test-and-coverage-gate`: Mandatory 100% coverage gate and parallel test verification.




## UI and interaction

- Before changing a screen, inspect the shared shell and comparable implemented assessment pages. Reuse the design tokens and components in `templates/partials/_design_system.html`, `templates/base.html`, and the relevant `templates/assessments/` pages.
- Keep task flows clear for first-time users: visible current state and next action, consistent content width and controls, concise labels, inline field errors, and the existing toast pattern. Do not introduce a second visual system for a service.
- For destructive confirmations, use LiveGrade's custom `DevPerf.confirm()` dialog through `data-confirm` (implemented in `static/js/overlay.js`). Never use browser-native `window.confirm()` or `window.alert()` in product UI.
- Check responsive layout, keyboard flow, visible focus, labels, and role-specific information when the task touches UI. Do not claim accessibility conformance from visual inspection alone.
- Do not use the installed Google Chrome browser for inspection or testing. If browser checks are authorized, use the installed Playwright workflow or another approved non-Chrome surface. The interactive Playwright skill requires an unsandboxed full-access session; do not enable that mode automatically.
- For a full UI critique, use `prompts/ui-design-review.md` and the local LiveGrade UI-review skill if present. These are optional review resources; this file contains the standing UI rules.

## Verification and reporting

- Follow `.claude/CLAUDE.md` for the parallel test command and 100% line/branch coverage requirement. Follow active user and environment instructions on whether checks may be run; state exactly what was and was not verified.
- Add focused regression coverage for behavior changes when testing is within the requested scope. Do not hide untested paths with coverage pragmas.
- Do not claim a flow, security property, test, or page is complete without observed evidence. Mark blocked and untested cases separately from failures.
