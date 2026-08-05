# Leaf platform — local container stack (WAVE 5 deploy pack)

## Warm instant execution staging image

`Dockerfile.instant-execution` is a separate staging-only image. It does not
join the five-service production supply set and no workflow in this lane deploys
it. Build it with an exact source revision:

```bash
docker build --build-arg LEAF_SOURCE_SHA="$(git rev-parse HEAD)" \
  -f deploy/Dockerfile.instant-execution -t leaf-platform-instant-execution:local .
```

The image defaults to the control-plane WSGI process. Use an explicit command
to run another warm instant execution process. Mount secrets at runtime. Do not
bake credentials into the image.

On ECS, first run one of these commands in a short-lived init container with a
task-local ephemeral volume mounted at `/run/leaf/secrets`:

```bash
python -m executor.bootstrap control
python -m executor.bootstrap executor
```

Only that init container receives raw Secrets Manager values. It writes the
allowlisted files with mode `0600` and exits. The control, reaper, and executor
containers receive file paths, not the raw certificate, key, seed, or trust
bundle values.

```bash
docker run --read-only --tmpfs /tmp --rm leaf-platform-instant-execution:local
docker run --read-only --tmpfs /tmp --rm leaf-platform-instant-execution:local \
  python -m executor.control_plane.reaper_main
docker run --read-only --tmpfs /tmp --rm leaf-platform-instant-execution:local \
  python -m executor.runtime.service --host 0.0.0.0 --port 8088
```

The WSGI and reconciler commands require their documented PostgreSQL, Redis,
TLS, and mounted-secret configuration. The non-loopback executor command also
requires its mTLS, control-secret, and trust-bundle configuration.

The first deploy-readiness artifact: the Leaf CAD-tool platform (app + broker +
harness + web) containerized and proven to run under `docker compose`. Everything
here is **new** — no existing repo file was modified. Runs at `APS_LIVE=0` with
**no secrets**; the real cloud legs (APS, Claude) are opt-in mounts documented below.

Contract sources: `RUN.md`, `server/README.md`, `server/CONTRACT-ADDENDUM.md`
§7–§16. Process stop behavior is frozen in
[`docs/SHUTDOWN-CONTRACT.md`](../docs/SHUTDOWN-CONTRACT.md).

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

**Local browser API:** docker-compose bakes `http://localhost:8130` into `VITE_API_BASE`
at build time (default `http://localhost:8130`) and the browser — running on the
host — calls the app's *published* port. To serve the stack from another host,
For deployed images the default is the current public origin. Override it with
`--build-arg VITE_API_BASE=https://your-host:8130` only when the API uses a
different origin.

---

## Canonical PostgreSQL worker overlay

The default four-service demo remains database-optional. To run the production-shaped
canonical job authority locally, add `docker-compose.canonical.yml`:

The production cutover gates and the complete mutable-authority inventory are
in [`docs/POSTGRES-CUTOVER.md`](../docs/POSTGRES-CUTOVER.md).

```bash
export AUTOFILL_SOLVER_REVISION="$(git -C ../autofill-solver rev-parse HEAD)"
docker compose -f docker-compose.yml -f docker-compose.canonical.yml build app canonical-worker migrate
docker compose -f docker-compose.yml -f docker-compose.canonical.yml up -d
docker compose -f docker-compose.yml -f docker-compose.canonical.yml ps
```

The canonical overlay requires the full 40-character `autofill-solver` commit.
The commit must exist in `deploy/autofill-solver-sources.json` with the digest
of its Python source tree. Compose uses the sibling checkout by default; set
`AUTOFILL_SOLVER_CONTEXT` to another clean checkout or archive when needed.
The image build rejects bytes that do not match the trusted digest, seals the
verified identity into the image, and the container smoke rejects a missing or
mismatched attestation.

After the worker is healthy, run the clean-room real-solver receipt:

