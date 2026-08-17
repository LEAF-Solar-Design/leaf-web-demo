# SQLite-to-Postgres store migration design (jobs, callback ledger, sessions)

**Status: FROZEN DESIGN — rev 3, 2026-08-17. Implementation starts only on
operator authorization; P1's code precondition is authorized and shipped (#645).**

> **Rev 3 supersedes P2 and §9.1. Read Appendix B before acting on §3, §5.2,
> §9.1, or §10-P2.** A source verification of rev 2's two remaining items found
> that `PostgresCustomizationStore` already exists and that `customization.db`
> is not a live dual-writer. P2 is a selector flip (S), not net-new store code
> (M), and the §9-class exposure set on staging is **already empty**. Rev 2's
> body is preserved verbatim below as the frozen record; Appendix B carries the
> corrections and the current routing.

**Rev 2 adversarial round, disclosed:** rev 1 went through a refute-first
adversarial pass (opus-critic lane; Codex usage-capped until Aug 19, so this
was a single-family Anthropic round). The pass and my own re-verification
overturned rev 1's central premise: rev 1 (like the blue/green cutover design
§9 it inherited from) described staging as `LEAF_JOBS_STORE=legacy`; the
staging terraform on main has run **jobs on Postgres since 2026-08-06**
(commit e38b4637, PR #489) and **sessions + session-annex on Postgres since
2026-08-11** (P4a posture, b979e7d8). Rev 1's local checkouts were stale;
every load-bearing claim below was re-verified against leaf-web-demo
`origin/main` (79a3e16) and leaf-automation-aws-terraform main. Caveat that
remains: claims about staging state read terraform **source**, not a live
`describe-task-definition`; staging deploys apply merged terraform, so drift
is unlikely but unproven here. Full findings ledger in Appendix A.

## 1. Problem, restated against current state

The historical problem was real and already bit: two app tasks sharing
`JOBS_DB=/data/state/jobs.db` (SQLite WAL on shared EFS) corrupted the
database during a task-replacement overlap — the terraform comment at the
jobs selector records the incident and is why #489 flipped staging jobs to
Postgres. The blue/green design §9's computed dual-writer exposure window,
the `hold_seconds` knob rationale, and operator decision D2 all describe the
**pre-#489** posture.

What actually remains of the SQLite/file single-writer constraint class,
verified on staging terraform today:

| Surviving store | Where | Written by | Exposure |
|---|---|---|---|
| Callback replay ledger (`LEAF_CALLBACK_REPLAY_STORE` unset → `legacy`) | `callback_consumed_nonces` table in `jobs.db` — broker-side default path since the broker does not set `JOBS_DB`, this lands in the broker container's local `server/jobs.db`, NOT on EFS | broker, per signed callback | worse than a dual-writer problem: replay protection does not span broker tasks at all today |
| Customization store (`LEAF_CUSTOMIZATION_STORE=sqlite`, `LEAF_CUSTOMIZATION_DB=/data/state/customization.db`) | SQLite on the shared EFS state access point, set on BOTH app colors (tf :1390/:1394, :1607/:1611) and the worker (:1725) | app + worker | this is the store that still carries the §9-class dual-writer exposure during color overlap |
| Platform repo + customize state dirs (`/data/state/platform-repo`, `/data/state/platform-customize`) | shared EFS, both colors | app | file-level shared mutable state; git-repo semantics, not SQLite |

So the honest scope of this design: **finish the store migration (callback
replay, customization), codify what already shipped, and carry the program to
production** — where `leaf-platform` still runs stop-first deploys (recorded
9m49s outage window) and none of the flips have happened.

## 2. Current authority map (staging, terraform source of truth)

Image defaults are `legacy` everywhere (`deploy/Dockerfile.app`,
`Dockerfile.broker`, asserted by `test_postgres_container_wiring.py`); the
staging task definitions override them. Live staging posture:

| Store | Selector | Staging value | Since |
|---|---|---|---|
| Async jobs | `LEAF_JOBS_STORE` | **postgres** (both colors) | 2026-08-06, #489, after the EFS WAL corruption |
| App sessions | `LEAF_SESSIONS_STORE` | **postgres** (walked `dual_write_shadow` 08-08 → `postgres` 08-11) | 2026-08-11 P4a |
| Session annex | `LEAF_SESSION_ANNEX_STORE` | **postgres** | 2026-08-11 P4a |
| Broker tenants/ledger | `LEAF_BROKER_STORE` | **postgres** (app carries it too — required-config manifest enforces presence) | — |
| Drawing / upload authority | `LEAF_DRAWING_STORE` / `LEAF_UPLOAD_STORE` | **postgres** | — |
| Harness sessions | `LEAF_HARNESS_SESSION_STORE` | **postgres** (posture gate requires it) | — |
| Guest caps | `LEAF_GUEST_CAP_STORE` | **memory** (per-task counters; not files, not pg) | — |
| Callback replay | `LEAF_CALLBACK_REPLAY_STORE` | **unset → legacy** (the gap) | — |
| Customization | `LEAF_CUSTOMIZATION_STORE` | **sqlite on shared EFS** (the other gap) | — |

The pg implementations behind these selectors are API-complete (adversarial
finding 15: every public jobs entry point branches on `job_store_mode()`
before touching SQLite, all 13 public session functions dispatch through
`_store_mode()`, approvals included). One code seam must be fixed before the
callback flip: `CallbackReplayStore.__init__` engages Postgres only when
`db_path is None` — an explicit `db_path` silently yields SQLite, failing
OPEN, unlike every other selector which fails closed on bad input.

Prior art: W7a/W7b durable-store lanes (utility-estimation PRs #67/#68,
merged 2026-07-30; source of the pooling-safe invariants in §6), harness
`pgSessionStore.ts` gate-tested 2026-08-05, `docs/POSTGRES-CUTOVER.md`
(production gate list — its "Async non-canonical jobs / App sessions" rows
are now DONE on staging and should be updated when this doc lands).

## 3. Which stores move, which stay

**Move (remaining):**

1. **Callback replay ledger → Postgres** (migration 0011 already carries the
   schema; `PostgresCallbackReplayStore` exists in `server/job_pg_store.py`).
   This is a correctness fix, not just a durability move: today's broker-local
   nonce table means a replayed signed callback delivered to a different
   broker task is accepted.
2. **Customization store → Postgres** (new pg implementation required — the
   only net-new store code in this program; today's `sqlite` mode with
   `BEGIN IMMEDIATE` writes on shared EFS is the last §9-class dual-writer).
   Alternative if the operator prefers zero new code: accept and document it
   as the named residual exposure, and keep the deploy receipts' overlap
   measurement alive for exactly this store.
3. **Production: everything** — jobs, sessions, annex, broker, drawing,
   upload, harness, callback replay, customization, on the production cluster.

**Explicitly stay file-based:**

- Harness conversation files (`LEAF_SESSIONS_DIR`) — append-only artifacts,
  no cross-writer contention.
- Claude grants (`LEAF_GRANTS_DIR`) — secret material; POSTGRES-CUTOVER.md:
  vault, not tables.
- Tenant repositories and drawing artifacts (EFS/S3) — bulk content;
  Postgres holds metadata only.
- Platform repo / customize-state dirs — git-semantics shared state; single
  logical writer by construction today; named here so the residual is
  explicit, disposition deferred to its own design if it ever multi-writes.
- Guest caps — `memory` on staging is per-task and resets on deploy; that is
  a (small, known) correctness gap under color overlap but a product
  decision, not a store-migration one. Named, not moved here.

## 4. Target database

**Reuse the existing staging Aurora Serverless v2 cluster
(`leaf-platform-staging`), one schema, via the existing RDS Proxy. No new
cluster.** (Adversarial finding 14 attacked and confirmed this section.)

- Aurora PostgreSQL 16 serverless v2, KMS-encrypted, deletion-protected, TLS
  forced (`rds.force_ssl=1`), defined in
  `terraform/environments/staging/us-east-1/leaf_platform_db.tf`.
- **SG two-group trap, stated precisely:** the cluster SG admits exactly two
  SGs — RDS Proxy and the one-shot bootstrap task. Never add ingress there.
  The proxy SG already admits all four service SGs (`app`, `broker`,
  `harness`, `worker` — verified: `aws_security_group.leaf_platform` is
  for_each over the services local, and
  `leaf_platform_db_proxy_from_services` covers the four). New consumers join
  the proxy-SG for_each set.
- `DATABASE_URL` (proxy endpoint) is **already wired** into app, broker,
  harness, and worker task definitions — rev 1's "P0 secret wiring" phase is
  discharged by inspection.
- The migration chain runs 0001–0044 on origin/main (rev 1 said 0019 — stale
  tree); the jobs/callbacks (0011) and sessions (0012/0019) schemas are long
  since part of the asserted chain. `assert_schema_current()` machinery is
  extended, never bypassed; no second ledger.
- `platform/db.py` disables prepared statements (`prepare_threshold=None`) —
  safe through RDS Proxy.

**Production topology:** mirror staging — Aurora Serverless v2 + RDS Proxy +
scoped app role + bootstrap task in
`terraform/environments/production/us-east-1`. The only net-new
infrastructure in this program. ACU band and retention are operator decisions
at that phase (POSTGRES-CUTOVER.md decision list).

## 5. Remaining migrations, mechanism per store

**5.1 Callback replay flip (staging).** Set
`LEAF_CALLBACK_REPLAY_STORE=postgres` on the broker (and app, if the manifest
set-difference check requires parity — verify at PR time). Precondition: fix
the fail-open `db_path` seam (§2) so the selector fails closed like its
siblings. Honesty about the window (rev 1 claimed a fence-closed flip is
traffic-free; refuted — the drawing-mutation fence gates drawing commits and
uploads, not job submission or callbacks): during the deploy that flips the
selector, a nonce consumed in the old store is invisible to the new one. The
replay-acceptance window is bounded by `LEAF_CALLBACK_MAX_AGE_S` (default
300s) HMAC freshness, and today's baseline is per-broker-task nonce state
anyway — the flip strictly improves every property it touches. No dual-write;
record the bound in the deploy receipt.

**5.2 Customization store (staging).** Requires a `PostgresCustomizationStore`
implementation (design follow-up spec, not this doc): same selector pattern
(`sqlite`|`postgres`, fail-closed), pooling-safe per §6, schema as the next
numbered migration. Until it ships, `customization.db` is the named surviving
dual-writer and §9-class exposure owner; the deploy receipt's measured
overlap window stays meaningful for this store alone.

**5.3 Sessions ladder — retained for production, done on staging.** Staging
walked `legacy → dual_write_shadow → postgres` over 08-08→08-11. Production
repeats the ladder under stop-first (single-task) deploys, which the
turn-fence constraint requires anyway (`session_store.py` `_store_mode()`
docstring: dual writes are valid only in the single-task phase — SQLite and
Postgres cannot share one atomic transaction). Production-ladder caution from
the adversarial round (finding 9, verified on the rev-1 tree, line numbers
drift on main but the ordering shape holds): on dual rungs,
`consume_approval`/`decide_approval` commit the legacy leg first and the pg
leg can raise afterward — the pg leg on dual rungs must log-not-raise (or
order pg-first with compensation) before production walks the ladder. That
is a pre-ladder code fix, listed in §10.

**5.4 Jobs — done on staging; production inherits the drained-window flip.**
The honest drain mechanism (rev 1 overstated it): nothing waits for job rows
to go terminal; leases expire, `ensure_started()` re-dispatches `submitted`
rows on the new color, and the reaper reclaims stale leases — all reads of
the SELECTED store only. Therefore the flip happens under stop-first (old
tasks gone before new start), which production still runs; no cross-store
re-dispatch ambiguity exists in that ordering.

## 6. Transaction semantics parity

| Legacy mechanism | Guarantee | Postgres equivalent |
|---|---|---|
| SQLite single-writer + WAL | serialized multi-table writes | ordinary transactions; `READ COMMITTED` default with per-store escalation + `SerializationFailure`/`DeadlockDetected` retry (`platform/db.py`) |
| Job claim mutex + lease columns | at-most-one worker per job | single-transaction `FOR UPDATE SKIP LOCKED` oldest-queued claim + lease CAS (W7a idiom; already the shipped `job_pg_store` shape) |
| Turn fence rows | one active turn per session | row-CAS / `INSERT ... ON CONFLICT` under a per-session transaction (already shipped in the pg session store) |
| Fence commit-guard `flock(LOCK_EX)` drain barrier | flip observes a quiesced store | **not carried over.** Rev 1 proposed an advisory-lock barrier and misdescribed the API: `pg_advisory_xact_lock` is exclusive-only (a shared variant `pg_advisory_xact_lock_shared` exists but appears nowhere in the repo; the existing call sites in `broker_pg_store.py`/`agent_pg_store.py` are tenant-scoped MUTEXES, not barriers). An exclusive-lock "barrier" taken by every writer would serialize all writers — reintroducing exactly the single-writer property this program deletes. Once both colors write the same Postgres tables, no store-level deploy barrier is needed; the drawing-file fence keeps its flock for drawing mutations, which stay on EFS. |

Hard rule (W7b, Neon-pooling finding): transaction-scoped locks only, no
session GUCs, `SET LOCAL` never `SET`. Compatible with RDS Proxy (xact-scoped
state releases at commit; pinning bounded to one transaction). The existing
static contract tests (`test_broker_migration_static.py` asserts no advisory
DDL leaks into migrations) extend to the customization store.

## 7. What each consumer changes (remaining work only)

| Consumer | Change |
|---|---|
| **broker** | `LEAF_CALLBACK_REPLAY_STORE=postgres` (after the fail-closed seam fix) |
| **app** | replay selector parity if the required-config manifest demands it; customization selector flip once the pg store exists |
| **worker** | customization selector flip (it carries `LEAF_CUSTOMIZATION_STORE=sqlite` today, tf :1725) |
| **harness** | nothing — already postgres, posture-gated |
| **deploy workflows** | choreography harvest per §9, gated on the LAST shared-EFS SQLite writer (customization), not the first |
| **production tf** | cluster + proxy + bootstrap module port; per-service selector walk per §5 |

Caution carried from finding 10: the required-config manifests are enforced
as a set-difference against the container environment by the deploy workflow,
so selector additions/removals are deploy-gated config changes, not "just an
env change" — sequence manifest edits with the TD edits in the same PR.

## 8. Rollback per phase

| Phase | Rollback | Data consequence |
|---|---|---|
| Callback replay flip | unset the selector (redeploy) | nonces consumed in pg invisible to legacy — bounded by the 300s max-age window; baseline was per-task anyway; accept and record |
| Customization flip | selector back to `sqlite` | writes made in pg during the window are absent from `customization.db`; a one-shot pg→sqlite export closes the gap if it matters; rollback deadline set before the flip (POSTGRES-CUTOVER.md gate 7) |
| Production ladder rungs | previous rung by env change; dual rungs keep SQLite authoritative | from `postgres` authority, flip-back strands the pg-window rows. Known sharp edge (finding 13): the reaper and re-dispatch read only the selected store, so stranded pg jobs are never reaped and their APS WorkItems never reach `/broker/reap`; idempotency keys are per-store, so re-submission can create a cross-store duplicate. Rollback runbook must include a one-shot pg-store reap before re-submitting. |
| Contract step (drop SQLite/file paths) | LAST, operator-gated, ≥1 clean full deploy cycle per environment on pg authority | deletes the rollback artifact — hence last |

## 9. Payoff inventory (corrected)

1. **Exposure-class deletion completes** when the LAST shared-EFS SQLite
   writer (customization) moves — not before. Jobs/sessions being done means
   the §9 computed window already overstates today's exposure; the receipts
   should say which stores the measured overlap still endangers.
2. **`hold_seconds`: the exposure term stops constraining it.** It does NOT
   reach 0 in color-route verify mode: the workflow enforces a 120s floor for
   ALB rule-propagation reasons unrelated to storage (current default 60s,
   measured safe). The knob's remaining constraints are routing physics and
   rollback coverage, and the doc trail should stop citing storage.
3. **Fence scope narrows** to actual drawing mutations (its real job); it was
   never a jobs/callback gate (finding 4) — the simplification is in the
   *documentation and receipts*, plus dropping any future temptation to
   bracket store flips with it.
4. **Production rolling deploys instead of stop-first** — the 9m49s
   stop-first outage class is deleted once production completes §5's walk;
   this is now the program's main unrealized payoff.
5. **Pre-warmed cutover without storage exposure accounting** — the sibling
   pre-warmed-cutover design (frozen rev 2) can drop its storage-exposure
   bookkeeping once customization moves; idle-warm TTL machinery keys on the
   same last-writer gate.
6. **Replay protection that actually spans broker tasks** (§5.1) — a net
   correctness gain rev 1 didn't even claim.
7. **One durability story** — backup/PITR/restore drills cover what moves;
   EFS SQLite files sit outside every restore drill today.

## 10. Phasing and cost (rebuilt)

| Phase | Content | Size | Ships alone? |
|---|---|---|---|
| **P1** | Code: fail-closed `db_path` seam fix in `CallbackReplayStore`; then staging `LEAF_CALLBACK_REPLAY_STORE=postgres` (+ manifest edits). | S | **Yes** — first shippable phase; closes the cross-task replay gap by itself. |
| **P2** | `PostgresCustomizationStore` spec + implementation + next-numbered migration; staging flip; deletes the last §9-class exposure. | M (the only net-new store code) | Yes. |
| **P3** | Choreography/doc harvest: §9 exposure text retired to history in the cutover design; receipts name surviving writers (none after P2); hold documented as routing-floor-bound; POSTGRES-CUTOVER.md inventory updated. | S | Yes — content shrinks if P2 lands first; safe in either order after P1. |
| **P4** | Production: cluster+proxy tf port; pre-ladder code fix (dual-rung approval consume log-not-raise, §5.3); per-store walk (jobs drained-window under stop-first, sessions ladder, then the rest); THEN stop-first → rolling deploy PR. | L | Yes — final payoff. |

Dependencies: P1 → (P2, P3) → P4 for the rolling-deploy end state; P4's
cluster provisioning can start any time. Nothing blocks or is blocked by the
pre-warmed-cutover lane; P2 is what unlocks its simplification.

## 11. Decision asks

1. Ratify target = existing staging Aurora, production mirrors it — §4.
2. Ratify P1 (callback replay, with the fail-closed seam fix) as the first
   authorized implementation slice.
3. Decide P2: build `PostgresCustomizationStore`, or accept customization.db
   as the documented residual exposure (this design recommends building it —
   it is the last member of the constraint class and the size is M).
4. Production sizing (ACU band, retention) at P4 time.

## Appendix A — adversarial round ledger (rev 1 → rev 2)

Refute-first pass by opus-critic (Anthropic-only round; Codex capped until
Aug 19), 16 findings; every load-bearing one re-verified by the authoring
session against leaf-web-demo origin/main 79a3e16 and tf main before folding.

- **3 BLOCKERs, all reproduced, all folded:** stale premise (staging already
  postgres for jobs/sessions — §1/§2 rebuilt); advisory-lock barrier
  misdescribed a nonexistent shared mode for `pg_advisory_xact_lock` and
  would have serialized all writers (§6 row replaced with "no barrier
  needed"); `customization.db` shared-EFS SQLite survived on both colors and
  the worker, unlisted (§1/§3/§5.2/§10 P2).
- **6 MAJORs folded:** callback flip decoupled from the already-done jobs
  flip and its broker-local nonce reality stated (§5.1); "fence-closed =
  traffic-free" refuted, fence scope corrected (§5.1, §9.3); "deploy drains
  jobs" corrected to lease-expiry + re-dispatch under stop-first (§5.4);
  `hold_seconds=0` corrected to the 120s color-route floor (§9.2); sessions
  ladder rescoped to production with the dual-rung approval-consume ordering
  fix as a precondition (§5.3); broker-store row and manifest set-difference
  coupling corrected (§7).
- **4 MINORs folded:** harness row corrected to postgres; guest-cap
  disposition corrected to `memory`/per-task; stranded-jobs rollback row
  strengthened with the reaper/idempotency cross-store consequence (§8);
  stale module comment noted and ignored.
- **2 attacks failed (claims confirmed):** the SG/proxy topology argument
  (§4) and pg API-completeness of the shipped stores (§2), the latter with
  the fail-open `db_path` seam now a named P1 precondition.
- **Authoring-session findings beyond the critic's list:** migration chain
  0001–0044 not 0019; broker legacy replay DB is container-local (not EFS)
  because the broker never sets `JOBS_DB`; both rev-1 checkouts were stale,
  which is itself the reproduced cause of the premise blocker.

## Appendix B — rev 3 source-verification round (rev 2 → rev 3)

Rev 2 closed by naming two remaining items and handing them forward. Both were
verified at source before any implementation began, against leaf-web-demo
`origin/main` (56921bb) and leaf-automation-aws-terraform `main`. Both premises
moved. Rev 2's body above is unedited; this appendix is the correction of
record.

### B1. Callback replay — mechanism CONFIRMED, "live exposure" REFUTED

Rev 2's claim, restated: "the broker never sets `JOBS_DB`, so its nonce table is
container-local and replay protection doesn't span broker tasks."

**Confirmed.** `terraform/environments/staging/us-east-1/leaf_platform.tf` sets
`JOBS_DB=/data/state/jobs.db` on the app colors only (`:1342` app, `:1580`
app_alt). The broker module (`:1679`–`:1764`) sets neither `JOBS_DB` nor
`LEAF_CALLBACK_REPLAY_STORE`, so under the legacy authority the nonce table is
the broker container's own `server/jobs.db` — not the EFS state mount the broker
does have. Two broker tasks keep two independent nonce tables.

**Refuted as a live exposure — it is latent.** `consume_callback` returns
`not_configured` before it constructs the replay store when
`LEAF_CALLBACK_SECRET` is unset. That variable appears nowhere under
`leaf-automation-aws-terraform/terraform/` and is absent from
`deploy/required-config.broker.json`'s secrets list (which carries only
`APS_CREDENTIALS_JSON`, `DATABASE_URL`, `LEAF_BROKER_SECRET`). No signed
envelope can be accepted, so **no nonce is ever written and there is nothing to
replay**. The only component that could mint a valid envelope,
`server/da/aps_callback_adapter.py`, has no production importer — `broker.py`
never loads it, only its own test does — and `LEAF_CALLBACK_PRIMARY` is unset
and explicitly fails closed.

Consequence for phasing: P1 is not incident response. It is a **sequencing
constraint** — `LEAF_CALLBACK_REPLAY_STORE=postgres` must land on the broker
before `LEAF_CALLBACK_SECRET` is ever provisioned, i.e. as part of the L3.1
callback-primary activation, not ahead of it.

**Shipped:** the fail-open `db_path` seam named in §2 and §10-P1 is fixed
(leaf-web-demo #645). `CallbackReplayStore` now treats the selector as sole
authority and raises when an explicit `db_path` contradicts `postgres`, instead
of silently downgrading to SQLite. The module docstring's claim that legacy
nonces are durable against "another broker worker" — the text that seeded rev
1's framing — is corrected in the same change.

**Remaining P1 work:** the selector flip itself, deliberately not bundled. It is
a deploy-gated change spanning two repos and must be sequenced: the broker task
definition gains `LEAF_CALLBACK_REPLAY_STORE=postgres` first, and
`deploy/required-config.broker.json` requires it second. Reversing that order
fails the deploy set-difference gate (§7). Trigger: before `LEAF_CALLBACK_SECRET`.

### B2. Customization — BOTH rev 2 premises refuted

**`PostgresCustomizationStore` already exists.** `server/customization_postgres_store.py`
is a shipped adapter that supplies PostgreSQL connections to the
storage-independent methods of `SQLiteCustomizationStore` and translates the
small dialect surface, over eight tables. It is backed by migrations 0020, 0025,
0026, 0031, 0036, 0037; wired into the authority-selector map at
`platform/db.py:1177` (`"LEAF_CUSTOMIZATION_STORE": {"postgres": "customization"}`);
constructed by `CustomizationService.configured()` at
`server/customization_service.py:463`; and covered by
`tests/test_customization_postgres_contract.py` and
`tests/test_customization_postgres_integration.py`.

So §3.2, §5.2 and §10-P2 are wrong to call this "the only net-new store code"
and to size it **M**. **P2 is a selector flip, size S** — the same shape as P1,
with no new store code and no new migration.

**`customization.db` is not a dual-writer and carries no §9-class exposure.**
`CustomizationService.configured()` raises `customization_shared_sqlite_unsupported`
(503) for any SQLite path under `/data/state` (`_shared_sqlite_path`,
`server/customization_service.py:109`), and every write path reaches the store
through `configured()`. Staging sets exactly that path
(`LEAF_CUSTOMIZATION_DB=/data/state/customization.db`, tf `:1397`/`:1614`), so
the file **is never opened on staging**. With `LEAF_CUSTOMIZATION_R5_MODE` and
`R6_MODE` both `off`, the read paths return the base catalog rather than
raising. The posture is fail-closed *unavailable*, not *exposed*.

Two further corrections to rev 2's §1 table: the third writer it lists as "the
worker (tf `:1725`)" is the **broker** module — staging has no worker service —
and the broker sets no `LEAF_CUSTOMIZATION_DB`, so it falls back to
`DEFAULT_DB` (`server/customization.db`), a container-local file that never
trips the shared-path guard and shares nothing.

**Therefore §9.1 is wrong.** It states that exposure-class deletion "completes
when the LAST shared-EFS SQLite writer (customization) moves — not before." The
class is **already empty on staging**: jobs, sessions and annex are Postgres,
the callback ledger writes nothing, and customization refuses the shared path.
No store flip is owed before the exposure claim can be retired.

**Handoff correction (supersedes the message sent to coordinator 84be1c56):**
the pre-warmed-cutover lane was told its exposure accounting "now keys on
`customization.db`, not `jobs.db`." It keys on **neither**. That lane can drop
its storage-exposure bookkeeping now, gated on nothing.

### B3. Rev 3 routing

| Item | Rev 2 said | Verified | Route |
|---|---|---|---|
| `CallbackReplayStore` fail-open seam | P1 precondition | real bug, fixed | **DONE** — #645 |
| `LEAF_CALLBACK_REPLAY_STORE=postgres` | P1, ships alone | latent, not live | tf-then-manifest PR pair; **trigger: before `LEAF_CALLBACK_SECRET`**, not urgent alone |
| `PostgresCustomizationStore` | build it (M) | already shipped | **no code owed** |
| `customization.db` exposure | last §9-class dual-writer | never opened on staging | **not an exposure**; retire the claim |
| `LEAF_CUSTOMIZATION_STORE=postgres` on staging | P2 migration | selector flip (S) | **operator decision, not a fix** — it turns a fail-closed feature on, and R5/R6 stay `off` regardless, so it buys availability, not safety |
| §9.1 exposure-class payoff | completes at P2 | already complete | **retire the §9 exposure text now**; P3 no longer waits on P2 |
| P4 production | the main unrealized payoff | unchanged | **unchanged, and now the only phase carrying real payoff** |

The program's honest remaining value is concentrated in P4 (production stop-first
→ rolling, the recorded 9m49s outage class). P1's flip is a sequencing
obligation, P2 is an availability decision, and P3 can proceed immediately.
