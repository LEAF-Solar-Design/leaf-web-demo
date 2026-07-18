# Leaf platform — local container stack (WAVE 5 deploy pack)

The first deploy-readiness artifact: the Leaf CAD-tool platform (app + broker +
harness + web) containerized and proven to run under `docker compose`. Everything
here is **new** — no existing repo file was modified. Runs at `APS_LIVE=0` with
**no secrets**; the real cloud legs (APS, Claude) are opt-in mounts documented below.

Contract sources: `RUN.md`, `server/README.md`, `server/CONTRACT-ADDENDUM.md`
§7–§16.

---

## TL;DR

```bash
cd C:/tmp/leaf-web-demo
docker compose build          # build all four images
docker compose up -d          # start; wait ~20s for healthchecks to go healthy
docker compose ps             # all four should read "healthy"

# open the app:
#   web  UI   -> http://localhost:8080
#   app  API  -> http://localhost:8130/api/health
docker compose down -v        # stop and drop the named volumes (fresh next time)
```

---

## The four services

| Service | Image | Port (host) | Base | Role |
|---|---|---|---|---|
| `web` | `leaf-web:local` | **8080** | `nginx:alpine` | Vite/React/three.js bundle, static-served |
| `app` | `leaf-app:local` | **8130** | `python:3.12-slim` | tenant-facing FastAPI: async job spine, catalog, write loop, NL router, usage/ops/tenant surfaces |
| `broker` | `leaf-broker:local` | 8140 (internal) | `python:3.12-slim` | APS credential chokepoint + attribution ledger + kill-switch (§8). The ONLY secret-holder. |
| `harness` | `leaf-harness:local` | 8150 (internal) | `node:22-slim` + git | Build lane: real Agent SDK author loop, per-tenant grant + repo (§15/§16) |

Only `web` (8080) and `app` (8130) publish to the host. `broker` and `harness`
are reachable only on the internal compose network (`http://broker:8140`,
`http://harness:8150`) — the browser never talks to them directly.

**Why the browser hits `localhost:8130`:** the web bundle bakes `VITE_API_BASE`
at build time (default `http://localhost:8130`) and the browser — running on the
host — calls the app's *published* port. To serve the stack from another host,
rebuild `web` with `--build-arg VITE_API_BASE=https://your-host:8130`.

---

## Env contract (wired by `docker-compose.yml`)

| Env var | Set on | Value | Purpose |
|---|---|---|---|
| `APS_LIVE` | app, broker | `0` | mock mode — pure-python results from real geometry; no cloud calls |
| `BROKER_URL` | app, harness | `http://broker:8140` | app/harness → broker HTTP boundary (§8) |
| `LEAF_AUTHOR_HARNESS_URL` | app | `http://harness:8150` | app → harness author sidecar (§15) |
| `LEAF_TENANTS_DIR` | app, harness | `/data/tenants` | per-tenant "mushy" repos — **MUST match on both** (§16.H); shared volume |
| `LEAF_GRANTS_DIR` | harness | `/data/grants` | per-tenant Claude token files (harness-only) |
| `LEAF_STORE_DIR` | app, broker | `/data/drawings` | versioned drawing store — **shared** app↔broker (write loop, §11) |
| `BROKER_LEDGER` | app, broker | `/data/state/broker_ledger.jsonl` | attribution ledger — broker writes, app reads for `/api/usage` (§13); **shared** |
| `BROKER_TENANTS` | broker | `/data/state/broker_tenants.json` | persisted per-tenant kill-switch |
| `JOBS_DB` | app | `/data/state/jobs.db` | durable async job records (restart-survivable, §7) |
| `LEAF_TENANT_FIXTURE` | harness | `/app/test/fixtures/tenant-repo` | seed for auto-provisioning a brand-new tenant repo (§16.D) |
| `DATABASE_URL` | app | `${DATABASE_URL:-}` (empty) | **opt-in** platform Project/Job persistence; empty ⇒ platform DB endpoints stay dark, demo-safe |

### Named volumes

| Volume | Mounted in | Holds |
|---|---|---|
| `leaf-tenants` | app + harness | per-tenant repos (shared per §16.H) |
| `leaf-grants` | harness | per-tenant Claude tokens |
| `leaf-drawings` | app + broker | versioned drawing store (shared, write loop) |
| `leaf-state` | app + broker | broker ledger + kill-switch + app `jobs.db` |

`docker compose down -v` drops all four → the next `up` starts from a clean slate.

---

## What works at `APS_LIVE=0` (no secrets, out of the box)

The full mock demo — the same experience as the native 90-second demo, now
containerized end to end:

- **Web UI** at `http://localhost:8080` renders the rooftop (2345 real panels).
- **Read tools** through the real async spine: `POST /api/run` → 202 `{job_id}` →
  poll/stream → §3 envelope. `count-by-layer` → `Panels: 2345` computed by the
  containerized **app → broker** chain (real geometry, pure-python engine).
