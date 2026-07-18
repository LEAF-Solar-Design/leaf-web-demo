# CONTRACT ADDENDUM — backend backbone (proposed §7–§10)

Proposed extensions to the frozen `contract/CONTRACT.md` (§1–§6 untouched).
Lives in `server/` because this lane must not edit `contract/`. **Promoting
these sections into the frozen contract is an OPERATOR action.**

Session: `async-broker-catalog-envelopes`, 2026-07-17. All shipped behavior
below is verified by `server/tests/test_backbone.py` at `APS_LIVE=0`.

---

> **PROMOTED 2026-07-17:** sections 7-10 below were promoted verbatim into the frozen
> `contract/CONTRACT.md` (operator-approved). This file remains the authoring record;
> `contract/CONTRACT.md` is now authoritative for these sections.

## §7 Async job model

### BREAKING CHANGE — `POST /api/run`

`POST /api/run` no longer blocks and no longer returns the §3 envelope
directly. It now returns **HTTP 202** immediately (<200 ms):

```json
{ "job_id": "<uuid4>", "status": "submitted", "error": null, "degraded_mode": false }
```

Frontends must consume `{job_id}` and poll/stream below. **Back-compat:**
`POST /api/run?wait=1` blocks and returns the final §3 envelope (old smoke
path; also the test convenience). Operator ratified this contract shape
(202 async + `?wait=1` sync convenience) before this session was dispatched.

### Job lifecycle

`submitted → running → complete | failed`. Durable in SQLite at
`server/jobs.db` (env `JOBS_DB`) — records survive a full app-process restart,
so a closed tab can reconnect and poll. Worker pool: `JOB_WORKERS` (default 4)
threads. Heartbeat: `updated_at` refreshed ~1 s while running. Timeout:
`JOB_MAX_S` (default **540**, env-overridable) → `status:"failed"` with
`error.error_code:"TIMEOUT"`; the underlying broker call is abandoned
best-effort (its HTTP timeout reaps the worker thread; no APS WorkItem cancel
in v1). Orphan reaper: jobs whose heartbeat is staler than
`HEARTBEAT_STALE_S` (default 60) are marked `failed` /
`INTERNAL "orphaned: heartbeat stale"` — the hook future sessions extend for
APS WorkItem reaping.

### Endpoints

| Method | Path | Behaviour |
|---|---|---|
| POST | `/api/run` | `{tool, params, dwg}` + `X-Tenant-Id` header (default `demo-tenant`) → **202** `{job_id, status:"submitted"}` |
| POST | `/api/run?wait=1` | blocks; final §3 envelope (back-compat) |
| GET | `/api/jobs/{job_id}` | full job record; §3 envelope in `result` when complete, job `error` object when failed |
| GET | `/api/jobs/{job_id}/stream` | SSE (`text/event-stream`): one event per (status, progress) transition, closes after terminal |
| GET | `/api/jobs?tenant_id=&limit=` | recent jobs (reconnect-after-tab-close) |

Job record fields: `job_id, tenant_id, tool, params, dwg, status, progress,
created_at, started_at, updated_at, finished_at, elapsed_ms, result, error,
degraded_mode`.

## §8 APS broker boundary

**Security property (tested):** the tenant-facing app process NEVER holds the
APS credential. `server/broker.py` is a separate process (default `:8140`, env
`BROKER_PORT`) and is the ONLY code that loads `da/client.py` / can read
`~/.aps/credentials.json`. `app.py`/`jobs.py` contain no `da` import (static
test) and run correctly with `APS_CRED=/nonexistent` (dynamic test). The app
reaches execution ONLY via `server/broker_client.py` → HTTP (`BROKER_URL`,
default `http://127.0.0.1:8140`); broker down → `BROKER_UNREACHABLE`.

| Method | Path | Behaviour |
|---|---|---|
| POST | `/broker/run` | `{tenant_id, tool, params, dwg, aps_live}` → extended §3 envelope. `aps_live:false` → pure-python mock path; `true` → `da.client.run_tool` (live) |
| POST | `/broker/tenants/{tid}/disable` / `.../enable` | per-tenant kill-switch, persisted to `broker_tenants.json` (env `BROKER_TENANTS`). Disabled tenant → `TENANT_DISABLED` (retryable:false), APS never touched |
| GET | `/broker/health` | role, ledger path, disabled tenants |

