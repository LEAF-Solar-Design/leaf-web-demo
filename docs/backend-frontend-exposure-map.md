# Backend → Frontend exposure map

**What this is:** the backend's own read of everything it has plumbed and ready
for UI display, measured against what the just-shipped Unified Surface actually
renders, with a concrete, prioritized suggestion for wrapping each un-surfaced
capability in the frontend's existing component vocabulary.

**Voice:** written by the backend lanes for the frontend lane. Every "the UI
does / doesn't show X" is cited to a real component; every endpoint claim is
cited to router code. Anything not directly read is marked *(inferred)*.

**Method:** read of `contract/CONTRACT.md` (§1–§10), `server/CONTRACT-ADDENDUM.md`
(§7–§12), `contract/AUTH.md`, all of `server/routers/`, `server/app.py`,
`server/{jobs,broker,broker_client,deps,catalog,write_loop,envelopes}.py`,
`server/capability_families.json`, `platform/{api,store,models,deps}.py`,
`da/{store,usage,tenant,reaper}.py` + `da/STORE.md`,
`harness/contract/HARNESS-CONTRACT.md`, the receipts in `data/*.json`, and every
file in `web/src/` incl. `api.js` + all `components/`.

**The one-line finding:** the async spine, prompt routing, versioned undo/redo,
degraded banner, and per-run cost receipt are genuinely wired end to end. The
biggest *ready-but-dark* surfaces are (1) the capability **families** catalog,
(2) a per-tenant **spend / quota** meter, (3) the **version-history** chain, (4)
the **projects/orgs** workspace, and (5) the **tab-close reap** signal the UI
never sends. None of these needs new engine work; four of five need only a thin
read endpoint or a `fetch` the UI isn't making yet.

---

## 1. One-screen summary

| Capability | Endpoint / module | UI today | Suggested wrap | Pri | Effort |
|---|---|---|---|---|---|
| Async job spine (submit/poll/SSE) | `POST /api/run` 202, `GET /api/jobs/{id}`, `/stream`, `/api/jobs` — `routers/jobs.py` | **Surfaced.** JobRail + ResultPanel; SSE `EventSource` + 1s poll (`api.js:127`) | — (shipped) | — |
| Versioned undo/redo (head step) | `POST /api/drawings/{id}/undo|redo`, `GET .../intake` — `routers/drawings.py` | **Surfaced.** Undo/Redo buttons + version-note (`App.jsx:566`) | — (shipped) | — |
| NL prompt routing (run/solve/build) | `POST /api/nl-prompt` — `routers/prompt.py` | **Surfaced.** PromptBox + RoutePanel; live lane hint | — (shipped) | — |
| Degraded-mode banner | `degraded_mode` field, all envelopes — `envelopes.py` | **Surfaced.** DegradedBanner + result chip + rail tag | — (shipped) | — |
| Per-run cost receipt | `cost:{engine_seconds,usd_est}` §3 — `envelopes.py:67` | **Surfaced (partial).** ResultPanel receipt line (`ResultPanel.jsx:138`) | Itemize + link to spend | P3 | S |
| Flat tool catalog | `GET /api/tools` — `routers/tools.py` | **Surfaced.** ToolsPanel flat list + footer count | (superseded by families) | — |
| **Capability FAMILIES** | `GET /api/capabilities` — `routers/capabilities.py`, `catalog.py` | **DARK.** UI never calls it; catalog is the flat `/api/tools` (`api.js:76`) | Group the left-rail catalog by family | **P1** | S |
| **Per-tenant spend / quota meter** | broker ledger `broker_ledger.jsonl`, `da/usage.py`, `402 quota_exceeded` — `broker.py:221` | **DARK** proactively; quota *rejection* lands only as a failed job | Header/footer spend chip + calm quota card; needs thin read endpoint | **P1** | M |
| **Auth login + tenant/org/tier** | `LEAF_AUTH_LIVE=1`, `require_tenant`, `tenant_echo` — `deps.py:197`, `contract/AUTH.md` | **DARK.** UI sends no `Authorization`; would 401 live. Tier chip shows tenant_id, not tier | Login state + real tier chip; fix `tenant_echo` to echo tier | **P1** | M |
| **Tab-close reap signal** | `POST /api/jobs/{id}/close` — `routers/jobs.py:121` + `da/reaper.py` | **DARK.** UI never POSTs close (no fn in `api.js`) | `sendBeacon` on unload for the in-flight job | **P2** | S |
| **Version-HISTORY browser** | `da/store.py` manifest (`versions[]`, sha256, parent, tool, checkout) — **no read endpoint** | **DARK.** UI only steps head via undo/redo | Version-chain popover; needs a thin `GET .../versions` | **P2** | M |
| **Real NL authoring (harness)** | `LEAF_AUTHOR_HARNESS_URL` seam — `routers/author.py:46`; proven `data/nl_author_receipt.json` | **Transparent but invisible.** `/api/author` already uses it when env-set; UI shows no provenance | Author card: "real agent vs template" + static-scan chips | **P2** | S |
| **Projects / Orgs workspace** | `/api/projects`, `/api/projects/{id}/jobs`, `GET /jobs/{id}` — `platform/api.py` (mounted) | **DARK.** UI never calls; single implicit drawing. Org-create not exposed | A projects list/open shell; honest prereqs | **P2** | L |
| Health / status diagnostics | `GET /api/health` — `app.py:79` | **DARK.** Footer states are hardcoded (`App.jsx:657`) | Feed real health into the footer chips | P3 | S |
| Kill-switch / tenant-disable | `POST /broker/tenants/{tid}/disable|enable`, `/broker/health` — `broker.py:272` | **DARK.** Broker-only; no app proxy, no UI | Ops-only view (out of tenant surface) | P3 | M |
| QA/internal role catalog toggle | `X-Internal-Role: qa` on `/api/capabilities` — `capabilities.py:20` | **DARK.** UI never calls capabilities | Depends on families + real role | P3 | S |
| Single-writer checkout lock | `acquire_checkout`/`release_checkout` — `da/store.py:411` | **DARK.** Store-only; no endpoint | "Someone else is editing" chip (needs endpoint) | P3 | M |
| SSE progress granularity | `/api/jobs/{id}/stream` — `routers/jobs.py:83` | **Surfaced (coarse).** Consumed; carries only status/progress/elapsed | Nothing to add until backend emits richer progress | P3 | — |

