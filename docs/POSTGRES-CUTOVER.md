# PostgreSQL production cutover

PostgreSQL support now spans migrations `0001` through `0020`. Production
authority has not been proven to have moved. Repository defaults still select
SQLite, files, process memory, or disabled mutation for every opt-in shared
authority.

The machine-readable source for selector ownership and cutover readiness is
[`platform/authority-inventory.json`](../platform/authority-inventory.json).
It records PostgreSQL tables, legacy sources, backfill and parity status,
cutover modes, rollback limits, and live selection evidence. The inventory
marks the current staging and production selections as unknown because this
repository has no current task-definition environment receipt for either
environment. Do not infer a live selection from a Dockerfile or Compose
default.

## Migration inventory

The migration runner applies every sorted `NNNN_*.sql` file, which is all 20
files in this commit. `assert_schema_current()` validates the API and canonical
worker's required table and column subset and reports the shipped manifest
count. It is not an applied-migration ledger and does not prove that every
authority table exists.

| Migration | PostgreSQL implementation |
|---|---|
| `0001` | orgs, projects, drawing versions, canonical jobs, built tools |
| `0002` | deletion and purge columns |
| `0003` | tenant and project authority modes, identity bindings, canonical history, solves, outbox |
| `0004` | canonical job leases, attempts, terminal fencing, worker heartbeats |
| `0005` | project share grants |
| `0006` | platform snapshots, channels, job and solve pins |
| `0007` | compliance runs, findings, waivers, waiver events |
| `0008` | evidence bundles and entries |
| `0009` | professional credentials, credential events, review signatures |
| `0010` | drawing artifacts and canonical drawing identity |
| `0011` | async jobs, terminal conflicts, callback replay nonces |
| `0012` | app sessions, events, approvals |
| `0013` | agent approvals, grants, counters, kill state, audit, tenant policy, usage |
| `0014` | broker tenants, usage ledger, admissions, APS slots, resolution audit |
| `0015` | guest upload counters |
| `0016` | drawing manifests and versions, upload attempts, purge receipts |
| `0017` | harness sessions, turns, events, confirmations, usage, tenant repository leases |
| `0018` | drawing import provenance and replay protection |
| `0019` | per-session model selection |
| `0020` | customization changes, confirmations, publication requests, effective catalogs, deployment snapshots, and audit |

Schema availability is not data migration. Most selectors have no historical
backfill command and no live-data parity command. The inventory states this
explicitly. App sessions are the exception in part: `LEAF_SESSIONS_STORE`
supports `dual_write`, `dual_write_shadow`, and `shadow` for new writes and
runtime read comparison, but it still has no historical SQLite backfill
command.

Customization is also an exception. The app can select
`LEAF_CUSTOMIZATION_STORE=postgres` after migration `0020`. The reconciliation
command first runs the SQLite store's own idempotent schema initialization and
guarded legacy migrations on the source (`SQLiteCustomizationStore.initialize`:
`CREATE TABLE IF NOT EXISTS` plus the confirmation-binding and
publication-request migrations, so a never-touched or pre-migration store
reads correctly instead of failing as incomplete). It therefore requires
write access to the SQLite file for schema and migration only; it never
inserts, rewrites, or deletes source rows outside those guarded migrations.
The subsequent snapshot itself opens the file read-only. It refuses a
shared-primary-key conflict. It inserts SQLite-only rows, preserves
PostgreSQL-only rows retained from an earlier partial cutover, and has no
update or delete path. Its v2 receipt reports both aggregate digests, source
and target counts, target-only counts, `source_incorporated`, and
`exact_equal`. A strict PostgreSQL superset is incorporated but not equal:

```shell
python /app/scripts/reconcile_customization_authority.py --mode backfill \
  --sqlite /data/state/customization.db
python /app/scripts/reconcile_customization_authority.py --mode parity \
  --sqlite /data/state/customization.db
```

The script path is absolute on purpose. `deploy/Dockerfile.app` COPYs the
script to `/app/scripts/` but its final `WORKDIR` is `/app/server`, so a
repo-relative `python scripts/reconcile_customization_authority.py` resolves
to `/app/server/scripts/...`, which does not exist. Because these commands are
run from the release image and nowhere else, the relative form fails in the
only place it is used, while still working from a source checkout — which is
why it went unnoticed. Do not tidy it back. The `--sqlite` path is a runtime
volume, not a path into the image, and is correct as written.
`server/tests/test_postgres_container_wiring.py` enforces this for every
authority command whose script the app Dockerfile copies.

Run both commands from the exact release image while SQLite writes are dark,
before selecting PostgreSQL. The historical `parity` mode name now proves
source incorporation and reports exact equality separately. Save both receipts
as staging evidence. The commands do not make a live selector change.

## Current authority summary

| Mutable authority | Selector | Repository default | PostgreSQL state |
|---|---|---|---|
| Canonical project ledger | tenant or project `authority_mode` row | `legacy_sqlite` when no row exists | implemented in `0001` to `0010` and `0018` |
| Async jobs and callback replay | `LEAF_JOBS_STORE`, `LEAF_CALLBACK_REPLAY_STORE` | `legacy` | implemented in `0011`; no backfill command |
| App sessions and approvals | `LEAF_SESSIONS_STORE` | `legacy` | implemented in `0012` and `0019`; dual-write and shadow modes exist |
| Agent security and usage state | `LEAF_AGENT_STORE` | `legacy` | implemented in `0013`; clean switch only |
| Broker tenants, admissions, slots, and usage | `LEAF_BROKER_STORE` | `legacy` | implemented in `0014`; clean switch only |
| Guest caps | `LEAF_GUEST_CAP_STORE` | `memory` | implemented in `0015`; clean switch only |
| Drawing and upload metadata | `LEAF_DRAWING_STORE`, `LEAF_UPLOAD_STORE` | `legacy` | implemented in `0016` and `0018`; bytes remain outside PostgreSQL |
| Drawing bytes | `LEAF_BLOB_STORE` | `legacy` | no PostgreSQL byte store; `filesystem` is required by the PostgreSQL startup gate |
| Harness sessions and repository leases | `LEAF_HARNESS_SESSION_STORE` | `file` | implemented in `0017`; no historical backfill command |
| Harness grants | `LEAF_GRANT_STORE` | `file` | `vault` is an unimplemented fail-closed seam; tokens are not in PostgreSQL |
| Tenant repository mutation | `LEAF_HARNESS_AUTHORING_MODE` | `disabled` | `singleton` exists; `fleet` is blocked until the vault exists |
| Customization R5 and R6 | `LEAF_CUSTOMIZATION_STORE` plus the R5 and R6 rollout modes | `sqlite`, with both rollout modes `off` | implemented in `0020`; reviewed source-incorporation and equality evidence commands exist |

