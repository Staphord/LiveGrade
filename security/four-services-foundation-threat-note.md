# Threat note: four-service domain foundation

Status: local foundation only; Panel owner setup and invited-examiner scoring,
and Showcase event-scoped rating, QR, print-label, and explicitly published
aggregate-results routes exist. Backend Showcase import preview/apply and judge
coverage operations exist. Organizer routes remain disconnected from released
service dashboards; no service is released through the registry.

## Data boundaries added

- Panel hearing, candidate, criterion, and invitation. Invitation tokens are
  random and only a SHA-256 digest is stored. The owner workspace creates and
  configures draft hearings, opens configured hearings, issues one-time
  invitation links, and revokes invitations. The invited examiner workspace
  accepts scores/comments and reasoned conflicts without an owner account.
  Criterion scores and conflicts bind to the invitation; score submission
  checks recusal and hearing state under row locks. Hearing, candidate, criterion,
  invitation, conflict, score, criterion abstention, sign-off, release, and
  entrant-result-link issue/revocation operations append audit events; audit rows reject normal ORM
  update/delete. A criterion abstention is append-only, conflicts with a score
  for the same invitation/candidate/criterion, and does not count toward the
  minimum-rater rule. Anonymous
  examiner POSTs retain CSRF protection. Examiner response caching and referrers
  are disabled. Closing a hearing enforces the minimum number of unrecused
  examiner scores for every candidate/criterion pair and writes immutable,
  anonymous aggregate snapshots with the configured method and rater count.
  Owners can review, sign off, and release those snapshots. After sign-off,
  corrections append a reasoned revision with actor and timestamp; the original
  aggregate snapshot remains unchanged and each correction is audited. After
  release, the owner can issue a random bearer link for one candidate only;
  only its digest is stored, and the page shows only that candidate's released
  aggregate/effective results. Owner revocation is audited; the link remains
  valid until revoked. Invitation and result tokens appear in URL paths;
  successful invitation delivery does not render the invitation URL in the
  owner response, but a mail failure exposes it once as a manual fallback.
  Infrastructure access-log redaction is not configured or verified, so Panel
  routes remain closed. Django's `django.request` and `django.server` loggers
  now redact Panel examiner/result, Competition judge, and Team feedback
  capability path segments and discard the raw request object from the log
  record. This does not cover Gunicorn/ASGI server access logs, reverse proxies,
  hosting-provider logs, analytics, or mail-provider traces. Examiner
  invitations are sent to the supplied email address via Django's configured
  mail backend; the bearer token is necessarily present in that email URL.
  Valid examiner
  links are throttled by a short-lived cache counter keyed from a salted digest
  of the stored token digest (default: 120 requests / 60 seconds); cache failure
  returns 503. Unknown and unusable bearer links are limited separately by a
  short-lived HMAC key derived from the server-observed remote address and
  service name (default: 300 requests / 60 seconds); forwarded headers are
  ignored and cache failure returns 503. This is application-cache protection,
  not a replacement for ingress controls. Durable,
  append-only attempt rows now cover accepted/rejected actions and rate-limited
  or unavailable POST attempts associated with known invitations, including
  expired/revoked invitation digests. Rows contain fixed codes and the scoped
  invitation relation only; raw token, IP, user agent, and free-text errors are
  omitted. Unknown tokens create no database row. Opening a valid invitation
  only prepares a signed, HttpOnly, Strict-SameSite browser cookie; a CSRF-
  protected Continue POST binds its digest once and appends one audit event.
  A different browser is refused and must receive a newly issued invitation.
  This is browser-cookie binding, not hardware attestation; cookie theft and
  infrastructure URL logging remain risks. The policy needs owner approval and
  independent audit review. Owners can export
  a hearing's reasoned recusals as private CSV, containing examiner email only
  beside the corresponding candidate and conflict reason; score data is
  excluded. Panel review lets an owner redact an examiner comment before
  sign-off, preserving its digest and moderation audit while clearing the
  comment from the score row. Neither feature addresses bearer-token exposure
  in infrastructure access logs.
- Showcase event, category, exhibit, rating. Visitor device tokens are Django
  signed, event-bound, time-limited, and stored only as a digest. The public
  rating page keeps one event token in an HttpOnly cookie, so the browser reuses
  it across exhibits. A uniqueness constraint prevents a second rating for the
  same event, exhibit, and token. The route rejects events outside their rating
  window and blocks minor-involving events without a consent timestamp. Each
  exhibit has a print-ready label and PNG QR route pointing to its rating form.
  XLSX preview is bounded and read-only; apply is atomic and owner/org scoped.
  Organizer routes require service scope and return 404 while Showcase remains
  unreleased. Draft event creation does not record minor consent. Judge
  assignments are owner scoped and coverage uses an explicit organizer
  threshold. Owners can set an optional plain-text tagline and select one of
  four high-contrast accent colors. Public rating, result, and certificate pages
  render only allowlisted color values and escaped tagline text. Published
  public results cache only aggregate data for five
  minutes and recheck the release flag on every request. Publishing an event
  issues immutable, UUID-verifiable certificates for unique category winners
  and the audience favorite; tied awards receive no winner certificate. The
  verification page identifies only the event, exhibit, award, issue date,
  and reference. Certificates remain verifiable as historical records if
  public results are later withdrawn. There is no event
  judge scoring identity/credential journey yet. Draft editing and event
  open/close/publish/revoke are owner/org scoped, row-locked and append an
  immutable audit record. Opening requires an exhibit and the model's consent
  guard. A draft-event owner can explicitly attest that required consent was
  collected, with a reason, timestamp, and append-only actor-linked record; the
  workspace does not collect minor names or contact details. This records the
  organizer's attestation, not independent guardian identity or proof of legally
  valid consent. The approved consent policy and release privacy review remain
  required.
  The route collects no visitor name or email. Visitor comments can contain
  personal data and still need moderation/retention policy before broader
  collection is approved. Device identity is a signed browser cookie; clearing
  the cookie can create a new identity and this is not hardware attestation.
