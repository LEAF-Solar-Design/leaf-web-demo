# Dependency readiness contract

`GET /api/health` is the app liveness endpoint. Its status code and response
fields are unchanged. A process supervisor can use it without restarting a
healthy app because an optional or remote dependency is down.

`GET /api/ready` is the dependency readiness endpoint. It returns HTTP 200 when
every required dependency is ready and HTTP 503 otherwise. It checks these
stable dependency classes:

| Name | Required when | Probe |
|---|---|---|
| `broker` | Always | Public broker health response |
| `harness` | A harness URL is configured, or `LEAF_AUTHORED_EXECUTION=1` | Public harness health response |
| `database` | `LEAF_PLATFORM_POSTGRES_REQUIRED=1` | PostgreSQL connection and required schema |
| `worker` | `LEAF_CANONICAL_WORKER_REQUIRED=1` | Fresh canonical worker heartbeat |
| `durable_stores` | Always | App state locations have a writable directory |
| `build` | `LEAF_BUILD_REVISION_REQUIRED=1` | A valid source revision is present |

Each dependency has one state: `ready`, `timeout`, `unavailable`, or
`degraded`. An optional unconfigured dependency is `degraded`. Optional
degradation keeps HTTP 200 but sets the top-level status to `degraded`.

The total probe budget defaults to 750 ms and is clamped between 50 ms and
2 seconds. `LEAF_READINESS_TIMEOUT_S` can select a value within that range.
Network probes also use the bounded timeout. Probes run through one
process-wide six-thread bulkhead. A short cache and single-flight guard coalesce
concurrent calls. A six-slot nonblocking admission guard sits in front of the
executor. When all slots are held, a probe becomes `timeout` without entering
the work queue. Repeated timeouts therefore cannot grow readiness threads or
queued probe work.

The response contains only dependency names, states, requirement flags,
latency, total duration, and a validated source revision. It never contains a
probe URL, filesystem path, database identity, credential, secret-presence
fact, exception message, worker identity, or tenant data.

The durable-store check covers app-accessible jobs, drawings, guest drawings,
agent state, and tenant state. It does not inspect grants or harness sessions
because the app process intentionally cannot mount those stores.

Public source revision selection accepts only a 7 to 64 character hexadecimal
Git SHA or `sha256:` followed by 64 hexadecimal characters. It uses the first
valid value from:
`LEAF_BUILD_REVISION`, `SOURCE_REVISION`, `GIT_COMMIT`,
`VERCEL_GIT_COMMIT_SHA`, or `RENDER_GIT_COMMIT`. Invalid values are omitted.

Worker freshness is independent of the app revision. When
`LEAF_EXPECTED_WORKER_REVISION` contains an immutable revision in the same
format, the worker heartbeat must match it. Without that setting, freshness is
the only worker revision condition.
