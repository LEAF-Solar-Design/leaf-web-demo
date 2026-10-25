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
