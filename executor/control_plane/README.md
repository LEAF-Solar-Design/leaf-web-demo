# Instant execution control plane prototype

This Python 3.12 package allocates a ready executor slot, asks that executor to
load the signed artifact, and returns a contract-valid session assignment only
after the executor confirms `ready`. It never imports,
loads, or runs user code. It has no `/v1/invoke` route and never proxies an
invocation.

## Interfaces

- `POST /v1/sessions` accepts the trusted artifact and creates a session binding.
- Host lifecycle routes register, readiness, heartbeat, drain, release,
  invalidation, renewal, and code-change rebind.
- `GET /.well-known/jwks.json` returns the public Ed25519 trust bundle only.
- The runtime client calls only `POST /v1/control/assign` and
  `POST /v1/control/release` on an executor.

`InMemoryStore` is deterministic for hermetic tests. `PostgresStore` is the
durable-authority boundary. Apply `migrations/001_control_plane.sql` before
using it. The adapter uses only direct DB-API transactions and does not depend
on migration-installed SQL functions. PostgreSQL loss rejects all state
changes. Existing leases can still be checked locally by executors until their
expiry.

## Run tests

```powershell
python -m unittest discover -s executor/control_plane/tests -v
python executor/contracts/validate_contracts.py
python -m compileall executor/control_plane
```

To run the opt-in integration test against a dedicated PostgreSQL database,
set `POSTGRES_CONTROL_PLANE_TEST_URL`. The test creates and drops an isolated
schema. It never reads a general application database URL.

## Production composition

`main.py` is development-only. It uses in-memory state and local test doubles.
Do not run it in production.

Install this package's requirements, apply `migrations/001_control_plane.sql`,
then run the WSGI factory with Gunicorn:

```bash
gunicorn 'executor.control_plane.production:create_wsgi_application()'
```

The factory fails before it accepts a request unless all of these values are
set and safe:

| Variable | Requirement |
| --- | --- |
| `LEAF_INSTANT_CONTROL_DATABASE_URL` | A `postgres://` or `postgresql://` URL with `sslmode=verify-full`. |
| `LEAF_INSTANT_CONTROL_REDIS_URL` | A TLS `rediss://` URL. |
| `LEAF_INSTANT_CONTROL_API_SECRET` | At least 32 non-whitespace characters. |
| `LEAF_INSTANT_HOST_LIFECYCLE_SECRET` | A different 32-character secret used only by executor registration, readiness, and heartbeat. |
| `LEAF_INSTANT_RUNTIME_CONTROL_SECRET` | At least 32 non-whitespace characters. |
| `LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME` | Verified executor certificate identity, normally `executor.instant.internal`. |
| `LEAF_INSTANT_EXECUTOR_CIDRS` | Comma-separated canonical private IPv4 pool CIDRs, such as the staging VPC CIDR. |
| `LEAF_INSTANT_EXECUTOR_PORT` | Exact registered executor port, default `8088`. |
| `LEAF_INSTANT_RUNTIME_CLIENT_CA_FILE` | Absolute mounted CA bundle used to verify executor hosts. |
| `LEAF_INSTANT_RUNTIME_CLIENT_CERT_FILE` | Absolute mounted control-plane client certificate. |
| `LEAF_INSTANT_RUNTIME_CLIENT_KEY_FILE` | Absolute mounted private client key. |
| `LEAF_INSTANT_LEASE_SIGNING_SEED_FILE` | Absolute path to a mounted private file containing exactly 32 raw bytes. |
| `LEAF_INSTANT_LEASE_SIGNING_KEY_ID` | Optional safe public key identifier. |

On POSIX, the seed file must not permit group or world access. The seed is read
only at process creation. It is not accepted from an environment variable and
is never logged or returned by the API.

Run the separate reconciler under process supervision:

```bash
python -m executor.control_plane.reaper_main
```

`LEAF_INSTANT_REAPER_INTERVAL_SECONDS` defaults to 30 and must be from 1 to
300. `LEAF_INSTANT_REAPER_IDLE_TIMEOUT_SECONDS` is optional and, when set,
reclaims idle bindings after that many seconds. The worker retries a failed
pass after one interval. Its durable outbox makes a repeated executor release
safe.

## Remaining production gaps

Ed25519 uses the reviewed `cryptography` provider. Production key custody still
needs KMS or HSM integration and rotation. Staging must prove certificate and
key rotation, host-specific routing, and observability before production.

## Lifecycle and reclamation

The app keeps only a bounded LRU cache of opaque assignments. It renews an
assignment before half of its lease life, without Redis in the invocation path.
A failed renewal can use the old assignment only until its signed expiry.
At or after expiry it removes the cache entry and reports no instant route.
Concurrent prepare or renewal work for one session is single-flight, so it
does not claim a second slot.

Run `ControlPlane.reconcile()` from a scheduled worker. It reads durable store
state only, marks expired or idle bindings terminal, then sends the executor a
release command. Redis is never a reclaim source of truth. `PostgresStore`
fences eligible sessions, releases their claims, and writes retryable
executor-release outbox records in one transaction. A slot remains
`RELEASING` until the executor accepts cleanup, then `complete_reclaim` makes
it ready. The in-memory fallback exists only for hermetic tests.

There is no public session close or archive route in the live API. Explicit
close remains parked until the app session contract defines it. Expiry and the
scheduled reconciler reclaim capacity in the meantime.
