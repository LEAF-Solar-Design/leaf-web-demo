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
  `gate.choice | site.demo_viewed | tour.started` behind a per-IP token
  bucket, recorded as tenant "anon".

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
| client ingest door | `POST /api/telemetry` | pre-auth trio + any valid authed event; reserved envelope/identity keys are stripped from client labels |

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
| `client.exception` | ErrorBoundary | message_class, component_stack_hash |

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
`tour.started`, `auth.completed`) is accepted anonymously at the ingest
door; every other client event identifies like any API call (stub tenant
off-auth, verified identity on).
Labels are additive within schema_version 1, so early rows stay queryable.
Fields named by the design but not yet stamped (e.g. `user_email`,
`aps_live`, `wrote_version`) arrive additively with later waves.

## Verification (binary, per the design)

After deploy: send one canary event and read it back from
`leaf_platform_events_*`; then break the SA credential in staging and
confirm every product path still answers 2xx while only the sink's stderr
drop line changes.
