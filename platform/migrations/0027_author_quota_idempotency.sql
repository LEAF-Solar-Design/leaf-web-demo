CREATE TABLE IF NOT EXISTS author_quota_attempts (
  tenant_id TEXT NOT NULL,
  attempt_key TEXT NOT NULL,
  counter_key TEXT NOT NULL,
  quota_day TEXT NOT NULL,
  quota_tier TEXT NOT NULL,
  quota_limit BIGINT NOT NULL,
  accepted BOOLEAN NOT NULL,
  used BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, attempt_key)
);

CREATE INDEX IF NOT EXISTS idx_author_quota_attempts_counter
  ON author_quota_attempts (counter_key);

CREATE TRIGGER author_quota_attempts_immutable
  BEFORE UPDATE OR DELETE ON author_quota_attempts
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
