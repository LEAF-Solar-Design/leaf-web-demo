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

## Production gaps

Ed25519 uses the reviewed `cryptography` provider. Production key custody still
needs KMS or HSM integration and rotation. Add full connection configuration,
mTLS, authenticated host registration, an outbox publisher, reconciliation,
key rotation, observability, and a production WSGI server before deployment.

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
