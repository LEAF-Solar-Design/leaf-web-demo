# Lab worker operator guide

## Canvas performance proof

The isolated production proof renders 500 blocks and 1000 pipes using the real
LabCanvas and embedded manifests. It builds only the performance entry into
`web/dist-lab-perf`, then serves it with Vite preview on port 5195. It needs no
backend or credentials. Run from the repository root with installed web
dependencies and the Playwright Chromium browser:

```powershell
$env:LAB_PERF_SHA = git rev-parse HEAD
Push-Location web
npx playwright test e2e/lab-canvas-perf.spec.mjs --config playwright.lab-perf.config.mjs --reporter=line
$a = $LASTEXITCODE
Pop-Location
exit $a
```

For the release proof, use the Mac mini through the shared tooling's
`scripts/mini_e2e.py` (the script is outside this repository). From the repository
root, using its installed Windows path:

```powershell
python "$HOME/.claude/scripts/mini_e2e.py" run --worktree . --config playwright.lab-perf.config.mjs --host mac-mini --log-dir <dir>
```

The limits are mount <= 500 ms, pan frame p50 <= 16.7 ms and p95 <= 33 ms.
Pan samples span at least two seconds. Relayout, both pipe budget states and
all four inspector tabs must also succeed. Video and trace are off. Timings
depend on hardware; this config is deliberately outside the CI gate catalog.
The receipt is `web/test-results/lab-canvas-perf-receipt.json`, schema
`leaf.lab-canvas-perf.v1`. The same compact JSON is printed on one line prefixed
`LAB_PERF_RECEIPT=` so snapshot runs retain evidence in their logs. Preserve
that line and the routed run log in the program receipt folder outside git;
the remote snapshot's receipt file may not return. `LAB_PERF_SHA` is used when
available, otherwise the test reads `git rev-parse HEAD` from the repository
root with a five-second timeout and falls back to `unknown` on error. Bind
routed evidence to the runner's source snapshot SHA before treating it as
release evidence.

The graph cap stays **200 blocks / 400 pipes** until a passing receipt on the
release candidate supports raising it in a later change. The harness constructs
its oversized rendering fixture directly, without calling the admission API or
validator. LabCanvas still runs its own validation and reports the existing cap;
this proof does not attest graph admission or execution. Mount ends at the first
animation frame after all 500 node cards enter the DOM; initial dependency layout
finishes separately before pan measurement. See [WAVE2-REPLAY.md](WAVE2-REPLAY.md).

Measured hardware: Apple M4 Pro, 12 cores, 48 GB, macOS (darwin 27.0.0), headless Chromium 148.0.7778.96 (Mac mini, mini_e2e job 147a73d9, 2026-10-26).

Measured: mount 134.9 ms (budget 500), pan p50 12.8 ms (16.7), pan p95 27 ms (33), relayout 2.43 s, 500 nodes and 1000 edges rendered. The 200-block / 400-pipe document cap is unchanged; raising it needs this receipt passing on the release candidate.

All Lab endpoints require the Lab surface rail, live authentication, and a
verified tenant. Run creation, reads, events, and cancellation require `build`.
Worker pause and resume also require `deploy`. Worker control is global: a
pause affects every tenant using this database.

## Stop

Set `LEAF_LAB_WORKER_DISABLED=1` in the worker process environment to stop
claims and suspend execution. Apply the environment change to every running
process (restart or redeploy as needed). To pause through the database, send an
authenticated `POST /api/lab/runs/worker/pause` with JSON
`{"reason":"operator maintenance"}`. The worker observes the pause, stops
claiming, and suspends active work; this is cooperative, not an immediate kill.
Keep the Lab surface enabled to use the control routes.

Check `GET /api/lab/status`: `worker.paused` reflects the database control;
`worker.workers` lists heartbeats from the last 60 seconds with build revision,
active/idle color, heartbeat age, active runs, and queued runs. An empty list
means no recent heartbeat. `worker.queue_age_max_s` is the age of the oldest
queued run, or null when there is no queued run. Counts and queue age describe
the worker fleet, not a tenant's private run history.

