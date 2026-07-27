-- 0020_customization_authority.sql
-- Shared PostgreSQL authority for tenant customization staging, publication,
-- confirmation consumption, effective catalog pins, and deployment rollback.

CREATE TABLE IF NOT EXISTS customization_change_sets (
  change_set_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'created', 'staging', 'staged', 'awaiting_approval', 'approved',
    'publishing', 'published', 'rejected', 'conflicted', 'failed',
    'superseded', 'rolled_back'
  )),
  version INTEGER NOT NULL CHECK (version >= 0),
  base_commit TEXT NOT NULL,
  staged_commit TEXT,
  catalog_digest TEXT,
  desired_platform_release TEXT NOT NULL,
  workspace_contract_digest TEXT NOT NULL,
  author_subject TEXT NOT NULL,
  approver_subject TEXT,
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  ),
  updated_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  ),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS effective_catalogs (
  tenant_id TEXT PRIMARY KEY,
  change_set_id TEXT NOT NULL REFERENCES customization_change_sets(change_set_id),
  catalog_commit TEXT NOT NULL,
  catalog_digest TEXT NOT NULL,
  effective_platform_release TEXT NOT NULL,
  workspace_contract_digest TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  )
);

CREATE TABLE IF NOT EXISTS customization_audit_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  change_set_id TEXT NOT NULL REFERENCES customization_change_sets(change_set_id),
  prior_state TEXT CHECK (
    prior_state IS NULL OR prior_state IN (
      'created', 'staging', 'staged', 'awaiting_approval', 'approved',
      'publishing', 'published', 'rejected', 'conflicted', 'failed',
      'superseded', 'rolled_back'
    )
  ),
  next_state TEXT NOT NULL CHECK (next_state IN (
    'created', 'staging', 'staged', 'awaiting_approval', 'approved',
    'publishing', 'published', 'rejected', 'conflicted', 'failed',
    'superseded', 'rolled_back'
  )),
  author_subject TEXT,
  approver_subject TEXT,
  base_commit TEXT,
  staged_commit TEXT,
  catalog_digest TEXT,
  platform_release TEXT,
  workspace_contract_digest TEXT,
  idempotency_key TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('ok', 'denied', 'failed')),
  reason_code TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  ),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS customization_recovery_idx
  ON customization_change_sets (state, tenant_id);

CREATE TABLE IF NOT EXISTS customization_confirmations (
  confirmation_id TEXT PRIMARY KEY,
  tenant_id TEXT,
  change_set_id TEXT,
  payload_json TEXT NOT NULL,
  signature TEXT NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  )
);

CREATE INDEX IF NOT EXISTS customization_confirmation_lookup_idx
  ON customization_confirmations (
    tenant_id, change_set_id, consumed, created_at DESC
  );

CREATE TABLE IF NOT EXISTS customization_publication_requests (
  tenant_id TEXT NOT NULL,
  change_set_id TEXT NOT NULL REFERENCES customization_change_sets(change_set_id),
  confirmation_id TEXT REFERENCES customization_confirmations(confirmation_id),
  status TEXT NOT NULL DEFAULT 'awaiting_approval',
  reason_code TEXT,
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  ),
  updated_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  ),
  PRIMARY KEY (tenant_id, change_set_id)
);

CREATE TABLE IF NOT EXISTS customization_deployment_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  effective_catalog_digest TEXT NOT NULL,
  platform_release TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  )
);

CREATE TABLE IF NOT EXISTS customization_deployment_audit (
  audit_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES customization_deployment_snapshots(snapshot_id),
  action TEXT NOT NULL CHECK (
    action IN ('snapshot', 'verify', 'restore', 'restore_verify')
  ),
  result TEXT NOT NULL CHECK (result IN ('ok', 'failed')),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  )
);
