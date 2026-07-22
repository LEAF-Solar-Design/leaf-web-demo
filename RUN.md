# Leaf platform — how to run it

Leaf is a **natural-language CAD-tool-building platform**: one prompt box, three
lanes (**Run / Solve / Build**), a per-tenant "mushy" tool repo the agent reshapes
by talking to it, and CAD compute that executes on **Autodesk APS Design
Automation** — all behind a credential broker that no tenant code can reach.

This is the platform's local run guide. For the containerized stack see
`deploy/README.md`; for the component map see `docs/ARCHITECTURE.md`; for the
mission and the honest bet-ledger see `~/.claude/MISSION.md`.

> This repo grew from a single-tenant July-17 demo into a multi-tenant platform.
> If you remember a synchronous `:8130` that returned results inline and "4
> tools" — that is gone. `POST /api/run` is now an async job spine, the catalog
> is per-tenant, and authoring drives a real Agent-SDK harness. This document
> describes the platform as it stands today.

---

## What it is (the one-paragraph version)

A person opens a real DWG (rendered in **our** three.js, not Autodesk's) and types
what they want. The NL router (`§12`) classifies the prompt into a lane:

- **Run** — execute a registered tool against the drawing (count, measure,
  highlight, delete-and-version, …). Real-engine work runs on APS DA; the same
  tools also run pure-python offline.
- **Solve** — optimisation lane (declared in the contract; nothing executes yet —
  the router says so honestly).
- **Build** — author a NEW tool from plain English. The **Build lane drives a real
  Claude Agent SDK loop** that edits the tenant's own mushy repo, commits it, and
  folds the new tool into that tenant's catalog. Once authored, a tool runs
  **zero-LLM** — no model in the request hot path, ever.

Every tenant gets its **own** repo, its **own** Claude grant, its **own** tool
catalog, and its **own** metered spend. The APS credential lives in exactly one
process (the broker); the tenant-facing app never holds it.

---

## Two ways to run it

### (a) Local dev — one command

```bash
cd C:/tmp/leaf-web-demo
python scripts/start-leaf.py
```

Boots the whole local stack at `APS_LIVE=0` (mock — no cloud, no secrets):

| Service | URL | Role |
|---|---|---|
| **web** | http://127.0.0.1:5175 | Vite/React/three.js dev server (**open this**) |
| **app** | http://127.0.0.1:8130/api/health | tenant-facing FastAPI (jobs, catalog, write loop, NL router, usage/ops) |
| **broker** | http://127.0.0.1:8140/broker/health | sole APS-credential holder, ledger, kill-switch |

The script picks **free ports automatically** if a default is already taken (a
stale squatter from a previous run is a real gotcha — it warns and moves on),
wires every dependent URL to match, waits for each `/health` to go green, prints
the URLs, and tears the whole tree down on **Ctrl-C** with no orphaned
processes left behind (a Windows job-object guard reaps children even on an
abrupt exit). The web dev server is launched with `VITE_MOCK=0` so the browser
talks to the live app.

Add the real Build lane (compiled Agent-SDK harness sidecar on `:8150`):

```bash
python scripts/start-leaf.py --with-harness
```

The harness comes up healthy with **no** Claude token, but **authoring a tool
needs a per-tenant grant** — without one, `POST /api/author` returns a calm
`GRANT_REQUIRED` (HTTP 401) and **spends no LLM credit** (the SDK is never
constructed). Link a grant per tenant with `POST /api/tenant/claude-grant
{token}` (the app forwards it to the harness store and never persists, logs, or
echoes it). Flags: `--no-web` (backend only), `--with-harness`, `--broker-port`,
`--app-port`, `--harness-port`, `--web-port`.

Prerequisites: Python 3.12+ with `fastapi`/`uvicorn` (verified on 3.13.5), Node
20+ with `web/node_modules` installed (`cd web && npm install`), and — for
`--with-harness` — the compiled sidecar (`cd harness && npm install && npm run
build`).

### (b) Container stack — `docker compose`

The full four-service stack (web + app + broker + harness), containerized and
proven to run at `APS_LIVE=0` with no secrets. See **`deploy/README.md`** for the
quickstart, the env/volume topology, and the opt-in cloud mounts — not duplicated
here.

```bash
cd C:/tmp/leaf-web-demo
docker compose build && docker compose up -d   # then http://localhost:8080
```

---

## Env-var contract

Set on the app and/or broker; the local dev script fills the ones below in for
you. Full container wiring is in `deploy/README.md`; the authoritative behavior
is in `server/CONTRACT-ADDENDUM.md`.

| Env var | Default | Purpose |
|---|---|---|
| `APS_LIVE` | `0` | `0` = pure-python results from real geometry (no cloud); `1` = route runs through the broker to APS DA |
| `APP_PORT` | `8130` | app (`python app.py`) listen port |
| `BROKER_PORT` | `8140` | broker (`python broker.py`) listen port (binds loopback) |
| `BROKER_URL` | `http://127.0.0.1:8140` | app/harness → broker HTTP boundary (§8) |
| `HARNESS_PORT` | `8150` | Agent-SDK harness sidecar (`§15`) |
| `LEAF_AUTHOR_HARNESS_URL` | _(unset)_ | app → harness author sidecar; unset = local templater path |
| `LEAF_TENANTS_DIR` | _(unset)_ | base for per-tenant mushy repos (`<base>/<tenant_id>`); **must match on app + harness** (§16.H) |
| `LEAF_TENANT_REPO` | _(unset)_ | single-repo override for the **demo** tenant (legacy wave-3 mode) |
| `LEAF_GRANTS_DIR` | `C:/tmp/leaf-grants` | per-tenant Claude token files (harness-only; `mode 0600`, never logged) |
| `LEAF_GRANT_FILE` | _(unset)_ | demo-tenant OAuth grant fallback path (token value never printed) |
| `LEAF_STORE_DIR` | `server/drawings/` | versioned drawing store; **shared** app↔broker (§11) |
| `BROKER_LEDGER` | `server/broker_ledger.jsonl` | attribution ledger — broker writes, app reads for `/api/usage` (§13) |
| `BROKER_TENANTS` | `server/broker_tenants.json` | persisted per-tenant kill-switch |
| `JOBS_DB` | `server/jobs.db` | durable async job records (restart-survivable, §7) |
| `JOB_WORKERS` / `JOB_MAX_S` | `4` / `540` | job worker pool size / per-job timeout seconds |
| `DATABASE_URL` | _(empty)_ | **opt-in** platform Project/Job/org persistence (Postgres/Neon); empty ⇒ those endpoints stay dark |
| `LEAF_AUTH_LIVE` | `0` | `1` = verify Auth0 RS256 JWTs (needs `server/requirements-auth.txt`); `0` = open demo, `X-Tenant-Id` stub |
| `LEAF_ENTITLEMENTS_FILE` | `server/entitlements.json` | tier → capability policy (run_read/run_write/build), §17 |
| `LEAF_TENANT_CAP_USD` / `LEAF_USAGE_CAPS` | _(unset)_ | per-tenant hard spend cap (broker pre-flight); off ⇒ no cap |
| `VITE_MOCK` / `VITE_API_BASE` | `1` / `http://localhost:8130` | web: `0` = hit the live app; API base URL |

---

## What's real vs what needs operator setup

**Real, out of the box (`APS_LIVE=0`, no secrets):**

- The web UI renders the rooftop (2345 real panels) from the proven extract.
- **Read tools through the real async spine** — `POST /api/run` → `202 {job_id}` →
  poll/SSE → §3 envelope. `count-by-layer` → `{Panels: 2345}` computed by the
  app→broker chain over real geometry.
- **Write loop** (§11) — a `drawing.write` run produces an immutable **version 2**
  with `undo` / `redo` / `versions`, restart-survivable.
- **Template authoring** (§15) — `POST /api/author` registers a runnable tool with
  zero LLM and zero Claude grant.
- **NL prompt router** (§12), **usage / ops / capabilities / entitlements** reads.
- **Multi-tenant isolation** — tenant A's authored tools are invisible to tenant B;
  per-tenant spend from the broker ledger.

**Needs an operator to switch on (the cloud legs):**

| Capability | What to provide |
|---|---|
| **Real APS run/extract (`APS_LIVE=1`)** | `~/.aps/credentials.json` mounted to the broker (`APS_CRED`), then `APS_LIVE=1` on app+broker. Credential is read ONLY by the broker, ONLY on live runs, never baked into an image. |
| **Real Claude authoring (Build lane)** | A per-tenant grant via `POST /api/tenant/claude-grant` — either a "sign in with Claude" OAuth token (`claude setup-token`, individual-use) or a **BYO Anthropic API key** (enterprise, `sk-ant-api…`, auto-detected). Boot the harness with `--with-harness`. |
| **Platform Project/Job/org persistence** | `DATABASE_URL` (Postgres/Neon). Empty by default so `/api/orgs` etc. stay dark on a plain demo. |
| **Real tenant identity + tier gates (`LEAF_AUTH_LIVE=1`)** | Auth0 RS256 JWT verification (needs PyJWT via `server/requirements-auth.txt`). Off = open demo, tier `demo` = full access. |
| **Entitlement floor proof (platform jobs lane)** | `python scripts/entitlement-gate.py` — binary READY/NOT-READY: exit 0 only when an entitled org's solve succeeds AND a restricted org's solve is DENIED (403 `entitlement_required`). Needs `DATABASE_URL`/`platform/.env.local`. |

---

## The proof it runs on real cloud AutoCAD (already executed — the credibility)

Every number below was measured on **APS Design Automation** (production, engine
`Autodesk.AutoCAD+26_0`, rate ~$6/engine-hour) and checked against a pure-python
oracle. Receipts live in `data/`.

**Read leg — DWG→JSON extraction on the cloud engine**
- Output **byte-identical** to local (4 layers / 2345 polylines, first+last
  geometry exact); billable **3.3 s → $0.00554/run** ($5.54 per 1000 runs).

**Read tools live on APS** (`RUN.md` history; each matches its oracle)
- `count-by-layer` → `{Panels: 2345}` (oracle 2345) · ~$0.008 · 4.06 s
- `measure-panel-area` → 48718.18 sqft (oracle 48718.195) · ~$0.007 · 2.37 s
- `highlight-panels-near-edge(200)` → 276 handles (oracle 276) · ~$0.008 · 2.71 s

**Write leg — LISP path** (`data/write_spike_receipt.json`)
- Activity `LeafWriteProbe+prod`: add a polyline on `LEAF_WRITE_PROBE` + delete
  handle `9462`, **verified by re-extract** (2345 in == 2345 out); 3 WorkItems,
  cumulative **$0.0272**, `pass: true`.

**Write leg — compiled ObjectARX/.NET AppBundle** (`data/arx_probe_receipt.json`)
- `net8.0-windows` AppBundle `LeafWriteTools+prod` loaded via `/al` and ran on DA
  engine `26_0`: add + delete (handle `7FA3`) **oracle-verified by re-extract**;
  3 billable WorkItems, cumulative **$0.0338**, `pass: true`. This is the spike
  that closed Lane 2's execution question: **APS DA runs our compiled packages.**

**Product-path write loop** (`data/write_loop_receipt.json`)
- v1 ingest → v2 write → undo through the product broker: `pass: true`,
  `undo_verified: true`, 3 WorkItems, **$0.0163**.

**Real NL authoring through the PRODUCT path** (`data/nl_author_product_receipt.json`)
- `app POST /api/author → harness serve.ts → real Agent SDK (operator grant) →
  tenant mushy repo → catalog fold → zero-LLM run`. Authored
  `count-closed-open-polylines`: closed **2345** / open **0**, independent check
  `match: true`; tenant repo commit `188cd6d`. Also proved `polyline-vertex-count`
  (9380 vertices / 2345 polylines / avg 4.0). `pass: true`.

---

## The app API surface (port 8130)

All bodies carry the §10 envelope (`error`, `degraded_mode`). Full behavior in
`server/CONTRACT-ADDENDUM.md`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | readiness (`ok`, `aps_live`, tool counts) |
| GET | `/api/session?dwg=` | intake JSON for a drawing (cached at `APS_LIVE=0`) |
| GET | `/api/tools` · `/api/capabilities` | flat / family-grouped catalog (tenant-scoped) |
| GET | `/api/entitlements` | this tenant's tier policy (`run_read/run_write/build`) |
| POST | `/api/run` (`?wait=1`) | submit a tool run → **202 `{job_id}`**; `?wait=1` blocks for the §3 envelope |
| GET | `/api/jobs/{id}` · `/stream` · `/api/jobs` | job record · SSE · recent-jobs list |
| POST | `/api/jobs/{id}/close` | tab-close reap beacon |
| GET/POST | `/api/drawings/{id}/intake` · `/undo` · `/redo` · `/versions` | versioned write loop (§11) |
| POST | `/api/nl-prompt` | classify a prompt into run/solve/build (§12) |
| GET | `/api/usage` | per-tenant spend/quota meter (§13) |
| GET/POST | `/api/ops/tenants` · `/{tid}/disable`·`/enable` | internal kill-switch surface (`X-Internal-Role: qa`) |
| POST | `/api/author` | author a tool (harness Build lane or templater) |
| GET/POST/DELETE | `/api/tenant/claude-grant` | link/inspect/unlink a tenant's Claude grant (token write-only) |
| POST/GET | `/api/orgs` · `/api/projects` | platform workspace (only when `DATABASE_URL` is set) |

Broker (port 8140, internal): `/broker/health`, `/broker/run`,
`/broker/tenants/{tid}/disable|enable`, `/broker/reap`.

---

## Layout

`contract/` frozen interfaces (`CONTRACT.md`, `AUTH.md`) · `server/` FastAPI
composition root + `routers/` + `broker.py` + `CONTRACT-ADDENDUM.md` (§7–§17) ·
`da/` APS Design Automation client + store + queue · `engine/` extract script +
read tools + registry + AppBundles · `harness/` Agent-SDK author loop (TS) ·
`platform/` Postgres Project/Job/org entity · `web/` Vite+three.js frontend ·
`deploy/` Dockerfiles + compose + `README.md` · `data/` sample + live intake +
capability receipts · `scripts/` this dev boot.