```bash
docker compose -f docker-compose.yml -f docker-compose.canonical.yml run --rm --no-deps \
  canonical-worker python /app/scripts/canonical-container-smoke.py
```

The overlay adds PostgreSQL 16, an idempotent migration job that applies every
checked-in numbered migration, and a non-root canonical worker with a database-heartbeat
healthcheck. The worker image receives `../autofill-solver` as a named BuildKit
context; solver code is not duplicated into this repository, and the adapter hashes
the exact source tree before and after every invocation. The overlay refuses to build
unless `AUTOFILL_SOLVER_REVISION` names the exact 40-character commit supplied by that
context. The local PostgreSQL password
is deliberately non-secret and the database port is not published. Staging must use
Secrets Manager and a managed PostgreSQL endpoint instead of these local credentials.

Stop without deleting PostgreSQL state using `down`. For a deliberate clean-room
rehearsal, use `down -v`; that removes all local named volumes, including
`leaf-postgres`.

### Shared authority staging

The three runtime images contain the code needed for PostgreSQL, but every new
authority keeps its legacy default:

| Process | Selector | Default | PostgreSQL connection |
|---|---|---|---|
| app sessions and approvals | `LEAF_SESSIONS_STORE` | `legacy` | `DATABASE_URL` |
| app and broker async jobs | `LEAF_JOBS_STORE` | `legacy` | their service-specific `DATABASE_URL` |
| app agent gate state | `LEAF_AGENT_STORE` | `legacy` | `DATABASE_URL` |
| app guest upload caps | `LEAF_GUEST_CAP_STORE` | `memory` | `DATABASE_URL` |
| app daily authoring quota | `LEAF_AUTHOR_QUOTA_STORE` | `memory` | `DATABASE_URL` |
| app drawing manifests and leases | `LEAF_DRAWING_STORE` | `legacy` | `DATABASE_URL` |
| app upload attempts and purge leases | `LEAF_UPLOAD_STORE` | `legacy` | `DATABASE_URL` |
| app customization changes and publication | `LEAF_CUSTOMIZATION_STORE` | `sqlite` | `DATABASE_URL` |
| broker tenant and ledger state | `LEAF_BROKER_STORE` | `legacy` | broker-only `DATABASE_URL` |
| broker async job callback completion | `LEAF_JOBS_STORE` | `legacy` | broker-only `DATABASE_URL` |
| broker drawing manifests and leases | `LEAF_DRAWING_STORE` | `legacy` | broker-only `DATABASE_URL` |
| broker callback replay nonces | `LEAF_CALLBACK_REPLAY_STORE` | `legacy` | broker-only `DATABASE_URL` |
| harness sessions | `LEAF_HARNESS_SESSION_STORE` | `file` | `LEAF_HARNESS_DATABASE_URL` |
| harness build authoring | `LEAF_HARNESS_AUTHORING_MODE` | `disabled` | n/a |

The base compose file maps the broker connection from
`LEAF_BROKER_DATABASE_URL`. It does not reuse the app's `DATABASE_URL`. This
keeps an app database setting from changing the secret-holding broker by
accident. Production should inject the broker connection as a broker-only
secret. The broker image still excludes the Anthropic SDK, Claude grants, and
tenant grant storage.

The app and broker share a lazy PostgreSQL pool with no minimum connection,
at most five connections, and a 600-second idle lifetime. This lets a direct
staging Aurora connection count reach zero during idle periods. It does not by
itself enable Aurora auto-pause. An associated RDS Proxy keeps its own database
connections open, so operators must first prove the direct TLS path and cold
resume behavior, then remove the proxy through the reviewed infrastructure
workflow before setting the staging Aurora minimum to zero ACUs.

Run schema work as a separate one-shot stage. Do not call
`apply_migration()` from an app, broker, or harness startup command.

```bash
# Local preflight. This starts PostgreSQL, applies all checked-in migrations,
# checks the core schema, and exits before any authority is selected.
docker compose -f docker-compose.yml -f docker-compose.canonical.yml \
  run --rm migrate
```

