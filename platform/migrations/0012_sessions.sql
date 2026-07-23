-- 0012_sessions.sql
-- Shared authority for conversational sessions, their ordered event stream,
-- and single-use approvals.

CREATE TABLE IF NOT EXISTS app_sessions (
  session_id       TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  drawing_id       TEXT NOT NULL,
  status           TEXT NOT NULL,
  created_at       DOUBLE PRECISION NOT NULL,
  updated_at       DOUBLE PRECISION NOT NULL,
  last_seq         BIGINT NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
  active_turn_id   TEXT,
  turn_started_at  DOUBLE PRECISION,
  active_turn_tier TEXT,
  UNIQUE (tenant_id, drawing_id)
);

CREATE TABLE IF NOT EXISTS app_session_events (
  session_id  TEXT NOT NULL REFERENCES app_sessions(session_id) ON DELETE CASCADE,
  seq         BIGINT NOT NULL CHECK (seq > 0),
  turn_id     TEXT,
  type        TEXT NOT NULL,
  data_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_app_session_events_recent
  ON app_session_events(session_id, seq DESC);

CREATE TABLE IF NOT EXISTS app_approvals (
  confirmation_id TEXT PRIMARY KEY,
  session_id      TEXT,
  tenant_id       TEXT,
  turn_id         TEXT,
  tool            TEXT,
  params_json     JSONB,
  capability      TEXT,
  rationale       TEXT,
  kind            TEXT,
  payload_json    JSONB,
  decided         BOOLEAN NOT NULL DEFAULT FALSE,
  approved        BOOLEAN,
  decided_by      TEXT,
  created_at      DOUBLE PRECISION NOT NULL,
  expires_at      DOUBLE PRECISION,
  consumed        BOOLEAN NOT NULL DEFAULT FALSE,
  CHECK (NOT consumed OR decided)
);

CREATE INDEX IF NOT EXISTS idx_app_approvals_session
  ON app_approvals(session_id, created_at DESC);
