CREATE TABLE IF NOT EXISTS author_quota_attempts (
  attempt_key TEXT PRIMARY KEY,
  counter_key TEXT NOT NULL,
  quota_limit BIGINT NOT NULL,
  accepted BOOLEAN NOT NULL,
  used BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_author_quota_attempts_created
  ON author_quota_attempts (created_at);
