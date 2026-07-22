-- Canonical PostgreSQL job authority for the first real Marathon solver.
-- Additive and idempotent: existing compatibility jobs remain readable.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS request_tenant_id TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS submission_fingerprint TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS execution_context JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS error JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provenance JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS terminal_fingerprint TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS terminal_conflict JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

DO $$ BEGIN
  ALTER TABLE jobs ADD CONSTRAINT jobs_worker_attempt_bounds
    CHECK (attempt >= 0 AND max_attempts > 0 AND attempt <= max_attempts);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_canonical_submission_idempotency_uq
  ON jobs(org_id, project_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS jobs_canonical_claim_idx
  ON jobs(status, lease_expires_at, created_at, job_id)
  WHERE deleted_at IS NULL AND status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS jobs_request_tenant_live_idx
  ON jobs(request_tenant_id, created_at DESC)
  WHERE deleted_at IS NULL AND request_tenant_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS canonical_worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  tool_name TEXT NOT NULL,
  runtime TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE canonical_worker_heartbeats ADD COLUMN IF NOT EXISTS source_sha256 TEXT;
DELETE FROM canonical_worker_heartbeats WHERE source_sha256 IS NULL;
ALTER TABLE canonical_worker_heartbeats ALTER COLUMN source_sha256 SET NOT NULL;
DO $$ BEGIN
  ALTER TABLE canonical_worker_heartbeats ADD CONSTRAINT canonical_worker_source_sha256_shape
    CHECK (source_sha256 ~ '^[0-9a-f]{64}$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS canonical_worker_health_idx
  ON canonical_worker_heartbeats(tool_name, observed_at DESC);