Legend — effort: S ≈ frontend-only or a one-function endpoint; M ≈ a thin new
read endpoint + a card; L ≈ multi-screen flow with real prerequisites.

---

## 2. Per-item detail (P1 / P2)

The Unified Surface's grammar (from reading the components): a **top bar**
(mark, Project tag, mock/live tag, tier chip), a collapsible **left rail**
(`aside.nav`: "Catalog · N caps" → Tools + Author sections), a **main column**
(kicker → hero question → PromptBox → RoutePanel → DegradedBanner →
workspace-card → ResultPanel → EntitlementGate), a **right rail** (`aside.rail`
"Job monitor"), and a **footer** (`foot-bar` status chips). Copy posture is
calm and loader-free: word-swaps not spinners, "safe to leave" not pills,
honest "Placeholder — not enforced" labels. Every suggestion below stays inside
that grammar.

### P1 — Capability FAMILIES grouping

- **Backend, ready.** `GET /api/capabilities` (`routers/capabilities.py`)
  returns `{ families: [ { family_id, label, description, capabilities:[{ name,
  version, description, params_schema, capabilities, provenance } ] } ] }`.
  Families come from `server/capability_families.json`: **Measurement**,
  **Selection & highlighting**, **Custom authored tools**, **Internal / QA**.
  `catalog.build_catalog` (`catalog.py:62`) drops empty families and filters
  internal/QA tools server-side. Authored tools land in `custom`.
- **UI today.** The left rail's "Tools" section renders the **flat**
  `/api/tools` list (`api.js:76` → `ToolsPanel.jsx`), and the footer says
  "capability catalog · N loaded" from `tools.length` (`App.jsx:658`). Families
  are never fetched or shown. `params_schema` per capability is already what
  `ToolsPanel`'s `ParamForm` consumes — so the richer endpoint is a drop-in.
