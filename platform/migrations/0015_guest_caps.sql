-- Fleet-wide daily guest upload caps.
-- IP addresses are stored only as versioned keyed-HMAC digests in counter_key.
-- The application deletes at most 100 expired rows after each accepted charge.
CREATE TABLE IF NOT EXISTS guest_upload_counters (
  namespace   TEXT NOT NULL,
  counter_key TEXT NOT NULL,
  value       BIGINT NOT NULL CHECK (value >= 0),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (namespace, counter_key)
);

CREATE INDEX IF NOT EXISTS idx_guest_upload_counters_updated
  ON guest_upload_counters (updated_at);