## Recover

Clear `LEAF_LAB_WORKER_DISABLED` (or set it to `0`) on every process that should
work again, and send an authenticated `POST /api/lab/runs/worker/resume` with no
body. Confirm a fresh heartbeat for the intended build. The adopted revision
claims work as active; an older build reports idle and normally does not claim.

After a crash or suspension, expired leases resume through `lease_takeover`.
The new lease fences the former owner and skips already committed block values.
Recovery waits for lease expiry; do not delete leases or edit fences manually.
Inspect `GET /api/lab/runs/{run_id}` and cursor pages from
`GET /api/lab/runs/{run_id}/events?after_seq=-1&limit=100` (limit 1–500).
Use the final sequence as the next `after_seq`.

Cancel stuck runs with `POST /api/lab/runs/{run_id}/cancel`. Queued runs cancel
immediately; running workers observe the request cooperatively. A repeated
request returns `cancel_requested: false`. A paused running run may need worker
recovery before its cancellation completes. Cancellation records the tenant
subject as actor. Run creation pins a stored document revision; it does not
accept inline graphs or caller-supplied run IDs. Host graphs and, by default,
effectful graphs are refused before enqueue. A tenant can queue at most 16 runs; queue overflow is
`409 lab_run_queue_full`.

## Signals

The worker emits EMF metrics: `LabRunTerminal` counts worker terminal outcomes
by status, `LabLeaseTakeover` counts reclaimed expired leases, and
`LabQueueAgeMax` measures the oldest queued run age in seconds (zero when the
queue is empty). Takeovers can be expected during recovery; sustained queue
age with absent heartbeats calls for worker and pause-state inspection.

Staging has no paging alarm on these metrics. Inspect status, run events, and
EMF output directly during staging operation and recovery.

## Stream and rail recovery

Use fetch with `Authorization: Bearer ...` for
`GET /api/lab/stream/runs?runs=<run_id>[,<run_id>...]&after_seq=-1`.
Native EventSource cannot supply this header. Query credentials (`token`,
`jwt`, `ticket`, `access_token`) are refused. Subscribe to 1–8 distinct runs;
each must belong to the caller. Keep the last sequence for each run and send
`Last-Event-ID: <run_id>:<seq>,<run_id>:<seq>` on reconnect. These per-run
cursors override the shared `after_seq` default.

An `event: gap` means retained events no longer cover the cursor. Fetch the
run snapshot and page its events starting with `after_seq=-1` (including seq
0), replace the displayed state, and reconnect from the recovered cursors.
Do the same after reopening a disabled Lab rail. A `coalesced` field on a
value reports deferred values folded into the latest value for its block;
intermediate values are observations, not a durable work queue.

`event: close` reasons `max_age` (300 seconds) and `drain` require reconnect
with the saved cursors. `binding_changed` and `binding_unavailable` require
refreshing the authenticated tenant binding before reconnect. `run_missing`
requires refreshing run access. `lease_expired` and `store_unavailable`
require checking store health and retrying with backoff. The binding is
rechecked every second. A comment `: hb` and `event: health` arrive every
15 seconds. Health `lag_ms` measures the newest observed event's store age
(null until an event is seen); it is separate from source `as_of` in values.

Each principal can hold four streams across processes and tenant moves;
the fifth returns `409 lab_stream_limit`. A subject is the principal when
available, otherwise the tenant is. Closing releases the lease; after a
crash it expires after 45 seconds without renewal. Reconnect with backoff
instead of deleting leases. Rollback disables the rail and retains migration
0088's table; expired admission rows are removed on the next acquisition.

Each run emits at most 50 values per second. Deferred values coalesce by
block; control events continue through. Value payloads above 64 KiB become
`{"truncated":true,"size":<original_bytes>}`. The stream has a 2 MiB/second
wire budget; clients needing full values should recover via snapshots.

