# Leaf server — backend backbone (FastAPI, two processes)

Async job spine + APS credential broker + capability catalog + extended error
envelope. Contract: frozen `contract/CONTRACT.md` §1–§6 plus the proposed
§7–§10 in [`CONTRACT-ADDENDUM.md`](CONTRACT-ADDENDUM.md) (operator promotes).

## Run (TWO processes)

```bash
cd C:/tmp/leaf-web-demo/server
pip install -r requirements.txt

# 1) the APS broker — the ONLY process that may hold the APS credential
uvicorn broker:app --port 8140
#   or: BROKER_PORT=8140 python broker.py

# 2) the tenant-facing app (mock demo mode)
APS_LIVE=0 uvicorn app:app --port 8130
#   or: APP_PORT=8130 python app.py
```

PowerShell: `$env:APS_LIVE=0; python -m uvicorn app:app --port 8130` (second
terminal: `python -m uvicorn broker:app --port 8140`).

Default ports: app **8130**, broker **8140** — both env-overridable
(`APP_PORT`/`BROKER_PORT` for `python x.py`, `--port` for uvicorn; the app
finds the broker via `BROKER_URL`, default `http://127.0.0.1:8140`). CORS is
permissive for localhost dev.

Env: `APS_LIVE` (default 0 = mock), `JOBS_DB`, `JOB_MAX_S` (540),
`HEARTBEAT_STALE_S` (60), `JOB_WORKERS` (4), `BROKER_URL`, `BROKER_LEDGER`,
`BROKER_TENANTS`, `BROKER_EGRESS_EXTRA`, `APS_CRED` (broker only),
`LEAF_AUTHOR_LLM` (default 0).

## Endpoints

### App (tenant-facing — never holds the APS secret)

| Method | Path | Behaviour |
|---|---|---|
| POST | `/api/run` | **202** `{job_id, status:"submitted"}` (async; `X-Tenant-Id` header, default `demo-tenant`). **Breaking change** vs the old sync demo — see ADDENDUM §7 |
| POST | `/api/run?wait=1` | blocks; returns the final §3 envelope (old smoke path) |
| GET | `/api/jobs/{job_id}` | durable job record (`result` = §3 envelope when complete) |
| GET | `/api/jobs/{job_id}/stream` | SSE status transitions until terminal |
| GET | `/api/jobs?tenant_id=&limit=` | recent jobs (reconnect after tab close) |
| GET | `/api/capabilities` | tools grouped into families; internal/QA tools filtered server-side (`X-Internal-Role: qa` opts in) |
| GET | `/api/session?dwg=` | `{intake: <§1>}` (cached at APS_LIVE=0) |
| GET | `/api/tools` | flat back-compat tool list (registry + authored) |
| POST | `/api/author` | `{description}` → `{tool, code, preview}`; registered + runnable |
| GET | `/api/health` | diagnostics |

### Broker (`broker.py` — the credential/attribution/kill-switch chokepoint)

| Method | Path | Behaviour |
|---|---|---|
| POST | `/broker/run` | run one tool for one tenant (mock or live); appends exactly ONE ledger line |
| POST | `/broker/tenants/{tid}/disable` / `enable` | per-tenant kill-switch (persisted) |
| GET | `/broker/health` | role + ledger path + disabled tenants |

Every response body carries `error: null|{error_code,message,retryable}` +
`degraded_mode: bool` (schema: `envelope_schema.json`; codes in `envelopes.py`).

### Quick verify (90-second demo path)

```bash
curl localhost:8130/api/capabilities
JOB=$(curl -s -XPOST localhost:8130/api/run -H "Content-Type: application/json" \
     -d '{"tool":"count-by-layer","params":{},"dwg":"rooftop_demo"}' | python -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl localhost:8130/api/jobs/$JOB          # -> status complete, result.result.counts.Panels == 2345
curl localhost:8130/api/jobs/$JOB/stream   # SSE until terminal
```

## Tests

```bash
cd C:/tmp/leaf-web-demo/server && python -m pytest tests/test_backbone.py -q
```

Boots real broker + app subprocesses (APS_LIVE=0, `APS_CRED=/nonexistent` for
the app — proving the credential boundary), covers 202 latency, restart
durability, TIMEOUT, kill-switch, ledger, catalog filtering, envelope schema,
and legacy-shape regression.

## Files (ownership map for sibling sessions)

- `app.py` — **stable composition root**; do not restructure. Add routes in your own router.
- `deps.py` — shared seam (settings, tool registry, `require_tenant` stub) → owned by `auth0-identity-signup`
- `routers/session.py` → `auth0-identity-signup`
- `routers/tools.py` (+ `da/client.py`) → `dynamic-tool-loader`
- `routers/jobs.py`, `routers/capabilities.py`, `routers/author.py` — this lane (async spine + catalog)
- `routers/projects.py` → `project-job-schema` adds it
- `broker.py` (+ `da/provision_live.py`) → `aps-multitenant-provisioning` extends
- `jobs.py`, `broker_client.py`, `catalog.py`, `envelopes.py`, `capability_families.json`, `envelope_schema.json` — this lane
- `tools_fallback.py` — pure-python APS_LIVE=0 tool logic + authoring templater (used by the broker mock path and authoring)
- Runtime artifacts (NOT committed): `jobs.db`, `broker_ledger.jsonl`, `broker_tenants.json`, `authored_tools.json`

## Boundaries

- This lane writes only under `server/`. `engine/`, `da/`, `contract/`, `web/` are read-only here.
- The app process must NEVER import `da.*` — all APS-capable execution goes
  through `broker_client` → the broker HTTP boundary (enforced by tests).
- No live APS calls at `APS_LIVE=0`; live runs are a broker-only concern.
