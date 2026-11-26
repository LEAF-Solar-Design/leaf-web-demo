-- Narrow, revocable authority for at most two queued Branch dispatches.
CREATE TABLE IF NOT EXISTS customization_stage_grants (
  change_set_id TEXT PRIMARY KEY REFERENCES customization_change_sets(change_set_id),
  tenant_id TEXT NOT NULL,
  author_subject TEXT NOT NULL,
  tier TEXT NOT NULL,
  binding_digest TEXT NOT NULL,
  authority_session_id TEXT NOT NULL,
  authority_turn_id TEXT NOT NULL,
  issued_ms BIGINT NOT NULL,
  expires_ms BIGINT NOT NULL,
  dispatches INTEGER NOT NULL DEFAULT 0 CHECK (dispatches BETWEEN 0 AND 2),
  last_attempt INTEGER NOT NULL DEFAULT 0,
  revoked_ms BIGINT,
  revoked_reason TEXT,
  CHECK (expires_ms > issued_ms)
);
