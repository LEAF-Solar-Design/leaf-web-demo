ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS request_description TEXT,
  ADD COLUMN IF NOT EXISTS request_fingerprint CHAR(64),
  ADD COLUMN IF NOT EXISTS stage_attempt INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS stage_lease_owner TEXT,
  ADD COLUMN IF NOT EXISTS stage_lease_expires_at BIGINT,
  ADD COLUMN IF NOT EXISTS stage_heartbeat_at BIGINT,
  ADD COLUMN IF NOT EXISTS stage_next_attempt_at BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS stage_error_code TEXT,
  ADD COLUMN IF NOT EXISTS stage_error_retryable BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS stage_phase TEXT NOT NULL DEFAULT 'queued',
  ADD COLUMN IF NOT EXISTS stage_started_at TEXT,
  ADD COLUMN IF NOT EXISTS stage_finished_at TEXT;

CREATE INDEX IF NOT EXISTS customization_stage_claim_idx
  ON customization_change_sets
  (state, stage_next_attempt_at, stage_lease_expires_at);