For staging, run the same migration command in a one-shot task built from the
release commit. Confirm that migrations `0011` through `0020` exist before
changing any selector. Then enable and verify one authority at a time. Session
migration may first use `dual_write`, `dual_write_shadow`, and `shadow`; the
other selectors accept only their documented legacy and PostgreSQL values. Do
not set all selectors in one deployment. The operator has pinned both production
domains to the current deployment, so do not promote aliases or apply this
rollout to production until staging is fully verified and a later operator
directive permits it.

The opt-in local overlay passes a database connection to each process but keeps
all selectors at their legacy values. After the preflight and the lane-specific
data checks, an operator can select one authority for a local rehearsal:

```bash
# Example only: rehearse broker authority after its data checks pass.
LEAF_BROKER_STORE=postgres \
docker compose -f docker-compose.yml -f docker-compose.canonical.yml up -d
```

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
| `LEAF_HARNESS_AUTH` | harness | `1` | F5 caller-auth gate ON: every non-`/health` harness route requires `X-Harness-Secret` |
| `LEAF_HARNESS_SECRET` | app, harness | `${LEAF_HARNESS_SECRET:-leaf-compose-dev-secret}` | the app→harness hop secret — **same value on both**; the compose default is DEV-ONLY (harness port is compose-network-internal), override via `.env` on any shared host |
| `LEAF_APP_URL` | harness | `http://app:8130` | harness → app base for the §18 converse back-edge (gate consult + dispatch) |
| `LEAF_APP_DISPATCH_SECRET` | app, harness | `${LEAF_APP_DISPATCH_SECRET:-}` (empty) | **opt-in** X-Dispatch-Secret (§18.5), same value on both. Empty ⇒ converse lane dark FAIL-CLOSED: harness answers `POST /turn` 501, app back-edge 401s. Author/Build lane unaffected |
| `LEAF_SESSIONS_DIR` | harness | `/data/sessions` | converse loop store (sdk resume ids, confirmation mirrors) |
| `SESSIONS_DB` | app | `/data/state/sessions.db` | conversational sessions/events/approvals (single-writer SQLite) |
| `DATABASE_URL` | app | `${DATABASE_URL:-}` (empty) | **opt-in** platform Project/Job persistence; empty ⇒ platform DB endpoints stay dark, demo-safe |
| `LEAF_SESSIONS_STORE` | app | `legacy` | app session authority selector; PostgreSQL remains opt-in |
| `LEAF_JOBS_STORE` | app | `legacy` | async job and delivery lease authority selector; PostgreSQL remains opt-in |
| `LEAF_AGENT_STORE` | app | `legacy` | agent gate authority selector; PostgreSQL remains opt-in |
| `LEAF_GUEST_CAP_STORE` | app | `memory` | guest daily-cap authority selector; PostgreSQL remains opt-in |
| `LEAF_DAILY_AUTHOR_QUOTA` | app | empty | authoring attempts per tenant per UTC day on `POST /api/author` and `/api/author/stage`. Empty ⇒ **no cap** (prior behavior). Bounds SERIAL authoring sessions, which the broker's USD cap and `LEAF_DAILY_RUN_QUOTA` never see: authoring spends Anthropic and sandbox money harness-side before any broker lease. `0` is a kill switch; an unparseable value fails closed to `0`. **Setting this without an explicit local posture (`LEAF_RUNTIME_ENV` must be development/test/local — unset counts as deployed), or under live auth, also requires `LEAF_AUTHOR_QUOTA_STORE=postgres`** — the app refuses the attempt otherwise, because a per-process counter is not a cap across replicas or restarts. |
| `LEAF_AUTHOR_QUOTA_STORE` | app | `memory` | authoring-quota counter authority selector; `postgres` requires migration `0023` and is the only mode that survives a restart or spans replicas. `memory` is for local runs, the demo, and tests |
| `LEAF_AUTHOR_QUOTA_RETENTION_DAYS` | app | `8` | how long spent authoring-counter rows are kept before the post-charge sweep deletes them (PostgreSQL mode; 2–366) |
| `LEAF_DRAWING_STORE` | app, broker | `legacy` | drawing manifest, version, checkout, and extraction authority selector |
| `LEAF_UPLOAD_STORE` | app | `legacy` | upload-attempt and purge authority selector |
| `LEAF_CUSTOMIZATION_STORE` | app, broker | `sqlite` | customization authority selector; `postgres` requires migration `0020`, a backfill, and an exact parity receipt |
| `LEAF_GUEST_CAP_HMAC_SECRET` | app | empty | required keyed IP pseudonymization secret in PostgreSQL guest-cap mode |
| `LEAF_GUEST_SECRET` | app | empty | required guest-upload session signing secret; staging generates it in Secrets Manager and injects it into the app task |
| `LEAF_CHECKOUT_CAP_SECRET` | app | empty | signing secret for the drawing-checkout capability (the proof of single-writer ownership). REQUIRED and at least 32 bytes when `LEAF_RUNTIME_ENV=production`. The app refuses to mint or verify without it because a weak or per-process fallback could not safely prove ownership across callers, replicas, or restarts. Off production, unset means a per-process random secret and checkouts do not survive an app restart. |
| `LEAF_BROKER_STORE` | broker | `legacy` | broker tenant and ledger authority selector |
| `LEAF_BROKER_DATABASE_URL` | broker | empty | compose input mapped to the broker's `DATABASE_URL` only |
| `LEAF_JOBS_STORE` | broker | `legacy` | broker callback completion uses the same async-job authority as the app |
| `LEAF_CALLBACK_REPLAY_STORE` | broker | `legacy` | callback nonce replay authority selector; PostgreSQL remains opt-in |
| `LEAF_HARNESS_SESSION_STORE` | harness | `file` | harness session authority selector |
| `LEAF_HARNESS_AUTHORING_MODE` | harness | `disabled` | build authoring requires explicit `singleton`; `fleet` stays blocked until a real vault exists |
| `LEAF_HARNESS_DATABASE_URL` | harness | empty | harness-only PostgreSQL connection |