`LEAF_PLATFORM_POSTGRES_REQUIRED=1` is a startup gate, not an authority
selector. It requires live auth, a direct `DATABASE_URL`, current migrations,
PostgreSQL drawing, upload, and customization metadata, and filesystem blob
storage. A database connection alone does not select any authority.

## Migration and reconciliation contract

The one-shot migration container applies all migrations and then calls
`db.assert_schema_current()`. The API can run the same subset check at startup
with `LEAF_PLATFORM_POSTGRES_REQUIRED=1`. The canonical worker checks its
required schema before entering its claim loop. Each opt-in store also has its
own startup or first-use table check. Save all of those results because the
platform assertion alone does not cover every table in migrations `0011`
through `0019`. These checks do not print the connection string.

Capture the credential-free aggregate snapshot from the same platform package
as the API:

```shell
python -c "import json,sys; sys.path.insert(0,'/app/platform'); import db; print(json.dumps(db.reconciliation_snapshot(),sort_keys=True))"
```

This snapshot proves schema status, aggregate record counts, and canonical
authority-mode counts only. It is not row-level parity evidence for migrations
`0011` through `0019`.

For a local schema rehearsal:

```shell
docker compose -f docker-compose.yml -f docker-compose.canonical.yml up \
  --build --abort-on-container-exit migrate
docker compose -f docker-compose.yml -f docker-compose.canonical.yml up -d \
  canonical-worker app
```

The overlay supplies database connections but keeps legacy selectors by
default. It is not evidence of a staging or production cutover.

## Required cutover evidence

1. Record the exact release commit, task definitions, environment, database,
   region, backup policy, RPO, RTO, pool limits, identities, and grants.
2. Run the protected one-shot migration and save `assert_schema_current()`
   output from the migration, API, broker, harness, and worker images that use
   PostgreSQL.
3. Resolve every `unknown` and `not_implemented` item in the authority
   inventory. Add reviewed backfill and live-data source-incorporation commands
   before a selector changes.
4. Rehearse each authority alone. Prove retries, task replacement, lease
   expiry, stale-owner fencing, and duplicate-charge prevention.
5. For app sessions, progress through the dual-write modes while the service is
   still single-task. SQLite and PostgreSQL cannot share one atomic
   transaction. Do **not** route the promotion through `shadow`: `shadow` is
   absent from `session_store._DUAL_WRITE_MODES`, so it stops mirroring to
   PostgreSQL entirely and every write made while it is selected is missing
   from the store the flip then promotes. `shadow` is on the return path.
   Promote `dual_write_shadow` straight to `postgres`, as
   `platform/authority-inventory.json` `rollback_mode` already records.
6. Prove rollback for each authority. A selector rollback without a reverse
   backfill can lose writes made after cutover, so it is not a complete
   rollback.
7. Change one staging selector at a time, then save live source-incorporation
   evidence and a rollback receipt. Do not roll back PostgreSQL to SQLite after
   PostgreSQL accepts writes until a separately fenced reverse backfill proves
   coverage and exact equality.
8. Use a production canary only after staging is complete and an operator has
   approved production work.
9. Keep the one-task deployment and 300-second drain until every mutable
   single-writer authority is removed or accepted with written evidence.

## Staging app-sessions pre-deploy source data: DISCARD (decided 2026-08-06)

