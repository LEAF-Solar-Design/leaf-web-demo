# Test orchestration — `scripts/`

CI-ready entry points for the Leaf web demo:

| File | What it is |
|------|------------|
| `run-all-gates.py` | Runs every test suite in the repo, each in its **own** subprocess, and prints one PASS/FAIL scoreboard. Exit 0 only when every selected gate passes. Test-level skips require an exact allowlist and never satisfy the executed-test floor. Pytest pins reasons; Vitest pins files and counts. Run in CI by `.github/workflows/test-gate.yml` on every pull request and every push to `main`, and again by `build-platform-images.yml` before any image is pushed to ECR. |
| `deploy-web.py` | Builds `web/` and deploys `web/dist` **itself** to the `leaf-platform-web` Vercel project, then verifies the live domain. Exit 0 iff every route returns 200 and the domain serves the asset filenames this build produced. |
| `production_web_release.py` | Validates attempt-bound release and handoff evidence, safely extracts artifacts, prepares a no-rebuild Vercel Build Output API package, and emits the sanitized protected-workflow receipt. It does not call Vercel itself. |
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

`deploy-web.py` deploys the build output directly. There is **no staging
directory** to keep in sync — that was the old arrangement and it drifted.

Two things make `web/dist` self-sufficient as the deploy root:

- `vercel.json` lives in `web/public/`, so vite copies it into `dist/` on every
  build. Without it every route except `/` returns 404, because `/` is the only
  path that exists as a real file.
- Project linkage comes from `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID`, set by the
  script. A committed `.vercel/` directory would not survive, because vite
  empties `dist/` on each build.

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
| `web-demo-gate` | repo root | `bash dispatch/run-local-ci.sh --only demo-gate` |

`web-demo-gate` is this runner's only entry point into `web/`. It drives the
demo-gate bucket, which runs web/'s seven golden-path node oracles
(`test/check_routes.mjs`, `test/check_integration.mjs`,
`scripts/check_author.mjs`, `check_writeloop.mjs`, `check_tourscript.mjs`, and
two more), the vite build with a `>=2` JS-chunk assertion, the offline
pre-flight, and the authored-tool registry probe. It needs a POSIX bash; on
Windows the runner resolves Git Bash explicitly rather than the System32 WSL
launcher, which would misread the tree as Linux.

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
--only SUBSTR   only run suites whose id contains SUBSTR (e.g. --only server);
                repeatable, and repeats UNION (--only a --only b runs a OR b)
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

**About `--only`.** It is repeatable and each occurrence adds to a **union**:
`--only server-backbone --only harness-vitest` runs every suite matching either.
A substring that matches **no** suite exits `2` and names itself instead of
letting the surviving substrings produce a green scoreboard for less than was
asked for. The scoreboard prints a `filter:` line echoing the exact selection,
so a result can be checked back against the command that produced it.

Full per-suite output goes to `<log-dir>/<suite>.log`; only the scoreboard prints
to stdout. Exit code is **0 only when every selected gate passed and every
test-level skip matched an explicit reason allowlist**, else 1. A skipped test
never counts toward a suite's minimum executed-test floor.
(`2` if any `--only` substring matched nothing).

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