### Named volumes

| Volume | Mounted in | Holds |
|---|---|---|
| `leaf-tenants` | app + harness | per-tenant repos (shared per §16.H) |
| `leaf-grants` | harness | per-tenant Claude tokens |
| `leaf-sessions` | harness | converse loop store. Dropping it burns in-flight approvals **fail-safe** (the gate denies the foreign-session redemption; the user re-proposes) |
| `leaf-drawings` | app + broker | versioned drawing store (shared, write loop) |
| `leaf-state` | app + broker | broker ledger + kill-switch + app `jobs.db` + `sessions.db` |

`docker compose down -v` drops them all → the next `up` starts from a clean slate.

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

One command proves all of the harness-lane claims end to end (authed hop, durable
grant/tenant volumes across a restart, §16.H catalog fold, secret-free logs) with
the mock agent — no Anthropic egress:

```
python scripts/harness-container-smoke.py
```

It runs an isolated compose project (`leaf-harness-smoke`) and probes from
INSIDE the compose network (`docker compose exec` in the app container), so it
needs no host port forwarding and the harness stays network-internal, exactly
like the base stack. It tears everything down (`down -v`) when it finishes.
Also runnable via the gate runner:
`LEAF_CONTAINER_SMOKE=1 python scripts/run-all-gates.py --only harness-container`.

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

## AWS/ECS: the production deploy path (live since 2026-07-22)

The ECS/Fargate translation exists and serves `platform.leafdesign.ai`: one
Fargate task (`leaf-automation-production-platform`) runs broker + harness +
app with the same health checks as this compose stack, `/data` on EFS with the
shared-mount topology preserved, secrets from AWS Secrets Manager
(`leaf-platform/*`), and only the app container on the public ALB.