**Attribution ledger** (metering/quota chokepoint other sessions read): every
`/broker/run` — including kill-switch denials — appends exactly ONE JSONL line
to `server/broker_ledger.jsonl` (env `BROKER_LEDGER`):
`{ts, tenant_id, tool, engine_op, aps_endpoint, aps_live, engine_seconds, usd_est, status}`.

**Egress allowlist (v1, in-process):** every outbound HTTP request from the
broker process passes a central guard (patched `requests` adapter). Allowed:
`developer.api.autodesk.com` + `*.amazonaws.com` (OSS direct-to-S3 signed
URLs used by the frozen §5 client) + `BROKER_EGRESS_EXTRA` env. Anything else
raises `EgressBlocked`, so tenant-authored `engine_op`s cannot redirect
egress. Network-layer enforcement (container/proxy) is another session.

**v1 assumptions:** the demo is single-process, so "tenant container" ==
"the app process"; `tenant_id` arrives as an `X-Tenant-Id` header stub until
the auth session lands. `GET /api/session` at `APS_LIVE=1` still extracts
in-process via Lane A's client (legacy, pre-broker) — migrating extract
through the broker belongs to the extract/provisioning session. The mock path
honors `params._qa_sleep_s` (capped 30 s) as a QA latency-simulation hook.

## §9 Capability catalog

`GET /api/capabilities` groups tools into families with server-side filtering:

```json
{ "families": [ { "family_id", "label", "description",
    "capabilities": [ { "name", "version", "description", "params_schema",
                        "capabilities", "provenance" } ] } ],
  "error": null, "degraded_mode": false }
```

Config: `server/capability_families.json` (family labels, tool→family map,
filter rules, seeded QA tools). A tool is INTERNAL iff `"internal": true` in
its package OR its name matches a QA prefix (`qa-`, `_`). Internal tools are
FILTERED OUT server-side and returned only with header `X-Internal-Role: qa`
(role header is a stub until real auth lands). Authored tools appear in family
`custom` unless mapped. `GET /api/tools` stays the flat back-compat list (and
never includes catalog-seeded QA tools).

## §10 Extended error/degraded envelope

Every server response body (app AND broker) carries at minimum:

```json
{ "error": null | { "error_code", "message", "retryable" }, "degraded_mode": false }
```

- `error_code` enum (frozen): `UNKNOWN_TOOL, BAD_PARAMS, APS_UNAVAILABLE,
  BROKER_UNREACHABLE, WORKITEM_FAILED, TIMEOUT, TENANT_DISABLED, INTERNAL`.
- The §3 run envelope keeps all existing success fields and ADDS
  `degraded_mode`; other bodies (`/api/session`, `/api/tools`, `/api/author`,
  `/api/health`, jobs, capabilities) are extended ADDITIVELY — existing keys
  unchanged.