- **Suggested wrap.** In `aside.nav`, replace the single "Tools" `Section` with
  one `Section` per `family_id`, header `label` + `· {capabilities.length}`,
  the family `description` as the muted `panel-sub`. Keep `ToolsPanel`'s card
  body verbatim — map each family's `capabilities[]` into the same cards
  (`name`, `description`, `capabilities` tags, `params_schema` → `ParamForm`).
  "Custom authored tools" naturally becomes the home for `/api/author` output,
  which the flat list currently mixes in. Calm posture: families are plain
  collapsible sections, no counts-as-pills.
- **Consume:** `GET /api/capabilities` → `families[].{label,description}` and
  `families[].capabilities[].{name,version,description,params_schema,capabilities,provenance}`.
  Add a `getCapabilities(mock)` to `api.js` beside `getTools`.

### P1 — Per-tenant spend / quota meter + quota state

- **Backend, ready (metering) / thin add (read).** Every `/broker/run` appends
  exactly one JSONL line to `broker_ledger.jsonl` with `{ts, tenant_id, tool,
  engine_op, aps_endpoint, aps_live, engine_seconds, usd_est, status}`
  (`broker.py:319`, ADDENDUM §8). `da/usage.py` sums a tenant's spend
  (`spent_from_broker_ledger`, `usage.py:139`) and enforces a hard pre-flight
  cap: over-cap runs are rejected **before any APS call** with the
  `quota_exceeded` envelope at **HTTP 402** (`broker.py:221`, `_cap_preflight`;
  `envelopes.py:32` promoted the code). Caps are OFF unless
  `LEAF_TENANT_CAP_USD` / `LEAF_USAGE_CAPS[_FILE]` is set — a demo run is never
  gated.
- **UI today.** Per-**run** cost is shown (ResultPanel receipt: `{engine_seconds}s
  engine · ~${usd_est}`, `ResultPanel.jsx:138`), but there is **no cumulative
  spend anywhere** and **no proactive quota state**. A quota rejection does land
  today — but only reactively: the broker 402 flows back as a failed job whose
  `error.error_code` is `quota_exceeded`, so JobRail shows a red "failed" card
  and ResultPanel shows the message (`JobRail.jsx:29`, `ResultPanel.jsx:114`).
  Nothing tells the tenant they're *approaching* the cap.
- **Suggested wrap.** Two pieces:
  1. A **spend chip** in the top bar next to the tier chip (or a footer
     `foot-bar` chip): "spend · $X.XX" and, when a cap is configured,
     "· $X.XX / $CAP". Calm text, no gauge animation.
  2. A **quota card** reusing the DegradedBanner pattern (amber tag, honest
     copy) that renders when a run fails with `quota_exceeded`: "Spend cap
     reached — this run wasn't charged. Nothing ran on the cloud." The message
     is already in `error.message`.
- **Needs (honest).** The app process has no spend read endpoint today — the
  ledger + `usage.py` live broker-side. Add a thin app route, e.g.
  `GET /api/usage` → `{spent, cap, projected, currency}` backed by
  `usage.spent_from_broker_ledger(tenant, LEDGER)` + `usage.cap_for(tenant)`.
  This is the only backend add in P1 and it touches no engine path.
- **Consume:** the new `GET /api/usage`; and the already-present failed-job
  `error.error_code === 'quota_exceeded'` + `error.message`.

### P1 — Auth login state + real tenant / org / tier

- **Backend, ready (env-gated, default off).** With `LEAF_AUTH_LIVE=1`,
  `require_tenant` (`deps.py:197`) verifies an Auth0 RS256 Bearer token,
  resolves a workspace, and returns a `TenantContext` carrying `tenant_id`,
  `org_id`, `tier`, `workspace`. `deps.tenant_echo` additively echoes identity
  into success bodies (`session.py:35`, `jobs.py:69`, `author.py:86`). Status
  contract: no/blank token → 401, verified-but-no-tenant-claim → 403
  (`contract/AUTH.md` §2). Default (unset/0) is byte-identical to today's open
  demo.
