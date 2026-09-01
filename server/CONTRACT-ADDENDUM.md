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

### Observing the `<200 ms` claim

> Added 2026-07-27, AFTER the promotion above. Non-normative: it changes no
> behaviour §7 specifies, it records how the latency claim §7 already makes is
> observed. `contract/CONTRACT.md` stays authoritative for §7 itself.

`<200 ms` is a p-latency claim on real traffic, and no test that runs on
`ubuntu-latest` can prove it — `server/tests/test_backbone.py`
`test_1b_submit_cost_is_independent_of_execution_time` bounds the difference of
means between a slow job and a 0s job for exactly that reason, and its own
docstring records why no single-sample threshold fits that host's tail.

So the absolute bound is alarmed on in production. `POST /api/run` emits
**`SubmitLatency`** (`Unit: Milliseconds`, namespace `Leaf/Platform/APS`,
dimensions `{environment, aps_live}`) from
`server/emf_metrics.py::emit_submit_latency`, measured from handler entry to the
202. `?wait=1` blocks on execution and is deliberately NOT sampled: one gauge
does not carry two request populations with two different contracts.

| | |
| --- | --- |
| metric | `Leaf/Platform/APS` `SubmitLatency` |
| dimensions | `environment="production"` AND `aps_live="true"` |
| statistic | p99 |
| threshold | `> 200` ms — UNVALIDATED, see below |
| missing data | `notBreaching` (no traffic is not a breach) |
| period | fit to measured submit volume; there is no correct default |

`environment` is NOT optional and `aps_live` does not substitute for it.
`Leaf/Platform/APS` is one namespace shared by staging and production in the same
account and region. Measured 2026-07-27: every `aps_live="true"` `BrokerRun`
datapoint in the account came from **staging**, because on the `/api/run` path
`aps_live` derives from the app's own `APS_LIVE` env var, which is `1` in staging
and `0` in production. An alarm scoped only by `aps_live` therefore cannot tell
the two deployments apart, and its name is not evidence of what it watches.

Two things this table deliberately does not assert:

- **`> 200` ms is unvalidated.** It is the bound published in
  `contract/CONTRACT.md` §7, not a fitted value. Fit it against real p50/p99 once
  datapoints exist.
- **No period is given.** An earlier version said "> 200 ms for 3 consecutive
  periods". A 3-of-3 alarm does evaluate the three most recent periods when data
  is present, so that phrasing is not wrong in general; it is wrong as a spec for
  a SPARSE metric. For a default sliding alarm CloudWatch retrieves a range WIDER
  than `evaluation_periods`, and once at least `evaluation_periods` real
  datapoints exist anywhere in that range it stops applying `treat_missing_data`
  and judges the most recent real ones. At a low submit rate a 5-minute period
  can therefore leave the alarm unable to fire at all, because the datapoints
  never land close enough together to fill the window.

### Migration consequence of the dimension change

`SubmitLatency` previously published under `{aps_live}` alone. CloudWatch treats
each dimension combination as a SEPARATE metric, so an alarm still scoped to the
one-dimension form does not consume the two-dimension series at all: it does not
break loudly, it simply stops matching. Under `notBreaching` that reads as a
green alarm measuring nothing, which is the exact failure mode this section warns
about above.

At the time of writing, `leaf-production-aps-submit-latency-p99-high` in
`terraform/environments/production/us-east-1/cloudwatch_aps.tf` is still scoped to
`aps_live="true"` only. It must gain `environment="production"` in the same change
that sets `LEAF_METRICS_ENVIRONMENT` on the task definitions. Until both land, that
alarm is green and blind, as it already was before this change, because
`SubmitLatency` had never been emitted in the account at all.

Alarm creation is an AWS action for the observability plane, not a repo change.
Emission is gated by `server/tests/test_submit_latency_metric.py`.

## §8 APS broker boundary