- `degraded_mode: true` ⇔ APS_LIVE execution was requested but the run fell
  back to the pure-python path (UI: "used the local fallback, not the cloud
  solver").
- HTTP status codes stay sane (404 unknown tool, 403 disabled tenant, 502
  broker/APS, 504 timeout…); the machine-readable part is the body.
  Validation errors (422) are also enveloped (`BAD_PARAMS`).
- Machine-checkable schema: `server/envelope_schema.json` (does not touch
  Lane B's `engine/envelope_schema.json`).

## §11 Drawing write loop (versioned edits + undo/redo)

Session: `m2-write-loop-backend`, 2026-07-18. Wires the proven `drawing.write`
capability into the product loop: a registered `drawing.write` tool run produces
a NEW immutable drawing version in the versioned store (`da/store.py`), with
undo/redo, working offline (`APS_LIVE=0`) and live (`APS_LIVE=1`). Verified by
`server/tests/test_write_loop.py` (offline) and `data/write_loop_receipt.json`
(one live loop: v1 ingest → v2 write → undo, `pass:true`, $0.0163, 3 WorkItems).

### Additive §3 envelope field

A successful `drawing.write` run's §3 envelope carries, inside `result`:

```json
"new_version": { "drawing_id": "demo", "version": 2, "parent": 1 }
```

This is ADDITIVE — the 8 core §3 keys are unchanged; read tools never carry it.
The mock envelope's `result` also carries `mutations` / `deleted_handle` /
`added_marker_handle` (tool-specific data).

### Endpoints (`server/routers/drawings.py`, envelope-wrapped per §10)

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/drawings/{drawing_id}/intake?version=head\|latest\|<n>` | `{intake, version, head, latest}` — the intake for the resolved version |
| POST | `/api/drawings/{drawing_id}/undo` | `{version, head, latest, intake}` — repoints head to its parent (objects never deleted → redo stays possible) |
| POST | `/api/drawings/{drawing_id}/redo` | `{version, head, latest, intake}` — re-advances head one step toward `latest` along the parent chain |

`undo` at the root version and `redo` when `head==latest` return a clean
`BAD_PARAMS` (HTTP 400); an unknown drawing/version returns `BAD_PARAMS` (404).
Tenant identity is the usual `X-Tenant-Id` stub (or the live JWT claim); the
store is per-tenant, per-drawing.

### Version-payload representation (read this — it is honest, not DWG-in-mock)

The store is a generic versioned blob store; a version's `…/v/NNNNNNNN.dwg` key
holds different bytes by mode:

- **`APS_LIVE=0` (mock):** the version blob IS the intake JSON (not DWG bytes).
  The `.dwg` key extension is the store's fixed key scheme, not a claim about the
  content. `read_intake` reads that blob directly.
- **`APS_LIVE=1` (live):** the version blob is real DWG bytes; a sibling
  `…/v/NNNNNNNN.intake.json` cache key holds the re-extracted intake, and
  `read_intake` prefers that cache (falling back to the blob-as-JSON for mock).

### Mock write semantics (`APS_LIVE=0`)

A write tool's `run(intake, params) -> (result, overlay)` is a PURE function that
DECLARES its edit as `result["mutations"] = {"added": [<intake entities>],
"removed": [<handles>]}`. The execution chain (`server/write_loop.py`) applies
those to the CURRENT version's intake → new intake → `store.put_drawing`
(parent = head) → stamps `result.new_version`. At `APS_LIVE=1` the chain instead
runs the proven `LeafWriteProbe+prod` Activity (HostDwg = current version's DWG,
Result = `output.dwg`), stores `output.dwg` via `put_drawing`, re-extracts for
the intake cache — same envelope shape.

### Demo bootstrap + registration

- Well-known `drawing_id` **`demo`** bootstraps on first use at `APS_LIVE=0`: its
  v1 payload is the cached `data/rooftop_demo.intake.json`, backed by a LOCAL
  `FilesystemBackend` rooted at `server/drawings/` (gitignored; env
  `LEAF_STORE_DIR` overrides so the app + broker share an isolated dir).
- Shipped write tool: **`delete-marked-panel`** (`capabilities:["drawing.write"]`,
  file `server/builtins/delete_marked_panel.py`) — mock: removes one polyline by
  `handle` (default = first on `layer`, default `Panels`) and adds a marker
  polyline on `LEAF_WRITE_PROBE`; live: reuses `LeafWriteProbe+prod`. Registered
  via the tracked, server-lane seed `server/write_tools.json` (folded into
  `deps.all_tools()` additively; read tools stay in `engine/registry.json`,
  authored tools in the gitignored `authored_tools.json`).

### Where the write branch hooks in

`server/broker.py::_execute` — right after params pre-validation, a
`write_loop.is_write_tool(tool)` guard delegates to
`write_loop.run_write_mock` (or `run_write_live` at `APS_LIVE=1`). Read tools do
NOT match and take the unchanged live/mock paths, so the read backbone is
byte-identical.

### v1 credential-boundary note

At `APS_LIVE=0` the drawing endpoints use the `FilesystemBackend` and read NO APS
credential (the tested condition; the app still runs with `APS_CRED=/nonexistent`).
At `APS_LIVE=1` the app-side drawing reads use `OSSBackend` directly; promoting
those reads through the broker (to keep strict credential isolation at live too,
matching §8) is a documented follow-up. `da/store.py` gained an additive `redo`
primitive and a `FilesystemBackend`; `da/client.py` is unchanged.

## §12 NL prompt router (one prompt box → lanes)

Session: `m3-nl-prompt-router`, 2026-07-17. MATRIX frontend gap #2: the
product's single prompt box dispatches across the **Run / Solve / Build** lanes.
This section adds the backend classifier the frontend lane builds against.
Verified by `server/tests/test_nl_router.py` (offline: no APS, no LLM, no
subprocess).

### Endpoint

| Method | Path | Behaviour |
|---|---|---|
| POST | `/api/nl-prompt` | `{text}` → §10-enveloped `{lane, tool, params, confidence, rationale}` |

```json
{ "lane": "run" | "solve" | "build",
  "tool": "<registered tool name>" | null,
  "params": { },
  "confidence": 0.0,
  "rationale": "<one calm, user-visible sentence>",
  "error": null, "degraded_mode": false }
```

Empty/whitespace `text` → `BAD_PARAMS` (HTTP 400); missing `text` →
`BAD_PARAMS` (422, enveloped via the app validation handler). No tenant/auth
dependency — classification is read-only and side-effect-free.

### Matching (v1 is deterministic, ZERO-LLM)

Resolves the MATRIX "LLM in the request hot path vs zero-LLM runtime" tension in
favour of the **zero-LLM runtime**: no model runs per request. `nl_router.classify`
is a pure function over the **live** catalog (`deps.all_tools()` at request time —
authored + write tools included, internal/QA tools filtered exactly as
`/api/capabilities` does).

- **Scoring** — normalised token/synonym overlap between the prompt and each
  tool's name / engine_op / description / param names / capabilities. The tool
  **name is weighted 3×** the other fields, so grouping intent ("count panels
  **per layer**" → `count-by-layer`) beats a shallower panel-name match
  (`count-panels`). Confidence is calibrated: exact name phrase ≈ 0.97, strong
  overlap 0.7–0.9, weak ≤ 0.5; an unmatched prompt returns 0.10 with `tool:null`
  (never a fake high score).
- **Lane rules** — authoring verbs + a tool noun, or "a tool that/to …" →
  **build** (`tool:null`, `params:{description:<original text>}`); explicit
  optimise/solve verbs → **solve** (`tool:null`; rationale states the Solve lane
  is future and nothing runs); otherwise the best catalog match → **run** with
  prefilled params. Precedence: build → solve → run.
- **Param extraction** — numbers with/without units fill the matched tool's
  numeric params in schema order (e.g. `distance=200`); a hex-ish token fills a
  `handle` param as a **string** (e.g. `handle="9462"`), so DWG handles never get
  coerced to ints.

### LLM-classifier seam (explicit, OFF in v1)

`classify(text, tools, llm_classifier=None)` is the documented forward seam. A
future lane may inject `llm_classifier(text, tools, deterministic) -> dict|None`;
it is consulted **only** when the deterministic confidence is below
`LLM_ESCALATION_CONF` (0.55) **and** a classifier was passed, and a broken
classifier can never take down routing (exceptions fall back to deterministic).
v1 always passes `None`, so the zero-LLM runtime is the default and the LLM never
enters the hot path unless a caller explicitly opts a request in. Ownership:
`server/nl_router.py` (pure logic), `server/routers/prompt.py` (endpoint), one
`include_router` line in `server/app.py`.

## §13 UI wave 1 — spend meter, tier echo, version history

Session: `ui-wave-1-backend`, 2026-07-18. Three additive reads the exposure map
(`docs/backend-frontend-exposure-map.md`) declared ready-to-wrap, built against
root-frozen shapes a sibling frontend lane consumes. All verified offline
(`APS_LIVE=0`) by `server/tests/test_ui_wave.py`; no live APS, no LLM.

### GET /api/usage — per-tenant spend / quota meter

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/usage` | `X-Tenant-Id` stub (default `demo-tenant`) or the live JWT `TenantContext` → §10-enveloped usage summary |

```json
{ "tenant_id": "demo-tenant",
  "today": { "runs": 0, "usd_est": 0.0 },
  "total": { "runs": 0, "usd_est": 0.0 },
  "cap":   { "usd_cap": null, "remaining": null, "enabled": false },
  "updated_at": "<UTC ISO-8601>",
  "error": null, "degraded_mode": false }
```

- **Source = the broker attribution ledger**, aggregated app-side per tenant via
  `da/usage.py::aggregate_usage(tenant, ledger)`. `today` is the **UTC-date**
  bucket of each line's `ts`; `total` is lifetime. A line counts as a run iff its
  `tenant_id` matches and its `status` is NOT a pre-flight denial
  (`quota_exceeded` / `TENANT_DISABLED`) — a denied run never touched APS and
  never spent. `usd_est` sums only numeric `usd_est` (a mock `APS_LIVE=0` run with
  `usd_est:null` counts as a run, adds $0), so
  `total.usd_est == usage.spent_from_broker_ledger(...)`.
- **Ledger read app-side is deliberate and safe.** The ledger
  (`server/broker_ledger.jsonl`) holds **no credential** — it IS the metering
  surface — so the tenant-facing app process reads it directly rather than adding
  a broker round-trip. Path resolved at request time:
  `LEAF_USAGE_LEDGER` > `BROKER_LEDGER` (point the app at the broker's file) >
  default `server/broker_ledger.jsonl`. A **missing/empty/corrupt ledger yields
  zeros — never an error.**
- **Cap fields** reflect `da/usage.py::cap_for(tenant)` (the same configured cap
  the broker's hard pre-flight gate enforces): caps are **OFF by default**, so
  `enabled:false` with `usd_cap:null` / `remaining:null` on a demo. When a cap is
  configured (`LEAF_TENANT_CAP_USD` / `LEAF_USAGE_CAPS[_FILE]`): `enabled:true`,
  `usd_cap` = the cap, `remaining` = `max(0, usd_cap − total.usd_est)`.
- Ownership: new `server/routers/usage.py`, one `include_router` line in
  `server/app.py`, additive read helper `aggregate_usage` in `da/usage.py`.

### Claim echo now carries `tier` (`deps.tenant_echo`)

Under `LEAF_AUTH_LIVE=1`, `deps.tenant_echo` additively stamps **`tier`** (from the
verified `TenantContext`) alongside the existing `tenant_id` / `org_id`, so a
live-auth `GET /api/session` (and every echoed body) can render an honest tier
chip. **Off-auth behaviour is byte-identical** — a plain-string tenant leaves the
body untouched (`tests/test_backbone.py` depends on this). See `contract/AUTH.md`
§5. `GET /api/session` already routes through `tenant_echo`, so it carries `tier`
through automatically — no route change.

### GET /api/drawings/{drawing_id}/versions — version-history chain

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/drawings/{drawing_id}/versions` | §10-enveloped full version chain straight from the store manifest |

```json
{ "drawing_id": "demo", "head": 3, "latest": 3,
  "versions": [ { "v", "parent", "created", "bytes", "sha256",
                  "tool", "workitem_id", "note" } ],
  "error": null, "degraded_mode": false }
```

Rows come verbatim from the `da/store.py` manifest `versions[]` (missing optional
fields → null); this upgrades undo/redo from "step head one hop" to "browse the
chain" and pairs with the existing `GET …/intake?version={n}` to preview a version.
Unknown-drawing 404 is **identical to the intake route** (the well-known `demo`
bootstraps on first read at `APS_LIVE=0`; any other unknown drawing →
`BAD_PARAMS` HTTP 404). Ownership: extends `server/routers/drawings.py` (read-only;
`da/store.py` unchanged).

### `POST /api/jobs/{job_id}/close` accepts a bodyless POST (verified)

The frontend fires the tab-close reap via `navigator.sendBeacon`, which cannot set
`Content-Type: application/json` or custom headers. The `close_job` route declares
**no request body** (only `job_id` path param + the `require_tenant` header dep),
so an empty `text/plain` beacon body is accepted and never 422s. Verified by
`test_ui_wave.py` (TestClient `post` with no `json`); **no signature change was
needed.**