- **UI today.** `api.js` sends **only** `X-Tenant-Id` (`api.js:204`); it never
  sends `Authorization`. So with `LEAF_AUTH_LIVE=1` every call would 401 and the
  UI has no login affordance. Two honest defects even off-auth:
  1. The header/footer **"tier" chip actually displays the tenant_id**:
     `tierLabel = tenant || 'demo'` and `tenant = data.tenant_id`
     (`App.jsx:107`, `api.js:48`). EntitlementGate is fed this same value
     (`App.jsx:645`).
  2. `tenant_echo` **omits `tier`** — it only adds `tenant_id`/`org_id`
     (`deps.py:231`). So even live, the UI *can't* show the real tier.
- **Suggested wrap.**
  1. **Backend one-liner:** have `tenant_echo` also emit `tier` (and `org_id`
     is already there) so the chip can be honest.
  2. **Header identity block** (`div.who`): when a token is present, show
     `org · {org_id}` and a real `tier · {tier}` chip; when absent under
     live-auth, a calm "Sign in" affordance rather than a silent 401. Keep the
     mock/live toggle exactly as is.
  3. Rename the current chip's source so tenant_id ≠ tier (correctness fix
     regardless of the hosted story).
- **Consume:** `GET /api/session` → `tenant_id`, `org_id`, (new) `tier`; on
  live-auth, attach `Authorization: Bearer …` in `api.js`'s `http()` /
  `runToolAsync` headers.

### P2 — Tab-close reap signal (don't bill abandoned WorkItems)

- **Backend, ready.** `POST /api/jobs/{id}/close` (`routers/jobs.py:121`) flags
  the in-flight job `progress='closed'`; the orphan reaper fails it on its next
  sweep (`jobs.py:259`), and the broker half cancels the APS WorkItem
  (`POST /broker/reap` → `da/reaper.py`). It's idempotent and 404s (never 403)
  across the tenant boundary.
- **UI today.** There is **no** close call — `api.js` has no `closeJob`, and
  `App.jsx` clears only the localStorage in-flight pointer. An abandoned live
  run relies solely on the 60 s heartbeat-stale reaper, so a closed tab can keep
  a WorkItem billing for up to a heartbeat window *(inferred from
  `HEARTBEAT_STALE_S` default 60)*.
- **Suggested wrap.** On `visibilitychange`/`beforeunload`, if
  `currentJobId` is set and the job is non-terminal, fire
  `navigator.sendBeacon('/api/jobs/'+id+'/close', …)` with the `X-Tenant-Id`
  header parity `/api/run` uses. No visible surface — it's a correctness/cost
  win. Small enough to bundle into the P1 wave.
- **Consume:** `POST /api/jobs/{job_id}/close` (idempotent; response
  `{job_id, closed, status}`).

### P2 — Version-history browser

- **Backend, plumbed in the store (needs a read endpoint).** `da/store.py`
  keeps a full manifest per tenant/drawing: `head`, `latest`, and `versions[]`
  where each entry is `{v, parent, created, bytes, sha256, workitem_id, tool,
  note}`, plus a `checkout` lock (`da/STORE.md` "Manifest", `store.py:223`
  `load_manifest`). `write_loop.intake_view` already loads it for head/latest
  (`write_loop.py:181`).
- **UI today.** The workspace toolbar exposes only **Undo / Redo** stepping head
  one hop, plus a transient `versionNote` ("version 2 created")
  (`App.jsx:566`). The `drawingState` it tracks is only `{drawing_id, version,
  head, latest}` (`App.jsx:270`) — the rich `versions[]` chain, sha256, authoring
  tool, and per-version cost are never fetched or shown. `routers/drawings.py`
  has **no** endpoint that returns `versions[]`.
- **Suggested wrap.** A **version-chain popover** off the toolbar's version-note:
  a calm vertical list (v3 → v2 → v1), each row `v{n} · {tool} · {created}` with
  the `note`, current head marked, click-to-preview via the existing
  `getDrawingIntake(id, n)` (which already accepts an integer version,
  `api.js:244`). No new viewer work — it reuses `seatVersion`.
- **Needs (honest).** Add a thin `GET /api/drawings/{id}/versions` returning
  `load_manifest(...)['versions']` + `head`/`latest`. The data exists; only the
  route is missing.