> Section 8 is FROZEN (2026-07-22, census #4 credential-broker keystone).
> The security property, the route set, the caller-auth discipline, and the
> ledger line schema below change only through the operator-promotion ritual.
> The ledger line schema is additionally machine-frozen at
> `server/broker_ledger.schema.json` (`leaf.broker-ledger-line.v1`) and gated
> by `server/tests/test_broker_ledger_schema_static.py` (file/literal/reader
> agreement) plus `server/tests/test_broker_ledger_schema_runtime.py` (every
> line broker_run actually appends — including denial and garbage-input
> lines — validates; `broker.py::_conform_ledger_entry` enforces the frozen
> types at the single append chokepoint).

**Security property (tested):** the tenant-facing app process NEVER holds the
APS credential at `APS_LIVE=0` — the tested condition: `app.py`/`jobs.py`
contain no `da` import and the app runs correctly with
`APS_CRED=/nonexistent` (dynamic test). `server/broker.py` is a separate
process (default `:8140`, env `BROKER_PORT`; its own container via
`deploy/Dockerfile.broker`, non-root, unpublished on the host network) and is
the ONLY code that loads `da/client.py` on the tool-execution path. TWO
app-side seams exist and are documented, not waived — both §11 store-read
paths, both credential-free at `APS_LIVE=0`: (a) the legacy loader
`deps.get_da_client()` (drawing reads at `APS_LIVE=1` via `OSSBackend`),
which every call-site must use APS_LIVE-gated verbatim
(`deps.get_da_client() if deps.APS_LIVE else None`); (b) `write_loop.py`'s
lazy `import store` (`da/store.py` via a `sys.path` append), where
`store.py` imports `da/client` as a MODULE but the credential file is read
only on a live token fetch (the `APS_CRED=/nonexistent` dynamic test proves
`APS_LIVE=0` stays credential-free). Promoting the live reads through the
broker is the documented §11 follow-up. The static half of
the invariant is its own gate — `server/tests/test_no_da_imports_static.py`
sweeps the whole app-side surface (app, jobs, deps, broker_client, every
router, and every app-loaded subpackage, recursively) for `da` imports —
AST-level, so conditional/inline imports count — plus dynamic-import calls,
ungated `deps.get_da_client()` call-sites, and undocumented `DA_DIR` loads.
The app reaches tool execution ONLY via `server/broker_client.py` → HTTP
(`BROKER_URL`, default `http://127.0.0.1:8140`); broker down →
`BROKER_UNREACHABLE`.

**Caller-auth hop (F4):** every protected `/broker/*` route (everything except
`/broker/health`) requires header `X-Broker-Secret`, verified constant-time
(`hmac.compare_digest`) against env `LEAF_BROKER_SECRET` — the SAME env the
app-side sender reads (`broker_client.broker_headers()`). Discipline: secret
set → always enforced (401 wrong/absent); live mode (`LEAF_AUTH_LIVE=1`) +
secret unset → 503 fail-closed; off-live + secret unset → friction-free demo.
The secret is never logged and never appears in a ledger line.

| Method | Path | Behaviour |
|---|---|---|
| POST | `/broker/run` | `{tenant_id, ledger_event_key?, tool, params, dwg, aps_live, dwg_version?, test_source?}` → extended §3 envelope. PostgreSQL posture requires a nonblank `ledger_event_key`; callers reuse it to deduplicate a retry. `aps_live:false` → pure-python mock path; `true` → `da.client.run_tool` (live). `dwg_version` pins the §11 store version (live reads reject the pin fail-closed until wired). Additive `test_source` is accepted only for a design-time non-live test, runs only in the configured sandbox, and contributes only its SHA-256 digest to request identity and ledgers. |
| POST | `/broker/extract` | `{tenant_id, dwg, upload?}` → `{intake}` envelope; the ONLY extraction path at `APS_LIVE=1` (`GET /api/session` relays through it — the pre-broker in-process extract is gone). `upload:true` resolves tenant-bound staged uploads (§19) |
| POST | `/broker/tenants/{tid}/disable` / `.../enable` | per-tenant kill-switch, persisted to `broker_tenants.json` (env `BROKER_TENANTS`). Disabled tenant → `TENANT_DISABLED` (retryable:false), APS never touched. Corrupt tenant state can NOT disarm the switch: unparseable records fail CLOSED and a corrupt file refuses broker boot (`BrokerStateError`) |
| POST | `/broker/reap` | cancel orphaned WorkItems (closed tab / expired lease); only the credential holder may issue the DA cancel |
| GET | `/broker/health` | open (liveness); role, ledger path, disabled tenants |

**Attribution ledger** (metering/quota chokepoint other sessions read): every
`/broker/run` — including kill-switch and quota denials — appends exactly ONE
JSONL line to `server/broker_ledger.jsonl` (env `BROKER_LEDGER`). Line schema
(FROZEN as `leaf.broker-ledger-line.v1`, see `server/broker_ledger.schema.json`):

| key | type | meaning |
|---|---|---|
| `ts` | number | append time, UNIX epoch seconds (UTC-day bucket key) |
| `tenant_id` | string | attributed tenant (all metering filters on it) |
| `tool` | string \| null | tool package `name` |
| `engine_op` | string | tool package `engine_op` (`""` when absent) |
| `aps_endpoint` | string | APS base URL of this broker process |
| `aps_live` | boolean | true iff the APS-money path; quotas count only these |
| `engine_seconds` | number \| null | engine time from the run's cost block |
| `usd_est` | number \| null | estimated USD spend from the cost block |
| `status` | string | `ok`, a §10 `error_code`, or `INTERNAL` |

New keys may be ADDED as optional fields; frozen keys are never renamed,
retyped, or dropped. Consumers: `da/usage.py` (spend cap 402 pre-flight, daily
run quota 429 pre-flight, and the §13 `GET /api/usage` aggregation).

**Preflight order on `/broker/run` (tested):** kill-switch → spend cap (402
`quota_exceeded`) → tool-package shape → tier entitlement re-check (F10,
broker-trusted tier source, never the request body) → daily run quota on the
live path only (429, F12+A4) → schema validation → execution.

**Egress allowlist (v1, in-process):** every outbound HTTP request from the
broker process passes a central guard (patched `requests` adapter). Allowed:
`developer.api.autodesk.com` + `*.amazonaws.com` (OSS direct-to-S3 signed
URLs used by the frozen §5 client) + `BROKER_EGRESS_EXTRA` env. Anything else
raises `EgressBlocked`, so tenant-authored `engine_op`s cannot redirect
egress. Network-layer enforcement (container/proxy) is another session.

**Deployment posture (compose/ECS):** the broker publishes NO host port —
`python broker.py` binds loopback; the container form is reachable only on the
internal service network (`http://broker:8140`). `BROKER_LEDGER` and
`BROKER_TENANTS` live on a durable volume shared read-side with the app
(the ledger holds no credential; it IS the metering surface). Operator
procedure for the kill-switch: `docs/runbooks/broker-kill-switch.md`.

**v1 assumptions:** the demo is single-process, so "tenant container" ==
"the app process"; `tenant_id` arrives as an `X-Tenant-Id` header stub until
verified claims replace it (`LEAF_AUTH_LIVE=1`). The mock path honors
`params._qa_sleep_s` (capped 30 s) as a QA latency-simulation hook — honored
only when QA hooks are enabled (`LEAF_QA_HOOKS`; default ON except live).

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
Live writes also carry additive `new_version_readable: boolean`. Once
`new_version` commits, the run succeeds and is never reported as retryable.
`false` means the immutable head exists but its derived intake cache could not
be verified, so clients must lock ordinary mutations and offer historical
restore recovery until a coherent head intake can be seated.
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
DECLARES its edit through additive `result["mutations"]` fields. Existing tools
use `{"added": [<intake entities>], "removed": [<handles>]}`. A panel layout
tool may also use the first-class `panel-transform/v1` field:

```json
{"transforms": [{"handle": "9462", "dx": 120.0, "dy": -48.0, "rotation_deg": 0.0}]}
```

Each transform rotates the source polyline's XY vertices around their vertex
centroid, then translates them. `rotation_deg` defaults to zero. The write loop
preserves Z and all other entity fields, rounds transformed XY values to nine
decimal places, and rejects the complete batch before persistence when a handle
or transform value is invalid. Translation is limited to 10000 drawing units per
axis and rotation to `[-360, 360]` degrees. The execution chain
(`server/write_loop.py`) applies the mutations to the CURRENT version's intake
→ new intake → `store.put_drawing`
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

> Section 12 is FROZEN and serves as the degraded-mode floor for the section 18 conversational surface.

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

## §14 UI wave 2 — Projects workspace made real + ops controls

Session: `ui-wave-2-backend`, 2026-07-18. Four additive surfaces the exposure map
(`docs/backend-frontend-exposure-map.md`) named as the "ready-but-dark" targets
for the Projects workspace, built against root-frozen shapes a sibling frontend
lane consumes. Verified by `server/tests/test_wave2.py` (Neon-backed create_org +
linkage, offline broker/app subprocess for ops + versions). The DB-less demo is
BYTE-IDENTICAL (`tests/test_backbone.py` unaffected).

### A. `POST /api/orgs` + `GET /api/orgs/{org_id}` (platform router)

| Method | Path | Behaviour |
|---|---|---|
| POST | `/api/orgs` | `{name, tier?}` → `{org: {org_id, name, tier, status, created_at, offboarded_at}}` (via `store.create_org`). **OPEN endpoint in dev** (no auth gate — bootstraps the first `org_id`; chicken/egg); **production gates it behind the auth/identity layer** (`platform/README.md`). `tier` optional, validated against `models.TIERS` (else 422); omitted → store default `hosted_starter`. |
| GET | `/api/orgs/{org_id}` | `{org}` — **404-not-403 isolation**: a caller may read only its OWN org (`X-Org-Id` must equal the path); cross-org or unknown → 404, missing header → 400. |

Org responses use the platform router's resource-wrapper convention (`{org: …}`),
not the §10 `error`/`degraded_mode` envelope (matches its `{project}`/`{job}`
siblings). Store read is org-scoped by construction; `store.py` unchanged.

### B. Platform Job linkage — the piece that makes workspaces non-hollow

`POST /api/run` gains two OPTIONAL headers, **`X-Org-Id` + `X-Project-Id`**. When
BOTH are present AND a platform `DATABASE_URL` resolves, the async spine records a
canonical platform **Job** row: `kind="run"`, `tool_name`, `params`,
`spine_ref=<spine job_id>`, `status` synced `queued → running → succeeded|failed`,
`result` = the §3 envelope on success, `cost_usd` = the envelope's `cost.usd_est`.

**STRICTLY BEST-EFFORT + ENV-GATED** (`server/platform_link.py`): a no-op unless
both headers are present AND the DB resolves; any DB/import error logs exactly one
line and NEVER affects the run (everything wrapped). Absent either header → the
spine + HTTP bodies are byte-identical to before. Correlation is in-process
(`spine_job_id → platform_job_id`); the create goes through `store.create_job`,
the terminal `UPDATE` is issued in `platform_link` (org-scoped) since `store.py`
has no update path. Ownership: new `server/platform_link.py`, call sites in
`server/jobs.py` (`submit_job` + `_run_job` + `_finish`), header plumbing in
`server/routers/jobs.py`.

### C. Ops surface (role-gated; app-side proxy of the broker chokepoint)

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/ops/tenants` | `X-Internal-Role: qa` (else 403) → `{tenants:[{tenant_id, runs, usd_est, llm_turns, llm_cost_tokens, llm_usd_est, disabled}], platform:{profiles, autocad_backend:{runs, usd_est}, llm:{turns, cost_tokens, usd_est}}}` — per-tenant runs/spend from the broker ledger (`da/usage.aggregate_usage`) joined with kill-switch state (broker `/broker/health`, `broker_tenants.json` fallback) and with lifetime LLM usage from the agent ledger (`agent_ledger.tenants_seen`, read ONCE per listing). The `llm_*` fields are `null`, never `0`, when the agent store cannot be read: an unknown spend must not render as an idle profile. `platform` is platform scope, not a row sum — its LLM totals cover tenants that never reached the broker, so `profiles` can exceed the row count |
| POST | `/api/ops/tenants/{tid}/disable` / `.../enable` | same role gate → **proxy** the broker's `/broker/tenants/{tid}/disable\|enable` over `BROKER_URL`; return the broker's §-enveloped ack. Broker unreachable → `BROKER_UNREACHABLE` envelope, HTTP 502 |

Internal-only surface (NOT the tenant surface), gated exactly like the QA catalog
filter. The 403 body is `BAD_PARAMS` at HTTP 403 (no frozen ErrorCode names
authorization). Ownership: new `server/routers/ops.py` + one `include_router`
line in `server/app.py`.

### D. `GET /api/drawings/{id}/versions` gains `checkout`

The versions response now carries `"checkout": {holder, acquired, expires} | null`
straight from the store manifest's single-writer lock — read-only (no
acquire/release endpoints this wave), for a calm "someone else is editing" chip.
Ownership: extends `server/routers/drawings.py` (`da/store.py` unchanged).

**Update (checkout capability, 2026-07-25).** `holder` on this read is a DISPLAY
LABEL only. It is public to every member of the tenant, so it cannot also be the
thing that proves ownership: a second session could read it and present it to
publish, release, undo or redo under the first session's lease. The lock
GENERATION is no longer published here at all (it was half of that same replay).

Ownership is now proved by an opaque capability, minted once by
`POST /api/drawings/{id}/checkout` and returned as `checkout_capability`, bound to
tenant + authenticated subject + drawing + lock generation
(`server/checkout_capability.py`). Present it in the `X-Checkout-Capability`
header — never a body field or query parameter — on:

| Route | Without a valid capability |
|---|---|
| `DELETE /api/drawings/{id}/checkout` | 403; the lock is left intact. The old `?holder=` parameter is GONE. |
| `POST /api/drawings/{id}/checkout` (refreshing a LIVE lease) | 409 not-acquired, as for any held lock |
| `POST /api/drawings/{id}/undo`, `.../redo` | 403; head does not move |
| `POST /api/run` for a `drawing.write` tool | the run is unnamed → 403 at the publish gate |

Unchanged in every case where no lease is live: an absent or EXPIRED lock is free,
needs no capability, and is re-acquirable by anyone — so a forgotten lock still
cannot wedge a drawing. Operators running more than one app process must set
`LEAF_CHECKOUT_CAP_SECRET` (required outright, with at least 32 bytes and about
128 bits of estimated entropy, when `LEAF_RUNTIME_ENV=production`). Generate it
with `openssl rand -hex 32`; repeated characters, words, or short pasted blocks
are refused. Unset off production means a per-process secret that does not
survive a restart. When `LEAF_AUTH_LIVE=1`, mint and verification
also require the verified token's nonempty `sub`; a subjectless authenticated
caller receives a fail-closed operator error rather than sharing an anonymous
capability identity with every other tenant member.

> **FREEZE (census #13, NL-build lane, 2026-07-22): §15, §16, and §17 are FROZEN.**
> The frozen surface is the contracts, not the prose: every wire shape, env name,
> route, status code, and law these three sections define — the §15 sidecar API and
> fold-precedence law; the §16 tenant→repo resolution law, per-tenant grant-store
> admin wire (`{linked, linked_at, kind}`, NEVER the token), 401 `grant_required`
> shape, and §16.H same-dir deployment law; the §17 grant-kind vocabulary
> (`oauth` | `api_key`) + auto-detection law, entitlement policy keys, and 403
> entitlement shape — plus the F5 app→harness caller-auth hop (`X-Harness-Secret`
> from `LEAF_HARNESS_SECRET`, gate `LEAF_HARNESS_AUTH`, fail-closed when enabled
> with no secret, `GET /health` exempt). A breaking change to any of these is
> stop-the-line: it needs an operator ruling and a new section, never an in-place
> edit; additive absent-safe fields remain allowed (the §10 additive rule).
> Enforced by `tests/test_wave3.py` / `test_wave4.py` / `test_wave5.py`, the
> harness hermetic suites (`harnessAuth`, `grantStore`, `grantAdmin.e2e`,
> `tenantProvision`), `tests/test_contract_freeze.py`, and the containerized smoke
> (`scripts/harness-container-smoke.py`). Sibling freeze:
> `harness/contract/HARNESS-CONTRACT.md` (same date). Grant handling for
> enterprise review: `docs/GRANT-PRIVACY.md`.

## §15 UI wave 3 — the Build lane made REAL (harness sidecar) + hygiene

Session: `ui-wave-3-backend`, 2026-07-18. Makes the web UI's authoring path drive the
real Agent SDK harness end to end, and closes four hygiene gaps. Verified offline by
`server/tests/test_wave3.py` (+ the harness `npm test` 4/4 and a boot probe of the
sidecar). The pre-wave-3 behavior is BYTE-IDENTICAL when the new env knobs are unset.

### A. Harness sidecar (Contract 1) — `harness/scripts/serve.ts`

A runnable HTTP sidecar composing the SAME four real ports as `harness/scripts/drive.ts`
(EnvOrFileGrantStore → OAuthGrantProviderImpl; in-place TenantRepoProviderImpl at
`LEAF_TENANT_REPO`; real `BrokerApsClientHttp` at `BROKER_URL`; `AgentSdkRunner`) and
listening on `HARNESS_PORT` (default **8150**). Compile with `npx tsc -p tsconfig.build.json`
and run `npm run serve` (`node dist/scripts/serve.js`). Point the app at it with
`LEAF_AUTHOR_HARNESS_URL=http://127.0.0.1:8150`. Secret discipline: the code reads only
the grant PATH (env `LEAF_GRANT_FILE`, default `C:/tmp/hosted-oauth-spike/.grant/token`);
the token value flows only into a scrubbed SDK child env and is never printed/logged. A
missing/malformed grant yields a clean HTTP-500 auth error from `POST /author` BEFORE any
SDK session is constructed (no LLM credit spent). `GET /health` needs no grant.

### B. Tenant-repo registry fold + entry resolution (Contract 2)

`deps.all_tools()`: when `LEAF_TENANT_REPO` is set, the tenant repo's `registry.json`
tools are folded in. **PRECEDENCE (last wins): engine registry < tenant-repo < write
seed < `authored_tools.json`** — a tenant-repo tool overrides an engine tool of the same
name, and an authored tool overrides both. With `LEAF_TENANT_REPO` unset the fold is
empty and `all_tools()` is BYTE-IDENTICAL to before. A tenant tool keeps its repo-RELATIVE
`entry` (e.g. `tools/<name>/tool.py`); `tool_loader.resolve_local_file` resolves it against
`$LEAF_TENANT_REPO` (absolute), so the broker resolves the file regardless of its cwd —
**removing the M1 broker-cwd hack**. Both the app (fold) and the broker (resolution) read
`LEAF_TENANT_REPO` from env. The M1-authored `layer-bounding-boxes` in the demo tenant repo
now appears in `/api/tools` and runs correctly at `APS_LIVE=0` through the normal broker
(Panels bbox matches `data/nl_author_receipt.json`).

### C. Author provenance (Contract 3)

`POST /api/author` gains top-level **`source: "harness"|"template"`** and
**`static_scan: [...]`** (the advisory SPEC §10.2 scan, surfaced consistently for both
sources — also mirrored in `tool.provenance.static_scan` + the preview). Harness-authored
tools are ALREADY registered into the TENANT repo by the harness build route, so they
surface in `/api/tools` via §15.B's fold — the app does NOT persist them to the local
`server/authored/` store or `_AUTHORED`. The template path (local persistence into
`server/authored/<name>.py` + `authored_tools.json`) is UNCHANGED.
**[SUPERSEDED: see §23. The flat path above stopped being true on 2026-07-25.
This sentence is left unedited because §15 is frozen.]**

### D. NL-router alternatives + build-intent boost (Contract 4)

`POST /api/nl-prompt` (and `nl_router.classify`) gain **`alternatives: [{tool, confidence}]`**
— the top ≤3 non-winning tool matches (for a RUN match, the ranked runners-up; for
build/solve, the top catalog matches; may be empty). **BUILD-INTENT BOOST:** explicit
authoring phrasing (`make/build/create/author (me )?a tool (that|to) …`) routes `lane=build`
even when a registered tool partially (contiguously) matches — UNLESS the text essentially
NAMES an existing tool exactly (after stripping authoring scaffolding, the residual content
tokens are all within the tool's name). Fixes the known miss: *"make a tool that counts
panels smaller than 10 sqft"* → **build** (previously misrouted to a `count-panels` RUN when
that tool exists in the catalog).

### E. Hygiene (Contract 5)

- **(a) Durable platform Job terminal sync** (`server/platform_link.py`): the running /
  terminal transitions now `UPDATE jobs … WHERE spine_ref = <spine job_id>` (globally-unique
  uuid4) instead of an in-process map, so the sync **survives a full app-process restart**
  (e.g. when the orphan reaper terminates a restarted job). Still best-effort + env-gated
  (no-op without project headers or a resolvable platform DB); a vestigial `_MAP` is kept for
  cheap within-process correlation but is no longer depended on.
- **(b) Ops read-your-write** (`server/routers/ops.py`): `GET /api/ops/tenants` resolves the
  kill-switch set FRESH on every request (per-request `GET /broker/health`, `Cache-Control:
  no-cache`, broker_tenants.json fallback) — a just-disabled tenant is reflected immediately,
  with no cached `/health`.
- **(c) Richer job progress** (`server/jobs.py`): the worker sets a `progress` phase before
  the (blocking) broker call so SSE/poll consumers see more than status flips. **Vocabulary
  (short + stable):** `queued` → `running` → **`executing`** (read tools) | **`storing
  version`** (write tools, mock `APS_LIVE=0`) | **`extracting`** (write tools, live re-extract
  `APS_LIVE=1`) → `done` | `error`. The reaper/timeout paths are unchanged (they key off
  `status`, not `progress`).

## §16 Wave 4 — the Build lane made MULTI-TENANT (per-tenant grant + repo + catalog + exec)

Session: `wave4-backend-multitenant`, 2026-07-18. Turns the single-grant / single-repo
Build lane into a per-tenant one so it satisfies the individual-use OAuth rule
(`research/agentsdk-usage-visibility.md`: ONE Claude token per end user, never pooled).
Verified offline (`APS_LIVE=0`) by `server/tests/test_wave4.py` (7) + the harness hermetic
suite (`grantStore` / `tenantProvision` / `grantAdmin.e2e`). Pre-wave-4 behavior is
BYTE-IDENTICAL when the new env knobs are unset.

### A. Tenant → mushy-repo resolution (`server/tenant_paths.py`, shared by deps + tool_loader)

`resolve_tenant_repo_dir(tenant_id)` is the single source of truth both the catalog fold
(`deps`) and entry resolution (`tool_loader`) use, so they always agree on where a tenant's
repo lives. Two modes, chosen by env (read at call time):

- **MULTI-TENANT** — `$LEAF_TENANTS_DIR` set → `<base>/<tenant_id>` (path-safe: a tenant_id
  is a single traversal-free component or it resolves to *no repo*). `$LEAF_TENANT_REPO`
  additionally OVERRIDES the **demo** tenant to a specific path. Tenant A and tenant B resolve
  to DIFFERENT dirs — the wave-4 isolation mode.
- **LEGACY SINGLE-REPO** — only `$LEAF_TENANT_REPO` set → that ONE repo serves EVERY tenant
  (the proven wave-3 demo: one repo, one grant; no isolation, by design).
- **NEITHER** → `None` (fold + entry resolution OFF; byte-identical to pre-wave-3).

`deps.all_tools(tenant_id)` / `deps.find_tool(name, tenant_id)` / `tool_loader.resolve_local_file(tool, tenant_id)`
/ `run_tool_dynamic(..., tenant_id=)` all default to the demo tenant, so every legacy no-arg
caller is unchanged.

### B. Per-tenant Claude grant store (harness TS) + admin endpoints

`harness/.../oauthGrantProvider.ts::FileTenantGrantStore` persists ONE token file per tenant
at `$LEAF_GRANTS_DIR/<tenant_id>.token` (default `C:/tmp/leaf-grants`; mkdir-safe, `mode 0600`,
token NEVER logged). **BACK-COMPAT:** the **demo** tenant with no per-tenant file falls back to
the existing env/file grant (`LEAF_GRANT_FILE` / `CLAUDE_CODE_OAUTH_TOKEN`), so the proven demo
loop is unchanged. **PRODUCTION swaps this class for a vault/DPAPI store** (same interface).
Harness admin endpoints (backed by the same store): `PUT /grants/{tenantId}` `{token}` →
`{linked, linked_at}`; `GET /grants/{tenantId}` → `{linked, linked_at}`; `DELETE /grants/{tenantId}`.
**`status()` and every wire return NEVER carry the token** (`linked_at` is the token file's mtime).

### C. Per-request tenant in the author loop

`POST /author` body gains **`tenant_id`** (default `demo-tenant`; body wins, else `X-Tenant-Id`
header). The author loop resolves THAT tenant's grant + repo. A missing grant → a clean **HTTP
401** `{ grant_required: true, error:{ message, code:"grant_required" } }` (the SDK is never
constructed — no LLM credit spent).

### D. Auto-provisioned per-tenant repos (harness TS)

`TenantRepoProviderImpl({ inPlace, autoProvisionFrom })`: the FIRST authoring for a brand-new
tenant materializes its repo from `harness/test/fixtures/tenant-repo` (copy + ensure the
`__pycache__` `.gitignore` + `git init` + ONE seed commit). Later checkouts skip provisioning;
an existing repo is never clobbered. The Windows spawn-pressure retry in `commit()` is intact.

### E. App grant-linking proxy (`server/routers/tenant.py`)

| Method | Path | Behaviour |
|---|---|---|
| POST | `/api/tenant/claude-grant` | `{token}` (tenant from `require_tenant`) → forwards to the harness `PUT /grants/{tenant}` → §10 `{linked:true, linked_at}`. **The app NEVER persists, logs, or echoes the token** (forwarded in one request; asserted in code + `test_wave4`). |
| GET | `/api/tenant/claude-grant` | → §10 `{linked, linked_at}` — never the token. |
| DELETE | `/api/tenant/claude-grant` | → §10 `{linked:false, linked_at:null}`. |

Harness unreachable / not configured → **`BROKER_UNREACHABLE`** envelope (HTTP 502).

### F. Author grant-required mapping (`server/routers/author.py`, error name `GRANT_REQUIRED`)

When the harness signals a missing grant (401 + `grant_required:true`), the app proxy returns
**HTTP 401** with a §10-COMPATIBLE body and does NOT fall back to the templater:

```json
{ "tool": null, "code": null, "preview": "Sign in with Claude to author tools …",
  "source": "grant_required", "grant_required": true, "reason": "GRANT_REQUIRED",
  "static_scan": [],
  "error": { "error_code": "BAD_PARAMS", "message": "…sign in with Claude…", "retryable": false },
  "degraded_mode": false }
```

The frozen §10 `error.error_code` enum is UNCHANGED — `GRANT_REQUIRED` is the app-proxy's name
(constant `GRANT_REQUIRED` in `author.py`), surfaced additively via top-level `grant_required` /
`reason` / `source` for the frontend to key on, while the `error` object keeps a valid enum code
(`BAD_PARAMS`, `retryable:false`). A harness that is *unreachable* still falls back to the
templater (unchanged); only an explicit grant-required signal short-circuits.

### G. Tenant-scoped catalog + execution (Python)

`GET /api/tools` and `GET /api/capabilities` now `Depends(require_tenant)` and fold **only the
REQUESTING tenant's** repo tools; the engine registry, write seed, and the process-global
authored store stay GLOBAL (visible to everyone). So tenant A's harness-authored tools are
invisible to tenant B, while shared globals are visible to both. `POST /api/run` resolves the
tool from the requesting tenant's catalog (a cross-tenant tool → `UNKNOWN_TOOL`) and threads
`tenant_id` → `jobs.submit_job` → `broker_client` → `POST /broker/run` → `run_tool_dynamic(...,
tenant_id=)`, so entry resolution + execution read the SAME tenant's repo.

### H. Deployment consistency + scope notes

- The app (`deps.resolve_tenant_repo_dir`) and the harness (`serve.ts`) MUST resolve a tenant to
  the SAME repo dir: set `$LEAF_TENANTS_DIR` on BOTH processes (and, for the demo tenant, the same
  `$LEAF_TENANT_REPO`). The proven demo loop uses only the demo tenant, where both sides agree.
- The process-global template store (`authored_tools.json` / `deps._AUTHORED`) is the single-node
  demo fallback and remains global; the per-tenant isolation guarantee is about the tenant-repo
  fold (the production harness authoring path).
- `POST /api/nl-prompt` remains GLOBAL (frozen §12: "no tenant/auth dependency"); making NL
  classification tenant-scoped would be a §12 change and is out of scope for this lane.

## §17 Wave 5 — enterprise readiness (BYO API-key grant kind + tier entitlement enforcement)

Session: `wave5-backend`, 2026-07-18. Two enterprise-lane pieces. Verified offline by
`server/tests/test_wave5.py` (14) + the harness hermetic suite
(`grantStore` api_key coverage + `runnerEnv` env-injection). Pre-wave-5 behaviour is
BYTE-IDENTICAL when the new knobs are unset (grants default to `oauth`; off-auth tier is
`demo` = full access).

> **§17 role-overlay extension (2026-08-03, contract/AUTH.md §11.5):** every
> §17 capability check now resolves `entitlements_for(tier, roles, elevated)` —
> the tier baseline OR'd with the verified identity's role grants
> (`server/roles.py` + operator-tunable `server/roles.json`, additive-only,
> fail-closed to no-grants). `GET /api/entitlements` additively reports the
> `roles` it used. Callers with no verified role context (back-edge, broker,
> guest, off-auth) omit the new arguments and are byte-identical to before.

### A. BYO API key as a grant kind (the enterprise auth lane)

The Agent-SDK grant is either an OAuth per-user "sign in with Claude" token (web lane,
individual-use — `research/agentsdk-usage-visibility.md`) OR a **BYO API key** (enterprise
lane, where Leaf holds/settles the bill — the MATRIX ToS-cleanest path). The `kind` is now
first-class end-to-end.

- **Harness store** (`FileTenantGrantStore`): persists the kind in a sidecar `<tid>.kind`
  file next to `<tid>.token`. `put(tenantId, token, kind?)` — an omitted `kind` is
  **AUTO-DETECTED** from the token prefix: `sk-ant-api…` → `api_key`; `sk-ant-oat…` →
  `oauth`; otherwise `oauth`. `get()` returns `{kind:"api_key", apiKey}` or
  `{kind:"oauth", oauthToken}`. A legacy token file with no sidecar falls back to prefix
  detection. The token is NEVER logged; `remove()` clears both files.
- **Harness admin**: `PUT /grants/{tenantId}` body gains optional `kind` (else auto-detect);
  `GET /grants/{tenantId}` → **`{linked, linked_at, kind}`** (kind present when linked;
  never the token).
- **SDK runner env injection** (`buildScrubbedEnv`): an `api_key` grant injects
  `ANTHROPIC_API_KEY`; an `oauth` grant injects `CLAUDE_CODE_OAUTH_TOKEN`; the other cred
  var (and every ambient Anthropic identity) is stripped from the scrubbed child env. The
  demo-tenant env fallback is unchanged (kind `oauth`).
- **App proxy** (`server/routers/tenant.py`): `POST /api/tenant/claude-grant` body gains
  optional `kind` (forwarded only when set, so the harness auto-detects otherwise);
  `GET` returns §10 `{linked, linked_at, kind}`. The token is still never persisted,
  logged, or echoed app-side; `kind` is not a secret.

### B. Tier-driven entitlement enforcement (MATRIX gap 6 — the tier schema was 0% enforced)

The Auth0 `tier` claim (AUTH.md §1) now DRIVES capability enforcement server-side. Policy
lives in the tracked, operator-tunable `server/entitlements.json` (override:
`LEAF_ENTITLEMENTS_FILE`), read at request time:

```json
{ "demo":           { "run_read": true, "run_write": true,  "build": true  },
  "self_hosted":    { "run_read": true, "run_write": true,  "build": true  },
  "hosted_starter": { "run_read": true, "run_write": true,  "build": false },
  "hosted_pro":     { "run_read": true, "run_write": true,  "build": true  } }
```

- **Tier source**: live auth (`LEAF_AUTH_LIVE=1`) → the verified `TenantContext.tier`
  (Auth0 claim). Off-auth / missing tier → **`demo`** (full access — the open demo stays
  friction-free, by design). Unknown tier → the `demo` entry (a tier only ever arrives from
  a VERIFIED claim, so an unrecognized value is operator config drift, not an attacker
  vector).
- **Enforcement (non-bypassable, in the execution chain — not the UI):**
  - `POST /api/run` rejects a `drawing.write`-capability tool when the tier lacks
    `run_write` (checked before job submit → covers async **and** `?wait=1`).
  - `POST /api/author` rejects when the tier lacks `build` (checked FIRST → before the
    harness delegation or the templater).
- **Rejection = HTTP 403**, §10-compatible body mirroring the §16 grant_required pattern:

```json
{ "entitlement_required": true, "required": "run_write" | "build", "tier": "<tier>",
  "error": { "error_code": "BAD_PARAMS", "message": "…plain sentence…", "retryable": false },
  "degraded_mode": false }
```

  The frozen §10 `error.error_code` enum is UNCHANGED (`BAD_PARAMS`); the frontend keys on
  the additive top-level `entitlement_required` / `required` / `tier`.
- **`GET /api/entitlements`** (`server/routers/tools.py`) → §10
  `{tier, entitlements: {run_read, run_write, build}, source: "policy"}` (from
  `require_tenant`'s tier; under live auth the tenant echo additively stamps
  `tenant_id`/`org_id` like every other echoed body). This is a READ of policy — the actual
  gate lives in the run/author chains and cannot be bypassed via this endpoint.

> **§17 daily authoring quota (2026-07-30):** `POST /api/author` and
> `POST /api/author/stage` additionally enforce a per-tenant DAILY cap on
> authoring ATTEMPTS, configured by `LEAF_DAILY_AUTHOR_QUOTA` (absent/empty ⇒
> no cap, prior behavior). It stands beside the entitlement gate, not in place
> of it: entitlement asks whether the plan includes Build at all, this asks how
> often. It exists because authoring spend is invisible to the two caps that
> already exist — R5 authoring runs the Agent SDK harness and enables the
> operator-funded sandbox BEFORE any broker lease, and its broker test is
> `aps_live=false` / `usd_est=null`, so neither the broker's USD spend cap nor
> the daily RUN quota (§ preflight order) ever observes it, while the harness's
> per-session ceilings bound one session and never a SERIES of them.
> A quota unit is one attempt that reaches the harness. On the R5 lane the
> charge lives inside `CustomizationService.stage`, immediately before the
> harness call, so anything the service deterministically refuses first — an
> invalid request, a missing tenant binding, a tier without Build, a role
> `authorize_stage` denies, an already-STAGED idempotency replay answered from
> its durable receipt, a harness with no usable configuration (a blank caller
> secret, or a URL/secret the HTTP client itself refuses at prepare time,
> before any network I/O) — never spends a slot; a retry under a key still in
> STAGING re-invokes the harness and IS counted. The legacy auth-off path calls
> the harness directly from the router, so it is metered there, after its own
> gates. With auth OFF the tenant id is an unverified request header rather
> than a principal, so the open lane shares one anonymous budget instead of
> handing a fresh one to every invented id. Rejection is **HTTP 429**
> carrying the daily-run-quota envelope shape verbatim (`error_code:
> "quota_exceeded"`, `retryable: true`, top-level and nested `tier`/`limit`/
> `used`), discriminated by `quota_kind: "daily_author"` rather than
> `"daily_runs"`. The counter is `leaf_platform.counters.SharedCounterStore`
> bound to `author_quota_counters` (migration 0023) under
> `LEAF_AUTHOR_QUOTA_STORE=postgres`, or per-process memory otherwise; a
> configured quota whose counter cannot be reached refuses with a 503 rather
> than admitting unmetered. The durable store is REQUIRED wherever a
> per-process counter would not be a real cap, keyed on deployment posture:
> any `LEAF_RUNTIME_ENV` outside development/test/local — including UNSET,
> since the app image bakes no posture — demands postgres regardless of auth
> (two replicas keep two independent counts and a restart returns every tenant
> to zero), and an explicit local posture still demands it once auth is live.

> **§10 enum update (2026-07-18):** `GRANT_REQUIRED` (HTTP 401) and `ENTITLEMENT_REQUIRED` (HTTP 403) promoted into the frozen ErrorCode enum + `envelope_schema.json`. The grant-required (§16) and entitlement-denied (§17) responses now carry these dedicated `error.error_code`s instead of `BAD_PARAMS`; the additive top-level markers (`grant_required`/`reason`, `entitlement_required`/`required`/`tier`) are unchanged, so existing consumers keep working.

> **§17 platform-lane extension (2026-07-22):** the platform jobs lane
> (`POST /api/projects/{id}/jobs`, `platform/entitlements.py`) now enforces the
> SAME tier policy: job kinds map onto §17's capabilities (`solve`/`run` →
> `run_write`, `extract` → `run_read`, `build` → `build`), resolved through
> `server/entitlements.py`'s fail-closed `entitlements_for` and denied with the
> §17 envelope. The org's STORED tier (orgs.tier) is the source there, not the
> JWT claim; non-`active` org status also denies. The same stored-org check
> runs at the canonical submission choke point
> (`platform/canonical_jobs.submit_solve_job`), so the `POST /api/run` spine
> path cannot bypass it with a permissive request-side (JWT/demo) tier — the
> denial surfaces through `server/platform_link.CanonicalEntitlementDenied`
> and is returned verbatim. When enforcement itself cannot be evaluated
> (policy file present-but-unreadable/invalid, org row unreadable) the refusal
> is a structured 503 carrying the full §17 envelope with
> `error.error_code = INTERNAL` (frozen §10 enum), `retryable = true` — never
> a bare 500 and never an allow (this covers the `/api/run` request-tier gate
> too). Two boundary semantics are deliberate: (1) idempotent REPLAY precedes
> enforcement — an already-accepted Idempotency-Key returns its original job
> even after a tier downgrade (the job exists; denying the lookup would only
> break client retries), while NEW submissions are gated; (2) the evaluated
> tier and `active` status are re-checked atomically INSIDE the job INSERT
> (TOCTOU guard), so a downgrade racing the submission denies rather than
> slipping a job through. Binary proof: `scripts/entitlement-gate.py`
> (READY/NOT-READY, exit 0/1; the denial leg validates the FULL envelope).

## §18 Conversational agent sessions (agent spine, Phase 1)

Session: `agent-spine-phase1`, 2026-07-20. The contract for the conversational spine:
durable per-drawing agent sessions, streamed turns, and gated dispatch into the existing
deterministic job chain. **FROZEN (2026-07-23, census #12 chip 5)** — promoted
proposed→frozen under the same discipline as §11–§17, AS THE LIVE WIRE IS: 18.1 app
routes with their implementation notes (the §2.1 sessions wire, `ConverseTurnInput`
with NO ContextPacket field), 18.2 parked by decision, 18.3 event vocabulary, 18.4
codes, 18.5 back edge. The ContextPacket module (`server/context_packet.py`) is frozen
as a PARKED schema: no live caller today, and its field set + `MAX_PACKET_CHARS`
size discipline are pinned by `server/tests/test_contract_freeze.py` so the parked
module cannot drift while it awaits spine unification. Design rationale lives in
`docs/AGENT-SPINE-DESIGN.md`. Verification: Phase-1 implementation + tests in this
change set; freeze gate: `tests/test_contract_freeze.py` (registered suite
`server-contract-freeze`).

Two ground rules frame everything below:

1. **§12 stays frozen and is the degraded-mode floor.** `POST /api/nl-prompt`
   (CONTRACT-ADDENDUM.md:225–289) remains global, tenant-free, and side-effect-free
   ("No tenant/auth dependency — classification is read-only and side-effect-free",
   :250–252; reaffirmed :607–608). Nothing in §18 modifies it. When the harness is
   stopped, the grant is missing, or the LLM quota is exhausted, the product falls back
   to the §12 classifier and behaves byte-for-byte as it does today.
2. **Registered-tool execution never touches the LLM.** The session plans, explains,
   and *dispatches*; execution is the existing chain `POST /api/run → jobs → broker →
   tool_loader` (entitlement gate at `server/routers/jobs.py:68–71`), unchanged.

### 18.1 App endpoints (`server/routers/sessions.py`, new)

> **IMPLEMENTATION NOTE (2026-07-21 merge resolution).** The live
> `server/routers/sessions.py` is the §2.1 sessions wire: state lives in the
> FastAPI-side store (`server/session_store.py`) and turns drive the harness via
> `POST /turn` — NOT the harness-proxy model this section originally described.
> Of the table below, POST create / POST messages / GET stream / GET transcript
> are LIVE (shapes as documented); `GET /api/sessions` (list) and
> `DELETE /api/sessions/{id}` (archive) are PARKED — not served — until spine
> unification. The normative wire spec is `leaf-backend-gaps.md` §2.1.
> **Wire correction (census #12 chip 2, 2026-07-23):** the messages row's "app
> assembles the ContextPacket and forwards it" sentence is the superseded
> §18-era proxy design. The live wire forwards the frozen §2.1
> `ConverseTurnInput` — `{tenant_id, session_id, turn_id, drawing_id, messages,
> text|confirm, images?}` — with NO ContextPacket field; `server/context_packet.py` has
> no live caller. Pinned by `server/tests/test_sessions_router.py`
> (no-packet body assertion); FROZEN by chip 5 (2026-07-23) —
> `tests/test_contract_freeze.py` pins the port field set and the parked
> packet schema.

All `/api/*` routes resolve the tenant via the existing `require_tenant` dependency
(`server/deps.py:251–277`; off-auth header stub, live-auth verified JWT — unchanged).
All response bodies carry the §10 envelope fields (`error`, `degraded_mode`;
`with_envelope_fields`, `server/envelopes.py:118–124`).

| Method | Path | Behaviour |
|---|---|---|
| POST | `/api/sessions` | `{drawing_id, model?, policy?}` → `{session_id, status, created_at, model, policy, instant_ready, instant_reason}`. (`project_id` was listed here historically but is **not** a field on `CreateSessionRequest`; pydantic drops it silently.) Idempotent per (tenant, drawing). `model` is the per-session "mount your LLM" choice (§18.7): validated against the allowlist and persisted, so later turns inherit it; an unknown id is 400 `BAD_PARAMS`. Omitting `model` on a repeat POST leaves the stored choice untouched. Requires the `converse` entitlement; denial is the standard 403 `ENTITLEMENT_REQUIRED` shape (`server/entitlements.py:123–134`). |
| GET | `/api/sessions?drawing_id=` | `{sessions:[…]}`, own-tenant only. |
| POST | `/api/sessions/{id}/messages` | `{text?, images?, confirm?, classifier_hint?, model?, credential_grant?, queue?}` (exactly one of *user message* / confirm, where a user message is text and/or images) → 202 `{turn_id, status:"started"}` \| 409 `TURN_IN_PROGRESS` \| 401 `GRANT_REQUIRED` \| 413 `BAD_PARAMS` \| 429 `LLM_QUOTA_EXHAUSTED` \| 429 `LLM_RATE_LIMITED`. The 413 has three sources that are indistinguishable to the caller and mean the same thing: the app's own body-cap middleware, the app measuring its own encoded forward, and a 413 relayed from the harness. All three are `approval_unredeemed`, so a confirm turn refused this way keeps its approval and can be retried with a smaller payload. The same holds for 401 and 429, which are also refused before anything can redeem a confirmation. Forwards the frozen §2.1 `ConverseTurnInput` to the harness with the resolved tenant id. (The superseded §18-era sentence about assembling a ContextPacket is removed here: the wire-correction note above is the live behaviour, and `server/context_packet.py` has no live caller.) |
| GET | `/api/sessions/{id}/stream?after_seq=` | SSE relay of the harness stream (§18.3). One upstream connection per session, fan-out to N clients; `after_seq` passes through for replay. |
| GET | `/api/sessions/{id}/transcript?limit=` | Passthrough of the harness transcript (most recent N events, ascending seq). |
| DELETE | `/api/sessions/{id}` | Archive; passthrough → `{archived:true}`. |
| POST | `/api/agent/approvals/{confirmation_id}` | `{approved: bool}` → `{resolved:true, approved}`. Records the decision in the app's pending store. Does **not** start the resume turn — the client posts the confirm message separately. |
| GET | `/api/agent/audit?limit=` | Tenant's own audit records, projected through the `audit_extra` allowlist. |
| GET | `/api/agent/killswitch` | `{active: bool}`, read-only. |

**Inline images (additive, optional, absent-safe; 2026-07-29).**
`ConverseTurnInput` gains `images?: Array<{media_type, data}>`, and
`POST /api/sessions/{id}/messages` accepts the same field. The rules are
normative. Enforcement is layered, and the layers are NOT equivalent, so
each one's job is stated rather than implied:

* the **app** is the authority: it decodes every image and enforces count,
  decoded size, media type and signature agreement;
* the **harness** re-enforces the same rules on its own input, because it
  does not assume the app validated, and bounds its own request body while
  reading rather than after;
* the **client** checks only the browser-reported type and size, to fail
  fast in the UI. It does NOT verify signatures, and nothing downstream
  relies on it having done so.

* A turn is EITHER a user message OR a confirmation. A user message is `text`,
  `images`, or both, so an image with no caption is a valid turn. A `confirm`
  turn MUST NOT carry images (400 `BAD_PARAMS`).
* At most **3** images per message; each at most **1 MiB DECODED**; at most
  **1 MiB decoded in total**. `data` is base64 with no data-URI prefix.
* `media_type` is one of `image/png`, `image/jpeg`, `image/webp`, `image/gif`,
  and MUST agree with the decoded bytes' signature. A mismatch is refused.
* Caps are refuse-not-truncate, and they run BEFORE the entitlement gate and
  before any approval is consumed, so an invalid image never burns an approval.
* Images are **turn-local**. The durable `turn_started` event stores only
  `{media_type, bytes}` per image, never the base64, so transcripts and SSE
  replay do not carry megabytes. Prior-turn context is text-only, so a resumed
  or replayed turn never re-sends image bytes.
* Oversized requests are refused before the body is parsed, by an ASGI
  middleware (`MessageBodyLimitMiddleware`), including chunked bodies that
  declare no Content-Length.

Pinned by `tests/test_contract_freeze.py` (the frozen field set includes
`images`), `tests/test_image_turns.py` (limits, signatures, the one-of rule,
the durable descriptor) and `harness/test/converseSdkRunner.test.ts` (the SDK
content blocks).
| GET | `/api/usage` | Existing endpoint (`server/routers/usage.py:70–78`): every existing field stays byte-identical; the response gains one **additive** `agent` key per §6.7 (`today`/`total`/`cap` token aggregates, `estimate_basis` with `"self_metered"` as the only Phase-1 value, `updated_at`). |
| POST | `/internal/agent/gate` | Back-edge only (§18.5). `{tenant_id, session_id, turn_id, action, args}` → `{decision:"allow"\|"deny"\|"awaiting_approval", reason?, confirmation_id?, policy, rung}`. Runs the full gate chain (kill switch → catalog → args schema → entitlement → revalidate → rate limit → policy) and creates the pending-approval record when `awaiting_approval`. |
| GET | `/api/ops/agent/tenants` | Ops read (per-tenant session/spend view). |
| GET | `/api/ops/agent/sessions/{id}` | Ops read (one session detail). |
| POST | `/api/ops/agent/tenants/{tid}/disable\|enable` | Ops toggle for a tenant's agent access. |

The three ops routes use the existing `LEAF_OPS_SECRET` gate exactly as implemented in
`server/routers/ops.py:53–89` (constant-time compare; fail-closed 503 when live-auth is
on and the secret is unset).

Cross-tenant probing returns 404, never 403, on every `/api/sessions/*` route — the
same no-existence-oracle rule the job routes already enforce
(`server/routers/jobs.py:98–105`).

### 18.2 Harness mirror routes (PARKED — not served)

> **PARKED at the 2026-07-21 merge resolution (spine × sessions-wire).** The harness
> registers `POST /turn` only; every `/converse/*` route below returns 404. The live
> path is §2.1: app `/api/sessions*` routes (which ARE live, per 18.1) drive the
> harness through `POST /turn` (`application/x-ndjson`). This spec is retained
> verbatim for the spine-unification follow-up; do not build against it.

> **DECISION (census #12 chip 2, chip-spine-sessions-routers, 2026-07-23).** Spine
> unification landed (chip 1 mounted ConverseLoop behind `POST /turn`) and this
> mirror surface stays parked **by decision, not circumstance**: the app owns every
> client-facing route — the §2.1 wire is the one client contract
> (`console/converse.js` and `web/src/converse.js` both speak it) — and `/converse/*`
> stays dormant until a harness-direct client exists (none today; un-parking would
> stand up a second public turn surface with zero consumers).
> `harness/test/converseRoutes.test.parked.ts` records the same decision in its
> header. The §18.1 `GET /api/sessions` (list) and `DELETE /api/sessions/{id}`
> (archive) rows likewise remain unserved on the same grounds: no live client calls
> them.
>
> **Phase-2 debt (ledgered here so the lane keeps it).** The live SSE relay
> (`server/routers/sessions.py` `stream_session`) serves each browser client its own
> connection + store-poll loop over the durable event log; §18.1's "one upstream
> connection per session, fan-out to N clients" is NOT what is built. Per-client
> polling is correct and cheap at current fan-out; the shared per-session fan-out is
> deferred to Phase 2.

All `/converse/*` routes sit behind the existing F5 shared-secret gate — header
`X-Harness-Secret`, checked by `harnessAuthDenial` (`harness/src/server.ts:114–130`;
timing-safe compare, fail-closed when the gate is enabled with no secret configured).

| Method | Path | Behaviour |
|---|---|---|
| POST | `/converse/sessions` | `{tenantId, drawingId}` → 200 `{sessionId, status:"idle"\|"active"\|"dormant", createdAt}`. Idempotent per (tenantId, drawingId). |
| POST | `/converse/sessions/{sessionId}/messages` | `{tenantId, text?, confirm?:{confirmationId, approved}, contextPacket, classifierHint?}` → 202 `{turnId}` \| 409 `{error:"turn_in_progress", turnId}` \| 401 `{error:"grant_required"}`. Exactly one of text/confirm. |
| GET | `/converse/sessions/{sessionId}/stream?afterSeq=N` | SSE: replays persisted events with seq > N from sessions.db, then live events. |
| GET | `/converse/sessions/{sessionId}/transcript?limit=N` | 200 `{events:[…]}` (most recent N, ascending seq). |
| DELETE | `/converse/sessions/{sessionId}` | 200 `{archived:true}`. |

Every route verifies the supplied `tenantId` matches the session's tenant; a mismatch
returns 404 `{error:"session_not_found"}` — the harness-side twin of the app's
no-existence-oracle rule.

### 18.3 SSE event vocabulary (the stream vocabulary, and who emits what)

> **FROZEN AS THE LIVE WIRE IS (chip 5, updated 2026-07-29).** The 13-type table below
> is the full STREAM vocabulary. The two hops are NOT identical (the original
> "identical on both hops" claim was the §18-era proxy design): the
> harness→app NDJSON hop (`POST /turn`) emits exactly the 10-member
> `HarnessTurnEvent` union (`harness/src/ports/converse.ts`). The three
> app-side types, precisely: `turn_started` is appended by the turn engine
> when a turn starts (`server/turn_runner.py:213`, live payload
> `{text?, images? | confirm?, classifier_hint?}` — the table row's `model`
> value was the §18-era design; the live wire carries the user input. `images`
> is the turn-local descriptor list (`{media_type, bytes}` per image), never
> the base64 payload: §2.1 "Inline images" holds the storage rule);
> `confirmation_resolved` is appended by the approvals route
> (`server/routers/agent.py:217–220`); `session_state` is RESERVED vocabulary
> with NO live emitter today — frozen so spine unification can surface
> session lifecycle without a contract change. `seq` is a per-session
> monotonically increasing integer persisted in the APP's session store
> (`server/session_store.py` `session_events` — NOT the harness sessions.db,
> which serves only the parked 18.2 mirror); that is what makes `after_seq`
> replay (reconnect, second tab) exact. Pinned by
> `tests/test_contract_freeze.py`.

One JSON object per SSE `data:` line; the SSE event name equals `type`. Envelope:

```json
{"v": 1, "session_id": "…", "turn_id": "…", "seq": 42, "type": "…", "data": { }}
```

| type | data payload | Notes |
|---|---|---|
| `turn_started` | `{text? \| confirm?, images?, classifier_hint?}` (live; the §18-era design said `{model, classifier_hint?}`). `images` is a per-image `{media_type, bytes}` DESCRIPTOR, never the base64: see §2.1 "Inline images". | First event of every turn; app-side (`turn_runner.py:213`). |
| `text_delta` | `{text}` | Streamed assistant prose. |
| `tool_call` | `{tool, args_summary}` | `args_summary` is a short human string — never full params. |
| `tool_result` | `{tool, ok, summary}` | |
| `job_linked` | `{job_id, tool}` | Dispatch handoff; job progress rides the existing per-job SSE (`server/routers/jobs.py:110`), not this stream. |
| `proposed_run` | `{confirmation_id, tool, params, dwg, capability, rationale}` | `params` and `dwg` are the full server-stored target. The UI renders server truth, never a model paraphrase. |
| `confirmation_required` | `{confirmation_id, kind, payload}` | |
| `question_required` | `{question_id, question, options:[{label, description?}]}` | Display-only question card. It creates no approval row, spends no money, and mutates no drawing or catalog state. |
| `confirmation_resolved` | `{confirmation_id, approved, by}` | First approval wins; other tabs observe this event. |
| `turn_usage` | `{turns, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cost_tokens, total_cost_usd?, models?}` | `total_cost_usd` is an estimate (no balance API exists — `research/agentsdk-usage-visibility.md`). |
| `turn_complete` | `{stop_reason}` | `stop_reason` ∈ `end_turn \| awaiting_approval \| cap_hit \| llm_quota_exhausted \| llm_rate_limited \| error \| timeout`. |
| `session_state` | `{status, head_version?, checkout?}` | |
| `error` | `{error:{error_code, message, retryable, retry_class, actor, next_action}, degraded_mode}` | The §10 actionable error object, verbatim. |

### 18.4 New ErrorCode values (`server/envelopes.py`, additive)

Four codes (added for §18 — shipped source: `server/envelopes.py:39–42`, in the
ErrorCode enum block at :22–62) plus their `DEFAULT_HTTP_STATUS` entries (map starts
:66). Wire values are lowercase, following the `quota_exceeded` precedent (:34).
Verified shipped + pinned by test (census #12 chip 2):
`server/tests/test_sessions_router.py` asserts the four wire values, their
`DEFAULT_HTTP_STATUS` rows, and the additive `confirmation_expired` (410) sibling.

| Enum name | Wire value | HTTP | retryable | degraded_mode | Meaning |
|---|---|---|---|---|---|
| `LLM_QUOTA_EXHAUSTED` | `llm_quota_exhausted` | 429 | true | true | LLM supply exhausted on a long horizon (subscription window / hard cap). Product drops to the §12 floor until it resets. |
| `LLM_RATE_LIMITED` | `llm_rate_limited` | 429 | true | false | Short-horizon rate limit; callers may auto-retry. |
| `TURN_IN_PROGRESS` | `turn_in_progress` | 409 | true | false | One in-flight turn per session (reject-not-queue lock, §2.3); the client retries after observing `turn_complete` on the stream. Additive opt-in: a TEXT message carrying `queue: true` parks instead (cap 1, `202 {status:"queued"}`, durable `turn_queued` event) and starts at the active turn's terminal event; a second parked prompt, and every confirm/credential_grant message, still gets this 409 / a 400 respectively. Absent the flag, behavior is byte-identical to reject-not-queue. |
| `SESSION_NOT_FOUND` | `session_not_found` | 404 | false | false | Unknown session **or** other tenant's session (no existence oracle). |

The 429 pair is disambiguated by reset horizon, not by a distinct upstream error class
— the threshold is inferred, not yet measured live
(`research/agentsdk-usage-visibility.md`). `GRANT_REQUIRED` (401) and
`ENTITLEMENT_REQUIRED` (403) are reused unchanged from the frozen enum.

### 18.5 Back-edge dispatch contract (harness → app)

The spine's tools never execute anything in-process; they dispatch back into the app.
That back edge is a new authenticated surface:

- **Secret**: `LEAF_APP_DISPATCH_SECRET`, presented as header `X-Dispatch-Secret`.
  Comparison must be constant-time, matching both existing secret gates
  (`hmac.compare_digest` in `server/broker.py:340`; `timingSafeEqual` in
  `harness/src/server.ts:103–107`).
- **Trust model**: when the secret is present and valid, the app trusts the
  accompanying `X-Tenant-Id` header as the *already-resolved* tenant — the same model
  the broker uses for `X-Broker-Secret` (`server/broker.py:329–341`): a shared-secret
  caller is a trusted internal service relaying an identity it resolved upstream, not
  an end user. The harness only ever forwards the tenant id the app itself resolved
  via `require_tenant` when the message arrived, so the identity round-trips through
  one trusted hop.
- **Fail-closed**: `LEAF_APP_DISPATCH_SECRET` unset ⇒ the back edge is disabled and
  every back-edge request gets 401. There is no off-auth demo passthrough on this
  surface (stricter than the broker gate's off-live behaviour, `broker.py:332–338`,
  deliberately — the back edge has no browser fallback to protect).
- **Allowlist**: the secret is accepted **only** on `POST /api/run`,
  `GET /api/jobs/{id}`, `GET /api/capabilities` (`server/routers/capabilities.py:19`),
  `GET /api/tools` (`server/routers/tools.py:21`), `GET /api/drawings/*`,
  `POST /internal/agent/gate`, and — the R7 spine mount (2026-08-03, this
  revision) — `POST /api/platform/customize`,
  `GET /api/platform/customize/{change_id}`, and
  `POST /api/platform/customize/{change_id}/land`. The R7 read side
  (2026-08-27 revision) adds three GET-only exact paths:
  `GET /api/platform/customize` (tenant-scoped change listing, bounded page),
  `GET /api/platform/source` (one source file at the review base, size-capped,
  object-store read only), and `GET /api/platform/source/tree` (one directory
  level, bounded). The R7 routes keep every
  server-side admission unchanged (admin-tier `platform_customize`
  entitlement, R7 rollout listing, branch-only writes, exact-commit land ack;
  the read routes run the same admission chain and can write nothing);
  the `/internal/platform-customize/*` co-sign routes remain OFF this
  allowlist — the harness never holds co-sign authority. Every other route
  ignores the header entirely.
  Note: today's GET surface under `/api/drawings/*` is `…/intake`
  (`server/routers/drawings.py:46`) and `…/versions` (:104); checkout state rides the
  versions response (`_checkout_view`, :78–85). The allowlist means that read subset.
- **No privilege escalation**: a trusted `X-Tenant-Id` substitutes only for tenant
  *resolution*. Every downstream gate still runs against that tenant — the app
  entitlement gate (`server/routers/jobs.py:68–71`), the broker's independent tier
  re-check and quota/kill-switch chain, and the §18 agent gate itself. The dispatch
  identity is the tenant, never a privileged service account.

Sequencing on every spine tool call: harness `canUseTool` → `POST /internal/agent/gate`
→ `allow` ⇒ dispatch via `POST /api/run` (with `X-Dispatch-Secret` + `X-Tenant-Id`);
`deny` ⇒ the tool returns the deny reason as an error result the model can relay;
`awaiting_approval` ⇒ the tool returns `{proposed:true, confirmation_id, …}`, the loop
emits `proposed_run`, and the turn ends (split-turn confirmation, TTL 300 s — design
constant, args-bound to tool+params+dwg).

Grant kinds remain `oauth | api_key` and the contract is grant-kind-agnostic
throughout. Whether subscription (oauth) supply can serve stranger-facing tenants at
scale is an **open bet** (see the epistemic ledger in the operator's mission file — the MISSION canon); the BYO-API-key lane is the priced
fallback, and nothing in §18 assumes either answer.

### 18.6 §12 freeze note

> **§12 is frozen and is the documented degraded-mode floor.** `POST /api/nl-prompt`
> keeps its exact v1 semantics: global, no tenant/auth dependency, read-only,
> side-effect-free, zero-LLM (CONTRACT-ADDENDUM.md:225–289, :250–252, :607–608), and
> `server/tests/test_nl_router.py` must stay green and untouched. §18 is additive on
> top of it: with the harness stopped, the grant absent, or `llm_quota_exhausted`
> active, the prompt box, the classifier, every registered tool, and the whole job
> spine (job lanes per §10 of the pinned wire contract: fast pool `JOB_WORKERS_FAST=8`,
> slow pool `JOB_WORKERS_SLOW=4` with existing `JOB_WORKERS` honored as the slow-lane
> default, `server/jobs.py:53`; per-job SSE 0.5 s poll, `server/routers/jobs.py:145`;
> public job API/schema unchanged) work at full fidelity — public API and behavior
> identical to today's product.

### 18.7 Mount your LLM — per-session model + bring-your-own credential

A session may choose which Claude model runs its turns, and a turn may carry the
caller's own Agent SDK credential instead of the tenant's linked grant. Both fields are
ADDITIVE and OPTIONAL: a request that omits them behaves exactly as before, and the
harness falls back to its env default (`LEAF_SPINE_MODEL`, else `claude-sonnet-5`).

**Model allowlist** (`server/turn_runner.py` `ALLOWED_MODELS`, mirrored in
`harness/src/ports/modelAllowlist.ts`): `claude-sonnet-5`, `claude-opus-4-8`,
`claude-haiku-4-5`, `claude-fable-5`. Anything else is 400 `BAD_PARAMS` at the app
boundary, and is rejected again at the harness's own `POST /turn` validation — an
unknown id never reaches `sdk.query`. The runner is the Anthropic Claude Agent SDK
only; a multi-provider adapter is a separate lane, not this contract.

**Precedence**, highest first: the message's `model` → the session's stored `model`
(set at `POST /api/sessions`) → `LEAF_SPINE_MODEL` → `claude-sonnet-5`.

**Credential grant** (`credential_grant` on a message, never on session create), exactly
one of:

```
{"kind": "api_key", "api_key": "<token>"}
{"kind": "oauth",   "oauth_token": "<token>"}
```

It is used for THAT turn only. The app validates the shape, forwards it on the frozen
§2.1 `ConverseTurnInput` as `credential_grant`, and the harness maps it to the internal
`AgentGrant` injected into the runner's scrubbed child env (`buildScrubbedEnv`) in place
of `OAuthGrantProvider.getGrant`. Any other shape is 400 `BAD_PARAMS`.

The token itself must **look like** a credential: at least 24 characters, printable
ASCII (`0x21`–`0x7E`), no whitespace. This floor is load-bearing, not cosmetic — the
harness strips the credential from the transcript by literal match, so a value like
`"a"`, `"error"`, or `"` would either shred ordinary text or be unstrippable. The rule
is kept byte-identical to `PRINTABLE_ASCII` in `harness/src/redact.ts`, because a value
accepted here but unstrippable there is the worst of both (sol-critic #117 round 4,
#123 rounds 6-8). Real `sk-ant-…` keys and OAuth tokens run to ~100 chars, so nothing
genuine is rejected.

**Secret discipline** (all enforced, each with a regression test):

- **TLS required.** A `credential_grant` may only ride an encrypted request.
  `X-Forwarded-Proto` is CALLER-CONTROLLED and is consulted ONLY when the deployment
  asserts it sits behind a rewriting proxy via `LEAF_TRUST_FORWARDED_PROTO=1`;
  otherwise the real transport decides. `LEAF_ALLOW_INSECURE_CREDENTIAL=1` is the
  explicit dev/test opt-out. Every flag fails CLOSED.
- **Never persisted.** The credential is not written to `sessions`, `session_events`, or
  any transcript row; only `model` is persisted (a `sessions.model` column, migration
  `0019_sessions_model.sql`).
- **Never logged or echoed.** A credential the user pastes into their own prompt is
  stripped at the SOURCE — once, in `SpineTurnAdapter.runTurn`, from `text` and
  `messages[].text` before the turn runs. Scrubbing the source rather than each sink is
  deliberate (PR #123 round 7): sink-by-sink redaction made the harness and app-gate
  argument hashes disagree, so approval replay died as `args_mismatch`, and walking
  event objects to redact keys dropped colliding fields and let `__proto__` mutate the
  rebuilt prototype. Every downstream copy — transcript, gate pending record,
  confirmation `args_json`, wire — derives from the one scrubbed input, so they agree by
  construction. Only long, whitespace-free values are eligible (`isRedactableSecret`) and
  removal is LITERAL (`stripSecrets`), so a pasted Git SHA is not shredded. Because the
  model never receives the credential, it cannot echo it into a `text_delta`.
- **Not leaked by validation errors.** The 422 handler allowlists `loc`/`msg`/`type`, so
  a future pydantic key cannot start echoing the offending `input` value.
- **Not queueable.** A `credential_grant` message never parks in the busy-turn queue
  (which is in-memory) — see the `TURN_IN_PROGRESS` row in §18.4.

Shipped in PRs #117 / #123 / #133 (three sol-critic RED rounds; the TLS gate above is
the fix for the round-1 blocker that made it spoofable).

## §19 Guest drawing uploads (ephemeral tenants, honest retention, fail-closed extraction)

The signed-out "upload your own DWG/DXF" lane. Operator decisions D-1 (retention
window, default **24 h**) and D-2 (**guest extraction runs**, capped + rate-limited)
are built against their defaults; both are env-tunable without a code change.

**Surface** (`server/routers/uploads.py` + one public policy route in `site.py`):

* `POST /api/drawings/upload` — multipart `{file}` (.dwg/.dxf, capped by
  `LEAF_UPLOAD_MAX_BYTES`, default 25 MB). Auth OPTIONAL by design:
  live+Bearer → the verified account tenant; live+`X-Guest-Session` → the same
  guest tenant again; live+neither → a freshly minted `guest-<hex>` tenant plus
  an HMAC guest-session token (`LEAF_GUEST_SECRET`; unset → honest 503, never an
  unsigned identity); auth-off → the X-Tenant-Id stub world, byte-compatible.
  → `202 {drawing_id, tenant_id, tenant_kind, retention_expires_at|null,
  guest_session|null, status: "extracting"}` (§10-enveloped). Guest rate caps:
  `LEAF_GUEST_UPLOADS_PER_IP_PER_DAY` (10), `LEAF_GUEST_UPLOADS_PER_DAY` (100)
  → 429 `quota_exceeded` (each live DWG extraction is a paid APS run; DXF is
  parsed locally by default — CPU on the service, not an APS charge — but a
  paid APS run too when `LEAF_GUEST_DXF_EXTRACT=aps`, see the Extraction note
  below).
  GUEST uploads are idempotent by content AND engine: the drawing id derives
  from (tenant, resolved-DWG-engine, sha256(bytes)) — engine omitted for
  .dxf — so re-posting the same bytes as the same guest ON THE SAME ENGINE
  returns the SAME drawing's receipt (its CURRENT `status`, original
  retention window, fresh session token) and consumes no quota — an aborted
  upload whose 202 the client never saw is recovered by re-uploading, never
  duplicated. The same bytes on the OTHER engine are a DIFFERENT drawing
  with their own real extraction and quota count: the two engines produce
  different intake fidelity, so a dedupe hit must never silently override
  the visible toggle (sol-critic #552). A terminally `failed` attempt is
  the exception: its retry reuses the derived id, replaces the failure, and
  counts quota (it extracts again). Account uploads mint random ids (two
  intentional copies of one file stay two drawings).
* `GET /api/drawings/{id}/upload-status` — the upload marker's honest state
  (`extracting|ready|failed` + §10 error; stale `extracting` past
  `LEAF_UPLOAD_EXTRACT_TIMEOUT_S` is PERSISTED as failed/TIMEOUT).
* `GET /api/site/guest-upload-policy` — public config `{enabled,
  retention_hours, max_bytes, accepted, extract_live, dxf_local_ok}`. The
  frontend renders ALL retention/size copy from this payload.

**The fabrication trap is closed** (`write_loop.ensure_demo_drawing` guards):
a guest tenant NEVER bootstraps the cached rooftop intake (KeyError → 404), and
any drawing with an upload marker but no manifest refuses the bootstrap
(ValueError → 404 naming `/upload-status`). An uploaded id yields the user's
real extracted geometry or an honest error — never demo data labeled as theirs.

**Storage + retention**: guest tenants live in an ISOLATED local filesystem
store (`write_loop.guest_store_dir`, env `LEAF_GUEST_STORE_DIR`) because the
StorageBackend interface has no delete — the purge promise is only honest on a
store deletion can provably cover. `guest_uploads.retention_hours()` is THE one
constant: it stamps `retention_expires_at` at upload AND rules
`purge_expired()` (which honors the STAMP, not the current env — the promise
shown at upload time is the promise kept). The purge daemon
(`LEAF_GUEST_PURGE_INTERVAL_S`, default 300 s) deletes expired drawing dirs +
staged upload files, drops empty tenant dirs, and appends one
`purge.log.jsonl` line per deletion. Account uploads ride the default backend
with NO retention promise (and none is shown).

**Extraction** (`guest_uploads.run_extraction`, background thread + durable
marker — deliberately NOT the tool-shaped jobs spine): `.dwg` extraction has
two ENGINES, chosen per upload by the `engine` multipart field (the /try
panel's visible APS-cloud-vs-Local toggle sends it; values `local` | `aps`,
anything else is a 400) with `LEAF_GUEST_DWG_EXTRACT` as the server default
(`local` | `aps`; unset or unrecognized = `auto`: local when the converter is
present, aps otherwise). The resolved engine is recorded in the upload marker
(`extract_engine`) so poll-triggered re-extractions replay the uploader's
exact choice:

- `local` — `server/dwg_convert.py` runs GNU libredwg's `dwg2dxf` as a
  CAGED subprocess, in two distinct tiers. Bounding **misuse and DoS**: fixed
  argv, no shell, scratch dir always deleted, `LEAF_DWG_CONVERT_TIMEOUT_S` wall
  clock, `LEAF_DWG_CONVERT_MAX_OUTPUT_BYTES` output cap. Bounding a
  **memory-corruption exploit** in the native parser: hard rlimits (address
  space `LEAF_DWG_CONVERT_MEM_BYTES`, CPU, file size, core, fds
  `LEAF_DWG_CONVERT_NOFILE`, procs `LEAF_DWG_CONVERT_NPROC`) plus
  `no_new_privs` and no inheritable capabilities, applied by
  `prlimit(1)`/`setpriv(1)` around the exec. The two tiers are named separately
  because the first does NOT imply the second, and an earlier version of this
  text read as though it did. `LEAF_DWG_CONVERT_REQUIRE_CAGE=1` (set in the app
  image) makes a MISSING cage a refusal rather than a silent downgrade to an
  uncaged parse. Binary from `LEAF_DWG2DXF_BIN` or PATH, shipped in the app
  image — version pin, provenance + GPL-3 subprocess-boundary note in
  `deploy/Dockerfile.app` and `dwg_convert.py`. Converts the staged bytes to
  ASCII DXF for the SAME `dxf_intake` parser the .dxf path uses. Free,
  APS-free, honest: a malformed/hostile DWG lands a structured failed marker
  (BAD_PARAMS/TIMEOUT/INTERNAL, never a crash, never partial intake) and
  there is NO silent fallback to the paid engine in either direction. An
  upload explicitly requesting `local` where no converter exists is refused
  up front (503, before the quota charge). The v1 version blob still holds
  the RAW DWG bytes exactly like the aps engine (a later live write signs
  and sends them as HostDwg); only the intake cache comes from conversion.
- `aps` — byte-identical to the pre-engine behavior: at APS_LIVE=1 through
  `POST /broker/extract {upload: true}`; the broker's `_resolve_upload_dwg`
  applies the IDENTICAL strictness as the library resolver (bare name, no
  symlink, parent must BE `data/uploads/`) and the two namespaces never
  cross-resolve. At APS_LIVE=0 it fails honestly (APS_UNAVAILABLE).

The policy payload advertises the toggle (`dwg_engines`, `dwg_engine_default`,
`dwg_local_ok`) so the control the /try panel renders and the routing the
server enforces cannot drift apart.

`.dxf` extraction has two paths, chosen by `LEAF_GUEST_DXF_EXTRACT`:

- `local` (DEFAULT) — parsed by `server/dxf_intake.py` in both modes: a REAL
  parse of the user's bytes (LWPOLYLINE/POLYLINE + layer names; nothing
  invented), free and instant. This is the honest baseline: a DXF sent to the
  DWG extract Activity is rejected (its HostDwg localName is `input.dwg`, so
  `accoreconsole` sees `.dwg` bytes that are really DXF and returns
  ErrorStatus=434).
- `aps` (OPT-IN, requires APS_LIVE=1 AND the `LeafExtractDxf` Activity
  provisioned) — routed through the broker to a DXF-correct Activity
  (`da.client.EXTRACT_DXF_ACTIVITY`: HostDwg localName `input.dxf`, plus a
  save-safe QUIT so the WorkItem exits cleanly instead of blocking on a SAVEAS
  prompt). Same LISP extract as DWG, so it recovers INSERT / 3DFACE / geo /
  xdata, and costs a paid APS run per upload just like DWG (bounded by the same
  guest rate caps). An unrecognized flag value falls back to `local`, so a typo
  never silently bills APS.

**Entitlements** (§17 extended): new capability `upload` (explicit grant
required everywhere, per-key omission stays False) + new tier `guest` =
upload-only, every other capability false. The guest-session leg in
`deps.require_tenant` resolves a valid token to `TenantContext(tier="guest")`;
any token defect falls through to the ordinary 401.

**Suites** (registered in `scripts/run-all-gates.py`, one process per file):
`tests/test_guest_uploads.py` (14 — incl. the byte-provable their-geometry-
not-rooftop test), `tests/test_guest_fail_closed.py` (7),
`tests/test_guest_purge.py` (6 — short-override deletion proof),
`tests/test_guest_session_auth.py` (12 — incl. the json↔hardcoded policy
mirror), `tests/test_broker_upload_resolver.py` (19),
`tests/test_dwg_local_extract.py` (the local DWG engine: caged conversion,
fail-closed rejections, engine routing + policy advertisement; its
real-dwg2dxf test runs wherever the binary exists and skips allowlisted
elsewhere).

**Review round 2 hardening** (sol-critic round-1 findings, all addressed):
the offline `/api/session` route and offline `/broker/run` now resolve a
NON-DEFAULT `dwg` through the tenant's own store (real geometry for extracted
uploads; the fail-closed guards -> honest 404/BAD_PARAMS) instead of ignoring
it; v1 ingest stores the user's RAW bytes at the version key + parsed intake
at the sibling cache key (the live representation — a later live write sends
real source bytes to APS, never intake JSON labeled .dwg); staged uploads are
tenant-BOUND (`<tenant>--<drawing><ext>`, resolver re-validates both parts);
guest-session identities are restricted to an upload-only route allowlist in
`require_tenant` (upload + intake/upload-status/versions reads; everything
else 403s naming the boundary); guest quota is counted only AFTER validation;
the purge daemon starts regardless of the enable flag (stamped promises
outlive the feature switch) and a failed deletion logs `status: "failed"`,
never a false kill; the upload route pre-rejects oversized declared
Content-Length. NOTE (round-2 concern, closed in round 3 below): FastAPI
spools multipart to temp disk before the handler runs, so a length-less
(chunked) oversized body would cost transient disk regardless of the
declared-Content-Length check. Round 3's ASGI middleware is the real outer
wall against that — not an ingress proxy; this deployment has none in front
of the app container (ALB routes straight to it, confirmed against
leaf_platform.tf both environments), and ALB itself has no body-size-limit
config to set.

**Review round 3** (round-2 findings, all addressed): the compose stack
shares a `leaf-uploads` volume between app and broker with matching
`LEAF_UPLOADS_DIR` (staging is broker-resolvable in containers, guarded by a
lockstep test); a byte-counting ASGI middleware bounds the upload body
in-process (declared oversize -> 413; chunked oversize -> the multipart
parser aborts at the cap with its 400 — spool bounded either way); purge
deletes staged raw files FIRST and verifies both deletions before any
success receipt (a surviving staged file keeps the marker for a full retry);
.dxf uploads ingest as intake-JSON blobs (the demo drawing's own mock
representation — never raw DXF under a `.dwg` version key) while .dwg
uploads keep raw HostDwg-correct bytes; a presented Bearer always beats a
guest-session header in `require_tenant`.

**Honestly out of scope v1**: converting a guest tenant's drawing into a
freshly created account (the UI copy never promises it — "Create account"
starts a signed-in workspace); OSS-backed guest storage; DWG extraction
without APS.

## §20 Production authored-execution containment

The broker uses `LEAF_RUNTIME_ENV=production` as its explicit production
posture. In that posture, `LEAF_AUTHORED_EXECUTION` defaults to off.

Tracked implementations under `server/builtins/` and APS-only tools remain
available. A local Python implementation resolved from a tenant repository or
`server/authored/` is denied before `run_tool_dynamic` can import or execute
the file.

Whenever authored execution is EXPLICITLY armed (`LEAF_AUTHORED_EXECUTION` set
to `1`/`true`/`yes`/`on`, in ANY posture), and also whenever it is EFFECTIVELY
enabled in a DEPLOYED posture (`LEAF_RUNTIME_ENV` is `staging` or `production`;
the flag unset defaults authored execution ON outside production), a real
out-of-process sandbox tier must be engaged, or the broker refuses to start.
`validate_runtime_safety` reads the effective tier from
`tool_loader._sandbox_tier()` and refuses boot unless it is `subprocess`
(`LEAF_SANDBOX=e2b`) or `microvm` (`LEAF_SANDBOX=e2b-microvm` or
`LEAF_TOOL_SANDBOX_PROVIDER=e2b`). The same floor is applied per request:
in a deployed posture `_execute` denies a non-builtin tool with
`TENANT_DISABLED` unless authored execution is enabled and a real tier is
engaged, even if startup validation was bypassed by a direct function call. In
production the stricter existing check also requires
`LEAF_TOOL_SANDBOX_PROVIDER=e2b` (the micro-VM tier) plus an E2B credential
source.

When `LEAF_TOOL_SANDBOX_PROVIDER` is unset, a NON-EMPTY but unrecognized
`LEAF_SANDBOX` value (e.g. `1`, `true`, `on`) is a truthy-but-invalid footgun:
`_sandbox_tier()` returns `invalid` (not a silent `off`), execution is refused,
and an armed broker refuses to boot. With a valid provider set, the provider
takes precedence and the tier is the provider's (`LEAF_TOOL_SANDBOX_PROVIDER=e2b`
gives `microvm`), so a lingering legacy `LEAF_SANDBOX` value is inert. Explicit
disable values (`off`, `0`, `false`, `no`, `none`, `disabled`) and an unset
variable read as `off`. Local and demo deployments (no deployed
`LEAF_RUNTIME_ENV`) that do not EXPLICITLY arm `LEAF_AUTHORED_EXECUTION` keep
their existing in-process behavior byte-for-byte: authored execution defaults on
for them, but the sandbox floor keys on the explicit flag outside deployed
postures, so it does not fire for them.

The deployment manifests (`deploy/required-config.{broker,harness}.json`) do
not require the legacy `LEAF_SANDBOX` variable: the sandbox contract is
provider-based, conditional on arming, and enforced by the boot floor and the
request gate above rather than by an unconditional required-variable entry.

The harness enforces the matching author-time boundary in
`harness/src/runtimeSafety.ts::assertAuthoredSandboxBoundary`, also
posture-independent: when authored execution is armed the tenant tool sandbox
must be `LEAF_TOOL_SANDBOX_PROVIDER=e2b`, the author model stays on the trusted
harness host (`LEAF_AUTHOR_SANDBOX_PROVIDER` off or unset), and the harness must
not hold an E2B credential.
## §21 Tenant customization and controlled platform promotion

Status: **FROZEN v1, 2026-07-23**

The normative customization contract is
[`../contract/CUSTOMIZATION.md`](../contract/CUSTOMIZATION.md), with the
machine-readable schema at
[`../contract/customization.v1.schema.json`](../contract/customization.v1.schema.json).

R5 stages tenant customization bytes without changing the effective catalog.
R6 publishes one exact, approved staged receipt through
`POST /api/author/register`. No other R6 route exists. In live-auth mode,
legacy `POST /api/author` cannot publish or persist bytes. R7 remains disabled
and route-less.

Tenant Git is the byte authority. The customization coordination store is the
authority for effective catalog and platform-release pointers, workflow state,
approval, audit, and rollback. The platform release bundle is the sole authority
for frozen and slushy path policy. Tenant-controlled content cannot widen it.

Deployment rollback is complete only when it restores the prior image digests,
effective catalog commit and digest, effective platform release, and one
idempotent audit record.

## §22 Signed Design Automation completion receipt

`POST /da/callback` accepts a Leaf-owned terminal completion envelope signed
with `LEAF_CALLBACK_SECRET`. Its HMAC covers the timestamp, nonce, and raw body.
The broker consumes each `(job_id, nonce)` once through the selected durable
replay authority, then applies the terminal result through the job spine's
single completion transition.

The receipt seam is live for producers that can generate this exact signed
envelope. Native APS `onComplete` cannot generate it, so
`LEAF_CALLBACK_PRIMARY=1` is reserved. Selecting that flag fails closed without
submitting a WorkItem and without silently reverting to polling. Polling remains
the only active completion mode until an APS-to-Leaf translation adapter owns
output metadata, pending-job leases and heartbeats, and concurrency accounting.

## §23 Authored-body persistence is per-tenant (supersedes §15.C)

Provenance: NO operator ruling was made on §15.C. Asked directly on 2026-07-25
whether they had ruled, the operator stated that no ruling was made. That answer
is the record, and it retracts the claim PR #177 put here -- that the operator had
"confirmed the ruling as their own" -- which was never true. It restores what PR
#174 said: the ruling the freeze note calls for is OWED, not given.

What did happen, and is auditable, is narrower: the operator instructed a session
to land this section on 2026-07-25. Landing it ratifies the SHAPE the section uses,
which is what the freeze note requires. That is not a ruling on §15.C.

Do not restore the ruling claim without an operator statement recorded at the time
it is made. Two prior attempts asserted one from inference -- the original bare
"Ruling: operator, 2026-07-25", then PR #177 -- and both were wrong. A commit
message reporting what the operator said is not the operator saying it.

The queries that prompted this were raised on PR #156 and carried forward on PR
#168. PR #131 (merged 2026-07-25) shipped the per-tenant layout and changed no
documentation; PR #144 then left the resulting documentation gap open rather than
closing it. The freeze requires a new section rather than an in-place edit, which is
the shape used here: §15.C keeps its now-false sentence byte-for-byte and this
section carries the correct contract.

The template author path no longer persists to a flat `server/authored/<name>.py`.
A template-authored body is written to
`server/authored/<sha256(tenant_id)[:32]>/<name>.py`, where the directory name is
the first 32 hex characters of the SHA-256 of the exact tenant id. The registry
entry NAMES that file: `tool['entry']` is `authored/<dir>/<name>.py`, still
stored relative to `server/` so the broker resolves it regardless of cwd. The
tool also carries `tenant_id`, and the `authored_tools.json` store keys its dedup
on `(tenant_id, name)` rather than on `name`.

That dedup is not retroactive. The eviction filter matches a concrete tenant id,
so a row persisted before this change carries no `tenant_id`, never matches, and
is not evicted. Re-authoring a demo-tenant tool that predates the change leaves
both the legacy row and the new tenant-scoped row in the store.

The tenant-visible catalog is still correct: `deps.all_tools` attributes an
unscoped row to the demo tenant and folds entries by name in list order, so the
later tenant-scoped row wins, and `GET /api/tools`, digest issuance and
`POST /api/run` all read that fold. The leftover row is not invisible, though.
Consumers that read the raw store instead of the fold still see it:
`engine/selfcheck.py`'s effective-registry gate iterates every row of
`authored_tools.json` and reddens if a row's referenced file is gone, and
`/api/health` counts raw rows in `n_authored`. A leftover row also predates
`tenant_id`, so its original author is unrecorded. The demo tenant inherits it
by default, which is not evidence that the demo tenant wrote it.

Resolution is by PRECEDENCE, not by absolute location, and the distinction is
load-bearing for anyone reasoning about which bytes actually execute.
`resolve_local_file` joins `entry` onto the calling tenant's repo root FIRST and
onto `SERVER_DIR` second, accepting only a candidate contained by that root
(`server/tool_loader.py`). So a tenant-repo file at the same relative
`authored/<dir>/<name>.py` takes precedence over the `server/authored/` copy
named above. That ordering is deliberate — a tenant tool runs its OWN repo file —
but it means the entry identifies a path to resolve, not a guaranteed location.

This is a security fix, not a layout preference. A flat path let tenant B
overwrite tenant A's file, and a name-only dedup evicted A's catalog entry, so B
could hijack A's tool by name. The directory name is a hash rather than a slug
because character substitution is not injective: `tenant.a` and `tenant_a` would
collide onto one directory and re-open the same overwrite. Hex output is also
inherently path-safe and can never be `.` or `..`.

Wire consequence, and the reason this warranted a ruling: `entry` is not
internal. `deps.catalog_tool_view` spreads the whole tool, so `entry` is
serialized by `GET /api/tools`, and `deps.catalog_tool_digest` hashes every key
except `catalog_digest`, so the `entry` value is inside the digest that GET
issues and `POST /api/run` requires again. A template-authored tool's
`catalog_digest` therefore changes as soon as its record is written with the new
`entry` and the added `tenant_id`. It does not change on deploy alone:
`deps.load_authored_tools` reads `authored_tools.json` verbatim and migrates
nothing, so a record persisted before 2026-07-25 keeps its old `entry`, carries
no `tenant_id`, and keeps its old digest until something rewrites it. Callers
must re-read the catalog for any tool authored or re-authored on or after
2026-07-25. What invalidates a cached digest is a change to the entry that wins
the tenant's catalog fold, not a rewrite of the row a caller happened to read
from: re-authoring shadows an older row of the same name, so a digest cached
from that older row is refused by `POST /api/run` even though the row itself was
never rewritten. A record's own digest is a function of its content, so a record
that is never rewritten computes the same digest it did before 2026-07-25.

Visibility is a separate axis, and it did change on deploy. The authored store
used to be global to every tenant. `deps.all_tools` now attributes an unscoped
row to the demo tenant, so a non-demo tenant that could previously resolve a
legacy authored tool no longer finds it in its fold. If no other visible catalog
entry carries that name, `POST /api/run` answers `UNKNOWN_TOOL` rather than a
digest mismatch. If a lower tier does carry it, that record is what the tenant
now resolves.

§15.C's other statements are unaffected: the harness path still does NOT persist
to `server/authored/` or `_AUTHORED`, and `source` / `static_scan` are unchanged.

## §24 Account-controlled authored-tool publication (supersedes §21 R6 routing)

`POST /api/author/publication-requests` is the browser and agent publication
continuation. It accepts only `change_set_id`; the server reloads the durable
staged receipt and keeps commit, digest, tenant, and one-use confirmation binding
off the browser wire.

Independent publication approval is disabled by default. In that mode the server
issues a reserved server-subject confirmation for the exact staged receipt and
publishes it in the same replay-safe request. This is a technical receipt, not a
human review. A missing, invalid, or unavailable policy authority fails closed and
does not auto-publish. A disabled publication action blocks publication. An
existing durable denial also remains denied.

A current account owner may enable strict independent approval through
`GET|PUT /api/admin/account-controls`. The verified JWT subject must resolve to
the current tenant's active platform identity binding, and the server re-reads
that binding's current role and requires `owner` for every request. The PUT body is
`{tool_publication_approval_required:boolean, expected_revision:int}`. Writes use
compare-and-set revision control, preserve unrelated tenant policy fields, and
audit the verified account owner subject. The browser never receives an approval
secret or confirmation material. When strict mode is on, the existing independent
approval, denial, expiry, receipt verification, and recovery rules remain in force.

## §25 The capability/catalog contract map (card F-7)

Five capability-shaped endpoints exist. They are NOT five contracts by
design drift; each has a distinct scoping model, and this section is the one
place that names which endpoint each product surface consumes and why. No
endpoint scopes by client surface: the catalog's only tenant-data scoping
axis is the resolved caller tenant, and every product surface projects the
SAME tenant fold (`deps.all_tools`).

| Endpoint | Scoping | Consumer |
|---|---|---|
| `GET /api/capabilities` | tenant-scoped families over `deps.all_tools` (globals + the caller tenant's own repo tools; `internal_qa` filtered unless the ops-verified QA header) | the app shell's catalog rail on every product tab, and (F-7) each surface frame's live capability projection |
| `GET /api/tools` | the same tenant fold, flat list | catalog fallback when families fail; slash-tools |
| `GET /api/site/capabilities` | public, builtin-only projection with HTTP validators — no tenant data | the unauthenticated site/demo shell |
| `GET /api/platform/capabilities` | authenticated org/project submission context + live worker health + entitlements; "capability truth, never promoted from configuration alone" | run/build gating; the F-4 engine selector lands here |
| `GET /api/capabilities/promotion/stringing` | UNMOUNTED — `capabilities_promotion.router` is not included in `app.py`; dead by record | nothing (documented standalone) |

Surface projection (web, card F-7): each product tab's frame renders the
live `/api/capabilities` families through `SurfaceCapabilities`, filtered by
the surface's `familyIds` (browser: custom/measurement/selection; solar:
stringing/placement; ios: the whole catalog). This filter is presentation
focus in `web/src/site/productSurfaces.js`, not a server scoping model, and
must never become one silently.

Vendor note: `server/_vendor/mushy_fold` (pinned to
`LEAF-Solar-Design/mushy-code`) is the WIRED core of the tenant fold:
`deps.load_tenant_repo_tools` delegates the call-time registry read to the
vendored `mushy_fold.registry.load_repo_registry_tools` (PR #474), with
tenant resolution and the malformed-registry diagnostic staying in-repo. The
disposition and its exact import surface are recorded in
`_vendor/VENDOR-PIN.json` and tripwired by
`tests/test_mushy_fold_vendor_disposition.py`.
