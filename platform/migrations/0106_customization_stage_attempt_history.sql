-- Private, append-only snapshots before an explicit same-request retry.
-- Messages already passed the authoring service's safe failure-text policy.
CREATE TABLE IF NOT EXISTS customization_stage_attempt_history (
  tenant_id TEXT NOT NULL,
  change_set_id TEXT NOT NULL REFERENCES customization_change_sets(change_set_id),
  failed_version INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  reason_code TEXT,
  reason_message TEXT,
  retryable BOOLEAN NOT NULL,
  authority_session_id TEXT,
  authority_turn_id TEXT,
  finished_at TEXT,
  retry_authority_session_id TEXT NOT NULL,
  retry_authority_turn_id TEXT NOT NULL,
  PRIMARY KEY (tenant_id, change_set_id, failed_version, attempt)
);
