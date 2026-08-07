# Warm executor runtime prototype

This directory implements a local Python 3.12 prototype of the instant
executor. `WarmExecutorSupervisor` starts its fixed pool before traffic. An
assignment validates the checked-in v1 schemas, verifies the immutable source
digest, and loads source once into an idle child. `POST /v1/invoke` only checks
the local binding, an Ed25519 compact JWS lease, and in-memory idempotency
before sending work to that already-running child.

Run the focused suite from the repository root:

```powershell
python -m unittest executor.runtime.tests.test_runtime
python -m unittest executor.runtime.tests.test_service
```

The HTTP interface is HTTP/1.1 and keeps connections alive. Its endpoints are
`POST /v1/control/assign`, `POST /v1/control/release`, `POST /v1/invoke`,
`GET /health`, and `GET /metrics`. Assignment input wraps the existing
`session-assignment`, `code-load`, and `catalog-entry` contract documents with
`source` field. The sanitized drawing context comes from the assignment schema.
Source bytes must hash to both
the code and artifact digests in all three documents.

Loopback listeners may use plaintext HTTP for local development and hermetic
tests. Every non-loopback listener requires `LEAF_INSTANT_RUNTIME_CONTROL_SECRET`,
an Ed25519 lease trust bundle, and these file paths:
`LEAF_INSTANT_RUNTIME_TLS_CERT_FILE`, `LEAF_INSTANT_RUNTIME_TLS_KEY_FILE`, and
`LEAF_INSTANT_RUNTIME_TLS_CLIENT_CA_FILE`. The server requires a client
certificate that chains to that client CA. Control plane clients use HTTPS with
`LEAF_INSTANT_RUNTIME_CLIENT_CA_FILE`,
`LEAF_INSTANT_RUNTIME_CLIENT_CERT_FILE`, and
`LEAF_INSTANT_RUNTIME_CLIENT_KEY_FILE`. When an assigned endpoint is a private
task IP, both the harness and control plane set
`LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME=executor.instant.internal` and verify
that certificate name. Certificate issuance and rotation are deployment
responsibilities.

Non-loopback startup also requires control-plane host registration. Configure
`LEAF_INSTANT_CONTROL_PLANE_URL`, `LEAF_INSTANT_EXECUTOR_ID`, slot and runtime
digests, the dedicated `LEAF_INSTANT_HOST_LIFECYCLE_SECRET`, and control-plane
mTLS client files. The endpoint may be explicit for local tests. On ECS it is
derived from the private `awsvpc` IPv4 address exposed by
`ECS_CONTAINER_METADATA_URI_V4`. Registration, per-slot readiness, and one
heartbeat complete before the server enters its normal serving loop.

The child strips its environment, has no parent credentials, denies filesystem
access, blocks network/subprocess/native-loading imports, allows only a small
standard-library import set, bounds output, and is replaced after a wall-time
timeout. Drawing context is loaded at assignment and passed as `intake` to
`run(intake, params)`. It is never fetched during invocation.

Lease verification uses the native `cryptography` Ed25519 provider so signature
checks do not consume the instant-call latency budget.

This is a trusted, read-only code profile. It is not hostile-code isolation and
does not claim live SLO evidence. The prototype does not use Redis, PostgreSQL,
broker, Docker, queues, object storage, or any credential-bearing client on the
direct invocation path.

## Contention timeouts: reviewed 2026-08-06, deliberately unchanged

PR #468 turned three hardcoded waits into constructor knobs and widened them
**for tests only**, leaving every deployed default alone. This is the review of
whether the deployed path needed the same headroom. It did not, and the knobs
keep their production defaults:

- `DEFAULT_CHILD_LOAD_TIMEOUT_SECONDS = 2.0` (child boot + source-load ack)
- `HttpRuntimeClient.request_timeout_seconds = 5.0`
- `HostRegistrationConfig.request_timeout_seconds = 5.0`

Measured, not assumed: the exact `_replace(restore=True)` rebind (cold CPython
spawn, then an immediate load with no warm-up grace) runs **0.199-0.400s,
median 0.304s** over 12 iterations at `pool_size=8` on a quiet 20-core host.
That is roughly 5x headroom under 2.0s. An attempt to reproduce a breach under
host saturation did **not** succeed and is reported as unproven rather than as
absence of risk: the harness's own positive control slowed only x1.12, so it was
not creating real contention.

Two things argue against widening anyway:

1. The deployed task is small (staging Fargate `cpu=512`, i.e. 0.5 vCPU,
   running `--pool-size 8`), so headroom there is genuinely tighter than the
   measurement above. That is an inference from task sizing, not an observed
   breach; nothing in production has ever reported one.
2. The two paths have opposite failure economics, and only one is a problem.
   On `assign`, a timeout **raises**, the control plane sees the failure and
   can place the session elsewhere: failing fast is correct, and widening would
   turn a fast failure into a slow one and mask a genuinely wedged child. On
   the post-replacement rebind, a timeout used to be **swallowed** and the
   binding lost for good.

