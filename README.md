# Leaf — build CAD tools with AI, run them on the cloud engine

Leaf is a **natural-language CAD-tool-building platform**. You open a real DWG in
a browser, type what you want in one prompt box, and the platform either **runs**
a registered tool, **builds** you a brand-new one by talking to a coding agent, or
tells you honestly that the **solve** lane isn't wired yet. Real-engine CAD work
executes on **Autodesk APS Design Automation**; authored tools then run
deterministically with **no LLM in the request hot path**.

> **Role in the mission:** this repo is the **hosted web lane (Lane 2)** of the
> fleet mission — the natural-language CAD tool-building platform. Identity, the
> canonical hierarchy, and the honest spiked-vs-bet ledger live in
> `~/.claude/MISSION.md`. Solar/Leaf Automation is a specialization of this mission, not the
> identity. Nothing here states the web lane as finished fact — see the ledger
> below.

---

## Start here

| I want to… | Go to |
|---|---|
| **Run it locally in one command** | [`RUN.md`](RUN.md) → `python scripts/start-leaf.py` |
| **Run the container stack** | [`deploy/README.md`](deploy/README.md) → `docker compose up` |
| **Understand how the pieces fit** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| **Read the API/behavior contract** | [`contract/CONTRACT.md`](contract/CONTRACT.md) + [`server/CONTRACT-ADDENDUM.md`](server/CONTRACT-ADDENDUM.md) (§7–§17) |
| **Understand identity / auth** | [`contract/AUTH.md`](contract/AUTH.md) |
| **Know why a main build skipped its test gate** | [`docs/GATE-TREE-REUSE.md`](docs/GATE-TREE-REUSE.md) |

Fastest path:

```bash
cd C:/tmp/leaf-web-demo
python scripts/start-leaf.py      # broker + app + web at APS_LIVE=0; open http://127.0.0.1:5175
```

---

## The three lanes (one prompt box)

- **Run** — execute a registered read/write tool against the drawing. Runs on APS
  DA when `APS_LIVE=1`; runs pure-python offline otherwise. Async job spine
  (`POST /api/run` → `202 {job_id}` → poll/SSE).
- **Solve** — optimisation lane, declared in the contract; **nothing executes
  yet**, and the router says so rather than faking a result.
- **Build** — author a new tool from plain English. A real **Claude Agent SDK**
  loop edits the tenant's own **mushy repo**, commits it, and folds the tool into
  that tenant's catalog. After authoring, the tool is a normal registered tool —
  zero-LLM at run time.

Every tenant is isolated: its **own** repo, its **own** Claude grant, its **own**
catalog, its **own** metered spend. The Autodesk credential lives in exactly one
process — the **broker** — which no tenant-authored code can reach.

---

## Proven capabilities (measured on production APS, oracle-checked)

Receipts in [`data/`](data). Engine `Autodesk.AutoCAD+26_0`, ~$6/engine-hour.

| Capability | Evidence | Cost / result |
|---|---|---|
| **Read leg** — DWG→JSON extract on the cloud engine | byte-identical to local (4 layers / 2345 polylines) | 3.3 s → **$0.00554/run** |
| **Read tools** on APS | `count-by-layer`→2345, `measure-panel-area`→48718.18 sqft, `highlight…(200)`→276 handles | each oracle-matched, ~$0.008 |
| **Write leg — LISP** | `write_spike_receipt.json` — add + delete, re-extract verified | 3 WorkItems, **$0.0272** |
| **Write leg — compiled ObjectARX/.NET AppBundle** | `arx_probe_receipt.json` — `net8.0-windows` bundle ran on DA, oracle-verified | 3 WorkItems, **$0.0338** |
| **Product write loop** (v1→v2→undo) | `write_loop_receipt.json` — `pass:true`, `undo_verified:true` | **$0.0163** |
| **Real NL authoring** (product path) | `nl_author_product_receipt.json` — app→harness→Agent SDK→tenant repo→zero-LLM run, `match:true` | commit `188cd6d` |

The compiled-AppBundle run is the spike that **closed Lane 2's execution
question**: Autodesk's cloud engine runs our own compiled CAD packages.

---

## Honest bet-ledger (do not read this repo as "done")

Per `~/.claude/MISSION.md`:

- **Spiked / real:** APS DA executes our compiled packages (read + LISP write +
  compiled ObjectARX write, all oracle-verified); the async multi-tenant backbone;
  per-user "sign in with Claude" server-side agent turns; the tenancy/tier schema.
- **Still a bet:** **LLM supply for serving strangers** — the mechanism is spiked
  per user, but the hosted-many-users ToS posture is unadjudicated
  (`research/agentsdk-usage-visibility.md`, open question 3). The purpose-built web
  UI at production grade, the containers, and billing are **still open**. This is a
  demo-grade shell on a real backbone — not a shipped product.

---

## Repo layout

```
contract/   frozen interfaces (CONTRACT.md §1-6, AUTH.md)
server/     FastAPI composition root (app.py) + routers/ + broker.py + CONTRACT-ADDENDUM.md (§7-17)
da/         APS Design Automation client, versioned store, fair queue, usage/reaper
engine/     extract script + read tools + registry + compiled AppBundles
harness/    Agent-SDK author loop (TypeScript): per-tenant grants + mushy repos
platform/   Postgres Project/Job/org entity (opt-in; DATABASE_URL)
web/        Vite/React/three.js frontend (our own DWG renderer)
deploy/     Dockerfiles + docker-compose + deploy/README.md
data/       sample + live intake JSON + capability receipts
scripts/    start-leaf.py — one-command local dev boot
docs/       ARCHITECTURE.md + supporting notes
```

License / trademark note: naming is decided law (`NAMING-FINAL.md`); internal
factory names never appear in public-facing text. See `~/.claude/MISSION.md`.
