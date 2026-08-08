-- Operator credential-rotation state (contract/OPERATOR.md section 4.1,
-- operator.worker_credential_rotate; Wave 3). One row per broker credential
-- handle, carrying the monotonic rotation revision that the one-use execution
-- authority binds to (target_revision), so a rotation approved against one
-- revision cannot redeem after a concurrent rotation moved it. No secret value
-- is ever stored here: the row records only that a rotation happened and when.
-- Expand-only: no contract-phase statement.

CREATE TABLE IF NOT EXISTS operator_credential_rotations (
  handle       TEXT PRIMARY KEY,
  revision     BIGINT NOT NULL DEFAULT 0,
  rotated_at   TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