So the fix is to the silence, not to the number. `_replace` now reports both
abandoned-rebind paths (the transport/timeout exception, and a child that
rejects the reloaded source) through `runtime_event_sink` as a
`slot_rebind_failed` JSON line on the container log plane, and counts them in
`instant_executor_rebind_failures_total` on `/metrics`. Revisit the 2.0s
default when that counter or that log line shows a real breach, which is now
possible to see.

`runtime_event_sink` is injectable, so the sink call is wrapped: this runs
inside `invoke()`'s own failure handling, and an escaping exception would cost
the caller its `DEADLINE_EXCEEDED` response and skip terminal accounting and
idempotency recording. Telemetry must not break the path it observes. The
counter is incremented before the sink runs, so even a sink that throws leaves
the failure visible on `/metrics`. The record is a fixed identifier-only
allowlist, held to the same payload-free discipline as the accounting emitter
and asserted against the fixture's real source, lease token, geometry ref,
content digest, and params.

## The capacity gauge

`health()` counts ready, bound, and total slots, and `/metrics` renders them in
Prometheus form. Both are **pull-only on :8088**, and the executor's ECS task
definition deliberately declares no `task_role_arn` ("Executor application code
cannot obtain AWS credentials"). Nothing scrapes that port. So for as long as
those were the only statements of free capacity, the free-slot count reached no
monitoring surface at all, and the terraform root's `CapacityAvailableSlots`
alarm named a metric nothing could ever publish.

`CapacitySampler` closes that without touching the credential boundary. It
drives `sample_capacity()` on a daemon thread, which writes `health()`'s own
numbers as one `capacity_sample` record through the same `runtime_event_sink`.
A log line is the only channel this process has that reaches AWS; the awslogs
driver already configured on the task carries it to CloudWatch Logs, and a
metric filter in the terraform root turns it into `CapacityAvailableSlots`.

Three details are load-bearing:

- **It samples on a timer, not on slot transitions.** An executor whose slots
  are all bound produces no transitions, and that is exactly the state the
  capacity alarm has to see. A gauge only alarms if it keeps arriving.
- **It samples once at start**, so a metric exists within a second of boot
  rather than one interval later. The interval defaults to 30s and is set by
  `LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS`.
- **The record is numbers and state only** — no tenant, session, assignment, or
  source value is in scope — and the sink call is wrapped for the same reason
  `_record_rebind_failure`'s is: a sampler thread that dies on a telemetry bug
  would silently stop publishing the gauge an alarm is watching, failing toward
  "quiet", which is the failure mode this exists to remove.

### Why the sampler cannot die quietly

Guarding the sink was not enough. `sample_capacity()` computes `health()` before
it reaches the sink, and `_run()` is a bare daemon thread, so a raise from
`health()` ended the thread for the life of the process. **No alarm sees that
state**, and that is the point: `capacity` stays green because the task is
alive, `registration` stays green because heartbeats continue, and
`capacity_slots` treats the missing data as `notBreaching`. That last choice is
correct for its stated purpose — when the whole executor dies the other two
alarms already fire, and a third would triple-page one outage — so a
sampler-only death has to be caught in the executor rather than in the alarm.

`_run()` therefore wraps the sample call, **and nothing else**: the deadline
arithmetic that keeps the sample's own cost out of the spacing has to run on the
failure path too, or a fast-failing sample would spin the loop.

A swallowed failure would publish exactly as much as a dead thread, so failures
are also stated:

- `capacity_sample_failed` carries `error_type` and `consecutive_failures`. It
  is emitted only when the run length is a power of two (1, 2, 4, 8, …), so the
  first failure is immediate and a permanently broken `health()` costs log2(n)
  lines instead of one per interval into the same log group the gauge uses.
- `capacity_sample_recovered` closes the run, so the newest failure line is
  bounded in time. A healthy sampler is otherwise silent about its own health.
  It carries `consecutive_failures: 0` and puts the run's length in
  `recovered_after_failures`, which keeps `consecutive_failures` meaning the
  same thing on both event types: one filter on `$.record.consecutive_failures`
  then sees the count climb while the sampler is blind and a 0 the moment it
  recovers, which is what lets an alarm on it clear.
- The exception **message** is deliberately omitted. The gauge record promises
  it carries no tenant, session, assignment, or source value; an uncontrolled
  exception string would put that promise back in play.
- The sampler holds its **own** sink rather than the supervisor's, because the
  thing it reports on is a supervisor whose `health()` just raised.

Publishing `consecutive_failures` as its own CloudWatch metric would close the
remaining gap — a sampler that runs but always fails still publishes no gauge,
so `capacity_slots` is still blind to it. That needs a second metric filter and
alarm in the terraform root and is **not** done; the event those would read is
what exists today.

**Companion change, in the terraform repo.** The metric filter that reads these
lines, and the alarm on the resulting metric, live there. The filter selects
`$.record.event_type = "capacity_sample"` and publishes `$.record.ready_slots`,
so both field names are a cross-repo contract; `test_runtime.py` pins the
record's exact field set and their JSON types for that reason. The ECS
`LiveTaskCount` alarm is kept alongside it rather than replaced, because the two
detect different failures: a dead task, versus a live task with every slot
bound.
