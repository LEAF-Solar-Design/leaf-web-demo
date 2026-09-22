# Leaf Web Demo — FROZEN CONTRACT v1

**Goal:** a browser demo where a person opens a real DWG (rendered in *our* three.js, not Autodesk's), runs AI-authored CAD tools whose engine work executes on **APS Design Automation** (occasional WorkItems), and sees results — proving the web lane of "build your own CAD tools with AI."

**Golden sample data (already real, use it):** `C:/tmp/leaf-web-demo/data/rooftop_demo.intake.json` — produced by the proven extractor from `rooftop_demo.dwg` (2345 polylines / 4 layers, extracted headless in 6 s). Every lane builds against THIS file so the demo works before APS is wired.

**Ownership (DISJOINT — do not write outside your dir):**
- Lane A → `C:/tmp/leaf-web-demo/da/` (APS Design Automation client)
- Lane B → `C:/tmp/leaf-web-demo/engine/` (extraction script, tools, registry)
- Lane C → `C:/tmp/leaf-web-demo/web/` (frontend)
- Lane D → `C:/tmp/leaf-web-demo/server/` (demo backend glue)
- Root owns `contract/`, `data/`, and ALL live APS calls (bucket/upload/appbundle/activity/workitem).

---

## 1. Intake JSON (extractor output → frontend render input) — FROZEN

```jsonc
{
  "dwg": "string (source path/name)",
  "layers": ["Panels", "0", ...],              // string[]
  "polylines": [                                // the main geometry — 2345 in the sample
    {
      "layer": "Panels",
      "closed": true,
      "pts": [[x,y,z], [x,y,z], ...],           // WCS coords, numbers; z often ~constant
      "xdata": null,                            // or object of app->string[]
      "handle": "9A2"                           // DWG entity handle (stable id)
    }
  ],
  "inserts":   [ {"name","layer","pt":[x,y,z],"rot","scale":[x,y,z],"handle"} ],  // may be []
  "faces3d":   [ {"layer","p1":[x,y,z],"p2","p3","p4"} ],                          // may be []
  "blockdefs": {},                              // object; may be empty
  "geodata":   ["none"] or [ {dxf pairs} ],
  "images": [], "imageNames": []
}
```
Frontend MUST handle empty inserts/faces3d (the sample has only polylines). Render polylines as
closed/open paths in a top-down 2D view (this is a rooftop plan), color-by-layer, fit-to-bounds, pan+zoom.

## 2. Tool package (registry entry) — FROZEN

```jsonc
{
  "name": "count-by-layer",                     // kebab-case, unique, = MCP tool suffix
  "version": "1.0.0",
  "description": "Counts entities per layer.",
  "kind": "script",                             // "script" (LISP/scr on DA) | "appbundle" (compiled)
  "engine_op": "count_by_layer",                // activity/command id the DA layer runs
  "params": { "type": "object", "properties": { }, "required": [] },  // JSON Schema
  "returns": { "type": "object" },
  "capabilities": ["drawing.read"],             // drawing.read | drawing.write
  "provenance": { "author": "agent|user", "created": "<iso8601>" }
}
```
Registry file: `engine/registry.json` = `{ "tools": [ <tool package>, ... ] }`.

## 3. Result envelope (WorkItem output → frontend) — FROZEN

Every tool run (mock OR real APS) returns EXACTLY this shape:
```jsonc
{
  "ok": true,
  "tool": "count-by-layer",
  "version": "1.0.0",
  "result": { /* tool-specific data, e.g. {"counts": {"Panels": 2345}} */ },
  "overlay": {                                  // OPTIONAL — how the frontend shows the effect
    "highlight_handles": ["9A2","9A3"],         // entity handles to emphasize in the viewer
    "markers": [ {"pt":[x,y], "label":"gap"} ], // points to draw
    "polylines": [ {"pts":[[x,y]], "color":"#f00"} ]  // extra geometry to overlay
  },
  "timing_ms": 412,
  "cost": { "engine_seconds": 3.1, "usd_est": 0.005 },  // null until real APS run
  "error": null
}
```

## 4. HTTP API (frontend ↔ server) — FROZEN

- `GET  /api/session?dwg=rooftop_demo` → `{ intake: <Intake JSON §1> }` (server extracts or serves cached sample)
- `GET  /api/tools` → `{ tools: [ <tool package §2> ] }`
- `POST /api/run` `{ "tool": "count-by-layer", "params": {}, "dwg": "rooftop_demo" }` → `<Result envelope §3>`
- `POST /api/author` `{ "description": "count panels within 18in of the roof edge" }` → `{ tool: <tool package §2>, code: "<generated script>", preview: "..." }`

Server MUST work with the sample data + mock tool results when APS is not yet wired (env `APS_LIVE=0`),
and switch to the Lane A DA client when `APS_LIVE=1`. Frontend never knows the difference.

## 5. DA client interface (server → Lane A) — FROZEN

Python module `da/client.py` exposing:
- `extract(dwg_local_path: str) -> dict`  # returns Intake JSON §1 (runs the extract WorkItem)
- `run_tool(dwg_local_path: str, tool: dict, params: dict) -> dict`  # returns Result envelope §3
- `auth_token() -> str`  # 2-legged; creds at `~/.aps/credentials.json`

Reference (working auth + engine list): `C:/tmp/aps-spike/probe-aps.ps1`.
Extraction recipe to port to a DA activity: the LISP block in
`C:/Users/ehaug/OneDrive/Documents/GitHub/utility-estimation/extracts/dwg_intake.py`.
APS AutoCAD engines available (confirmed): `Autodesk.AutoCAD+24_3` (net48), `+25_1`, `+26_0` (net8).

## 6. MVP demo scope (what must work end to end)

1. Open `rooftop_demo` → see 2345 panels rendered, colored by layer, pan/zoom. (Lane C + sample data)
2. Run **count-by-layer** → table of counts. (read-only, safest first tool)
3. Run a **spatial** tool (e.g. **highlight-panels-near-edge** or **measure-panel-area**) → viewer overlays the result. (Lane B tool + Lane C overlay)
4. **Author** a tool from a text description → new tool appears in the list and is runnable. (Lane D generate + Lane B template)
5. At least one tool run executed for REAL on APS with a measured cost receipt. (Root, live)

Stub allowed for the demo: real LLM tool-gen may be templated for a constrained family (select-by-layer + measure/count); mark it. Everything else must be real.

---

<!-- Sections 7-10 PROMOTED 2026-07-17 from server/CONTRACT-ADDENDUM.md, operator-approved. -->

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

> Section 8 is FROZEN (2026-07-22, census #4 credential-broker keystone).
> Full normative text: `server/CONTRACT-ADDENDUM.md` §8. The ledger line
> schema is machine-frozen at `server/broker_ledger.schema.json`
> (`leaf.broker-ledger-line.v1`) and gated by
> `server/tests/test_broker_ledger_schema_static.py`.

**Security property (tested):** the tenant-facing app process NEVER holds the
APS credential at `APS_LIVE=0` — the tested condition. `server/broker.py` is a
separate process (default `:8140`, env `BROKER_PORT`; its own non-root
container, unpublished on the host network) and is the ONLY code that loads
`da/client.py` on the tool-execution path; the TWO documented app-side seams
are the §11 store-read paths — the legacy loader `deps.get_da_client()`
(drawing reads at `APS_LIVE=1`), every call-site APS_LIVE-gated, and
`write_loop.py`'s lazy `import store`, whose `da/store.py` imports `da/client`
as a module but reads the credential file only on a live token fetch —
promotion through the broker a documented follow-up. `app.py`/`jobs.py` contain no `da` import (static gate
`server/tests/test_no_da_imports_static.py`, whole app-side sweep, AST-level +
recursive) and run correctly with `APS_CRED=/nonexistent` (dynamic test). The
app reaches tool execution ONLY via `server/broker_client.py` → HTTP
(`BROKER_URL`, default `http://127.0.0.1:8140`); broker down →
`BROKER_UNREACHABLE`. Every protected
`/broker/*` route requires the `X-Broker-Secret` hop (env
`LEAF_BROKER_SECRET`, constant-time compare; 503 fail-closed when live and
unset; `/broker/health` stays open for liveness).

| Method | Path | Behaviour |
|---|---|---|
| POST | `/broker/run` | `{tenant_id, tool, params, dwg, aps_live, dwg_version?}` → extended §3 envelope. `aps_live:false` → pure-python mock path; `true` → `da.client.run_tool` (live) |
| POST | `/broker/extract` | `{tenant_id, dwg, upload?}` → `{intake}`; the ONLY extraction path at `APS_LIVE=1` (`GET /api/session` relays through it) |
| POST | `/broker/tenants/{tid}/disable` / `.../enable` | per-tenant kill-switch, persisted to `broker_tenants.json` (env `BROKER_TENANTS`). Disabled tenant → `TENANT_DISABLED` (retryable:false), APS never touched; corrupt state fails CLOSED (refuses boot rather than disarm) |
| POST | `/broker/reap` | cancel orphaned WorkItems; credential holder only |
| GET | `/broker/health` | open; role, ledger path, disabled tenants |

**Attribution ledger** (metering/quota chokepoint other sessions read): every
`/broker/run` — including kill-switch and quota denials — appends exactly ONE
JSONL line to `server/broker_ledger.jsonl` (env `BROKER_LEDGER`), frozen
schema `leaf.broker-ledger-line.v1`:
`{ts, tenant_id, tool, engine_op, aps_endpoint, aps_live, engine_seconds, usd_est, status}`.
New keys only as additive optional fields; frozen keys never renamed, retyped,
or dropped. Preflight order on `/broker/run` (tested): kill-switch → spend cap
(402) → tool shape → tier entitlement re-check → daily run quota (429, live
path only) → schema validation → execution.

**Egress allowlist (v1, in-process):** every outbound HTTP request from the
broker process passes a central guard (patched `requests` adapter). Allowed:
`developer.api.autodesk.com` + `*.amazonaws.com` (OSS direct-to-S3 signed
URLs used by the frozen §5 client) + `BROKER_EGRESS_EXTRA` env. Anything else
raises `EgressBlocked`, so tenant-authored `engine_op`s cannot redirect
egress. Network-layer enforcement (container/proxy) is another session.

**v1 assumptions:** the demo is single-process, so "tenant container" ==
"the app process"; `tenant_id` arrives as an `X-Tenant-Id` header stub until
verified claims replace it (`LEAF_AUTH_LIVE=1`). The mock path honors
`params._qa_sleep_s` (capped 30 s) as a QA latency-simulation hook. Operator
kill-switch procedure: `docs/runbooks/broker-kill-switch.md`.

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
{
  "error": null | {
    "error_code", "message", "retryable", "retry_class", "actor", "next_action"
  },
  "degraded_mode": false
}
```

- `error_code` enum (frozen): `UNKNOWN_TOOL, BAD_PARAMS, APS_UNAVAILABLE,
  BROKER_UNREACHABLE, WORKITEM_FAILED, TIMEOUT, TENANT_DISABLED, INTERNAL`.
- The §3 run envelope keeps all existing success fields and ADDS
  `degraded_mode`; other bodies (`/api/session`, `/api/tools`, `/api/author`,
  `/api/health`, jobs, capabilities) are extended ADDITIVELY — existing keys
  unchanged.
- Every failure names its retry class, the actor who can resolve it, and one
  plain next action. The legacy `retryable` boolean remains unchanged for
  existing clients.
- `degraded_mode: true` ⇔ APS_LIVE execution was requested but the run fell
  back to the pure-python path (UI: "used the local fallback, not the cloud
  solver").
- HTTP status codes stay sane (404 unknown tool, 403 disabled tenant, 502
  broker/APS, 504 timeout…); the machine-readable part is the body.
  Validation errors (422) are also enveloped (`BAD_PARAMS`).
- Machine-checkable schema: `server/envelope_schema.json` (does not touch
  Lane B's `engine/envelope_schema.json`).

## Solar W1 design graph v1

The Leaf Automation Rooftop parity lane uses
`contract/solar-design-graph.v1.schema.json` (JSON Schema draft 2020-12).
`server/solar_design_graph.py` validates and serializes this public graph.
`server/solar_dependencies.py` computes memberships and transitive dependents.
These are helpers for the existing capability, entitlement and job rails in
sections 7 to 10, not a separate runtime or a saved-design claim for an overlay.

The root contains `project`, `settings`, `electrical_zones`, `frames`, `panels`,
`strings`, `inverters`, `routes` and `schedules`. All W1 fields are explicit in
the schema, including the six cold-voltage values, sizing confirmation,
counters, matrix cells, sequences, panel assignments, circuit endpoints,
inverter input assignments, route units and schedule source revision.
Every entity has `id`, `kind`, `rev`, `provenance`, `extra` and `validity`.
Entity `kind` is the stable type; `circuit_kind` and `route_kind` hold subtypes.
Assignments use zero-based `seq`, matrix row/column and input numbers. Null
assignment fields represent an unassigned panel. Arrays preserve order.

Application ids have the form `leaf:<kind>:<uuid4>` and are never derived from
handles. Handles are source provenance only; import/export adapters own the
separate drawing-scoped identity mapping. Geometry references may name objects
preserved outside the W1 entity set. The private drawing storage mapping is not
part of this public contract.

`graph_schema_version` is 1. `rev` starts at 0 with `parent_rev: null`; each
accepted batch advances it once and names the prior revision. Entity and
schedule source revisions cannot exceed the graph revision. `require_revision`
refuses a stale job result before apply. Actual atomic apply, undo, persistence
and drawing transport remain the responsibility of the existing execution rails.

Graph and provenance source hashes and catalog versions survive serialization.
Unknown JSON properties at every level and `extra` contents survive unchanged.
Adapters capture unmodeled source fields in `extra`, merge them before known
fields on export, and report collisions. `opaque_stores` and `orphaned_xdata`
carry bounded payload references, hashes and byte lengths, never raw service
payloads. The adapter preserves referenced bytes and untouched drawing objects;
the graph validator does not fetch or relocate them.

Coordinates and geometry lengths use normalized metres; angles use degrees.
`length_ft` remains feet and must never be rescaled as a drawing length.
The units block records the original drawing units, scale, winning source,
WCS-to-UCS matrix, elevation datum, optional CRS, user preference and warnings.
The resolver wins over the feet preference and records any disagreement.
Missing or unknown units, nonfinite numbers, malformed input and unsupported
schema versions refuse mutation. The v1 transport rejects unsupported versions
rather than attempting a lossy conversion. A separate opaque read-only viewer
may retain future documents without passing them to these mutation helpers.

A correction that transfers one panel between two distinct strings changes
**both** ordered memberships and **both** module counts. Its panel assignment
and affected matrix/sequence data must also agree. It does not mean exactly
one string changed. Dependency calculations use both the prior and new edges
to include strings, assigned equipment, connected routes and source-linked
schedules. Callers record stale validity reasons or recompute those consumers
before presenting them as valid. Unrelated payloads remain unchanged.

Transport bounds are 16 MiB, 500,000 JSON nodes, depth 32 and at most 100,000
items per collection. Unknown fields are subject to the same bounds. Validation
errors expose named reasons, not input values. The contract uses the existing
`jsonschema` dependency and performs no network access.
