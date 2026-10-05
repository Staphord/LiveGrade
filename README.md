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

The suite needs no Redis, no DevPerf and no network. Line and branch coverage are held at 100%:

```
coverage run manage.py test --parallel
coverage combine && coverage report
```

## Configuration

| Variable | Meaning |
|---|---|
| `LIVEGRADE_BASE_URL` | This service's public address. |
| `DEVPERF_URL` | DevPerf's address (the sign-in provider). |
| `OIDC_RP_CLIENT_ID`, `OIDC_RP_CLIENT_SECRET` | From `register_livegrade_client` in DevPerf. |
| `SECRET_KEY`, `ALLOWED_HOSTS` | Required when `DEBUG` is off; the app refuses to start without them. |
| `REDIS_URL` | Channels and Celery. Defaults to database 1 so DevPerf's is never shared. |
| `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database (default: local SQLite). |
| `SESSION_COOKIE_AGE` | Seconds a sign-in lasts (default 8 hours). It is also the longest a withdrawn permission can linger. |
| `DEVPERF_DB_*` | Only for the one-off data copy below. |

Production also needs HTTPS, a Celery worker, and a web server that serves `static/`.

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