- **Consume:** new `GET .../versions` for the chain; existing
  `GET .../intake?version={n}` to preview a specific version.

### P2 — Real NL authoring via the harness seam

- **Backend, live-capable and proven.** `POST /api/author` delegates to the
  Agent-SDK author harness when `LEAF_AUTHOR_HARNESS_URL` is set, flowing the
  returned package through the **same** persist/register pipeline as the
  templater, with a templated fallback on any harness failure
  (`routers/author.py:46`). `data/nl_author_receipt.json` is a real run
  (`pass:true`, tool `layer-bounding-boxes`, 5 turns, zero-LLM at run time,
  independent check matched). The response is `{tool, code, preview}` and the
  tool carries `provenance.static_scan` findings (`author.py:76`).
- **UI today.** AuthorPanel calls `/api/author` and shows `tool.name`, the
  `preview` string, and the generated `code` (`AuthorPanel.jsx:65`). Because the
  seam is server-side, the UI **already benefits transparently** when the env is
  set — but it shows **no provenance**: no "authored by the real agent" vs
  "templated fallback" signal (the `preview` string encodes it — "[harness
  unreachable; templated fallback] …" — but it's buried in prose), and no
  static-scan surface.
- **Suggested wrap.** In the AuthorPanel `authored` card: a small provenance
  chip — "agent-authored" vs "templated (fallback)" derived from the `preview`
  prefix or `provenance.author` — and, when `provenance.static_scan` is
  non-empty, render the findings as calm advisory chips (they're advisory/
  non-blocking by design). Keep the code preview as is.
- **Needs (honest).** Rich agent telemetry (turns, tokens, `total_cost_usd` seen
  in the receipt) comes from the harness *drive* path, **not** from
  `POST /author`, whose contract is exactly `{tool, code, preview}`
  (`HARNESS-CONTRACT.md` §1). Surfacing turn/cost detail would need the harness
  `/author` response extended — out of scope for a pure frontend wrap; the
  provenance chip + static-scan is the immediately-wrappable part.
- **Consume:** `POST /api/author` → `tool.provenance.{author,static_scan}`,
  `preview`.

### P2 — Projects / Orgs workspace

- **Backend, mounted, org-scoped.** `platform/api.py` is mounted at startup
  under the `leaf_platform` alias → `/api/projects`, `/api/projects/{id}`,
  `/api/projects/{id}/jobs`, `/api/jobs/{id}`, `DELETE /api/orgs/{id}`
  (`app.py:56`). `GET /api/projects/{id}` hydrates a workspace payload:
  `project + drawing_versions[] + jobs[] + built_tools[]`
  (`platform/api.py:56`). Ownership is enforced per `org_id` with 404-not-403 on
  cross-org reads (`platform/README.md`).
- **UI today.** **Zero** UI. `api.js` never references projects/orgs and sends
  no `X-Org-Id`. The demo operates on a single implicit `demo` drawing bootstrapped
  by the write loop (`write_loop.py:155`), not a project entity.
