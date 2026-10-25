# Lab worker operator guide

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

## Activations

Deploys can activate only a published revision, with trusted pure, read or
simulate blocks. Host, compute and effectful blocks are refused. Read and
simulate also require `LEAF_LAB_TOOL_NODES=1`; activation runs use a restricted
adapter that never admits compute. The activating subject is stored privately.

An activation starts `active`. Pause moves active to `paused`; resume re-admits
a paused activation. Off moves active, paused, expired or stopped to terminal
`off`. A due activation whose 24-hour lease has elapsed becomes `expired` and
never resumes silently. Renew is allowed from active, paused or expired,
re-admits now, grants 24 hours from renewal and lands in active. Stopped and off
cannot renew or resume; create a fresh activation after resolving the cause.
An expired paused lease also requires renewal. Controls and scheduler state
changes append transitions; the store keeps 200 and the UI shows the latest 20.

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

Intervals are integers from 60 to 86400 seconds. A tenant may have at most ten
active, paused or expired activations. There is one run in flight per activation;
due ticks during a run coalesce instead of creating more work. Queue saturation
also coalesces. The next successful harvest replaces the prior declared output
and summary (run id, terminal state, finish time, duration and coalesced ticks).
Output retention is capped at 64 KiB; overflow becomes `{"truncated": true}`.
It does not retain output history or undeclared intermediate values.

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
