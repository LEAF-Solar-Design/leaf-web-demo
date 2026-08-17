# SQLite-to-Postgres store migration design (jobs, callback ledger, sessions)

**Status: FROZEN DESIGN — rev 1, 2026-08-17. Design-only chip; no store code ships
from this document. Implementation starts only on operator authorization.**

Operator-sanctioned design spike for the decision previously parked as a
correctness lever (staging blue/green design doc
`leaf-automation-aws-terraform/docs/staging-bluegreen-cutover-design.md` §9:
"The real fix is the jobs/callback/sessions → Postgres store migration
(explicitly a correctness lever, out of this program's scope)").

## 1. Problem

Every staging blue/green deploy carries a SQLite dual-writer exposure window:
two app processes holding `JOBS_DB=/data/state/jobs.db` (SQLite on shared EFS,
`LEAF_JOBS_STORE=legacy`) open concurrently. Measured baseline ~70s both-alive
per deploy; the weighted-TG blue/green design stretches it to ~155-170s at
`hold_seconds=0` and ~335-350s at the proposed 180s hold (cutover design §9.1).
Operator decision D2 accepted the window; the `hold_seconds` knob, the
drawing-mutation fence choreography, and the pre-warmed-cutover chip's
idle-warm TTL all exist to manage this one constraint. Production
`leaf-platform` runs stop-first deploys partly BECAUSE of the single-writer
SQLite constraint, with a recorded 9m49s stop-first outage window as the
motivating incident.

Moving the jobs, callback-replay, and sessions stores to Postgres deletes the
constraint class: Postgres is multi-writer by design, so two app tasks alive
concurrently stops being an exposure at all.

## 2. Where the code already is (verified 2026-08-17)

This is much closer to a wiring exercise than a build. Every store this design
moves already has a Postgres implementation behind a fail-closed selector,
merged and tested in-repo:

| Store | Selector (default `legacy`) | Postgres impl | Migration | Tests |
|---|---|---|---|---|
| Async jobs | `LEAF_JOBS_STORE` (`legacy`\|`postgres`) | `server/job_pg_store.py` (`PostgresJobStore`, wired in `server/jobs.py`) | `platform/migrations/0011_jobs_callbacks.sql` | `test_jobs_callbacks_postgres.py` |
| Callback replay ledger | `LEAF_CALLBACK_REPLAY_STORE` (`legacy`\|`postgres`) | `server/da/callbacks.py` pg path | `0011_jobs_callbacks.sql` | `test_jobs_callbacks_postgres.py` |
| App sessions / events / approvals | `LEAF_SESSIONS_STORE` (`legacy`\|`dual_write`\|`dual_write_shadow`\|`shadow`\|`postgres`) | `server/session_store.py` pg backend | `0012_sessions.sql`, `0019_sessions_model.sql` | `test_session_store_postgres.py` |

Selector typos fail closed (`RuntimeError`, `server/jobs.py:106`,
`server/session_store.py:635`, `server/da/callbacks.py:42`). Startup fails
closed when any Postgres authority is selected without a ready database:
`server/platform_link.py` `validate_postgres_startup()` requires
`DATABASE_URL` and runs `db.assert_schema_current()`; `server/jobs.py`
`validate_store_startup()` calls `PostgresJobStore.ensure_ready()`.

Adjacent stores already share the same pattern and target (not required for
the exposure deletion, listed for completeness): agent state
(`LEAF_AGENT_STORE`, 0013), broker tenants/ledger (`LEAF_BROKER_STORE`, 0014),
guest caps (`LEAF_GUEST_CAP_STORE`, 0015), drawing upload authority
(`LEAF_DRAWING_STORE`/`LEAF_UPLOAD_STORE`, 0016), harness sessions (0017).

Prior art outside this repo:

- **W7a durable-stores lane** (utility-estimation PR #67, merged 2026-07-30):
  PostgresBuildStore + billing stores; single-transaction row-locked CAS,
  oldest-queued `SKIP LOCKED` claims. (Memory note `w7a-durable-stores-merged`.)
- **W7b durable-ledgers lane** (utility-estimation PR #68, merged 2026-07-30):
  froze the **pooling-safe store invariants** this design adopts wholesale —
  no session-scoped advisory locks, no `FOR UPDATE` held across statements, no
  session GUCs; `SET LOCAL` never `SET`; loud skip banners in pg tests; bounded
  poll loops. (Memory note `w7b-durable-ledgers-merged`.)
- **Harness Postgres session store**: `harness/src/ports/impl/pgSessionStore.ts`
  + `harness/POSTGRES-SHARED-STATE.md`, gate-tested 2026-08-05
  (`pgSessionStore.contract.test.ts`).
- **`docs/POSTGRES-CUTOVER.md`**: the standing production-gate list (backfill
  one authority at a time, dual-write failure proof, canary tenant, rollback
  drill). This design is the concrete instantiation of its "Async non-canonical
  jobs / App sessions" rows.

## 3. Which stores move, which stay

**Move to Postgres (this design):**

1. Async jobs (`jobs.db` on EFS) — the store that creates the §9 exposure.
2. Callback replay ledger (nonce/replay state behind `LEAF_CALLBACK_REPLAY_STORE`).
3. App sessions, events, turn fences, approval consumption (`SESSIONS_DB`).

**Explicitly stay file-based (out of scope, with reasons):**

- Harness conversation files under `LEAF_SESSIONS_DIR` — append-only
  per-conversation artifacts, no cross-writer contention; harness *shared
  state* already has its own pg store when needed.
- Claude grants under `LEAF_GRANTS_DIR` — secret material; POSTGRES-CUTOVER.md
  already rules "use a vault rather than ordinary tables".
- Tenant repositories and drawing artifacts (EFS/S3) — bulk binary content;
  Postgres holds metadata only (0010/0016 already do this for canonical
  drawings).
- Guest upload quota *files* — the pg selector (0015) exists; flip is optional
  and independent, not part of this program's critical path.

The drawing-write single-writer constraint on shared EFS drawing files is NOT
deleted by this migration; the mutation fence remains the drawing-lane guard.
What this migration deletes is the *jobs/sessions* dual-writer exposure that
the deploy choreography currently has to account for (§8).

## 4. Target database

**Decision: reuse the existing staging Aurora Serverless v2 cluster
(`leaf-platform-staging`), one database, one schema, via the existing RDS
Proxy. No new cluster.**

Facts (from `leaf-automation-aws-terraform/terraform/environments/staging/us-east-1/leaf_platform_db.tf`):

- Aurora PostgreSQL 16 serverless v2 (`min_acu`/`max_acu` vars), KMS-encrypted,
  deletion-protected, TLS forced (`rds.force_ssl=1`).
- RDS Proxy `leaf-platform-staging` (TLS required, SECRETS auth, scoped
  `leaf_platform_app` role) fronts the cluster;
  `DATABASE_URL` secret (`leaf_platform_database_url`) already points at the
  proxy endpoint.
- **The SG two-group trap, stated precisely**: the cluster SG
  (`leaf-platform-staging-database`) admits exactly two security groups — the
  RDS Proxy SG and the one-shot bootstrap SG. Nothing else can reach 5432 on
  the cluster, ever. The trap bites anyone who tries to wire a new consumer
  straight at the cluster endpoint. The correct path already exists: the
  *proxy* SG admits all four service SGs (`app`, `broker`, `harness`,
  `worker` — `aws_vpc_security_group_ingress_rule.leaf_platform_db_proxy_from_services`).
  Rule: **new consumers join via the proxy SG for_each set; never add ingress
  to the cluster SG.** Migration/one-shot tasks reuse the bootstrap SG.
- The migration ledger container already applies 0001-0019 and calls
  `db.assert_schema_current()`; `platform/db.py` disables prepared statements
  (`prepare_threshold=None`) so pooled endpoints are safe.

Why not a second cluster: the jobs/sessions write volume is small relative to
canonical authority traffic; serverless v2 absorbs it inside the existing ACU
band; a second cluster doubles secret/bootstrap/proxy/alarm surface and
re-opens the SG design; and the schema is already one migration chain
(0001-0019) asserted as a unit — splitting it would fork
`assert_schema_current()`.

**Production topology**: mirror staging exactly — one Aurora Serverless v2
cluster + RDS Proxy + scoped app role + bootstrap task, defined in
`terraform/environments/production/us-east-1`. Production already carries the
proxy-name-as-constant alarm idiom (PR #276), so the module shape ports
cleanly. Production cluster provisioning is Phase 4 and is the only net-new
infrastructure in this design.

## 5. Migration strategy: expand-contract, per-store ladder

The expand half is already merged (schemas 0011/0012/0019 exist additively;
`assert_schema_current()`'s `_REQUIRED_COLUMNS` contract in `platform/db.py`
extends additively per its own comment). The existing migration
ledger/`assert_schema_current` machinery is **extended, not bypassed**: the
new stores' tables ride the same numbered chain and the same startup
assertion; no second ledger is introduced.

Per-store cutover, in order of blast radius:

**5.1 Jobs (first).** Job rows are transient (queue + lease + terminal
receipt). No backfill is needed or wanted: the flip happens inside a deploy
where the fence is closed and in-flight jobs are drained (the same drain the
deploy already performs), so the pg store starts empty and legacy `jobs.db`
stays on EFS untouched as the rollback artifact. `LEAF_JOBS_STORE` is a binary
flag by design — a dual-write jobs store would have to dual-write *leases*,
which cannot be made atomic across engines and would reintroduce the very
ambiguity this migration deletes. Verification is the existing
`test_jobs_callbacks_postgres.py` suite plus one staged deploy on staging with
the flag flipped and the receipt's job-lane smoke green.

**5.2 Callback replay ledger (same flip).** The replay ledger is a nonce
store; its one invariant is "a nonce is consumed at most once *somewhere*".
Flipping it in the same fence-closed window as jobs keeps the invariant: no
callback is admitted during the window, so no nonce can be consumed in SQLite
after the pg store becomes authoritative. `LEAF_CALLBACK_REPLAY_STORE` flips
together with `LEAF_JOBS_STORE`; they share migration 0011 and a test suite.

**5.3 Sessions (the ladder store).** Sessions are long-lived and user-facing,
so this store gets the full expand-contract ladder that
`server/session_store.py` already implements:

`legacy` → `dual_write` (writes mirrored, reads SQLite) →
`dual_write_shadow` (reads compared, divergence logged) → verify window →
`postgres` (authority) → contract (SQLite path retired).

Two constraints the code itself documents and this design preserves:

- Turn-fence dual writes are valid **only during the single-task phase**,
  because SQLite and Postgres cannot share one atomic transaction
  (`server/session_store.py` `_store_mode()` docstring). Therefore the
  sessions ladder walks **before** blue/green is exercised with sessions in a
  dual mode; the ladder itself runs under today's single-task rolling deploy.
- `shadow` mode (compare reads, never write pg) exists as the zero-risk first
  probe and the post-rollback re-verify tool.

Shadow-read verification (dual_write_shadow) is the chosen verification
mechanism over a bulk backfill-and-fingerprint pass: sessions have short
half-lives, so a mirrored-write window of a few days converges the live set
naturally, and the shadow comparator gives per-read divergence evidence
instead of a one-shot count comparison. Historical sessions older than the
dual-write start are read-only tail data; they stay readable from the legacy
path until the contract step and are not migrated (operator-visible cutoff,
recorded in the flip receipt).

## 6. Transaction semantics parity

What legacy semantics actually rely on, and the Postgres equivalent:

| Legacy mechanism | What it guarantees | Postgres equivalent |
|---|---|---|
| SQLite single-writer + WAL | serialized multi-table writes per process | ordinary transactions; `READ COMMITTED` default, per-store escalation available (`platform/db.py` exposes isolation + `SerializationFailure`/`DeadlockDetected` retry) |
| Job claim under `_lock` (thread mutex) + lease columns | at-most-one worker owns a job | single-transaction claim: oldest-queued `SELECT ... FOR UPDATE SKIP LOCKED` + lease CAS (W7a idiom, already the `job_pg_store` shape) |
| Fence commit guard `flock(LOCK_EX)` on `<fence>.lock` — the **drain barrier** (deploy workflow "Close the drawing-write lane": taking LOCK_EX waits for every admitted in-flight commit to finish before the flip lands) | flip observes a quiesced store | `pg_advisory_xact_lock(hashtextextended(key, 0))`: writers take the lock shared-scope per transaction; the flip transaction taking the same lock exclusively blocks until in-flight writers commit, then flips — identical barrier shape, already the merged idiom in `broker_pg_store.py` (:271,:400,:467,:656) and `agent_pg_store.py` (:584) |
| `SESSIONS_DB` turn fence (begin/end turn rows) | one active turn per session | same rows under a serialized per-session transaction; `try_begin_turn` becomes an `INSERT ... ON CONFLICT` / row-CAS |

Hard rule, inherited from W7b's Neon-pooling finding: **transaction-scoped
locks only** (`pg_advisory_xact_lock`, never `pg_advisory_lock`), no session
GUCs, `SET LOCAL` only. This is exactly compatible with RDS Proxy: xact-scoped
state releases at commit, so proxy session pinning windows stay bounded to a
single transaction. The existing pg stores already comply;
`test_broker_migration_static.py` even asserts no advisory-lock DDL leaks into
migrations. The static contract test for new store code extends the same
assertions to the jobs/sessions paths.

The important honesty note: the *drawing-file* fence (EFS drawing mutations)
keeps its flock semantics — those files stay on EFS. The Postgres barrier
replaces the fence's role in guarding **store** consistency during deploys,
which is what lets the deploy choreography shrink (§8).

## 7. What each consumer changes

All changes are environment/terraform wiring; the read/write paths already
branch on the selectors.

| Consumer | Change |
|---|---|
| **app** | task definition gains `DATABASE_URL` (from `leaf_platform_database_url` secret — SG path already exists); selectors flip per phase. `validate_postgres_startup()` already fail-closes boot if the DB is unready. |
| **broker** | same `DATABASE_URL` wiring; `LEAF_CALLBACK_REPLAY_STORE=postgres` at the 5.2 flip. (`LEAF_BROKER_STORE` stays legacy; separate decision.) |
| **worker** | same wiring; its claim loop follows `LEAF_JOBS_STORE` through `server/jobs.py` — no worker-code change. Dockerfile/compose defaults currently pin `legacy` (`test_postgres_container_wiring.py`); the flip is a task-definition env change, not an image rebuild. |
| **harness** | no change for this program (harness conversations stay file-based). Its pg session store stays available behind `sessionStoreFactory.ts` for the later shared-state phase. |
| **deploy workflows** | after Phase 3: `hold_seconds` default → 0; §9.3 exposure measurement demoted from receipt requirement to informational; the mutation-fence close/reopen cycle no longer needs to bracket the store flip (drawing-lane closure remains for drawing mutations only). Production workflow gains a rolling (or blue/green) strategy in Phase 4. |

## 8. Rollback per phase

| Phase | Rollback | Data consequence |
|---|---|---|
| P0 wiring (secrets/SG/migrations applied, all selectors `legacy`) | revert terraform | none — expand-only, dead schema |
| P1 jobs+callbacks flip (staging) | flip both flags back to `legacy` in the task definition, redeploy (same fence-closed drain) | jobs created during the pg window are stranded in pg — transient queue rows, re-submittable; `jobs.db` was never deleted. Replay ledger: nonces consumed in pg are not visible to SQLite; the fence-closed flip-back makes the gap traffic-free, same argument as forward. |
| P2 sessions ladder (staging) | any rung reverts to the previous rung by env change; `dual_write*` rungs keep SQLite authoritative, so reverting them loses nothing. From `postgres` authority, rollback = `legacy` + accept sessions created during the pg window are frozen (visible via `shadow` for audit); or replay them by a one-shot pg→SQLite export if the window matters. Rollback deadline per POSTGRES-CUTOVER.md gate 7 is set before the authority flip. |
| P3 choreography simplification | revert the workflow PRs; knobs return | none — pure workflow change, store already pg |
| P4 production | production keeps stop-first until its own P1/P2 walk completes on the production cluster; rollback at each rung identical to staging. The stop-first deploy itself remains the fallback posture at any point. |
| Contract step (drop SQLite paths) | LAST, operator-gated, after ≥1 full deploy cycle per environment on pg authority with clean receipts | deletes the rollback artifact — hence last |

## 9. Payoff inventory

1. **`hold_seconds` → 0 with no exposure trade** — the knob currently trades
   instant-rollback coverage against dual-writer seconds linearly (§9.1); with
   pg stores the hold can sit at whatever rollback coverage wants, exposure
   term gone.
2. **Fence choreography simplification** — the deploy no longer closes the
   drawing lane to protect `jobs.db` schema/write integrity across task
   overlap; fence scope shrinks to actual drawing mutations.
3. **Production rolling deploys instead of stop-first** — the recorded 9m49s
   stop-first outage window class is deleted; production inherits staging's
   overlap-tolerant deploy.
4. **Pre-warmed cutover without exposure accounting** — the sibling
   pre-warmed-cutover design (frozen rev 2, `staging-prewarmed-cutover-design`
   chip) spends significant machinery bounding idle-warm dual-writer time
   (idle-warm TTL, exposure receipts); with pg stores a warm idle task is just
   a warm idle task.
5. **Schema-migration race deleted** — §9's named sub-risk (green migrates
   `jobs.db` at boot while blue writes) becomes the ordinary
   expand-contract-on-Postgres discipline the repo already runs for 0001-0019.
6. **One durability story** — backup/PITR/restore drills cover jobs and
   sessions the moment they land in the cluster; EFS SQLite files currently
   sit outside every restore drill.

## 10. Phasing and cost

| Phase | Content | Size | Ships alone? |
|---|---|---|---|
| **P0** | Terraform: `DATABASE_URL` secret into app/broker/worker TDs; migrations 0011-0019 applied via the existing protected migrate task; proxy-SG membership audit (should be a no-op). All selectors stay `legacy`. | S (1 tf PR + 1 migrate run) | **Yes** — expand-only, zero behavior change, and it is the P1/P2 prerequisite. This is the first shippable phase. |
| **P1** | Staging flip: `LEAF_JOBS_STORE=postgres` + `LEAF_CALLBACK_REPLAY_STORE=postgres` inside one fence-closed deploy; receipt captures the flip + empty-start inventory. | S (env change + one supervised deploy) | Yes — deletes the §9 jobs exposure on staging by itself. |
| **P2** | Staging sessions ladder: `dual_write` → `dual_write_shadow` (multi-day soak, divergence log gate) → `postgres`. Runs under single-task rolling deploys per the turn-fence constraint. | M (calendar time dominates; code exists) | Yes — completes staging store migration. |
| **P3** | Choreography harvest: `hold_seconds` default 0; exposure accounting demoted; fence scope narrowed; pre-warmed-cutover design simplification handed to that lane. | S-M (workflow PRs + doc updates) | Yes. |
| **P4** | Production: cluster+proxy terraform (staging module shape), bootstrap, P1+P2 walk on production, then stop-first → rolling deploy PR. | L (net-new infra + two supervised flips) | Yes — final payoff. |

Dependencies: P0 → P1 → (P2, P3 in either order; P3's fence narrowing that
touches sessions waits for P2) → P4. Nothing here blocks or is blocked by the
pre-warmed-cutover lane; P3 explicitly simplifies it.

## 11. Decision asks (for the operator, before implementation starts)

1. Ratify target = existing staging Aurora (no second cluster) — §4.
2. Ratify jobs/callbacks = drained-window flip, sessions = dual-write ladder — §5.
3. Ratify the P0-P4 phasing, P0 authorized first.
4. Production cluster sizing (ACU band, retention) at P4 time, per
   POSTGRES-CUTOVER.md operator-decision list.
