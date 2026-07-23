# Harness PostgreSQL shared state

The harness keeps file-backed sessions and grants as the default. PostgreSQL is
opt-in. Schema creation is an operator step and never occurs at application
startup.

Set `LEAF_HARNESS_SESSION_STORE=postgres` and provide
`LEAF_HARNESS_DATABASE_URL` (or `DATABASE_URL`) only after migration
`platform/migrations/0017_harness_sessions.sql` has run. This enables the
PostgreSQL session store and the per-tenant repository lease.

The repository lease has:

- one row per tenant
- a random owner token and monotonic generation
- a bounded TTL with heartbeat renewal
- advisory transaction locks for acquisition, provisioning, and commit
- an owner and generation check before a commit action runs

The author loop starts from `HEAD` with untracked files removed. It holds the
lease from checkout through Agent SDK edits, registry update, and Git commit.
Registry update and commit share one fenced transaction. Failed authoring and
one-off authoring reset to `HEAD`. A dead worker stops heartbeating, so another
worker can take over after expiry and clean its abandoned edits. Lease rows stay
present after release, so generation increases across every owner. A stale
owner token cannot commit.

Registered-tool lookup and broker execution hold the same tenant lease. Readers
therefore cannot observe partial or uncommitted authoring files.

## Production gate

`LEAF_GRANT_STORE=file` remains the default. Grant tokens are not stored in
PostgreSQL. The `vault` selector is still an unimplemented seam, not a secret
store.

Build authoring defaults to disabled. Set
`LEAF_HARNESS_AUTHORING_MODE=singleton` only on the one task that may accept
build requests. `LEAF_HARNESS_AUTHORING_MODE=fleet` fails closed until an AWS
Secrets Manager implementation backs `LEAF_GRANT_STORE=vault`. Remove that gate
only when the real provider has tests for tenant isolation, link, unlink,
rotation, and deletion.

To run the two-writer lease tests against a disposable database, set
`PG_REPO_LEASE_TEST_URL`. The tests prove lease exclusion, heartbeat renewal,
hard process death, dead-worker expiry, dirty-tree cleanup, and stale-owner
fencing.