Latency targets from store timestamp to emission are local p95 below 500 ms
and staging p95 below 1500 ms. `lab_stream_closed` logs one structured summary
with per-run counts and rolling p50/p95 milliseconds, without value payloads.
The injected-clock fixture in `test_latency_summary_has_counts_without_payload`
asserts p50 = p95 = 125 ms for one event on each store authority. This is a
deterministic fixture expectation, not a measured local or staging runtime
result. Live local and staging measurements remain to be recorded from the
close summaries before rollout; no live latency measurement is claimed here.
## Tool nodes

`LEAF_LAB_TOOL_NODES=1` enables server-side tool nodes; the default is off.
The browser never executes these nodes. With the flag off, runs retain the
existing `lab_graph_effect_refused` policy. Host blocks always fail first with
`lab_graph_host_refused`.

Admission proceeds in this order: trusted compute/read effect (no host, live
APS, drawing.write, solve or cloud-proposal); canonical UUID tenant identity
(the platform org id); clean input values; a drawing
binding; a seed catalog tool and matching drawing input; the tenant's current
provisioned tier and required entitlement; the daily jobs window; the sandbox
seconds ledger; then durable submission. The drawing setting must contain
exactly `project_id`, `drawing_id` (strings of 1–200 characters), and
`dwg_version` (an integer at least 1, never a boolean).

Adapter refusal codes use the `lab_tool_` prefix: `effect_refused`,
`input_error`, `drawing_required`, `unknown`, `drawing_mismatch`,
`binding_unavailable`, `policy_unavailable`, `entitlement_required`,
`budget_unreadable`, `daily_jobs_exhausted`, `daily_sandbox_exhausted`,
`inflight_cap`, `submit_refused`, and `submit_unavailable`. After submission,
`timeout`, `failed`, or `output_invalid` describe remote failures; schema
validation can produce `output-refused`. Cancellation stops polling.

`LEAF_LAB_TOOL_DAILY_JOBS` is an integer from 1 to 200 (default 100).
`LEAF_LAB_TOOL_DAILY_SANDBOX_S` is a number from 1 to 86400 (default 3600).
Both budgets use the UTC day; sandbox usage sums tenant `e2b_exec` cost events
in `sandbox_seconds`. Invalid configuration, unreadable ledgers, invalid tenant
UUIDs, or a truncated ledger window fail closed with `lab_tool_budget_unreadable`.
`LEAF_LAB_TOOL_TIMEOUT_S` is an integer from 1 to 600 (default 300), with
polling every 0.5 seconds. Remote waits retain run wall and output byte limits.

Admission is serialised per tenant inside one worker process, from the jobs-list
read through durable submission; waiting for the job result happens outside the
lock. Across worker processes (for example two app colors), the daily job budget
can be exceeded by at most one job per extra process at the moment of the race.
The sandbox-seconds budget is read from completed e2b cost events, so in-flight
sandbox time is not yet counted.

## Git-authoritative mode

Activation is an operator decision. Apply migration `0090_lab_git_authority.sql`
before adopting this release. The default `LEAF_LAB_GIT_TENANTS` allowlist is
empty. Add the tenant explicitly and set `LEAF_LAB_GIT_SINGLE_WRITER=attested`
only after checking all of the following:

- One leaf-platform ECS task writes the repositories: desired count 1,
  maximum percent 100, minimum healthy percent 0.
- No other service, manual RunTask, or process using a shared volume writes
  these tenant repositories. Service deployment settings alone cannot prove this.
- Every harness serving this tenant supports `lab-generation-v1`; no older
  harness can access or mutate its repository. Responses must carry
  `x-leaf-lab-capability: lab-generation-v1`, including error responses.

The attestation states that the operator checked these facts; the server cannot
prove that another process is absent. Keep these conditions true throughout
git mode, including maintenance and recovery.