The release path is a staged, receipt-bound chain:

1. **Build (this repo)**: `.github/workflows/build-platform-images.yml` runs on
   every push to `main` and builds app, broker, canonical-worker, harness, and
   web from one full application commit — unless its `noop` gate job proves
   from the real push diff that every changed file is documentation (under
   `docs/`, or root-level `*.md`; nested markdown such as `server/README.md`
   is copied into images and always builds). A docs-only run skips the whole
   build graph, publishes a `docs-noop-<sha>-attempt-<n>` marker artifact,
   and the staging relay skips cleanly on that marker; staging stays on the
   previous build. The gate fails open by construction — an unusable
   before-sha, unfetchable commit, failed diff, or unexpected filter output
   builds normally (deliberately NOT a native path filter, which GitHub
   evaluates over a truncated ~300-file window). A skipped commit has no
   `prod-<shortsha>` supply set of its own — the promote path is unaffected
   because it pins an explicit earlier build run ID and only requires its
   source to be an ancestor of `main`, not the tip. The canonical-worker build also checks
   out the exact solver commit attested by `deploy/autofill-solver-sources.json`.
   After all five immutable ECR digests resolve, the workflow uploads one
   `leaf.staging-supply-set.v1` manifest. It binds every digest to the full
   application SHA, records the worker's application SHA, solver SHA, and
   attested solver source SHA-256, and records a deterministic SHA-256 for the
   exact web `dist/` artifact uploaded by the same run. This five-OCI document
   describes the staging supply set. It is not final production identity.
2. **Accept staging (terraform repo)**: run the protected staging authored-CAD
   acceptance in `execute` mode. Its successful artifact must name the same
   full source SHA and the same five digests as the staging supply-set manifest.
   The producer must include its GitHub run ID and attempt in the artifact name.
3. **Create the production handoff (this repo)**: manually run the build
   workflow with `promote=true`, the full release `source_sha`, the successful
   build workflow run ID and attempt, the successful acceptance workflow run ID
   and attempt, and its exact attempt-specific
   `staging-authored-execute-*-run-<run-id>-attempt-<attempt>` artifact name.
   The build and push jobs are disabled on this handoff run. It accepts only the
   canonical workflow file and workflow ID, expected event, `main` branch,
   trusted head SHA, and supplied attempt for both upstream runs. It downloads
   artifacts by their verified artifact IDs, verifies that the source is an
   ancestor of `main`, binds the receipt's internal run ID to its artifact name,
   and proves that all five accepted staging digests equal the release manifest.
   It uploads a
   `leaf.production-handoff-candidate.v1` receipt. It does not rebuild, retag,
   or deploy an image.
4. **Deploy (terraform repo, not yet receipt-enabled)**: production must consume
   that handoff before it may change ECS or public traffic. Production app,
   broker, harness, and canonical-worker are OCI/ECS workloads. Production web
   is the Vercel project `leaf-platform-web`, not the staging web OCI image. The
   final production identity must therefore use a typed v2 shape with four OCI
   digests plus the exact Vercel deployment ID and the matching web artifact
   SHA-256. A staging web image digest alone is never production web proof. The
   old three-image tag-only dispatch was removed from this repository. Until
   protected infrastructure and Vercel workflows consume and revalidate the
   candidate, promotion stops at the artifact and fails closed.

The ECR permission change is a required companion infrastructure PR. The
`leaf-github-web-demo-ecr-push-role` must grant only the image upload and read
actions needed for the five named `leaf-platform-*` release repositories. This
application change does not grant, emulate, or bypass that IAM authority. The
release build must remain blocked until the reviewed infrastructure PR supplies
the role and immutable-tag repository policy.

The historical one-shot CLI provisioning script (`apply.sh` + `taskdef.json`,
outside this repo) is RETIRED from deploy duty; it remains provisioning
history only. Do not CLI-roll task definition revisions; use the workflow
chain above so Terraform state, digests, and rollback baselines stay coherent.
