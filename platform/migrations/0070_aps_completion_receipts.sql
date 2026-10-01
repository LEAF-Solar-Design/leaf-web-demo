-- Expand only: dispatch binding and immutable, recoverable completion receipts.
ALTER TABLE async_jobs ADD COLUMN IF NOT EXISTS aps_workitem_id TEXT;
ALTER TABLE async_jobs ADD COLUMN IF NOT EXISTS aps_workitem_attempt INTEGER;

CREATE TABLE IF NOT EXISTS aps_completion_receipts (
  job_id TEXT NOT NULL REFERENCES async_jobs(job_id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL CHECK (attempt > 0),
  workitem_id TEXT NOT NULL CHECK (length(workitem_id) > 0),
  body BYTEA NOT NULL CHECK (octet_length(body) > 0),
  timestamp TEXT NOT NULL,
  nonce TEXT NOT NULL,
  signature TEXT NOT NULL,
  reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (job_id, attempt)
);