From a server operator Python session with the deployment environment loaded,
first call `lab_artifacts_git.enable_mirror(tenant, actor)` and let mirror
catch-up finish. Then call `lab_artifacts_git.cutover(tenant, actor)`. These are
operator functions, with no public HTTP route. Cutover requires mirror mode,
no live pending writes, digest equality and recorded ancestry. It freezes the
ledger mirror, allocates a monotonic generation, records `/lab/fence`'s
receipt, and repeats the proofs at its barrier head. Success leaves
`lab_git_tenants.frozen=TRUE` and an authority row with mode `git`, generation
equal to the receipt's destination, and base/verified heads at the barrier.
Writes now use git and complete into the ledger; publication retries through
the durable outbox.

For rollback, call `lab_artifacts_git.flip_back(tenant, actor)`. With authority
present this first sets mode `draining` and fences to the next generation.
Every pending operation is looked up at the barrier: landed operations complete
in parent order; only definitively absent operations are abandoned. It drains
the outbox, catches the verified cursor up, and proves digest equality and
recorded ancestry from the authority's base to the barrier. Only then does one
authorized database transaction delete authority and set ledger mode with
`frozen=FALSE`. A mirror-only flip-back keeps its existing behavior.

Do not flip ledger mode or clear frozen by hand. `lab_flip_back_refused` with
reason `foreign_commits_need_import` means some git history lacks ledger commit
evidence. Reason `lab_replay_divergence` means the ledger digests differ. Either
refusal returns authority to git under one additional fenced generation,
retains the frozen flag, and records one notification in `lab_git_outbox`.
Resolve the evidence/content discrepancy before retrying rollback. Foreign
commits are a residual requiring an explicit import/reconciliation procedure;
this release does not automatically import them. A stale ledger cannot be
made authoritative merely by changing a mode flag.

Read `lab_git_transitions` filtered by `tenant_id`, ordered by `created_at`.
`transition_id` identifies the harness receipt; `from_generation` (0 means no
prior generation), `to_generation`, and `barrier_head` bind its fence request.
States are `planned`, `fenced`, `resolved`, `drained`, `proven`, and terminal
`done` or `reverted`. Cutover skips the rollback-only resolve/drain states.
`owner` and `lease_expires_at` identify the current 120-second owner lease.
A planned row's barrier field stores the expected head so an identical fence
can be replayed after a lost response. Refused rollback's extra fence plan is
retained in `lab_git_tenants.receipt.rollback_refusal`, including its fence ID
and generations. Do not edit these records to force recovery.

The active worker color runs the git background tick at most every 30 seconds
while the worker is enabled, the rail is on, and the worker is unpaused. It
rotates through at most 20 tenants, oldest first, taking over expired
transitions, recovering pending writes, and processing the outbox. A planned
transition whose generation never changed reverts; an applied fence is replayed
to recover its receipt; fenced work continues from its recorded barrier.
Failures are isolated per operation and tenant and log exception class only.
Outbox failures retry with backoff. Missing capability, ambiguous operation
lookup, or incomplete ancestry keeps rollback draining until it can be proved;
never abandon a possibly landed write to unblock it.

Before deploying a release older than this one, roll **every git-mode tenant**
back successfully and verify that no authority rows or live transitions remain.
Removing a tenant from the allowlist does not roll it back. Preserve the
additive tables, generations and receipts across image rollback. Global worker
pause also pauses git recovery; resume it and wait for terminal transitions
before a downgrade.

## Activations

Deploys can activate only a published revision, with trusted pure, read or
simulate blocks. Host, compute and effectful blocks are refused. Read and
simulate also require `LEAF_LAB_TOOL_NODES=1`; activation runs use a restricted
adapter that never admits compute. The activating subject is stored privately.
Creation requires deploy entitlement from the tier alone: roles and elevation
cannot supply it (`403 lab_activation_entitlement_required`).

