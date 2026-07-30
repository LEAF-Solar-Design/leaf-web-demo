# platform/ — canonical Project/Job entity

The Leaf web-CAD platform's foundational persistence layer: an org-scoped **Project**
(holds a drawing + its versions + jobs + built tools) and **Job** (one async run),
plus the create/list/open HTTP API the frontend workspace expects and a day-one
tenant offboarding/deletion cascade. Net-new — no such entity existed in the fleet.

Self-contained `platform/` subtree. **Zero edits to any existing file** (this is the
parallel-safety story: ~12 siblings would otherwise collide on `server/app.py`).

## Layout

| File | Role |
|------|------|
| `migrations/0001_project_job.sql` | 5 tables: `orgs`, `projects`, `drawing_versions`, `jobs`, `built_tools`. `CREATE TABLE IF NOT EXISTS` throughout (idempotent, concurrent-sibling-safe). |
| `models.py` | Dataclasses mirroring the tables + row mappers (dependency-free). |
| `db.py` | Lazy shared connection pool + cursor helpers; reads `DATABASE_URL`. |
| `store.py` | Org-scoped reads/writes. **Every read binds `WHERE org_id = %(org_id)s`.** |
| `offboard.py` | `offboard_org()` — the ONLY hard-delete path (compliance exception). |
| `api.py` | FastAPI `APIRouter` (create/list/open projects + jobs; admin offboard). |
| `deps.py` | `get_org_id` dependency (dev: `X-Org-Id` header; prod seam for the auth sibling). |
| `tests/` | pytest suite (isolation, offboard, project-API, store-guard). |

## Integration — the one line to mount the router (out of scope to apply here)

A sibling owns `server/app.py`; do NOT edit it as part of this work. To wire the
platform API into the demo backend, an integration/root session adds one line:

```python
# in server/app.py, after `app = FastAPI(...)`
from platform.api import router as platform_router
app.include_router(platform_router)
```

`server/app.py` already inserts `PROJECT_ROOT` onto `sys.path`, so `import platform`
in that process resolves THIS package (see the shadow note below) — acceptable there
because the demo backend does not need the stdlib `platform` module. If that changes,
mount via the alias instead: load this package as `leaf_platform`
(`importlib.util.spec_from_file_location("leaf_platform", ".../platform/__init__.py",
submodule_search_locations=[".../platform"])`) and `include_router(leaf_platform.api.router)`.

## The `platform/` vs stdlib `platform` shadow (the known trap — solved)

This directory is named `platform/`, which shadows Python's stdlib `platform`
module whenever the repo root is on `sys.path`. Solved deliberately:

1. **This package never imports the top-level name `platform`** and uses only
   *relative* imports internally (`from .db import ...`). So loading it never forces
   third-party code's `import platform` to resolve here.
2. **The test suite imports the package under the non-colliding alias `leaf_platform`**
   (registered in `tests/conftest.py`), never as `platform`.
3. **There is no `platform/tests/__init__.py`.** With it present, pytest imports the
   tests as a subpackage of `platform`, which collides with the cached stdlib module
   (`ModuleNotFoundError: 'platform' is not a package`) and the whole run errors at
   conftest load. Omitting it keeps the tests as top-level modules. **This is the one
   sanctioned deviation from the planned file list** — it is exactly the "conftest /
   sys.path hygiene" the plan called for, and it is required for the acceptance command
   `pytest platform/tests/ -q` to pass.
4. `tests/conftest.py` additionally evicts any shadowing `platform` from `sys.path` /
   `sys.modules` and re-resolves the stdlib, so the suite is robust to both the console
   `pytest` script and `python -m pytest` (which puts CWD on `sys.path`).

Verified: `test_store_guard.py::test_stdlib_platform_not_shadowed` asserts
`import platform` resolves the genuine stdlib module inside the test process.

## Tenant isolation (the enforced boundary, v1)

- **Application-layer org scoping (enforced):** every read in `store.py` takes
  `org_id` as its required first arg and binds `WHERE org_id = $1`. A read for a
  resource not owned by the caller returns `None`/`[]`; the API turns a missing single
  resource into **HTTP 404, never 403** (a 403 leaks existence).
- **Structural guard:** `test_store_guard.py` statically asserts every SELECT in
  `store.py` carries an `org_id` predicate and every read function's first parameter
  is `org_id`.
- **RLS (defense-in-depth, optional):** the policy SQL ships commented in the
  migration; a deployment that wants belt-and-suspenders sets `app.current_org` per
  connection. Not required to pass acceptance.

## Offboarding / deletion (day-one compliance exception)

`offboard.py::offboard_org(org_id, *, key_purge_hook, blob_purge_hook,
secret_ref_provider=None)` is the **only** hard-delete path:

1. Collect secret refs (via `secret_ref_provider`, default = the org's canonical
   `leaf/<org_id>/credentials` ref) and out-of-band blob refs (`oss_object`,
   `intake_ref`, `source_ref`) owned by the org.
