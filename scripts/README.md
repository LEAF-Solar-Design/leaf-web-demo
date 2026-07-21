# Test orchestration — `scripts/`

CI-ready entry points for the Leaf web demo:

| File | What it is |
|------|------------|
| `run-all-gates.py` | Runs every test suite in the repo, each in its **own** subprocess, and prints one PASS/FAIL scoreboard. Exit 0 iff every non-skipped gate passes. |
| `deploy-web.py` | Cloud-builds `web/`, verifies the unaliased artifact, then promotes and verifies production. The production contract rejects localhost/API-less/Auth0-less bundles. |
| `../server/tests/test_e2e_golden.py` | One self-contained golden-path e2e that boots the broker + app and drives the whole product over HTTP. Run by the gate runner, also runnable alone. |

---

## Quick start

```bash
# full gate (from the repo root)
python scripts/run-all-gates.py

# just the golden-path e2e
cd server && python -m pytest tests/test_e2e_golden.py -q

# build + deploy the web demo to production, then verify it
python scripts/deploy-web.py
```

---

## Deploying the web demo

`deploy-web.py` asks Vercel to cloud-build `web/`. This is required because
sensitive project variables are intentionally withheld from `vercel env run`;
only Vercel's cloud build can inject the production `VITE_*` values safely.

Production deploys are staged with `--prod --skip-domain`. The script crawls
the generated JavaScript chunks, rejects localhost/API-less/Auth0-less output,
requires the exact source commit injected as `LEAF_SOURCE_SHA`, and verifies
every SPA route before calling `vercel promote`. It then verifies
the production alias serves the exact staged entry asset. A failed post-promote
check requests `vercel rollback` automatically.

The pre-promotion backend check requires the configured platform host to return
200 from `/api/health` and 401 (the expected no-token boundary) from
`/api/session`. This prevents a healthy but unrelated API hostname from passing
the bundle string check and leaving every real platform request at 404.

The SPA fallback explicitly excludes `/api/*`. The web origin therefore cannot
answer an intuitive health probe with the HTML shell and falsely masquerade as
the external backend.

The local build is only a structural preflight. It checks that Vite still emits
an entry asset and that the SPA rewrite is present; it does not claim to test
sensitive production environment values. Two files preserve the route contract:

- `vercel.json` lives in `web/public/`, so vite copies it into `dist/` on every
  build. Without it every route except `/` returns 404, because `/` is the only
  path that exists as a real file.
- `web/vercel.json` configures Vercel's cloud build and its output directory.
  Project linkage comes from `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID`, set by the
  script, rather than a committed `.vercel/` directory.

The script refuses to deploy a `dist` that would 404 — it fails if `vercel.json`
is missing, has no `rewrites`, or sets `cleanUrls: true`. That last one is not
hypothetical: `cleanUrls` 308-redirects `/index.html` to `/`, which makes the
catch-all rewrite point at a redirect, and every deep route 404s.

Prereq: `vercel login` once (the CLI's auth token is the credential — the org
and project ids in the script are identifiers, not secrets).

Prereqs: Python deps (`pytest requests jsonschema fastapi uvicorn psycopg`) and,
for the harness gates, Node ≥18 with `npm install` already run in `harness/`.

---

## Why separate processes (the whole point)

These suites **cannot** share one pytest process — they cross-contaminate:

- **Global auth toggle.** The auth suites flip `LEAF_AUTH_LIVE`, a process-global
  env flag read at call time. A suite that expects off-auth behaviour breaks if a
  prior suite left it on.
- **Shared on-disk state.** `jobs.db`, the versioned drawing store
  (`LEAF_STORE_DIR`), `authored_tools.json`, and the broker ledger/tenants are all
  files. Two suites in one process step on each other's rows.
- **Fixture / port collisions.** Every backend suite boots its own broker + app on
  ephemeral ports via module-scoped fixtures; combining them multiplies the
  fixtures and races the ports.