An activation starts `active`. Pause moves active to `paused`; resume re-admits
a paused activation. Off moves active, paused, expired or stopped to terminal
`off`. A due activation whose 24-hour lease has elapsed becomes `expired` and
never resumes silently. Renew is allowed from active, paused or expired,
re-admits now, grants 24 hours from renewal and lands in active. Stopped and off
cannot renew or resume; create a fresh activation after resolving the cause.
An expired paused lease also requires renewal. Controls and scheduler state
changes append transitions; the store keeps 200 and the UI shows the latest 20.
Every move out of active requests cancellation of the in-flight run as actor
`activation` after the activation transaction commits. Cancellation failures log
only the exception class and do not undo the transition. Before evaluation, the
worker cancels an activation run with `activation_inactive` unless an active
activation holds it.
This guard also covers a process exit between the transition and cancellation.
Resume and renew preserve the interval after `last_started_at`; neither can
schedule a second run sooner than one full interval after the last start.

Each due tick checks the principal's current binding and deploy entitlement,
the published revision and graph hash, registry/catalog digest, operations
revision and input digest. This also applies while a run is in flight. Failures
stop scheduling with `binding_changed`, `binding_unavailable`,
`entitlement_revoked`, `policy_unavailable`, `publication_changed`,
`registry_changed`, `ops_changed` or `input_changed`. A disabled tool flag stops
tool activations with `lab_activation_tools_disabled`; newly inadmissible
blocks produce `lab_activation_effect_refused`. Missing retained runs stop
with `run_unavailable`. Auth-off and tenant-only principals skip subject binding
and entitlement resolution, matching the existing tenant-context behavior.
Artifact/store exceptions skip the activation's tick without enqueueing and
increment `failure_count`. Three consecutive failures stop it with
`readmit_unavailable`; a successful tick resets the count. Each activation is
processed in its own tenant transaction, so a failure does not abort other
tenants' ticks. A missing pinned artifact during harvest records
`last_summary.harvest=unavailable` and stops with `publication_changed`.

Intervals are integers from 60 to 86400 seconds. A tenant may have at most ten
active, paused or expired activations. There is one run in flight per activation;
due ticks during a run coalesce instead of creating more work. Queue saturation
also coalesces. The next successful harvest replaces the prior declared output
and summary (run id, terminal state, finish time, duration and coalesced ticks).
Output retention is capped at 64 KiB; overflow becomes `{"truncated": true}`.
It does not retain output history or undeclared intermediate values.
Harvest selects only the latest value event for each declared output block in
one bounded query. Stored payloads above 64 KiB are marked truncated without
decoding; outputs are added only while the combined size remains within 64 KiB.
Listing includes active, paused and expired rows plus the newest 20 stopped or
off rows, with the latest 20 transitions fetched together. Each tick purges at
most 100 stopped/off rows older than seven days and their transitions. Deploys
polls every 15 seconds while the view and browser document are visible.

Use the global worker pause (`POST /api/lab/runs/worker/pause`) to pause all
activation ticks, including expiry processing. Resume the worker through
`POST /api/lab/runs/worker/resume`; elapsed leases then expire on their next
tick. Only the active worker color schedules, at most once every five seconds.
The same tenant transaction serializes enqueueing and the activation pointer.

Production migration `0089_lab_activations.sql` must run **forward-normal**
through `migrate-leaf-platform-production-db.yml` before adopting any image that
reads these tables, even with the Lab rail off. Roll back the image without
dropping the additive tables; pause the worker to stop scheduling globally.

Tool nodes submit only through `jobs.submit_job` and the broker. The durable
key is `lab:<run_id>:<block_id>:1`; a lease takeover reuses it and skips the
daily budget checks when that job is already present. Lab tool jobs count
against `LEAF_TENANT_MAX_INFLIGHT` (default 32).

The broker requires the microvm sandbox for lab jobs, checked at request time
(`LEAF_TOOL_SANDBOX_PROVIDER=e2b`). Missing, off, invalid, and subprocess tiers
return 503 `lab_sandbox_unavailable`. Malformed execution markers return 422
`lab_execution_invalid`; live APS and write/solve/cloud-proposal capabilities
return 403 `lab_aps_refused` or `lab_write_refused`. Lab data plans return 403
`lab_plan_refused`.