- Team series, competency, and a link to a Team-scoped shared assessment
  session. The link validates service, owner, and organization; every future
  query must repeat these scopes. Closed demo sessions can collect one numeric
  usefulness rating per signed browser token. Only the token digest and score
  are stored; no learner, account, or comment is recorded. An owner-configured
  response floor suppresses both the aggregate and response count until met.
  The public feedback URL and browser token are capability credentials; the
  cookie is HttpOnly, SameSite=Lax, time-limited, and scoped to that URL. Clearing
  browser state can create another token, so this is not physical-device identity.
  The Team outcomes CSV is available only to the series owner in the active
  organization and Team route scope. It exports latest competency decisions,
  names/references, scores, thresholds, pass status, revisions, and timestamps;
  it omits learner email and assessor evidence and escapes formula-like text.
  Team certificates snapshot one passing result revision after the demo closes.
  A certificate UUID verifies the learner name/reference, series/demo names,
  competency, score/threshold, and issue time; email and assessor evidence are
  excluded. The immutable link reports when a later correction supersedes the
  certified result. Public certificate links are shareable identifiers and
  must be handled accordingly; responses use no-referrer, noindex, and no-store.
- Competition audience ballots are now linked to competition, round, and track.
  Public links use an unguessable competition UUID; a signed, expiring
  HttpOnly/SameSite cookie binds the browser token to that competition, round,
  and track. The database stores only the signed token's digest, and a database
  uniqueness constraint permits one ballot for the browser token per round and
  track. Rankings are anonymous and append-only. Organizer tallies use the
  owner/organization scoped operation and instant-runoff counter. Clearing
  browser state can create another voting token, so this is cookie-level
  uniqueness rather than hardware-backed device attestation. Invited judge
  scorecards store one immutable score per entry and only a token digest. An
  identical retry returns the prior submission receipt without duplicating
  scores or audit events; a changed retry is rejected. The receipt remains
  available through the invitation expiry unless the owner revokes it. The
  browser can queue an offline scorecard in tab-scoped `sessionStorage` and
  retry after connectivity returns. The queued payload is scoped by competition,
  invitation row, and round; it excludes the bearer and CSRF tokens and is
  removed after confirmed submission or when the tab closes. This requires
  browser JavaScript and does not survive closing the tab. The
  owner-scoped workspace creates competitions, tracks, and entries, starts a
  valid initial round, and enables judge scoring and audience ballots. Score
  tallying and score-only advancement are implemented when audience weight is
  zero. Combined judge/audience ranking, authorized public leaderboard reveal,
  and load evidence remain open; keep Competition closed.
  A printable eight-character audience join code is derived with keyed HMAC
  from the competition, latest round, and track rather than persisted. It
  changes on round transition. The resolver checks active rounds and rate
  limits invalid codes using the server-observed source address; the code is a
  public convenience address, not a voter identity or device credential.
  Owners can now request a private, live instant-runoff count for the latest
  round and track. It discloses candidate first-choice counts and any unresolved
  head-judge-required tie only in the owner-scoped workspace; it neither mixes
  audience totals with judge scores nor publishes or persists a result.

## Required controls before any service route is released

- Authenticate owners through DevPerf and scope every owner operation by
  organization and owner. Do not treat service access-choice rows as grants.
- Never expose examiner identity beside score data. Complete a route-by-route
  disclosure matrix and adversarial tests before Panel results are shown.
- Panel review now shows examiner comments without the invitation identity and
  permits owner redaction before sign-off. Redaction clears the comment from the
  score row, preserves the aggregate score, and appends a reasoned record with
  the original comment's SHA-256 digest, moderator, and time plus one audit
  event. The digest is evidence of the prior text, not a way to restore it.
  Reviewers should avoid putting personal data in moderation reasons. This is
  application-level removal; backups and database media retention still follow
  the platform's separate retention controls.
- Enforce conflict recusal server-side and audit each change exactly once.
- Keep Showcase open evaluators anonymous; accept only a verified event token,
  enforce event state and per-device uniqueness, and avoid collecting visitor
  identity. Do not log tokens or comments.
- Resolve the approved minor-consent policy before opening a Showcase event
  involving minors. A timestamp field by itself is not proof of valid consent.
- Keep Competition leaderboards hidden until authorized reveal; use explicit
  approved tie behavior and persist any head-judge decision.
- Ensure Team feedback is aggregated above an owner-approved privacy threshold.
- Add CSRF protection, rate limiting, input limits, CSP-compatible UI, audit,
  accessibility, and service-isolation tests for each future endpoint.
- Verify bearer invitation tokens are scrubbed from proxy, application,
  analytics, and error logs; configure ingress-level abuse limits for unknown
  links; establish durable attempt monitoring and approve the implemented
  first-browser binding and reissue policy before Panel invitations are sent
  to real examiners. Application-level
  short-lived cache throttles now cover both valid invitation digests and
  unknown-link source addresses. Email delivery is implemented but its provider
  and operational configuration still need verification.

## Evidence and limitations

The current focused suite validates organization/owner parent consistency,
hashed invitations, signed event-bound Showcase tokens, per-device uniqueness,
minor-consent timestamp gating, lifecycle timestamp validation, decimal
aggregation, ranked-choice tie deferral and persisted anonymous ballot tally,
advancement rules, Team trend inputs, and additive migration behavior. These
tests do not constitute independent security review, deployed isolation
evidence, abuse/load testing, privacy/legal approval, or service acceptance.
