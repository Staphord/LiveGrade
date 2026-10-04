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
- Cookie names are LiveGrade's own (`livegrade_sessionid`, `livegrade_csrftoken`)
  so `localhost:8000` and `localhost:8001` do not log each other out. Never set
  `SESSION_COOKIE_DOMAIN`.
- Implementation plans go in `plans/` (gitignored). The migration plan lives in
  `../devperf/plans/livegrade-microservice.md`.
