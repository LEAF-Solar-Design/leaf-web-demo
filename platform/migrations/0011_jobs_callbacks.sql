-- Shared authority for application async jobs and callback replay protection.

CREATE TABLE IF NOT EXISTS async_jobs (
  job_id                  TEXT PRIMARY KEY,
  tenant_id               TEXT NOT NULL,
  tool                    TEXT NOT NULL,
  params_json             JSONB NOT NULL,
  dwg                     TEXT NOT NULL,
  status                  TEXT NOT NULL CHECK
                            (status IN ('submitted', 'running', 'complete', 'failed')),
  progress                TEXT,
  created_at              DOUBLE PRECISION NOT NULL,
  started_at              DOUBLE PRECISION,
  updated_at              DOUBLE PRECISION NOT NULL,
  finished_at             DOUBLE PRECISION,
  elapsed_ms              BIGINT,
  result_json             JSONB,
  error_json              JSONB,
  execution_json          JSONB NOT NULL,
  attempt                 INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  lease_owner             TEXT,
  lease_expires_at        DOUBLE PRECISION,
  heartbeat_at            DOUBLE PRECISION,
  provenance_json         JSONB,
  terminal_fingerprint    TEXT,
  terminal_conflict_json  JSONB,
  org_id                  TEXT,
  project_id              TEXT,
  authority_mode          TEXT NOT NULL DEFAULT 'legacy_sqlite',
  idempotency_key         TEXT,
  submission_fingerprint  TEXT NOT NULL,
  dwg_version             INTEGER,
  CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS async_jobs_project_idempotency_uq
  ON async_jobs(tenant_id, project_id, idempotency_key)
  WHERE project_id IS NOT NULL AND idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS async_jobs_tenant_created_idx
  ON async_jobs(tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS async_jobs_reclaim_idx
  ON async_jobs(status, lease_expires_at, updated_at)
  WHERE status IN ('submitted', 'running');

CREATE TABLE IF NOT EXISTS async_job_terminal_conflicts (
  job_id         TEXT NOT NULL REFERENCES async_jobs(job_id) ON DELETE CASCADE,
  fingerprint    TEXT NOT NULL,
  evidence_json  JSONB NOT NULL,
  received_at    DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (job_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS callback_consumed_nonces (
  job_id       TEXT NOT NULL,
  nonce        TEXT NOT NULL,
  expires_at   DOUBLE PRECISION NOT NULL,
  consumed_at  DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (job_id, nonce)
);

CREATE INDEX IF NOT EXISTS callback_consumed_nonces_expiry_idx
  ON callback_consumed_nonces(expires_at);
