# LiveGrade

Live, QR-joined peer assessment for presentations, demos and project defences.

A lecturer builds a session (groups with their students, and a rubric - each can come from one
Excel file), puts a QR code on screen, and
students join from their phones with no account. Groups present one after another; the
students in the room grade the group on stage against the lecturer's rubric, and the
lecturer watches participation and results update live.

LiveGrade is its own service. It does not store passwords: lecturers sign in with their
**[DevPerf](https://github.com/Staphord/DevPerf)** account through single sign-on
(OpenID Connect), and see LiveGrade and nothing else.

## Services

LiveGrade is one platform with several services, each at its own address: `/classroom/`, `/panel/`,
`/showcase/`, `/team/`, `/competition/` and `/polls/`. Opening an address is choosing that service; a visitor signs in
or registers and lands on that service's dashboard, and a signed-in person is taken straight there. A service
that is not open shows a coming-soon page. Classroom and Anonymous Polls are open by default; `LIVEGRADE_LIVE_SERVICES` switches
services on once they have a dashboard. Sessions belong to one service and no owner page reaches another's.
Participants are unaffected: they join with the session link. Design: `docs/adr/ADR-001-service-model.md`.

Classroom owner pages now live under `/classroom/app/`. Existing `/assessments/...` links
redirect permanently with HTTP 308, preserving POST bodies and query strings. Participant
addresses under `/assess/` and WebSocket addresses under `/ws/assessments/` are unchanged.
The service choice is attached to each OIDC transaction, so concurrent sign-ins in different
tabs cannot replace one another's destination. Sign-up starts at `/classroom/?signup=1`.

## Anonymous Polls

Use `/polls/` to open the Anonymous Polls service and `/polls/app/` to manage polls.
Create a draft, add questions and options directly or from the sample Excel file, then publish
and share its link or QR code. Voters do not need accounts or a roster entry. A random browser
token lets a voter revise their anonymous ballot while voting remains open; no identity or
individual response status is displayed or associated with the ballot. The owner sees aggregate
answer counts and the total number of responses. Polls support response deadlines, single or
multiple choice, abstention, closing, cancellation, and confirming a decision with an optional note.
Both standalone and Classroom polls include a public live-results display; the creator can open it
from the poll controls, and voters see the same aggregate tally beside the voting form. The display
refreshes while voting is open and never lists names or individual ballots.

The home page shows the services and, when signed in, shortcuts to services already used.
Inside a service, navigation and the organization switcher stay within that service. New
Classroom owners can download synthetic group/student and rubric samples before creating
their first session.

Coming-soon pages accept a consented, single launch-notification signup. Repeated email/service
pairs receive the same confirmation. The abuse limit is shared across web workers in Redis;
it uses a keyed hash of the server-established client address, never a caller's forwarded
header. Configure the trusted proxy/server so `REMOTE_ADDR` contains the verified client
address; otherwise visitors behind that proxy share a limit. A Redis outage refuses signup
with a retryable response. Notification delivery and retention need their release review
before the collected addresses are used.

## Reusing a Classroom session

Owners can choose **Duplicate session** from the session list or a session's page.
The confirmation lets them name the new draft and optionally copy its student roster
and presentation groups. Memberships are copied only when both options are selected.
The rubric, evaluator notice, joining method, penalty and timer settings are retained.
The copy receives a new join link; assessments, scores, participation, presentation
history, live state and ownership transfers are not copied. The original stays intact.
Copies remain in the owner's current organisation and service, including when the
source is closed. Oversight access does not grant permission to duplicate a colleague's session.

The optional **Evaluator notice** in session basics is plain text (up to 4,000 characters).
Participants see it when joining and before scoring. Use it to explain the rubric and
how peer scores count. This owner-written notice does not replace a platform privacy
notice or the planned consent controls.

## Classroom session templates

Choose **Save as template** on an owned session to save its rubric and settings.
The snapshot belongs to the same owner, organisation and service. It has no roster,
groups, presentation history, submissions or scores, and remains usable after the
original session is deleted.

On **New session**, choose a template and select **Load setup**. Review the rubric
preview, adapt the session name and basics, then create a fresh draft. Timers come
from the saved setup; add groups and students before going live. The template library
also offers three editable Classroom starter examples: Class presentation, Project
demonstration and Individual presentation. These examples are starting points for
course-specific review, rather than a prescribed assessment policy.

Deleting a saved template does not affect sessions already created from it. To revise
a snapshot, adapt a session and save a new template. Templates for the later services
will add their service-specific panels, criteria and policies when those workflows
are implemented; this release slice implements Classroom's configuration.

Classroom assessment scores come **100% from peer grading**, per the owner's decision
of 26 October 2026. Rubric weights divide that assessment score; the existing configured
participation penalty is a separate deduction. The proposed adjustable peer/non-peer
split has been superseded. The working goal and decision record are in `plans/`.

## Classroom practice presentations

In **Groups**, select **Practice presentation** when creating or editing a group.
Run it with the usual presentation and scoring controls. Owner, participant and
projected-room screens label the practice round. Its scores are stored so the
participant can complete the normal submission flow, but they never contribute
to final marks, rankings, grading participation, missed-group penalties or exports.
Practice uses the existing eligibility, self-scoring, duplicate-submission and
pause rules. Ordinary roster membership rules still apply.

Practice status can change only before that group has a presentation turn. This
prevents an already assessed group from being hidden as practice, or a practice
round from becoming assessed later. Duplication retains this setup flag and
creates a new draft without any practice submissions.

Migration `0007_practice_entry` defaults existing groups to assessed. It refuses
to reverse while any practice group exists: dropping the flag would make old code
count practice as assessed work. After practice is used, code rollback must retain
practice-aware scoring, or use a verified database restore from before its use.

## Rubric score level guidance

Each Classroom rubric criterion can have owner-defined score anchors. Enter one
point value and short description per line, for example `15 | Partly demonstrated`.
Anchors must be unique and between zero and that criterion's weight; evaluators
still enter any score on the existing continuous scale. The rubric spreadsheet
accepts the same entries in its **Level descriptors** column. Guidance is shown
with the criterion while scoring, copied when a session is duplicated, and saved
with reusable templates. After a criterion has received a score, its guidance is
locked so later evaluators see the same rubric.

## Recovering a participant score submission

If a participant loses their connection while submitting, the scoring page keeps the
original scores and submission key and offers **Retry confirmation**. It sends the same
submission for the same presentation turn, so an accepted score is confirmed without
creating another evaluation. The participant can reconnect, reload the page, or rejoin
with the same roster identity if their participant cookie expired; an unsent draft from
an earlier turn is not copied into the current turn. Pending drafts are stored in the
browser for that session and participant and do not include the CSRF token. The server
accepts a new score only while that turn is open; a retry can confirm a submission the
server already accepted even after the turn closes. The ordinary HTML form remains
available when JavaScript is unavailable.

## How it fits with DevPerf

```
 Lecturer ──► LiveGrade ──(redirect)──► DevPerf login ──(signed token)──► LiveGrade
 Student  ──► LiveGrade  (QR link, no account, no DevPerf involved)
```

- **DevPerf** owns accounts, passwords, organizations and roles. It is the sign-in provider.
- **LiveGrade** owns sessions, rosters, rubrics, groups, turns and grades, in its own database.
- At sign-in DevPerf sends LiveGrade a signed token with the person's DevPerf id, name, email
  and the organizations they may run LiveGrade for. Nothing else is shared.
- Every query is scoped to the lecturer's active organization; another organization's data
  cannot be reached, even by guessing an id.
- Students are anonymous: they pick themselves from the lecturer's roster, and their session is a
  signed cookie.

## Requirements

- Python 3.12
- Redis (live updates and turn timers; database 1 by default)
- A running [DevPerf](https://github.com/Staphord/DevPerf) with the sign-in provider enabled
- SQLite for development, PostgreSQL for production

## Run locally

One-time setup:

```
python3.12 -m venv venv
venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

In DevPerf, create the sign-in tables and register LiveGrade as a client:

```
python manage.py migrate
python manage.py register_livegrade_client --base-url http://localhost:8001
```

Put the client id and secret it prints into `.env` (`OIDC_RP_CLIENT_ID`, `OIDC_RP_CLIENT_SECRET`).
The secret is shown once.

Then, with DevPerf running on port 8000:

```
scripts/dev.sh
```

It checks Redis and DevPerf, migrates, and starts the web server (port 8001) and the Celery
worker together. Open http://localhost:8001/ and sign in with a DevPerf account that is an
organization admin or has the **Lecturer** role.

To invite a lecturer from outside the organization, use **People → Invite a lecturer** in
DevPerf. They create one DevPerf account and see only LiveGrade.

## Tests

```
venv/bin/python manage.py test --parallel
```

The suite needs no Redis, no DevPerf and no network. The release target is 100% combined line and branch coverage:

```
coverage run manage.py test --parallel
coverage combine && coverage report
```

## Configuration

| Variable | Meaning |
|---|---|
| `LIVEGRADE_BASE_URL` | This service's public address. |
| `LIVEGRADE_PUBLIC_BASE_URL` | Optional public origin for student-facing share and QR links. Set a stable hostname or the current Wi-Fi address for local network testing; production values must use HTTPS. |
| `DEVPERF_URL` | DevPerf's address (the sign-in provider). |
| `OIDC_RP_CLIENT_ID`, `OIDC_RP_CLIENT_SECRET` | From `register_livegrade_client` in DevPerf. |
| `LIVEGRADE_LIVE_SERVICES` | Comma-separated services open to the public (default `classroom,polls`). Any other service shows a coming-soon page at its address. |
| `LIVEGRADE_INTEREST_RATE_LIMIT`, `LIVEGRADE_INTEREST_RATE_WINDOW` | Positive signup limit and window in seconds, per server-established client address across services (defaults: 5 requests / 600 seconds). Redis stores only short-lived hashed counter keys. |
| `PANEL_EXAMINER_RATE_LIMIT`, `PANEL_EXAMINER_RATE_WINDOW` | Positive request limit and cache window per active Panel examiner link (defaults: 120 requests / 60 seconds). The shared cache stores a short-lived salted digest key; cache failure blocks the request. |
| `PUBLIC_BEARER_UNKNOWN_RATE_LIMIT`, `PUBLIC_BEARER_UNKNOWN_RATE_WINDOW` | Positive limit and window for unknown Panel examiner and Competition judge links, keyed by an HMAC of the server-observed remote address (defaults: 300 requests / 60 seconds). Forwarded headers are ignored; cache failure blocks unknown-link requests. |
| `SHOWCASE_PUBLIC_RATE_LIMIT`, `SHOWCASE_PUBLIC_RATE_WINDOW` | Positive Showcase visitor-rating request budget per event and server-observed source address (defaults: 2,000 requests / 60 seconds, sized for a 600-person burst of page loads and submissions). The shared cache stores only a short-lived HMAC key; cache failure blocks ratings. |
| `SECRET_KEY`, `ALLOWED_HOSTS` | Required when `DEBUG` is off; the app refuses to start without them. |
| `REDIS_URL` | Channels and Celery. Defaults to database 1 so DevPerf's is never shared. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | SMTP server for Panel examiner invitation email. Credentials must come from an untracked `.env` or deployment secret manager. |
| `EMAIL_USE_TLS`, `EMAIL_USE_SSL`, `EMAIL_TIMEOUT`, `DEFAULT_FROM_EMAIL` | SMTP transport and sender settings. TLS and SSL default off; set the option required by the provider. Mail delivery failure leaves a one-time copy fallback for the organizer. |
| `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database (default: local SQLite). |
| `DB_CONN_MAX_AGE`, `DB_CONNECT_TIMEOUT` | Database connection lifetime and PostgreSQL connect timeout. ASGI deployments default to no persistent Django connections and a five-second connection timeout; configure and capacity-test an external pool. |
| `COMPETITION_JOIN_RATE_LIMIT` | Competition join requests per observed source in the public request window (default 6,000). Set this with shared-network traffic and edge limits in mind. |
| `POLL_PUBLIC_READ_RATE_LIMIT`, `POLL_PUBLIC_SUBMISSION_RATE_LIMIT` | Per-event, per-observed-source poll request budgets in a 60-second window. Configure for the deployment and shared-network traffic. |
| `SESSION_COOKIE_AGE` | Seconds a sign-in lasts (default 8 hours). It is also the longest a withdrawn permission can linger. |
| `CSP_ENFORCE` | Content-Security-Policy. Enforced by default when `DEBUG` is off; report-only (the console lists what it would block) while developing. `CSP_ENFORCE=False` is the emergency switch back to report-only. |
| `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` | HSTS lifetime (default 30 days) and, once every subdomain serves HTTPS, whether it covers them and may be preloaded. Both flags default to `False`. |
| `DEVPERF_DB_*` | Only for the one-off data copy below. |

Production also needs HTTPS, a Celery worker, and a web server that serves `static/`.

Use Python 3.12 and install the reviewed production dependency lock with `pip install --require-hashes -r requirements.lock`. Regenerate and re-audit the lock when changing dependencies. The CBOR package may require a Rust compiler when a wheel is unavailable, including on some Intel Macs.

`/healthz` is process liveness; `/readyz` checks the database and shared cache and returns 503 on failure without exposing connection details. Deployment checks both endpoints. Set `READINESSCHECK_URL` in the server deployment configuration if readiness is served at a different origin or path. Readiness does not verify workers, queued jobs, email, sign-in, or capacity: monitor and test those separately.

## Importing sessions from DevPerf

LiveGrade began as part of DevPerf. To copy the sessions that still live in DevPerf's database:

```
python manage.py import_from_devperf --dry-run   # rehearse; nothing is kept
python manage.py import_from_devperf             # copy, keeping ids and join links
```

DevPerf's database is only read. The command is safe to repeat, verifies that every source row
arrived and that grade totals match, and `--replace --yes` does a full re-copy. Set the
`DEVPERF_DB_*` variables first.

## Project layout

```
accounts/      sign-in with DevPerf, the lecturer user, organization scoping
portal/        the services (Classroom, Panel, ...), their entry addresses, and who uses which
assessments/   sessions, roster, rubric, groups, turns, scoring, student flow, live updates
config/        settings, URLs, ASGI (websockets), Celery
templates/     pages (lecturer and student)
scripts/       dev.sh
```

## Notes

- Cookie names are LiveGrade's own (`livegrade_sessionid`) so `localhost:8000` and
  `localhost:8001` do not log each other out in development.
- There is no Django admin: LiveGrade has no passwords, so a staff login would be a second way
  in that DevPerf's sign-in could not govern.
