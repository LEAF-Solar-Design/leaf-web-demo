# LEAF Platform product-event telemetry (contract of record)

Design of record: the P2 events layer (audit package
`C:/tmp/leaf-platform-telemetry/events_design.md`, 2026-07-30). This doc is
the CONTRACT both the emitters (this repo) and every dashboard query
(ops-dashboard) cite. Modeled on ops-dashboard's `TELEMETRY_STRUCTURE.md`.

## What this layer is, and is not

Product events add IDENTITY (tenant, user, session) to what the platform
already measures. They deliberately do NOT carry:
- reliability/cost counting (CloudWatch EMF `Leaf/Platform/APS` owns SLOs
  and alarms),
- billing numbers (`broker_usage_ledger` + `/api/usage` + the ops read-API
  are the money authority; BigQuery telemetry is directional),
- chat transcripts (deep per-turn forensics stay in `session_events` /
  `agent_ledger`).
Telemetry is loss-tolerant BY CONTRACT and can never break the product:
bounded queue, drop-oldest, kill switch, never-raise emitters.

## Storage

- Dataset `leaf_platform_analytics` (same GCP project the ops-dashboard
  reader already uses; sibling to `branch_analytics`). Dataset default
  table expiration: 425 days.
- Date-sharded tables `leaf_platform_events_YYYYMMDD`, wildcard-queried as
  `leaf_platform_events_*` (the exemplar's `_TABLE_SUFFIX` idioms port
  unchanged). Sharding, not native partitioning, on purpose.
- Promoted columns: `timestamp` (server-stamped), `event_type`,
  `event_name`, `tenant_id`, `tenant_kind` (guest/account/anon),
  `user_email` (NULLABLE; account only, from verified auth),
  `session_id`, `environment` (EVERY query floors on this),
  `app_version`, `labels` (JSON; ALL values strings).
- `schema_version` (currently "1") rides in `labels`. Additive-only within
  a version; a breaking change bumps it AND records a `_TABLE_SUFFIX`
  cutover floor here on day one.

## Envelope and naming

- `event_type` vocabulary: `custom_event | error | exception | page_view`.
- `event_name`: `domain.action`, lowercase snake, exactly one dot,
  past-tense verbs. Walls are always `<domain>.wall_hit` + `wall_kind`.
- Identity is SERVER-STAMPED from the verified principal, never trusted
  from a client payload. Guests never carry an email. No raw IPs, no
  drawing names/paths (ids only), no prompt text (`text_len` only), no
  tokens of any kind.
- Correlation contract: every `error`/`exception` event MUST carry `tool`
  when a tool flow is active; every run-lane event MUST carry `job_id`
  once one exists. Analytics quality depends on emitters honoring this.
- Client timestamps ride in labels as `client_ts`, clamped to +-24h of
  server time; the `timestamp` column is always the server clock.

## Ingest

- Server emitters: `telemetry_sink.emit(...)` in-process (bounded queue
  2000, batches of 250 or 2 s, BigQuery `insertAll` with day-table
  auto-create, drop-oldest on overflow, one stderr line per dropped
  batch).
- Client events: `POST /api/telemetry` `{schema_version, session_id,
  events:[{event_type, event_name, client_ts, labels}]}`, max 50 events /
  32 KB. Always answers `202 {accepted: n}` (validation drops, oversize,
  sink overflow, disabled sink included): the client can never observe a
  telemetry failure. Auth rides the platform's existing stack (Auth0
  bearer / guest HMAC / mock tenant); anonymous callers may send exactly
  the pre-auth allowlist (`gate.choice | site.demo_viewed | tour.started |
  auth.completed | client.exception`, the single list in
  `routers/telemetry.py`) behind a per-IP token bucket, recorded as tenant
  "anon".

## Config (staging first; prod rides the normal deploy train)

- `LEAF_TELEMETRY_DISABLED=1`: kill switch (mirrors `APS_EMF_DISABLED`).
- `GOOGLE_APPLICATION_CREDENTIALS_JSON`: insert-only service account for
  the ONE dataset (NEVER the ops-dashboard reader SA), via AWS Secrets
  Manager + terraform.
