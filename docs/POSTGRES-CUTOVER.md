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
python scripts/reconcile_customization_authority.py --mode backfill \
  --sqlite /data/state/customization.db
python scripts/reconcile_customization_authority.py --mode parity \
  --sqlite /data/state/customization.db
```

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

Mitigation: flip the DRAINED color first, verify it through the color-addressed
header rules, then drain the live color and flip it, so the two modes never
serve overlapping traffic. That is the operational form of the "quiesce legacy
writes" prerequisite above. **Re-read the live desired counts immediately before
acting rather than trusting any recorded value, including this document.** Which
color is drained flips with every blue/green cycle, and the counts in
`platform/authority-inventory.json` disagreed with a live read on 2026-08-06.

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
this stops being invisible and starts looking like a regression to a user. Give
those two tables a PostgreSQL home, or classify them as disposable staging state
in writing.