2. Fire `key_purge_hook(ref)` once per secret and `blob_purge_hook(ref)` once per blob
   — injected, not implemented here (they target the vault `deleteSecret` / APS-OSS
   purge contracts the credential-broker & storage siblings own).
3. Delete the org's projects (the `ON DELETE CASCADE` wipes versions/jobs/built_tools),
   then write an `orgs` tombstone (`status='deleted'`, `offboarded_at=NOW()`) — an audit
   line survives without retaining tenant IP.

## Database provisioning + running the tests

`DATABASE_URL` is read from the environment or `platform/.env.local` (gitignored).

```bash
pip install -r platform/requirements.txt
# apply the schema (fresh DB):
psql "$DATABASE_URL" -f platform/migrations/0001_project_job.sql
# run the suite (applies the migration itself via a session fixture):
pytest platform/tests/ -q
```

The Wave-1 build ran against an **ephemeral Neon branch** `leaf-platform-dev-w1`
(`br-small-bar-ahkshkll`) of project `raspy-paper-88661739` (`leaf-portal-db`); the
"exactly five tables on a fresh DB" check ran against a local Docker Postgres
(`postgres:16`, `docker run -d --name leaf-platform-pg -e POSTGRES_PASSWORD=leafdev
-p 5433:5432 postgres:16`).

**Resource decision (2026-07-18): KEEP both.** The Neon branch is ready and the
`leaf-platform-pg` container is healthy/running. They remain the low-cost,
reproducible backing stores for platform integration tests; do not delete them
until those tests have moved to the deployed environment.

## Tier entitlement enforcement on the jobs lane (P1 floor)

`POST /api/projects/{id}/jobs` branches on the caller org's stored `tier`
(`platform/entitlements.py`): each job kind consumes a server-lane capability
(`solve`/`run` → `run_write`, `extract` → `run_read`, `build` → `build`) and
the tier→capability policy is the SAME operator-tunable file the server lane
enforces (`server/entitlements.json`, override `LEAF_ENTITLEMENTS_FILE`),
resolved through `server/entitlements.py`'s fail-closed `entitlements_for`.
Denials return the documented `entitlement_required` 403 envelope
(CONTRACT-ADDENDUM §17). Fail-closed rules: missing org row, non-`active` org
status, unknown/blank tier (→ `restricted`), unmapped kind, and an
unevaluable enforcement seam (policy file present-but-invalid, org row
unreadable) all refuse the job — the last as a structured 503 with the full
envelope (`error.error_code = INTERNAL`, `retryable = true`), never a bare
500. The same stored-org check runs inside
`canonical_jobs.submit_solve_job`, the choke point of the `POST /api/run`
canonical spine path, so a permissive request-side tier cannot bypass the
floor there.

Binary proof: `python scripts/entitlement-gate.py` exits 0 (READY) only when
an entitled org's solve succeeds AND a restricted org's solve is DENIED;
anything else — including an unreachable enforcement point — is NOT-READY
exit 1. Tests: `platform/tests/test_entitlement.py`.

## Org bootstrap route — `POST /api/orgs` (dev posture: OPEN)

`POST /api/orgs {name, tier?}` mints an org and returns `{org: {org_id, name,
tier, status, created_at, ...}}` (calls `store.create_org`). It is the HTTP way
to mint the `org_id` every project/job route requires — the true blocker the
exposure map named (`create_org` existed in the store but was unexposed).

**This endpoint is intentionally OPEN in dev** (no auth gate) to solve the
bootstrap chicken/egg: you cannot present an `X-Org-Id` you do not yet have.
**In production it MUST be gated behind the auth/identity layer** — org creation
becomes a side effect of first login / provisioning, and a client-supplied
identity is never trusted here (same seam as `deps.get_org_id`). `tier` is
optional and, when supplied, validated against `models.TIERS` (else 422);
omitted → the store default (`hosted_starter`).

Dev-only extra: an optional `external_subject` (+ `external_authority`,
default `auth0`) bootstraps the org WITH its identity binding
(`store.create_org_with_identity`), mirroring the live-auth shape so the
billing org-resolve path (contract/BILLING.md §3.1) is exercisable without
Auth0. With `LEAF_AUTH_LIVE=1` the field is refused (422).

`GET /api/orgs/{org_id}` keeps the **404-not-403** isolation posture of the
project/job reads: a caller may read only its OWN org (the `X-Org-Id` header
must equal the path `org_id`); a cross-org or unknown id returns 404, never 403
(a 403 leaks existence).

## Open integration note — orgs-table ownership

The chosen dev DB (`leaf-portal-db`) already contains a Prisma-style `Organization`
table (capital O), plus `User`, `OrganizationMember`, `Subscription`. This entity's
`orgs` table (lowercase) is deliberately distinct and net-new. **A forthcoming
auth/identity sibling may own canonical tenant identity** — reconcile `orgs` vs
`Organization` at integration (adopt one, or make `orgs.org_id` a FK/mirror of the
identity table). Out of scope for this session.