- **Suggested wrap.** A **projects shell** ahead of the workspace-card: a
  left-rail or top-bar "Projects" list (`GET /api/projects`) and an open action
  that hydrates (`GET /api/projects/{id}`) into the existing viewer + a per-
  project JobRail (the hydrate payload's `jobs[]` maps straight onto JobCard).
  `built_tools[]` feeds the "Custom authored tools" family.
- **Needs (honest, load-bearing).** This is the only **L** item and has real
  prerequisites the demo hasn't wired:
  1. **DB.** The platform router connects lazily to `DATABASE_URL` (Postgres);
     import failure only logs and the demo runs without it (`app.py:56`). No DB
     → these routes 500/aren't usable.
  2. **Org bootstrap.** `platform/store.py` has `create_org` (line 39) but
     `platform/api.py` exposes **no** create-org route — only `DELETE
     /orgs/{id}`. So there is currently no HTTP way to mint the `org_id` that
     every project/job route requires. A `POST /api/orgs` (or the auth sibling
     provisioning orgs on first login) must land first.
  3. **Identity.** `get_org_id` trusts an `X-Org-Id` header in dev
     (`platform/deps.py:15`); prod must derive org from the verified session
     (ties to the P1 auth item). `orgs` (lowercase) vs the DB's existing
     `Organization` table is an unreconciled integration note
     (`platform/README.md` "Open integration note").
  Recommend sequencing this **after** the P1 auth item and a `create_org`
  route; until then it's a design-ready shell, not a shippable screen.

---

## 3. Suggested next UI wave (the backend's shipping order)

One coherent milestone — "make the ready surfaces visible" — 5 items the backend
would ship first, each frontend-light and resting on already-plumbed data:

1. **Families catalog** — swap the flat left-rail list for `GET /api/capabilities`
   groups. Pure frontend; richest ready payload, zero backend change. *(P1/S)*
2. **Spend + quota surface** — add `GET /api/usage` (thin, ledger-backed) → a
   top-bar spend chip and a calm `quota_exceeded` card reusing the degraded
   pattern. Turns an existing hard cap from a red failure into an honest meter.
   *(P1/M — one small endpoint)*
3. **Honest identity chip + login seam** — fix `tenant_echo` to echo `tier`,
   stop labeling tenant_id as "tier", and attach `Authorization` so
   `LEAF_AUTH_LIVE=1` actually works from the UI. Correctness win even off-auth.
   *(P1/M)*
4. **Tab-close reap** — `sendBeacon('/api/jobs/{id}/close')` on unload. Invisible
   but stops abandoned WorkItems from billing; backend is fully ready. *(P2/S)*
5. **Version-history popover** — add `GET /api/drawings/{id}/versions` (returns
   the manifest chain that already exists) and a calm version list that reuses
   the existing preview path. Upgrades undo/redo from "step" to "browse". *(P2/M)*

Deferred to a following wave (heavier or prerequisite-gated): the **Projects/Orgs
workspace** (needs DB + a `create_org` route + auth), an **ops kill-switch** view
(broker-only today), **checkout-lock** and **health-footer** polish, and richer
**agent-authoring telemetry** (needs the harness `/author` response extended).

---

## 4. Surprising / notable discoveries beyond the brief's list

- **The "tier" chip is mislabeled tenant_id.** `tierLabel = tenant || 'demo'`
  and `tenant` is `data.tenant_id` (`App.jsx:107`, `api.js:48`); EntitlementGate
  is fed the same. Even in live-auth the real `tier` can't be shown because
  `deps.tenant_echo` never echoes `tier` (`deps.py:231`) — a two-line backend
  fix unblocks an honest tier chip.
- **The quota path is already half-surfaced, reactively.** A broker 402
  `quota_exceeded` propagates as a normal failed job (`broker_client` returns
  the envelope, `jobs._run_job` fails it, JobRail/ResultPanel render the
  message). So "quota" isn't zero-surface — it's just unstyled and never
  proactive.
- **Version metadata is richer than undo/redo implies but has no read route.**
  The manifest carries `sha256`, `parent`, `bytes`, authoring `tool`,
  `workitem_id`, and `note` per version plus a `checkout` lock
  (`da/STORE.md`) — none reachable over HTTP; `routers/drawings.py` stops at
  intake/undo/redo.
- **`create_org` exists but is unexposed.** `platform/store.py:39` can mint an
  org, yet `platform/api.py` has no create route — the true blocker for the
  projects workspace, not the CRUD itself.
- **A whole reap/cancel spine is wired and dark.** `POST /api/jobs/{id}/close` →
  reaper → `POST /broker/reap` → `da/reaper.py` `DACancelClient` (double-gated
  `APS_LIVE=1` + `BROKER_REAP_LIVE=1`) is complete end to end, but the UI never
  pulls the first trigger.
- **The broker is also a metering + egress + kill-switch chokepoint** the tenant
  UI can't see: per-run attribution ledger, `*.amazonaws.com`/APS egress
  allowlist (`broker.py:82`), and persisted per-tenant disable — all legitimate
  future **ops** surfaces, distinct from the tenant surface.
- **SSE is genuinely consumed** (not just polled): `api.js:175` opens an
  `EventSource` on `/stream` with a 1 s poll as belt-and-suspenders — but the
  stream only carries `status/progress/elapsed`, so there's no richer progress
  to surface until the backend emits finer steps.
