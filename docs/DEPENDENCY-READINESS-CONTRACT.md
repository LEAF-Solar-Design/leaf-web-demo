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
| `harness` | A harness URL is configured, or `LEAF_AUTHORED_EXECUTION=1` | Harness `GET /ready` (store readiness), falling back to `GET /health` only on a 404 |
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
agent state, and tenant state. The app process intentionally cannot mount the
grant or harness session stores, so the harness answers for them itself.

The harness's `GET /ready` inspects its own stores: the session store (file or
PostgreSQL, `LEAF_HARNESS_SESSION_STORE`) and the grant store
(`LEAF_GRANT_STORE`). A file store is probed by creating, syncing and removing
one uniquely named sentinel file in the store's existing directory; the probe
never creates the directory and removes its sentinel even when a later step
fails. It proves the directory accepts a create, write, sync and unlink, not that
every store file inside it is writable. A PostgreSQL store runs `SELECT 1` under
a statement timeout on a client from the store's own pool; a client still busy at
the probe's own timeout is destroyed, never returned to the pool. Both probes share one
deadline (1500 ms by default, clamped to 100 ms to 3 seconds); a probe still
running at the deadline is `timeout`. Store states are `ready`, `unavailable`,
`timeout`, or `not_configured`. The grant store is always required. A session
store the harness does not construct (no app URL or dispatch secret) is
`not_configured` and not required. A tenant with no grant is normal and never
makes the grant store unready. The harness answers HTTP 200 with `ready: true`
when every required store is ready and HTTP 503 with `ready: false` otherwise,
with `cache-control: no-store`. Concurrent calls share one in-flight probe and
the last result is reused for one second. The harness `GET /health` stays a
constant liveness answer that never depends on a store.

The app's `harness` dependency consumes the harness `GET /ready`. A 404 there
means a harness older than this contract (a rolling deploy) and falls back to
the harness `GET /health` under the previous rule within the same budget.
Neither request follows a redirect: any 3xx is `unavailable` and never takes
the fallback. Any other non-200 answer, a timeout, a body over 4096 bytes,
invalid JSON, or `ready` not exactly `true` is `unavailable`.

The harness readiness body carries only store kinds, states and requirement
flags. Like the app response, it never contains a probe URL, filesystem path,
connection string, database identity, credential, secret-presence fact,
exception message, grant, tenant id, or tenant data.

Public source revision selection accepts only a 7 to 64 character hexadecimal
Git SHA or `sha256:` followed by 64 hexadecimal characters. It uses the first
valid value from:
`LEAF_BUILD_REVISION`, `SOURCE_REVISION`, `GIT_COMMIT`,
`VERCEL_GIT_COMMIT_SHA`, or `RENDER_GIT_COMMIT`. Invalid values are omitted.

Worker freshness is independent of the app revision. When
`LEAF_EXPECTED_WORKER_REVISION` contains an immutable revision in the same
format, the worker heartbeat must match it. Without that setting, freshness is
the only worker revision condition.
