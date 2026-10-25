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
accept inline graphs or caller-supplied run IDs. Host and effectful graphs are
refused before enqueue. A tenant can queue at most 16 runs; queue overflow is
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
