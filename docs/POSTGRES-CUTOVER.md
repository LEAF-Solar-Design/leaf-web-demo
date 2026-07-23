# PostgreSQL production cutover

The repository contains safe prerequisites for PostgreSQL, but production
authority has not moved. The default remains the current SQLite and file-backed
stores.

## Current authority inventory

| Mutable authority | Current store | PostgreSQL schema today | Cutover state |
|---|---|---|---|
| Canonical orgs, projects, drawings, jobs, built tools | PostgreSQL when opted in | migrations 0001 to 0010 | Implemented, opt-in |
| Canonical solve leases, attempts, pins, history, evidence, compliance and review records | PostgreSQL | migrations 0003 to 0010 | Implemented, opt-in |
| Async non-canonical jobs | `JOBS_DB` SQLite | none | Not migrated |
| App sessions, events and approval consumption | `SESSIONS_DB` SQLite | none | Not migrated |
| Agent pending approvals, session grants and rate buckets | JSON files under `LEAF_AGENT_*` | none | Not migrated |
| Harness conversations and confirmation mirrors | files under `LEAF_SESSIONS_DIR` | none | Not migrated |
| Claude grants | private files under `LEAF_GRANTS_DIR` | none, use a vault rather than ordinary tables | Not migrated |
| Broker tenant kill switches and usage ledger | `BROKER_TENANTS` JSON and `BROKER_LEDGER` JSONL | none | Not migrated |
| Guest upload quotas, markers and purge ledger | process memory and files | none | Not migrated |
| Tenant repositories and drawing artifacts | filesystem or EFS | metadata exists for canonical drawing artifacts only | Not migrated |

Do not set a tenant or project to `postgres_canonical` unless the migration job,
canonical worker, API, and rollback path have passed the gates below.

## Image and migration contract

The migration container applies every numbered migration and then calls
`db.assert_schema_current()`. The API can use the same check at startup by
setting `LEAF_PLATFORM_POSTGRES_REQUIRED=1`. That switch also requires:

- `LEAF_AUTH_LIVE=1`
- `DATABASE_URL` supplied directly in the process environment
- a reachable PostgreSQL database with the required 0001 to 0010 schema

The canonical worker always checks the required schema before it starts its
claim loop. The check does not print the connection string.

Capture a credential-free reconciliation snapshot from an image that has the
same platform package as the API:

```shell
python -c "import json,sys; sys.path.insert(0,'/app/platform'); import db; print(json.dumps(db.reconciliation_snapshot(),sort_keys=True))"
```

The snapshot contains schema status, aggregate record counts and authority-mode
counts. It contains no tenant records or connection string. Store it with the
cutover evidence before and after each backfill.

For a local rehearsal:

```shell
docker compose -f docker-compose.yml -f docker-compose.canonical.yml up \
  --build --abort-on-container-exit migrate
docker compose -f docker-compose.yml -f docker-compose.canonical.yml up -d \
  canonical-worker app
```

The local overlay is not a production database design.

## Operator decisions still required

Choose and record:

1. Managed PostgreSQL provider, region, version, availability level and size.
2. Connection mode, TLS verification, secret rotation and pool limits.
3. Backup retention, point-in-time recovery, RPO, RTO and restore drill owner.
4. Maintenance window, monitoring thresholds and on-call alerts.
5. Migration identity, application identity and least-privilege grants.
6. Which non-canonical authorities move into PostgreSQL and which move to a
   purpose-built store or vault.
7. Dual-write duration, backfill window, conflict policy and rollback deadline.

## Production gates

1. Provision the database and secret through reviewed infrastructure code.
2. Run migrations through a protected one-shot task.
3. Run `assert_schema_current()` from the migration, API and worker images.
4. Pass the full PostgreSQL integration suite against an empty database and an
   upgraded copy.
5. Backfill one authority at a time. Compare tenant-scoped counts and stable
   record fingerprints before enabling shadow reads.
6. Prove dual-write failure handling and replay without duplicate jobs,
   approvals or quota charges.
7. Switch authority for a test tenant, then a production canary tenant.
8. Prove task replacement, lease expiry, stale-worker rejection, backup restore
   and rollback to the old authority.
9. Move all remaining single-writer state or document why it is safe.
10. Only then run two ECS tasks, enable automatic rollback and measure a shorter
    target drain interval.

The 300-second drain and one-task deployment remain safety controls until the
single-writer authorities in the inventory are removed.