- **The `platform/` stdlib shadow.** The repo contains a package literally named
  `platform/`, which shadows Python's stdlib `platform` module the moment the repo
  root lands on `sys.path`. The platform suite therefore runs from a **different
  cwd** (the repo's parent) with its own shadow-defusing `conftest.py`. Import
  `jsonschema` (→ `attr` → `platform.python_implementation()`) with the repo root
  on `sys.path` and it explodes — which is exactly why the server/da suites run
  with their own cwd and this runner never puts the repo root on its own path.

So the runner launches **one subprocess per suite**, each with the correct cwd and
a cleaned env, captures pass/fail + counts, and reports a single scoreboard.

---

## What `run-all-gates.py` runs

| Suite (id) | cwd | Command |
|------------|-----|---------|
| `server-*` (backbone, auth, dynamic-loader, write-loop, nl-router, ui-wave, wave2–5) | `server/` | `python -m pytest <file>` |
| `server-e2e-golden` | `server/` | `python -m pytest tests/test_e2e_golden.py` |
| `da-store`, `da-multitenant` | `da/` | `python -m pytest <file>` |
| `platform` | repo **parent** | `python -m pytest leaf-web-demo/platform/tests` (DB-gated) |
| `harness-vitest` | `harness/` | `npm test` |
| `harness-tsc-noemit` | `harness/` | `npx tsc --noEmit` |
| `harness-tsc-build` | `harness/` | `npx tsc -p tsconfig.build.json` |

Two special behaviours, both surfaced on the scoreboard:

- **`authored_tools.json` reset before nl-router.** Gitignored authored-tool
  pollution can outrank `count-by-layer` and make NL routing flaky, so the runner
  backs the file up into the log dir and resets it to `{"tools": []}` before the
  `server-nl-router` suite.
- **Platform DB gating.** The platform suite needs a reachable Postgres
  (`DATABASE_URL`, or `platform/.env.local`). The runner probes it first (from the
  shadow-safe cwd); if unreachable the suite is **SKIP**-with-reason instead of a
  red failure.

### Flags

```
--fail-fast     stop at the first failing gate (default: run all)
--continue      run every gate even if one fails (this IS the default)
--only SUBSTR   only run suites whose id contains SUBSTR (e.g. --only server)
--retry N       re-run a FAILED suite up to N more times (default 1)
--log-dir DIR   where per-suite logs land (default: C:/tmp/leaf-web-demo-gates)
```

**About `--retry`.** Every backend suite boots a real broker + app on ephemeral
ports and drives them over HTTP. On a heavily loaded box the broker's first
`/broker/run` (which lazily loads the engine registry + the 2345-polyline cached
intake) can be CPU-starved and time out — a transient, not a logic failure. The
default single retry re-runs a failed suite once; a suite that then passes is
annotated on the scoreboard (`flaked; passed on attempt 2/2`) rather than masked,
and a genuinely broken suite fails every attempt and is reported red. Use
`--retry 0` to capture raw first-attempt results (e.g. to measure flake rate).

Full per-suite output goes to `<log-dir>/<suite>.log`; only the scoreboard prints
to stdout. Exit code is **0 iff every non-skipped gate passed**, else 1
(`2` if `--only` matched nothing).

---

## What `test_e2e_golden.py` covers

One test, proving the **composed** system across real process boundaries
(complementary to the unit suites, which each test one concern). At `APS_LIVE=0`
— no live APS, no Agent SDK/harness, no LLM — it boots the broker + app on
**ephemeral free ports** (never assumes 8130/8140; stale processes squat them)
and drives:

1. `GET /api/health` — both processes up, offline demo mode.
2. `POST /api/nl-prompt("count panels per layer")` → RUN lane, `count-by-layer`.
3. `POST /api/run` → `202 {job_id}` → poll `GET /api/jobs/{id}` to `complete`;
   `result.counts.Panels == 2345` (the golden oracle).
4. `GET /api/capabilities` — capability families present.
5. `POST /api/run delete-marked-panel` — write loop creates **v2** (parent v1),
   `result.new_version == {drawing_id: demo, version: 2, parent: 1}`.
6. `GET /api/drawings/demo/versions` — the v1 → v2 chain.
7. `POST /api/drawings/demo/undo` — head steps back to **v1**.
8. `GET /api/entitlements` — off-auth **demo** tier, full access.
9. `POST /api/author` (template path) — the new tool appears in `/api/tools`.

**Self-contained.** It resets `authored_tools.json` to clean for its booted app
(deterministic NL routing), **restores it byte-for-byte** on teardown, removes any
`authored/*.py` the author step created, and tears down both processes in a
`finally`. Runs in well under a minute.