- `LEAF_TELEMETRY_DATASET` (default `leaf_platform_analytics`),
  `LEAF_TELEMETRY_TABLE_PREFIX` (default `leaf_platform_events_`),
  `BIGQUERY_PROJECT_ID` (defaults to the SA's project),
  `LEAF_METRICS_ENVIRONMENT` (staging/production; mandatory, shared
  dataset), `LEAF_APP_VERSION` (build sha).
- Dependency boundary: `google-cloud-bigquery` ships in the APP image only
  (`server/requirements-telemetry.txt`, installed by `Dockerfile.app`).
  The broker image stays SDK-free; `telemetry_sink` reports disabled where
  the SDK or credentials are absent.

## Events live in v1 (waves A + B)

Wave B (server emits, one per verified choke point):

| Event | Choke point | Labels |
|---|---|---|
| `agent.wall_hit` | `routers/sessions.py`: the TurnRejected response choke point PLUS the two walls that answer without one (TurnBusy 409, entitlement denial) | wall_kind (grant/llm_quota/llm_rate/busy/entitlement/unreachable/raw code), http_status, error_code |
| `agent.approval_decided` | `routers/agent.py decide_approval` (recorded + expired) | outcome (approved/denied/expired), turn_id, tool when recorded, decision_latency_ms when created_at known |
| `job.orphan_reaped` | `jobs._reap_orphans_once` (both branches) | job_id, reason (session_closed/stale_redispatched), tool, staleness_s when known |
| `grant.linked` / `grant.unlinked` | `routers/tenant.py` link/unlink success | kind ALLOWLISTED (oauth/api_key; harness response wins, else a valid requested kind, else omitted), NEVER the token |
| `drawing.uploaded` / `drawing.upload_rejected` | `routers/uploads.py upload_drawing` response wrapper (single choke point over every branch) | uploaded: drawing_id, minted_guest (the resolver's own minted flag), status; rejected: reason (size/quota/disabled/validation), http_status, error_code; identity is the RESOLVED tenant once resolution happened, "anon" only before it |
| `drawing.extraction_finished` | the exact terminal-transition commits only (pg ready commit, legacy ready marker write, a written=True `_mark_failed`); claim losers / purge races / replaced attempts emit nothing | drawing_id, ok, status (ready/failed), error_code when failed |
| `author.requested` | `routers/author.py author()` entry | mode, desc_len |
| `author.wall_hit` | customization gate deny + AuthorQuotaExceeded | wall_kind (entitlement/daily_quota) |
| `author.fallback_served` | templated-fallback branch (harness set but unreachable) | reason (exception type) |
| `author.staged` | `/internal/customization/staged` callback success | source=harness |
| `author.published` | `register()` service success | change_set_id |
| `author.rolled_back` | `rollback()` service success | change_set_id |
| `org.created` / `billing.tier_changed` / `org.offboarded` | `platform/api.py` (tier event only when the tier actually changed) | tier / from_tier+to_tier / status; tenant_id = org_id |

## Events live in v1 (wave A)

| Event | Source | Labels live today |
|---|---|---|
| `job.terminal` | server, `jobs.complete_callback` (both store modes) | job_id, status, tool, duration_ms (pg mode), error_code, attempts, execution_path |
| `agent.turn_completed` | server, `turn_runner._finalize_terminal` | turn_id, stop_reason, tools_called_n, usd_est, tokens_in, tokens_out; model / grant_kind / degraded ONLY when the usage wire supplies them (optional fields the sink drops when absent) |
| client ingest door | `POST /api/telemetry` | the five pre-auth events + any valid authed event; reserved envelope/identity keys are stripped from client labels, and `client.exception` is additionally held to its label schema for EVERY caller |

## Events live in v1 (wave C: the client tracker)

All client events ride `web/src/telemetry.js` (buffer 20/5s, one retry per
batch, pagehide beacon, kill switch `VITE_TELEMETRY_DISABLED=1`). Identity
is SERVER-stamped at the ingest door; the client only ever sends names,
labels, and its browser-session UUID. While the guided tour is active every
organic event additionally carries a `tour_step` label (the tour rides the
real handlers; there are no tour.* duplicates of product events).

C-1 (merged #427):

| Event | Choke point | Labels |
|---|---|---|
| `session.started` | `api.getSession`, AFTER the session resolved (both branches) | mock, tier (live), catalog_source (default/custom, never the raw dwg) |
| `prompt.submitted` | `createCatalogController.dispatch` (THE active path) | input_kind (slash/typed), text_len |
| `prompt.routed` | `api.nlPrompt` both outcomes | lane, tool, stub, confidence_bucket, alternatives_n |
| `run.confirm_shown` | `App.armDecision` (2s same-tool de-dupe over the tour double-arm) | tool, is_write, source (prompt/slash/catalog/tour/agent) |
| `error.shown` | the two transport seams: `api.http()`, `converse.tagged()`; cap 20/session | http_status, error_code, endpoint_class |
| `agent.stream_down` | `converse.onStreamDown`; cap 10/session | reconnects_n |
| `client.exception` | ErrorBoundary; UNCAPPED (React bounds it) | message_class, component_stack_hash |

C-3 (global error capture):

| Event | Choke point | Labels |
|---|---|---|
| `client.exception` | `window` `error` + `unhandledrejection` listeners, installed by importing `telemetry.js` (first import in `main.jsx`); 10 per session | source (window.onerror/unhandledrejection), message_class, message_hash, stack_hash, route, ua_class |

The boundary only sees what a component throws DURING RENDER, so a three.js
draw tick, a `setTimeout` callback, and every unawaited promise failed with
nothing recorded: the server answered a healthy 200 for a page that was dead
in the browser. These handlers close that gap under the SAME event name,
distinguished by `source` — a second event name would need its own allowlist
entry, dashboard row, and docs for no added meaning.

### No free text leaves the browser

Every label is structural. There is no free-text field, and that is a
correction made under review, not the original design:

- `message_hash` is a stable ~53-bit digest (cyrb53) of the exception message. The
  message itself never leaves the browser. Two independent adversarial
  reviews of the first attempt each found fresh PII surviving a redactor -- a
  bare name in a path (`/app/alice/settings`), a quoted name, a Windows path,
  an IP literal, an all-letter id like `deadbeef` -- and every pattern strong
  enough to catch them also destroyed the ordinary diagnostics the label
  existed for. A denylist over free text is an arms race, and losing it once
  puts customer data in BigQuery permanently. The hash is the convention this
  codebase already settled on for the same tension: the ErrorBoundary has
  always emitted `message_class` + `component_stack_hash` and no raw text.
- `message_class` is accepted only if it is one of the PLATFORM's own error
  names (the ECMAScript set plus the DOMException names). Checking the SHAPE
  is not enough: `name` is an ordinary writable property, so
  `Promise.reject({name: 'AliceSmith'})` is a legal rejection any visitor can
  produce and an identifier-shaped check passes it. Anything else is `Other`.
- `stack_hash` is a digest of the FIRST frame, never its text. It was
  `fn@file:line:col` built from parts, and that was the one label still
  exporting caller-controlled text: the frame regexes checked SHAPE, not
  provenance, so `Promise.reject({stack: 'at AliceSmith (index.js:1:2)'})`
  put `AliceSmith` into a label. Keeping it would have made the guarantee on
  this page FALSE, which is worse than an honest best-effort because
  consumers build on the claim.
- `route` is the app's own scene name (`site`/`tool`/`sheets`/`app` from
  `site/routeScene.js`), never the pathname. There is no redactor that
  reliably tells a customer name from a route word, so the label is an enum by
  construction.
- `ua_class` is a browser family plus mobile/desktop, never the raw
  user-agent string. The release marker is the sink's server-stamped
  `app_version`; the client does not send one.

A digest of a string that MAY have carried personal data is PSEUDONYMOUS,
not anonymous: anyone with BigQuery access and a candidate list can hash the
candidates and compare. That limit is real and deliberate. It is still
strictly better than shipping the text, and BigQuery access is already
privileged and already sees the row's server-stamped tenant.

What this costs is reading the message at a glance. That is the right price
for a guarantee instead of a best effort. If a specific message ever needs to
be readable, the honest way to add it is a server-side allowlist of known
engine strings, not a client-side denylist over arbitrary text.

### The door holds EVERY caller to the schema

The guarantees above are properties of the BROWSER half. The door is where
the data actually lands, and it applied only generic bounding, so a direct
POST could put `message_class: "AliceSmith"` or an invented `raw_secret` key
into a `client.exception` row. That was true of the whole pre-auth allowlist
already, but this is the event whose contract claims structural labels, so
the claim was only ever true of rows the real client sent.

`routers/telemetry.py` now validates `client.exception` labels against
`PREAUTH_LABEL_SCHEMAS` for EVERY caller, not only anonymous ones.
Authentication proves identity, not label provenance: a bearer holder or a
self-mintable guest can POST free text exactly as a stranger can. Unknown
keys are dropped, every value must match its rule, and a class outside the
allowlist degrades to `Other` rather than travelling.

The enumerated labels are CLOSED SETS, not shapes. A shape rule is what
accepted `alicesmith/desktop` as a `ua_class`, which is the same
shape-not-provenance failure as an identifier-shaped class. Digests are
FIXED WIDTH (16 characters) for the same reason: the door cannot prove a
value came from the hash function, but requiring exactly one digest's width
means the field's capacity is a digest and not a sentence — a variable-width
decimal accepted `5550142`, which is a phone number.

One label has a compatibility window, and it has an expiry.
`component_stack_hash` accepts BOTH 16 digits and the 1-to-10-digit decimal
the pre-#537 ErrorBoundary emitted (`String(hash >>> 0)` from its own 32-bit
shift-hash). It is the only label a client older than this release already
sends, and a tab opened before the deploy keeps sending the old width for as
long as it lives; a 16-digit-only rule accepts those rows and strips their
ONLY stack fingerprint, on exactly the event that reports crashes, for the
whole rollout. `message_hash` and `stack_hash` are new here, so no stale
client can emit them and they keep the strict rule. The legacy branch comes
out once no pre-#537 bundle can still be running — one browser session's
lifetime after the release is everywhere is the honest bar — and
`test_component_stack_hash_accepts_both_client_versions` fails loudly when it
does, so removing it is a deliberate act rather than a drift.

`tour_step` is deliberately absent from the schema. Closing it would need a
mirror of two product-owned step tables that change with ordinary feature
work, so it would drift by design, and a shape rule would accept
`customer_secret`. It is marginal on a row that already carries `route` and
both digests.

Two costs, taken deliberately. A new label on THIS event needs a server
release, unlike every other event, which keeps the generic additive
contract. And the class list is a hand-maintained mirror of `KNOWN_CLASSES`
in `web/src/telemetry.js` — drift is safe-by-default (an unlisted class
degrades rather than travelling) but silent, so
`server/tests/test_client_exception_vocab_freeze.py` enforces it, in the
same spirit as the auth vocab freeze.

### Caps

ONE budget, and it belongs to the NEW path only: 10 per session for the global
handlers. A stray callback can throw thousands of times a minute with nothing
to stop it, so an uncapped global emitter would spend the door's whole token
bucket on one broken page.

The ErrorBoundary stays UNCAPPED, exactly as it was before global capture
existed. Capping it at 5 looked like symmetry and was a regression: the
boundary fires once per crash and the card it renders invites a reload, so
five crash-and-reload cycles in one browser session would have silenced every
LATER production crash on that tab — and the crash a user hits after already
hitting several is the one most worth seeing. React bounds that emitter's
volume; we do not have to.

The global budget counts OCCURRENCES, not distinct failures: there is no
same-signature de-duplication and no slot refund. A loud repeating error
class — a ResizeObserver loop from the resizable panels, an animation
callback throwing every frame from the viewer — can consume the whole
per-session budget before a genuinely different crash gets a slot. That is an
accepted tradeoff, not an oversight: cross-emitter de-duplication was tried
and proved unsolvable on the signals the client actually has (see "One row
per React crash" below), and a same-signature suppression built on those same
signals carries the identical risk — silently collapsing two genuinely
distinct failures that happen to share a class, message, and frame into one
dropped row.

Resource-load failures (a 404 `<img>`, a blocked `<script>`) dispatch an
event with neither `error` nor `message` and are ignored: they are not JS
exceptions, and they arrive in bursts.

### One row per React crash, in production; two, in development

There is NO client-side de-duplication. The global handler
(`window.onerror` / `unhandledrejection`) and the ErrorBoundary each call
`trackException` independently, with no shared key and no coordination
between them.

In PRODUCTION, a React crash records exactly ONE row: the boundary's
(`component_stack_hash`, no `source`). React's production build catches a
render error with a plain try/catch, so the error never reaches
`window.onerror` — there is nothing for the global handler to see.

In DEVELOPMENT, the same crash can record TWO rows. React 18's development
build re-throws a render error through a synthetic DOM event so DevTools can
observe it, which reaches the global handler as well. Measured in
`web/src/ErrorBoundary.test.jsx`: a real component, a real render failure,
both rows land. This is an accepted, documented artifact of dev-serving or
running tests locally; it does not happen in production and does not affect
production telemetry.

WHY THERE IS NO DE-DUPLICATION: three rounds of client-side timing
(emit-then-retract, then hold-then-release-on-flush-or-pagehide) were tried
and each made the row count depend on how busy the page was — a batch flush
or a `pagehide` landing between the two emits committed one row before the
other could suppress or release it, so the guarantee held on a quiet page and
broke on a busy one.

A fourth attempt gave both emitters a shared occurrence key: a digest of the
failure's signature (message class, message digest, first-frame digest,
route), memoized on the Error object so two emitters reporting the SAME
object would agree on it. It depended on an assumption that does not hold —
that DEV React's re-throw hands `window.onerror` and `componentDidCatch` the
SAME Error object instance. A probe against the real ErrorBoundary (a real
component, a real render failure, no mocking) disproved it: DEV React calls
the throwing render function more than once for one logical crash, so the two
handlers routinely observe two DIFFERENT Error instances with identical
message, class, and stack. With no reliable object identity, the key fell
back to matching on signature alone within a coarse time window, which
silently merged two genuinely DISTINCT same-signature crashes into one row —
a worse defect than the duplicate it was built to remove, because it is data
loss rather than a cosmetic double-count. Both failure modes were measured,
not assumed: the timing breaks and the object-identity mismatch each have a
reproducing case in the history of `web/src/telemetry.js`.

So the client stopped trying. A consumer counting crashes should filter on
`source`: its absence is the boundary's row, its presence is the global
handler's, and in production there is only ever one row to find.

### Surfaces still uncovered

- `deploy/presenter-flipbook.html` ships its own inline script and never
  loads `main.jsx`, so failures there stay invisible.
- A failure to fetch, parse, or link `main.jsx` itself happens before any
  module evaluates. Nothing in-band can report it.

C-2 (this change):

| Event | Choke point | Labels |
|---|---|---|
| `gate.choice` | SignedOutGate's two buttons (pre-auth allowlisted) | choice (demo/sign_in) |
| `auth.completed` | `auth.handleRedirectCallback`, BOTH branches (the failure branch was invisible before); flushed immediately via keepalive fetch ahead of the post-auth reload, and pre-auth allowlisted because the failure branch never has a bearer | ok, first_time (browser-local: this browser never completed sign-in) |
| `route.outcome` | the controller's own route-clearing transitions (no caller can double-count): run started (accepted/invalidated by tool match), typed over (invalidated), explicit dismiss, alternative picked | outcome (accepted/alternative_picked/dismissed/invalidated), tool |
| `run.confirmed` | `App.onConfirmCatalogRun`, the one armed-intent-to-run path | tool, source + ms_since_shown (from the confirm_shown record; omitted, never guessed, if it names another tool) |
| `run.wall_hit` | `api.runToolAsync` at the exact branches the UI classifies (403 entitlement, 429 daily quota, quota_exceeded pass-through = 402 spend cap) | wall_kind (entitlement/daily_quota/spend_cap), tool, tier when present |
| `run.interrupted` | the Esc ladder's running rung (the one interrupt gesture) | tool, elapsed_ms |
| `run.reattached` | `useJobController` boot re-attach, ONLY for a still-running job (the only reattach:true site) | from (boot), job_age_s (from the inflight pointer's own ts) |
| `agent.job_linked` | `App.onAttachAgentJob` | tool |
| `tour.started` / `tour.step_reached` / `tour.exited` | tour deep-link mount effect + the entry button / DemoTour index changes / `App.onTourExit` | entry (deeplink/button) / step_id / at_step, completed |
| `site.demo_viewed` | `site/intakeCache.loadDemoSolve` resolve (memoized: one per session; pre-auth allowlisted) | live_or_fallback (the loader's own degraded flag) |
| `degraded.shown` | DegradedBanner mount | source (workspace/toolcast), never the free-text reason |
| `drawing.version_navigated` | undo/redo success, History open, preview click | action (undo/redo/history/preview) |

Only the pre-auth allowlist (`gate.choice`, `site.demo_viewed`,
`tour.started`, `auth.completed`, `client.exception`) is accepted
anonymously at the ingest door; every other client event identifies like any
API call (stub tenant off-auth, verified identity on). `client.exception` is
on that list for the reason `auth.completed` is: it fires on marketing and
sign-in pages where no principal exists yet, so requiring one would drop
exactly the failures nothing else can see. The anonymous exposure it adds is
the shape `gate.choice` already carries, bounded by the same per-IP token
bucket (burst 30, 0.5/s), the 50-events-per-body cap, the 512-char label
cap, and the client's own 10-per-session cap on the global handlers (the
boundary path is uncapped, and React bounds it instead).

That pre-auth lane has a measured denial-of-wallet ceiling, unchanged by
`client.exception` because the bucket and the label caps are name-agnostic:
one IP sustains 30 burst + 43,200 refill events/day, and a maximum legal
event is ~23 KB, so ~1 GB/day of row payload per IP (~$0.05/day streaming
insert, ~$0.60/GB-month storage). Client-side session caps do not constrain
an internet caller, and IP rotation multiplies it. Accepted as pre-existing,
recorded here so the next change to that allowlist starts from the number
rather than rediscovering it.
## Reading the data: identity is stamped at FLUSH time

A client event's identity is resolved when its BATCH reaches the ingest
door, not when the event happened. The tracker buffers 20 events / 5 s and
reads its auth header at flush; a sign-in inside that window therefore
back-stamps earlier events with the identity the browser acquired
afterwards.

This is not a bug and it is not fixable at the door (the door can only see
the credential it is given), but it MISREADS in exactly one direction, so
read every client row with it in mind:

- Signed-out errors can arrive as `tenant_kind=account`. Observed live
  2026-08-17: 112 of 200 events were `error.shown` 401 UNAUTHENTICATED
  across 8 endpoints x 11 sessions, all stamped `account`, which reads as
  "authenticated users are being rejected". They were not. Per session the
  8 rows are the signed-out boot fan-out (`/api/session`, `/api/tools`,
  `/api/capabilities`, `/api/drawings`, `/api/jobs`, `/api/tenant`,
  `/api/entitlements`, `/api/usage`), and `auth.completed` lands AFTER the
  last of them; 16-error sessions are two such boots.
- The discriminator is ordering, not the identity column: compare an
  error's timestamp against the session's `auth.completed`. Errors before
  it are the expected signed-out posture under `LEAF_AUTH_LIVE=1`; errors
  after it are real.
- Server-side emits are unaffected: they stamp identity in-process at the
  choke point, with no buffer between the fact and the stamp.

A second reading, independent of identity: a signed-out boot spends 8
user-visible errors before the user does anything, which is why one
afternoon's dominant event was `error.shown`. That is a product question
(should the boot fan-out defer without a token?), not a telemetry defect,
and the layer surfacing it is the layer working.

Labels are additive within schema_version 1, so early rows stay queryable.
Fields named by the design but not yet stamped (e.g. `user_email`,
`aps_live`, `wrote_version`) arrive additively with later waves.

## Verification (binary, per the design)

After deploy: send one canary event and read it back from
`leaf_platform_events_*`; then break the SA credential in staging and
confirm every product path still answers 2xx while only the sink's stderr
drop line changes.

Both checks EXECUTED on staging 2026-08-04, all binary results green:

- Canary read-back: POST /api/telemetry answered 202 and the row landed in
  `leaf_platform_events_20260804` (event gate.choice, environment staging,
  labels readable via JSON_VALUE).
- Broken-SA drill (key-disable at the GCP end, no AWS or config change).
  Paths tested through the broken window: POST /api/telemetry answered 202,
  GET /api/health answered 200, and unauthenticated GET /api/session and
  GET /api/tools answered 401 identically before, during, and after (the
  standing LEAF_AUTH_LIVE=1 posture, not a drill effect). On those served
  responses nothing changed; the two observable effects were exactly the
  designed ones: the sink's stderr line
  `[leaf-telemetry] flush dropped N event(s): RefreshError: invalid_grant`
  and the dropped events' verified absence from BigQuery. After
  `gcloud iam service-accounts keys enable`, the very next flush recovered
  without a restart.
- Fuse caveat for future drills: disabling the SA key does NOT break a
  running sink immediately. The google-auth client holds a cached access
  token for up to an hour, and inserts keep succeeding until the next token
  refresh (observed live: three post-disable events still landed). Rolling
  the service surfaces the break on the replacement task's first flushed
  batch, since a fresh process must mint a fresh token (old tasks may keep
  their cached token until the rollout drains them); the roll also shows
  that task launch does not depend on GCP credential validity.
