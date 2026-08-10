-- 0035 P5a durable request and queued-turn journal.
--
-- The session transcript remains the conversation record. This table is the
-- request lifecycle authority that makes message admission, queue handoff,
-- crash settlement, and idempotent terminal replay durable across app task
-- replacement. It stores no credentials or image bytes.

CREATE TABLE IF NOT EXISTS app_session_requests (
  request_id       TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  drawing_id       TEXT NOT NULL,
  session_id       TEXT NOT NULL,
  principal_key    TEXT NOT NULL,
  payload_digest   TEXT NOT NULL,
  recoverable_json JSONB,
  state            TEXT NOT NULL,
  turn_id          TEXT,
  lease_owner      TEXT,
  lease_expires_at DOUBLE PRECISION,
  response_status  INTEGER,
  response_json    JSONB,
  created_at       DOUBLE PRECISION NOT NULL,
  updated_at       DOUBLE PRECISION NOT NULL,
  terminal_at      DOUBLE PRECISION,
  CONSTRAINT app_session_requests_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES app_sessions(session_id) ON DELETE CASCADE,
  CONSTRAINT app_session_requests_payload_digest_check
    CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
  CONSTRAINT app_session_requests_state_check
    CHECK (state IN (
      'admitted', 'queued', 'executing',
      'completed', 'failed', 'abandoned'
    )),
  CONSTRAINT app_session_requests_response_status_check
    CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599),
  CONSTRAINT app_session_requests_queued_payload_present_check
    CHECK (state <> 'queued' OR recoverable_json IS NOT NULL),
  CONSTRAINT app_session_requests_nonqueued_payload_absent_check
    CHECK (state = 'queued' OR recoverable_json IS NULL),
  CONSTRAINT app_session_requests_executing_lease_shape_check
  CHECK (
    state <> 'executing'
    OR (turn_id IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CONSTRAINT app_session_requests_terminal_result_shape_check
  CHECK (
    state NOT IN ('completed', 'failed', 'abandoned')
    OR (terminal_at IS NOT NULL AND response_status IS NOT NULL AND response_json IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_app_session_requests_one_executing
  ON app_session_requests(session_id)
  WHERE state = 'executing';

CREATE UNIQUE INDEX IF NOT EXISTS idx_app_session_requests_one_queued
  ON app_session_requests(session_id)
  WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS idx_app_session_requests_recovery
  ON app_session_requests(state, lease_expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_app_session_requests_scope
  ON app_session_requests(tenant_id, drawing_id, session_id, created_at DESC);