`scripts/reconcile_sessions_authority.py` (PR #488) reaches a container only
through an app image build, and deploying that image replaces the ECS task.
Staging's `leaf-platform-app` container sets no `SESSIONS_DB`, so its legacy
source is the task-local `server/sessions.db`, which the image does not carry.
The deploy therefore destroys the database the backfill exists to read. Every
row PostgreSQL retained from **before** the deploy then classifies as an
expected target-only row, with no surviving source row to contradict it, so the
command is expected to report success having recovered nothing. Rows written
after the deploy land in both stores and match normally, and a fresh
post-commit mirror failure would still be reported, so the clean result is the
expected outcome rather than a guaranteed one.

**Decision: do not preserve or export it. Deploy and let it go, accepting the
loss described below.** This is the deliberate record the backfill note asks
for, so that a later clean parity run on staging is never mistaken for a
successful recovery.

**Discard accepts real, if small, loss. It is not lossless, and an earlier draft
of this record wrongly claimed it was.** Two things are actually at risk.

*Source-only rows from the unmirrored window.* Dual-write is **not** an atomic
two-phase commit, and the inventory's own `rollback_mode` says so: "The two
stores have no atomic transaction." What the code provides is a **pre-flight
check that narrows the window, not a fail-closed write**. Before mutating the
legacy store, `get_or_create_session` calls `_pg_ensure_started()` ("Do not
mutate the legacy authority when its required mirror is already known to be
unavailable"), `append_event` raises when the parent mirror row is missing, and
`create_approval` raises when the mirror already exists. But the legacy write
still **commits first** (`session_store.py` legacy at `:1260`, `:1295`, `:1447`;
mirror at `:1262`, `:1298`, `:1455`), and an unwrapped mirror exception cannot
roll it back. `test_reconcile_sessions_authority.py` documents exactly this:
"The app can commit a legacy write ... and fail before its PostgreSQL mirror,
leaving a source-only row." Turn acquisition, turn release, approval decision
and approval consumption share the shape.

*Tables with no PostgreSQL home at all.* `server/checkpoints.py` and
`server/session_policy.py` resolve `SESSIONS_DB` the same way and put
`session_checkpoints` and `session_policies` in the **same file**. Neither is
mirrored, and neither is among the reconciler's three table pairs. Preserving
the file would therefore not let the backfill recover them; that would need
migration work which does not exist. Under the task-local configuration measured
here, any task replacement destroys them, so this deploy is not special for them
and neither is the next one. Whether earlier staging revisions were also
task-local was not checked, so read that as a property of the current
configuration forward, not as a history.

What bounds the loss, and why discard is still right for staging:

1. `deploy/Dockerfile.app` ships no `sessions.db`, and `server/session_store.py`
   resolves `SESSIONS_DB` to the task-local `server/sessions.db` by default, so
   the file is created empty when the task starts.
2. Task `48b23717a06a4d3b9af71f685ca396d3` started 2026-08-06T14:29:48Z on
   `leaf-platform-app-alt:27` and runs that revision for its whole life; a task
   cannot change task definition mid-life.
3. That revision sets `LEAF_SESSIONS_STORE=dual_write_shadow`, so mirroring was
   active for the file's entire existence and the mirror is the common case.

So the exposure is one task-lifetime of staging traffic, and within that only
rows that hit the post-commit/pre-mirror window, plus checkpoints and policies
that no task replacement preserves under the configuration measured here. The
alternative costs a new access path into
a running task: `enableExecuteCommand` is false on both app families, and
`aws ecs run-task` is refused by the deploy workflow because it would start
`init-drawing-mutations-fence` and disturb the drawing-mutation fence that
sibling lanes depend on. That path would itself need review. For staging, the
loss is not worth that.

**None of this was checked against the file.** It is an argument from the code
and the task definition; nobody read the database, and after the deploy nobody
can.

**Production is the opposite case, and this decision must not be reused there.**
Production runs sessions on `legacy`, measured 2026-08-07 across two layers.
`leaf-automation-production-platform:98`, the revision service `leaf-platform`
runs at desired 1 / running 1, does not set `LEAF_SESSIONS_STORE` in any of its
four container definitions, in `environment`, in `secrets`, or in
`environmentFiles`. That alone would not settle it: **a Docker image `ENV` stays
in the process environment when ECS supplies no override**, so an absent
task-definition variable does not mean the code fallback runs. The image that
revision pins, `leaf-platform-app@sha256:1504240d...`, bakes
`LEAF_SESSIONS_STORE=legacy` in its config blob, matching
`deploy/Dockerfile.app`. So the app reads an explicit `legacy`. Both paths agree,
and the effective mode is `legacy` either way.

`platform/authority-inventory.json` records this as `measured_no_override`
rather than `measured`, because **nobody selected `legacy` for production**. It
is the value the image ships, left unoverridden. The same revision *does*
override `LEAF_AGENT_STORE`, `LEAF_DRAWING_STORE`, `LEAF_UPLOAD_STORE` and
`LEAF_BROKER_STORE` to `postgres` over the same image defaults, so production
deliberately cut those over and has not cut sessions over.

Two consequences follow. Under this revision production performs no PostgreSQL
writes for sessions, so nothing is mirrored forward as it is written, and a
cutover has to account for the **entire** production history rather than a
post-deploy remainder. Production also sets `SESSIONS_DB=/data/state/sessions.db`
on the durable EFS volume instead of leaving it unset, so that history survives
task replacement and the whole reason discard was cheap for staging is absent.

**What this does not establish.** It is a measurement of write *routing*, not of
table *contents*. Nobody queried either store. PostgreSQL may already hold
production session or nonce rows from an earlier revision, a canary, or a manual
backfill, and none of the above is evidence that the target is empty. Verify the
target before any backfill or parity run rather than assuming a clean slate.
Re-read the live revision **and** the image digest too: both age, and either
layer can change the answer.

**What a clean staging parity run does and does not prove.** After this deploy,
`--mode parity` on staging compares only rows written since the NEW task
started. Exit 0 there certifies agreement over that window. It is **not**
evidence that history was recovered, and it is **not** a cutover certificate:
the two stores are snapshotted separately and the app holds no lock across its
dual write, so quiescing legacy writes remains a prerequisite of the flip
transaction itself.

Sharpen that one step further, because it is the trap this record exists to
disarm. Source-only rows are precisely what the pre-deploy window could have
produced, and the deploy destroys the only store that could still reveal them.
So the first backfill run on staging is expected to insert nothing, and that
expected-empty result is **indistinguishable from the false-clean**. Read it as
"there was nothing left to read", never as "there was nothing to recover".

**Production is unaffected by this decision.** Production sets
`SESSIONS_DB=/data/state/sessions.db` on the durable EFS volume, so its legacy
source survives task replacement and the backfill has real value there. None of
the ephemerality above applies. Do not reuse this record to justify skipping
preservation in production.

## Executing the staging flip: two things step 5 does not say

Recorded 2026-08-06 after the mismatch fired live on staging (ten HTTP 500s in
`/ecs/leaf-platform-app` between 03:00:18 and 03:14:25 UTC, on
`GET /api/sessions/{id}/transcript` and `GET /api/agent/approvals/pending`).
Both points below are read from the code, not from a completed flip.

**1. The flip can briefly reproduce the very symptom it fixes.** `postgres` mode
short-circuits before any comparison, so a fully flipped fleet cannot mismatch.
A PARTLY flipped fleet can. `_SHADOW_READ_MODES` contains `dual_write_shadow`,
and `get_session` runs
`_shadow_equal("session", legacy, _pg_get_session(session_id))`. So an OLD task
still on `dual_write_shadow` raises whenever its legacy row disagrees with what
a NEW `postgres`-mode task has written.

**The exposed set is: sessions MUTATED by a postgres-mode task, then READ by an
old shadow task.** Both halves are required. A shared read creates no
divergence, and an append by the OLD task is a dual write that keeps the two
stores together, so neither alone can trigger it. Only the new task writes to
one store and not the other. Two shapes, and the second is the easy one to miss:

- A session the postgres task MINTED exists only in PostgreSQL, so the old task
  compares a legacy `None` against a real row.
- A PRE-EXISTING session the postgres task merely APPENDED to has moved in
  PostgreSQL and not in SQLite: `_pg_append_event` runs
  `UPDATE app_sessions SET last_seq = last_seq + 1, updated_at = %s`, and
  `_shadow_equal` compares whole rows, so the stale legacy row and the advanced
  PostgreSQL row differ on `last_seq` and `updated_at` alone. So a session that
  existed long before the flip is still exposed, with no new session and no
  legacy write anywhere in the sequence.

**The obvious mitigation, flipping the drained color first, is NOT AVAILABLE.**
An earlier revision of this section prescribed it. That was wrong, and the
correction is recorded here rather than quietly deleted, because the idea is
attractive enough that the next reader will propose it again.

`deploy-leaf-platform-staging.yml` cannot apply
`app_deploy_intent=configuration` to a drained color. Three gates, in order:

1. `configuration` always downgrades the strategy to `direct`
   ("always runs direct; strategy downgraded").
2. In direct mode `LIVE_SERVICE="$DEPLOY_SERVICE"`, so the live-service check
   inspects the DEPLOY TARGET, and a drained one fails on `desiredCount < 1`
   with "refusing an image deploy to an inactive service".
3. The only escape is barred by name: "A configuration deployment cannot
   activate a drained service".

A later gate would also stop it, though the run never reaches that far: the
rollback baseline requires a RUNNING task ("No running task is available for a
digest-pinned rollback baseline"), which a drained service has none of. It is a
second mechanism on the same path, not an independent escape route, because the
desired-count refusal above fires first.

**`expected_task_definition` does not route the color, but do not read that as
"it only matters for the baseline check".** It has three jobs: it is
pattern-validated against the family, it gates the rollback baseline, and under
configuration intent it also SELECTS the configuration source, because
`CONFIG_TD="${CONFIG_TD:-$EXPECTED_TD}"` when `configuration_task_definition` is
omitted. Routing is `target_color`, which **defaults to `live`**.

The consequence of getting that wrong is a REFUSAL, not a silent misdeploy. A
dispatch naming the drained color's ARN with `target_color` omitted routes to
the live color, and the rollback baseline then compares that service's current
task definition against the supplied ARN and exits: "Live task definition
changed after review. Expected ...; found ...". The two colors use different
task-definition families, so the mismatch is guaranteed. An earlier draft of
this section called that a silent wrong-target deploy. It is not; it fails loudly.

Both app services run `minimumHealthyPercent=100, maximumPercent=200` (measured
2026-08-07), so a direct rolling deploy starts the new task BEFORE stopping the
old one. The overlap window above is therefore inherent to the only executable
path, not something the ordering could have removed.

So the executable shape is: flip the LIVE color and accept a bounded mixed-mode
rollout. **What happens to the other color afterwards is not settled.** A
forward deploy clones whatever configuration baseline it is given, and
`configuration_task_definition` can name any ACTIVE revision, so the drained
color inherits the live selector only when no alternate baseline is supplied.
Do not plan on automatic inheritance: confirm the idle color's registered
revision carries `LEAF_SESSIONS_STORE=postgres` after its next warm, and flip it
explicitly if it does not.

**Re-read the live desired counts immediately before acting rather than trusting
any recorded value, including this document.** Which color is drained flips with
every blue/green cycle: three distinct states were observed within a few hours
on 2026-08-07, and the counts in `platform/authority-inventory.json` disagreed
with a live read on 2026-08-06.

**2. Rollback has a mandatory ordering, and getting it backwards recreates this
incident.** The return path is `postgres` to `shadow` to `legacy`. Every mode on
that path except `postgres` reads the legacy SQLite. Staging's `SESSIONS_DB` is
UNSET, so on staging every one of them resolves to the task-local
`server/sessions.db` that dies with the task, which is exactly the configuration
that produced the 500s. **Any rollback off `postgres` on staging must set
`SESSIONS_DB` to durable storage in the same transaction, before or with the
selector change, never after.** Note also that rolling back after PostgreSQL has
accepted writes needs the fenced reverse backfill named in the contract above;
`scripts/reconcile_sessions_authority.py` is SQLite to PostgreSQL only.

**`app_sessions` exists on staging and holds rows. That is ALL the incident
proves, and it is much weaker than "0012 is applied".** Both failing endpoints
reach `get_session` first, which calls `_pg_get_session` and selects from
`app_sessions` alone; the shadow compare then raises and aborts the request
before anything queries transcript events or pending approvals. So the recorded
mismatches say nothing about `app_session_events`, about `app_approvals`, or
about the unique index on `(tenant_id, drawing_id)` that the separate
`ON CONFLICT` path relies on. Do not stretch this evidence past the one table.

Two things that do NOT prove it, so do not substitute them:

- **A healthy service does not.** `session_store.ensure_started()` has no
  startup or lifespan caller; it runs lazily on the first session request, and
  `/api/health` never consults `session_store`. A task can be healthy having
  never touched the sessions schema.
- **`_pg_ensure_started()` passing does not.** It checks `to_regclass` on the
  three tables and nothing else: not migration history, not columns, not
  constraints, not indexes.

So a pre-flight that asserts schema currency properly (`assert_schema_current()`
from the exact candidate image, as the deploy path already does elsewhere) is
still worth running. What is settled is only that the tables are present.

**Two tables do not move, and the flip is not what strands them.**
`server/checkpoints.py` and `server/session_policy.py` resolve `SESSIONS_DB`
independently and consult neither `_store_mode` nor `LEAF_SESSIONS_STORE`, so
`session_checkpoints` and `session_policies` stay on task-local SQLite before and
after. The SELECTOR is neutral for them; the DEPLOYMENT that applies it is not,
because any task replacement takes that file. Expect checkpoint restores to 404
and custom policies to fall back to `DEFAULT_POLICY = "confirm_all"`, which is
the safe direction and is already inside the DISCARD class above. What IS new
after the flip: a session outlives its own checkpoints for the first time, so
this stops being invisible and starts looking like a regression to a user.

That choice is now closed. See the next section.

## The session annex tables: DECIDED, migrate (2026-08-07)

**Decision: give them a PostgreSQL home. They are NOT classified as disposable.**
`platform/migrations/0029_session_annex.sql` creates `app_session_checkpoints`
and `app_session_policies`, and both modules now dispatch on a store mode
instead of resolving `SESSIONS_DB` and stopping there.

**Why not "disposable".** The disposable reading is TRUE on staging and FALSE on
production, and nothing in the code can tell the two apart. Staging leaves
`SESSIONS_DB` unset, so the file is task-local and the rows genuinely are
throwaway. Production sets `SESSIONS_DB=/data/state/sessions.db` on durable EFS,
so the same two tables there hold restore points and non-default policies that
have accumulated across task replacements. `checkpoints.py` and
`session_policy.py` have no notion of environment, so a written classification
would have been a claim no code could honour, and the first person to read it in
a production context would read it as licence to discard real user state. A
label that is only true in one environment is worse than no label.

**APPLY 0029 BEFORE DEPLOYING THIS IMAGE ANYWHERE POSTGRESQL IS CONNECTED. It
is a hard ordering requirement and it has nothing to do with the selector.**
`migration_manifest()` globs every shipped `.sql` file and `schema_status()`
fails on any `missing_migrations` entry, with no reference to any selector. So
0029 makes `assert_schema_current()` fail for every PostgreSQL-connected
deployment until it is applied, even with `LEAF_SESSION_ANNEX_STORE=legacy`.
Staging sets `LEAF_PLATFORM_POSTGRES_REQUIRED=1` on both app containers
(`terraform/environments/staging/us-east-1/leaf_platform.tf` in the
infrastructure repository, read from remote `main` on 2026-08-07), which runs
that assertion at startup and **fails closed**, so deploying this image to
staging before applying 0029 means the app does not start. Read that as the
ordering rule for any migration, not a property of this one.

**Selector: its own, `LEAF_SESSION_ANNEX_STORE`, not `LEAF_SESSIONS_STORE`.**
Both were arguable, and reusing the sessions selector has a real virtue: it
makes the harmful combination unrepresentable in one stroke.

*A retraction first, because this record originally led with it.* An earlier
draft argued that reuse would retroactively fail `assert_schema_current()` on
staging's current mode while a separate selector would not. **That is false**,
for the reason stated immediately above: selector-scoped requirements cover
columns and catalog contracts only, and the migration check is selector-blind.
Review round 1 caught it. The correct consequence is the deploy-ordering rule,
which applies either way.

Three reasons survive.

1. *The two authorities have different prerequisites, and coupling would hide
   that.* Sessions has a backfill command and a parity command. This annex has
   NEITHER, and no reverse writer. On one selector there is no second decision
   to take, so flipping sessions would silently move an authority whose
   prerequisites are unmet. A separate selector forces that decision to be made
   explicitly, and refused where it is not ready — which is exactly the state
   production is in.
2. *Rollback would couple in the wrong direction.* The sessions rollback path
   recorded above is `postgres` → `shadow` → `legacy`, and every mode but
   `postgres` reads SQLite. This annex cannot follow that path at all: nothing
   writes PostgreSQL back to SQLite. Shared, a sessions rollback would drag the
   annex into an unreadable state with no separate decision point.
3. *A selector cannot have two owners.* State this the right way round: the
   inventory contract enforces one AUTHORITY per selector, not one selector per
   authority. Seventeen authorities own nineteen selectors, because two
   authorities each own more than one. Sharing `LEAF_SESSIONS_STORE` would give
   a single selector two owning authorities, which the contract rejects.

**The coupling is declared and ENFORCED, not left to the selector's shape.**
`platform/authority-inventory.json` gains a `selector_dependencies` entry —
`LEAF_SESSIONS_STORE=postgres` requires `LEAF_SESSION_ANNEX_STORE=postgres` —
using the same mechanism `LEAF_UPLOAD_STORE` → `LEAF_DRAWING_STORE` already
uses. `server/platform_link.validate_session_annex_authority` refuses to start an
app in that combination, so this is a startup failure rather than a document.
Note the requirement is `postgres` EXACTLY: under `dual_write` and both shadow
modes the annex still READS SQLite, so those modes do not fix the ephemerality.

**What this changes about step 5.** The staging flip must now set
`LEAF_SESSION_ANNEX_STORE=postgres` in the SAME task-definition change that sets
`LEAF_SESSIONS_STORE=postgres`. An app that gets one without the other does not
start. Apply 0029 before either. Step 3's "resolve every `unknown`" requirement
also now covers the new `session_annex` authority, whose selection is recorded
`unknown` in both environments because the selector is introduced by this change
and no deployed image bakes it yet; record `measured_no_override` against a real
revision and image digest once one does, not from this document's intent.

**What is deliberately NOT built, and what it blocks.**

- *No backfill.* `scripts/reconcile_sessions_authority.py` covers three table
  pairs and neither annex table is among them. Selecting a non-`legacy` mode
  starts from an empty target.
  **Task-local does not mean empty, and the difference is a real user-visible
  loss.** The RUNNING staging task can hold up to one task-lifetime of
  checkpoints and non-default policies, so flipping the annex straight to
  `postgres` discards those rows while the mirrored session itself survives —
  producing precisely the mismatch this section exists to remove, once, at
  cutover. That is inside the discard class staging already accepted for
  sessions, and it is bounded by one task lifetime rather than by history, but
  it is not nothing. Do not read "no accumulated history across replacements" as
  "no rows to lose".
  **Production cannot skip this at all**: cutting it over without writing a
  backfill would silently drop every existing restore point and every
  non-default policy, accumulated on durable EFS.
- *No reverse writer, so rollback is undesigned rather than merely awkward.*
  There is no PostgreSQL-to-SQLite direction anywhere in this repository, in
  either the sessions lane or this one. Once `postgres` is the annex authority,
  leaving it makes every row written under it unreadable, permanently. On
  staging that sits inside the discard class already accepted above and degrades
  safely (empty checkpoint lists, `DEFAULT_POLICY = "confirm_all"`). On
  production it is a blocker: the rollback would revert users to whatever the
  EFS file held at cutover while newer rows survive unread in a table nothing
  consults.

So the supported end state today is staging on `postgres` and **production on
`legacy`**. Production needs the backfill and the reverse writer first, and the
inventory record says so in both the `backfill` and `rollback_mode` fields
rather than leaving a future reader to infer it.

**No foreign key to `app_sessions`, on purpose.** `app_session_events` has one;
these do not. With independent selectors, `annex=postgres` while
`sessions=legacy` is a representable configuration, and under an FK every
checkpoint write in it would fail at INSERT time as a 500. The declared
dependency covers the harmful direction at startup instead, and an orphaned
annex row reads as an empty checkpoint list and a default policy — both
harmless.

**Unverified.** Nothing here was run against a live PostgreSQL. The tests cover
dispatch, the emitted SQL, and the enforcement, against a fake in place of
`platform.db`; 0029 itself has been applied nowhere. Treat the PostgreSQL paths
as reviewed code, not as exercised code, until a real apply and a real request
say otherwise.

## Which execution path, and what happens to SESSIONS_DB (decided 2026-08-07)

The section above establishes that the drained-color-first ordering cannot be
executed. It stops there, at "the executable shape is: flip the LIVE color and
accept a bounded mixed-mode rollout", and it explicitly leaves the other color
unsettled. This section closes both questions.

**It also retracts one word from the sentence it just quoted.** That section, and
an earlier draft of this one, called the rollout window "bounded". It is not, and
the measured subsection below shows why. Read every earlier "bounded mixed-mode
rollout" in this document as "single mixed-mode rollout": what option A buys is
that there is ONE such window rather than two, not that the window has a known
maximum.

### The decision: option A

Four paths were considered.

- **A. One configuration deploy against the LIVE color**, accepting ONE
  mixed-mode rollout window. The idle color picks the selector up later. The
  window is single, not bounded; see the measured section below, which retracts
  the word "bounded" wherever this document used it of that window.
- **B. Build a control that can flip a DRAINED color**, then do both colors with
  no overlap at all. Needs a workflow change, so it is not available today.
- **C. Two-cycle.** Flip the live color now, then flip the other color the next
  time IT is live. Never touches a drained service and needs no new control, but
  leaves the fleet mixed-mode between cycles.
- **D. Do not flip.** Leave the defect recorded.

**Chosen: A.**

Why the others lose.

- **C loses because A already contains C's second flip, as a conditional instead
  of a commitment.** The section above already prescribes the corrective step:
  confirm the idle color's registered revision carries
  `LEAF_SESSIONS_STORE=postgres` after its next warm, and flip it explicitly if
  it does not. So the second flip is not the difference between the two paths.
  The difference is that C schedules it unconditionally, while A performs it only
  when the inheritance check fails. Since the workflow permits an alternate ACTIVE
  configuration baseline, inheritance is genuinely not guaranteed, and that
  corrective flip may well be needed. **When it is needed it is necessary
  remediation, not waste.** What C buys with its unconditional second flip is
  nothing, because its stated appeal is avoiding a mixed-mode fleet and there is
  no reachable mixed-mode fleet to avoid: the idle color sits at desired 0
  between cycles, and at 2026-08-07T06:57Z its target group was EMPTY, with zero
  registered targets. C therefore pays a second rollout window in the case where
  A pays none, and matches A in the case where the check fails.
- **B loses on availability, not on merit.** It is the only zero-overlap answer
  and it is the right long-term shape, but it needs a reviewed workflow change.
  It is recorded as the follow-up, not the path.
- **D loses because the defect is live and recurring.** It fired ten times in
  fourteen minutes on 2026-08-06 and its trigger is ordinary traffic after any
  task replacement, which blue/green performs routinely.

Independent support, with its limits stated: the same fork was put to Codex,
Kimi and DeepSeek on one shared prompt on 2026-08-07. Codex and DeepSeek both
returned A independently; Kimi timed out and cast no vote; neither answering
lane argued for B, C or D. Weigh that at what it is worth. Both lanes reached A
partly by calling C's second flip redundant, which is the reasoning corrected
above, and both were wrong about the overlap window in the ways corrected below.
**Agreement on the choice is not agreement on the argument**, and here the
choice survived while much of the shared reasoning did not.

### Option A is decided but NOT dispatchable today, and this is a hard blocker

Found while re-reviewing this section after `0029` landed. It is not a caveat on
the decision, it is a prerequisite that lives in the OTHER repository, and
without it a release-complete go would fail.

**Half of this is already recorded above and is deliberate.** The annex section
documents the coupling and its enforcement: a `selector_dependencies` entry in
`platform/authority-inventory.json`, plus
`server/platform_link.validate_session_annex_authority` refusing to start an app
in the wrong combination, with the requirement being `postgres` EXACTLY. That is
a good design and this section does not argue with it.

What follows is the part that section does not cover, because it lives in
another repository.

**The coupling is reachable on the ordinary serving path, and it is armed by the
very change being made.** `server/app.py` calls
`platform_link.validate_postgres_startup()`, which calls
`validate_session_annex_authority()`, and
`postgres_startup_required()` becomes true BECAUSE `LEAF_SESSIONS_STORE=postgres`
was just set. So a delta carrying only the sessions selector produces a task that
refuses to serve, and it is the flip itself that arms the refusal.

**But the deploy workflow will not carry the second selector.** In
`deploy-leaf-platform-staging.yml`, `allowed_delta_pair()` is a closed
allowlist, and its entire contents are `LEAF_JOBS_STORE=postgres` and the five
`LEAF_SESSIONS_STORE` values. `LEAF_SESSION_ANNEX_STORE` does not appear
anywhere in that workflow: verified against terraform `main` at `4e86975`, zero
occurrences in the file. A delta naming it exits with
`"configuration_delta pair is not on the reviewed migration-variable allowlist"`.

So both dispatches available today fail, in opposite ways:

| dispatch | outcome |
|---|---|
| `LEAF_SESSIONS_STORE=postgres` alone | workflow accepts, new task raises at startup |
| both selectors together | workflow refuses the delta before deploying |

**The prerequisite** is therefore a reviewed change in
`LEAF-Solar-Design/leaf-automation-aws-terraform` adding
`LEAF_SESSION_ANNEX_STORE=postgres` to `allowed_delta_pair()`. Until it merges,
option A cannot be executed, and neither can B or C, because all three need the
same delta. This blocker is independent of the operator's release hold: lifting
the hold does not make the dispatch work.

**Stated plainly, because it is the kind of thing that gets lost between
repositories:** `0029` made the staging flip require two selectors, and the
deploy workflow can carry only one, so `0029` left the staging flip
undispatchable. Nothing was done wrong. The enforcement is correct and the
allowlist is correctly closed; they were simply written in different repositories
and no gate spans both. This document is the only place that currently says so.

The decision itself is unaffected. A is still the path; it now has a named,
locatable prerequisite instead of an assumed-clear runway.

### The overlap window, measured rather than argued

The two lanes that answered disagreed on the size of the window, so it was
measured directly against the live target group on 2026-08-07 rather than taken
from either. All four numbers below are from `describe-target-groups`,
`describe-target-group-attributes` and `describe-services` on the LIVE color:

- health check interval **10s**, timeout 5s, healthy threshold **2**,
- `deregistration_delay.timeout_seconds` = **30s**,
- `minimumHealthyPercent=100`, `maximumPercent=200`, strategy `ROLLING`.

One lane read a 30s interval and a 300s deregistration delay from the Terraform
module defaults and concluded the window was seconds. **The live values are 10s
and 30s, so that reading was wrong on both numbers**, and the module default is
not what is deployed. Read the live target group, not the module.

**Do not multiply the interval by the healthy threshold to predict how long the
new task takes to start serving.** That threshold does not apply to a newly
registered target. AWS is explicit: "After your target is registered, it must
pass one health check to be considered healthy", and `HealthyThresholdCount` is
defined as the consecutive successes required "before considering an UNHEALTHY
target healthy". So a first registration costs roughly one interval plus
registration time, not two intervals.

**The exposure window is NOT bounded by the 30s drain, and an earlier draft of
this section said it was.** That draft argued a client keep-alive connection
could keep delivering new requests to the old task during the drain. It cannot,
and the error was reading the client connection as if it terminated on the task.
The client's persistent connection is to the load balancer, not to the target.
AWS: "The load balancer stops routing requests to a target as soon as you
deregister it", and connection draining is only the balancer waiting "until
in-flight requests have completed". So the 30s delay covers work already in
progress, and no new request reaches a draining target.

The real window is the interval **between the new task becoming healthy and
being routed to, and ECS beginning deregistration of the old one**. In that
interval both targets are registered and healthy, so the load balancer spreads
traffic across both, and a request can land on either mode. **AWS documents no
maximum for it.** It is ECS scheduler behavior, not a configured value, so no
measurement of this target group can bound it and none of the numbers above do.

That leaves the honest position: the pieces are measured, the window itself is
not bounded by anything we control or can cite. Treat it as short but open, plan
to watch it rather than to wait it out, and do not put a number on it in a
runbook. It is also not "a few seconds of scheduler latency", because that
phrasing claims the same unbounded quantity is small.

Note also that the live service runs with `deploymentCircuitBreaker` **disabled**
and `rollback: false`. ECS will not undo a bad rollout on its own; the workflow's
own rollback script is the only automatic path, and per the section above a
rollback off `postgres` is an incident path rather than a safe undo.

### The SESSIONS_DB drift is deliberate until the flip, and the flip is what closes it

Staging omits `SESSIONS_DB`, so it falls back to the task-local
`server/sessions.db` that dies with the task. Production sets
`SESSIONS_DB=/data/state/sessions.db` on EFS. That difference is the mechanism
behind the mismatch, and the obvious repair is the wrong one.

**Do not set `SESSIONS_DB` on staging before the flip.** A fresh durable path is
an EMPTY SQLite database facing a POPULATED PostgreSQL, and
`scripts/reconcile_sessions_authority.py` runs SQLite to PostgreSQL only,
insert-only, with the source opened `mode=ro`. There is no reverse direction. So
that change converts a mismatch that self-clears at the next task replacement
into one that never clears.

**Adding `SESSIONS_DB` to `deploy/required-config.app.json` is not the fix
either, though not for the reason an earlier draft gave.** That draft claimed it
would make the harmful state mandatory. It would not, and the manifest is weaker
than that: it is a flat list of NAMES, and the gate only asserts membership. See
`test_required_config_manifests_fail_closed_for_postgres_authority`, whose whole
check is `}.issubset(app_environment)`. It never inspects a value, so it cannot
tell a durable EFS path from the task-local default, and an explicit task-local
path would satisfy it while changing nothing.

That is precisely why it does not help. The requirement here is conditional,
"durable if and only if the selector still reads legacy SQLite", and a flat
name list cannot express a condition. Adding the name would buy no safety and
would invite the next reader to satisfy it with a durable path while the
selector is still `dual_write_shadow`, which is the forbidden ordering above.
Leave it out until the condition disappears.

What actually closes the drift is the flip itself. In `postgres` mode
`get_or_create_session`, `get_session` and `append_event` all return at
`if mode == "postgres"` before touching legacy SQLite or `_shadow_equal`, so the
`app_sessions` path stops reading `SESSIONS_DB` entirely.

**Two consumers survive, and since 0029 they answer to a SECOND selector.** An
earlier draft of this subsection said `server/checkpoints.py` and
`server/session_policy.py` consult no store mode and never shadow-compare, so
that after the flip nothing could compare the two stores any more. **The annex
section above landed while this one was being written and made that false.**
Both modules still resolve
`Path(os.environ.get("SESSIONS_DB", str(SERVER_DIR / "sessions.db")))`, but they
now dispatch on `LEAF_SESSION_ANNEX_STORE`, whose vocabulary is its own
(`session_annex.SHADOW_READ_MODES = {"shadow", "dual_write_shadow"}`), and in
those modes `checkpoints.py` calls
`session_annex.shadow_equal("checkpoint", legacy, postgres)`, which raises on
inequality exactly as `session_store` does.

So the ordering rule needs BOTH selectors, not one:

- `LEAF_SESSION_ANNEX_STORE` defaults to `legacy` (`os.environ.get(SELECTOR,
  "legacy")`), and under `legacy` the annex is SQLite-only with no comparison.
- It is **UNSET on both live staging revisions**, verified in the same
  2026-08-07T06:57Z read as the table below, so today the annex genuinely cannot
  mismatch and the conclusion still holds.
- But it is now a lever someone can pull independently of the flip. If the annex
  selector is moved to `shadow` or `dual_write_shadow` while `SESSIONS_DB` points
  at a fresh durable path, that reproduces the original hazard on the annex
  tables, for the same reason and with the same permanence.

**Restated: setting `SESSIONS_DB` needs `LEAF_SESSIONS_STORE=postgres`, with
`LEAF_SESSION_ANNEX_STORE` at `legacy` or `postgres`.** Do NOT compress that
into "no selector is in a shadow-reading mode": an earlier draft did, and that
phrasing admits plain `dual_write`, which compares. The exact rule, and the
reason it is asymmetric between the two selectors, is derived below under "not
comparing is not the same as being inert". Check both before touching the
variable, and re-derive the list if a
third consumer appears, because the earlier draft was wrong within hours purely
because a second one did.

**The replacement argument is weaker than the one it replaced, and that is worth
saying rather than leaving a reader to infer they are equivalent.** The old
reason was a property of the CODE: no shadow compare existed on those two
modules, so only a merged, reviewed change could falsify it. The new reason is a
property of the ENVIRONMENT: `LEAF_SESSION_ANNEX_STORE` happens to be unset.

Be accurate about how much weaker that is, because an earlier draft of this
paragraph overstated it as "anyone can falsify that by setting a variable, with
no pull request and no review". Not on this deployment. A task-definition
override takes a reviewed terraform change or a `configuration_delta`, and the
allowlist described above does not even carry the annex selector today. That
draft also wrote the list as "means either", which is not exhaustive:
`deploy/Dockerfile.app` bakes `LEAF_SESSION_ANNEX_STORE=legacy` into the image,
so a reviewed application change moves it too, and the effective value is
whichever of image default and task-definition override wins.

The real difference is narrower than "no review" and still worth having. The old
reason was fully visible to a reader of this repository. The new one is only
half visible here: the baked default is in this tree, any override is not. So a
reader here cannot confirm the EFFECTIVE value and must go and look at the live
environment, which is what the unset observation above actually is.

So state the condition rather than the observation. A first attempt at that
condition said "outside `session_annex.SHADOW_READ_MODES` and
`session_store._SHADOW_READ_MODES`", **and it was wrong, because it let
`dual_write` through.** Plain `dual_write` is not a shadow-read mode and still
compares: `session_store.get_or_create_session` runs
`_shadow_equal("session identity", legacy["session_id"], postgres["session_id"])`
for every mode in `_DUAL_WRITE_MODES = {"dual_write", "dual_write_shadow"}`,
with only the whole-row compare gated behind the shadow set. `checkpoints.py`
has the identical shape around
`session_annex.shadow_equal("checkpoint identity", ...)`. A review probe run at
`LEAF_SESSIONS_STORE=dual_write` with a fresh legacy identity against an existing
PostgreSQL identity raised `RuntimeError: session identity shadow mismatch`,
which is precisely the failure the condition claimed to exclude.

The second attempt then failed the opposite way. It read "safe only while every
selector that resolves it is `legacy` or `postgres`", which is right about
COMPARISON and wrong about SAFETY, because it admits
`LEAF_SESSIONS_STORE=legacy`. **Not comparing is not the same as being inert**,
and blurring those two is what made both attempts wrong.

There are two independent hazards, and `SESSIONS_DB` is only safe when neither
applies:

1. **The comparison hazard.** A selector in `dual_write`, `dual_write_shadow` or
   `shadow` compares the two stores and raises on disagreement. Only `legacy`
   and `postgres` never compare: `legacy` never consults PostgreSQL at all, and
   `postgres` short-circuits before the legacy read.
2. **The authority hazard, which the earlier drafts missed entirely.** Any mode
   that still READS the SQLite file makes `SESSIONS_DB` a live authority
   pointer, so repointing it swaps the authority and abandons whatever the old
   path held. That covers `legacy`, `dual_write`, `dual_write_shadow` and
   `shadow`. Only `postgres` makes the variable inert for its own tables.

So the rule is ASYMMETRIC between the two selectors, exactly as the earlier
paragraph in this section already said:

- `LEAF_SESSIONS_STORE` must be `postgres`. Under `legacy` there is no mismatch
  to create, and repointing still moves the live session authority to an empty
  file, which is a different failure and not an acceptable one.
- `LEAF_SESSION_ANNEX_STORE` may be `legacy` or `postgres` for the comparison
  rule, but under `legacy` the annex tables are still read from the file, so
  repointing abandons them. On staging that adds **no loss beyond what task
  replacement already causes**, which is the accurate phrasing and not "costs
  nothing": the annex section above is explicit that a running task can hold up
  to one task-lifetime of checkpoints and non-default policies, that discarding
  them is a real user-visible loss, and that it "is not nothing". Bounded by one
  task lifetime rather than by history, and inside the discard class staging
  already accepted. On production, where the file is durable EFS, it would not
  be bounded and would not be acceptable.

And the startup gate above then narrows every EXECUTABLE state to both selectors
at `postgres`, which is the only combination where `SESSIONS_DB` is inert for
sessions and annex alike. Prefer naming the behaviour to naming a mode set: the
first attempt reasoned from `_SHADOW_READ_MODES` and never checked what that set
excluded.

Re-read the live task definition for BOTH selectors immediately before touching
`SESSIONS_DB`, exactly as this document already says to re-read the desired
counts, and for the same reason: a recorded environment fact is evidence about
the past, not a guarantee about the present.

More generally, and this is the reusable half: **a claim about what COVERS
something decays faster than a claim about what something IS.** "These tables
live in SQLite" survived months here. "And nothing compares them" died the day a
peer shipped the comparison. Both of the claims corrected in this section were
coverage claims. When you write one, write its expiry condition next to it.

So there is one forbidden ordering and two acceptable terminal states:

- **Forbidden:** set `SESSIONS_DB` while `LEAF_SESSIONS_STORE` is anything but
  `postgres`. In `dual_write`, `dual_write_shadow` or `shadow` it is a permanent
  mismatch; in `legacy` it is not a mismatch at all but it moves the live
  session authority to an empty file, which is worse rather than better. Note
  the forbidden set is NOT "the shadow-reading modes": plain `dual_write`
  compares too.
- **Acceptable, and now the ONLY terminal state:** both selectors reach
  `postgres` together. **This is no longer a proposal**:
  `platform/migrations/0029_session_annex.sql` and the dispatch in both modules
  landed in the section above, and the startup gate documented earlier makes it
  the only legal combination once the sessions selector moves. At that point the
  annex tables are on PostgreSQL and **no serving operation touches the SQLite
  file**. Say it that way and not "nothing resolves `SESSIONS_DB`": all three
  modules still RESOLVE it at import, unconditionally and regardless of mode
  (`session_store.py:114`, `checkpoints.py:25`, `session_policy.py:48`). The
  variable keeps a resolved value; what stops is any read or write through it.
  Note the annex section's own caveat that those PostgreSQL paths are reviewed
  code, not exercised code.

**An earlier draft listed a third state, "after the flip, set `SESSIONS_DB` to
durable storage so those two tables stop dying with every task replacement".
Delete that idea; it is unreachable.** It assumed a world where
`LEAF_SESSIONS_STORE=postgres` coexists with an annex still reading SQLite, and
`validate_session_annex_authority()` rejects exactly that combination at
startup. In the only state the gate permits, the annex is already on PostgreSQL
and a durable `SESSIONS_DB` would preserve nothing.

**And "no consumers at all" was too strong even then.** A third shipped reader
survives: `scripts/reconcile_sessions_authority.py`, whose
`default_sqlite_path()` returns `session_store.DB_PATH` precisely so the
reconciler resolves the legacy file the same way the app does, and which
`deploy/Dockerfile.app` COPYs to `/app/scripts/`. State it as **no serving-path
consumers**, not none.

It is not a request-path hazard, because it is operator-run and nothing about it
can raise on a user request. **But do not describe it as read-only, which an
earlier draft of this paragraph did.** That holds for `parity` mode only. In
`backfill`, `reconcile()` calls `_ensure_source_schema()`, which yields
`store._db()`, the app's own WRITABLE connection, to run the lazy schema
bootstrap; its docstring says outright that this "runs against the SAME database
the live app is serving from, so a write lock held by a concurrent request is
expected and recoverable". The `mode=ro` URI cited earlier governs the snapshot
read, not the whole command. The direction claim is unaffected: it still never
writes PostgreSQL back to SQLite, and it still has no UPDATE or DELETE path.

The rollback requirement in the section above is the same rule read backwards
and does not conflict with this: every mode on the return path except `postgres`
reads legacy SQLite, so any rollback off `postgres` must set `SESSIONS_DB`
durable in the same transaction, before or with the selector change, never after.

### Live state this decision was made against

Read read-only from account `807034087062`, `us-east-1`, 2026-08-07T06:57Z.
These are observations, not artifacts committed here, so re-derive rather than
cite them. The exact reads were `aws ecs describe-services --cluster
leaf-automation-staging --services leaf-platform-app leaf-platform-app-alt`,
then `aws elbv2 describe-target-health` on each service's
`loadBalancers[0].targetGroupArn`, then `aws ecs describe-task-definition` on
each reported revision. The timing values above came from
`aws elbv2 describe-target-groups` and
`aws elbv2 describe-target-group-attributes` on the LIVE color's target group
`leaf-stg-platform-app-alt/5f41a0a56acd6ab6`, and the deployment percentages
from `describe-services … --query 'services[0].deploymentConfiguration'`.

| service | desired/running | task definition | target group |
|---|---|---|---|
| `leaf-platform-app-alt` | 1/1 | `leaf-platform-app-alt:31` | 1 healthy target |
| `leaf-platform-app` | 0/0 | `leaf-platform-app:552` | EMPTY |

Both revisions still carry `LEAF_SESSIONS_STORE=dual_write_shadow` with
`SESSIONS_DB` unset, so the defect is unfixed as of that read.

**The colors have inverted since the earlier passages of this document were
written**, which is the fourth distinct topology observed inside one day. The
warning above bears repeating with teeth: re-read the live desired counts
immediately before acting, including against this table.

### What is NOT authorized here

No deploy was dispatched. The staging flip is not operator-authorized, and this
section decides only which path to take once it is.

**Do not copy an earlier draft's dispatch line.** It read
`configuration_delta=LEAF_SESSIONS_STORE=postgres`, which is the single-selector
delta that produces a task refusing to start. The correct delta names both
selectors, and cannot be sent until the terraform allowlist accepts the second
one. In order:

1. Land the allowlist change in `leaf-automation-aws-terraform` adding
   `LEAF_SESSION_ANNEX_STORE=postgres` to `allowed_delta_pair()`.
2. Wait for operator release-complete.
3. Re-read the live desired counts and pick the color that is live AT THAT
   MOMENT, which is not necessarily the one in the table above.
4. Dispatch with `target_color=live` and a delta naming BOTH
   `LEAF_SESSIONS_STORE=postgres` and `LEAF_SESSION_ANNEX_STORE=postgres`.
5. Verify the idle color's next warm on BOTH selectors, not just the sessions
   one. The inheritance check described earlier is incomplete as written for the
   same reason the dispatch was.