- **Write loop** (§11): a `drawing.write` run (e.g. `delete-marked-panel`) produces
  a new immutable **version 2** in the shared drawings volume, with
  `/api/drawings/demo/undo` / `redo` / `versions` — persists across a container
  restart (it's on `leaf-drawings`).
- **Author (template path)** (§15): `POST /api/author` with a templatable
  description registers a runnable tool with zero LLM and zero Claude grant.
- **NL prompt router** (§12): `POST /api/nl-prompt` classifies into run/solve/build.
- **Usage / ops / capabilities**: `/api/usage`, `/api/capabilities`,
  `/api/ops/tenants` (with `X-Internal-Role: qa`).
- **Harness reachability** (§16): `GET http://harness:8150/health` is up; an
  author request for a tenant with **no linked Claude grant** returns the honest
  **`grant_required`** shape (HTTP 401) through the app — proving the app→harness
  network path without spending a single LLM credit.

## What needs an operator mount (the real cloud legs)

All commented in `docker-compose.yml` — uncomment + provide the artifact:

- **Real APS authoring / extraction (`APS_LIVE=1`)** — mount your credentials into
  the **broker** and point `APS_CRED` at them, then set `APS_LIVE=1` on app+broker:
  ```yaml
  broker:
    environment:
      APS_LIVE: "1"
      APS_CRED: /run/secrets/aps/credentials.json
    volumes:
      - ${HOME}/.aps:/run/secrets/aps:ro
  ```
  (also flip `app.environment.APS_LIVE` to `1`). The credential is read only by the
  broker, only on live runs, and is **never baked into any image**.

- **Real Claude authoring (Agent SDK build lane)** — link a per-tenant Claude
  OAuth token via the app: `POST /api/tenant/claude-grant {token}` (the app forwards
  it to the harness store and never persists/logs it), **or** mount a demo-tenant
  grant file and set `LEAF_GRANT_FILE` on the harness:
  ```yaml
  harness:
    environment:
      LEAF_GRANT_FILE: /run/secrets/claude-grant
    volumes:
      - ./.secrets/claude-grant:/run/secrets/claude-grant:ro
  ```

- **Platform Project/Job persistence** — set `DATABASE_URL` (Neon/Postgres) in a
  `.env` next to the compose file (compose substitutes it into the app). Empty by
  default, so `POST /api/orgs` etc. stay dark on a plain demo.

- **Platform live auth (`LEAF_AUTH_LIVE=1`)** — PyJWT and its crypto dependency
  from `server/requirements-auth.txt` are installed in the app image. The
  verifier remains dormant when `LEAF_AUTH_LIVE=0`, while the ECS deployment can
  enable RS256 verification through environment configuration alone.

---

## Honest limitations

- **Real SDK authoring in-container is untested here.** The grant-required 401 path
  (no token) IS exercised and proves app→harness networking; a full author run with
  a real Claude token was NOT run in-container in this pack (no secret was mounted).
  The harness image installs `git` and keeps the `@anthropic-ai/claude-agent-sdk`
  runtime dep, so it is expected to work when a grant is mounted — but that is
  **inferred, not verified** in this artifact.
- **`APS_LIVE=1` in-container is untested here** for the same reason (no creds
  mounted). The mock path is fully verified.
- **Windows-host volume perf**: named volumes on Docker Desktop for Windows live in
  the WSL2 VM (fast). Avoid bind-mounting Windows host paths (`C:/...`) for the
  drawings/tenants dirs — cross-OS bind mounts are slow and can break file locks
  used by the versioned store's single-writer checkout.
- **The `web` healthcheck targets `http://127.0.0.1:8080/`, not `localhost`** —
  nginx listens on IPv4 `0.0.0.0:8080` and busybox `wget` resolves `localhost` to
  IPv6 `::1` with no fallback, which reports a false "unhealthy" even though the
  page serves. Keep the IPv4 literal if you edit that check.
- **`broker` runs via `uvicorn --host 0.0.0.0`**, not `python broker.py` — the
  latter binds `127.0.0.1` (loopback only) and would be unreachable from the app +
  harness containers. Same behavior, container-reachable bind. (The app already
  binds `0.0.0.0`; both run under uvicorn here for consistency.)

---

## Toward AWS/ECS (next deploy step)

This compose stack is the local proof; an ECS/Fargate translation per the
`leaf-dev-ops` skill would need: one task definition per service with the same
health checks; the four named volumes → EFS access points (or the drawings/tenants
stores → S3-backed) with the SAME shared-mount topology (app↔broker drawings +
ledger, app↔harness tenants); secrets (`APS_CRED`, Claude grants, `DATABASE_URL`)
via AWS Secrets Manager injected as mounted files / env, never baked; broker+harness
kept off the public ALB (internal service discovery only, mirroring "not published
to the host"); and `VITE_API_BASE` baked to the public app hostname at web-build time.
