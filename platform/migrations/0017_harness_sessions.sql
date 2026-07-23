-- 0017_harness_sessions.sql
-- Fleet-safe durable authority for the TypeScript harness SessionStore.

CREATE TABLE IF NOT EXISTS harness_sessions (
  session_id      UUID PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  drawing_id      TEXT NOT NULL,
  sdk_session_id  TEXT,
  status          TEXT NOT NULL
                  CHECK (status IN ('idle', 'active', 'dormant', 'archived')),
  summary         TEXT,
  created_at      TIMESTAMPTZ NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS harness_one_active_session
  ON harness_sessions (tenant_id, drawing_id)
  WHERE status <> 'archived';

CREATE TABLE IF NOT EXISTS harness_turns (
  turn_id     TEXT NOT NULL,
  session_id  UUID NOT NULL
              REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
  seq_start   BIGINT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('active', 'complete', 'failed')),
  stop_reason TEXT,
  started_at  TIMESTAMPTZ NOT NULL,
  ended_at    TIMESTAMPTZ,
  PRIMARY KEY (session_id, turn_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS harness_one_active_turn
  ON harness_turns (session_id)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS harness_events (
  session_id UUID NOT NULL
             REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
  seq        BIGINT NOT NULL,
  turn_id    TEXT NOT NULL,
  type       TEXT NOT NULL,
  data       JSONB NOT NULL,
  ts         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS harness_confirmations (
  confirmation_id TEXT PRIMARY KEY,
  session_id       UUID NOT NULL
                   REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
  turn_id          TEXT NOT NULL,
  action           TEXT NOT NULL,
  args_json        TEXT NOT NULL,
  kind             TEXT NOT NULL,
  status           TEXT NOT NULL
                   CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
  created_at       TIMESTAMPTZ NOT NULL,
  expires_at       TIMESTAMPTZ NOT NULL,
  decided_at       TIMESTAMPTZ,
  decided_by       TEXT
);

CREATE TABLE IF NOT EXISTS harness_usage (
  usage_id   BIGSERIAL PRIMARY KEY,
  session_id UUID NOT NULL
             REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
  turn_id    TEXT NOT NULL,
  usage      JSONB NOT NULL,
  ts         TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_events_turn
  ON harness_events (session_id, turn_id, seq);

CREATE INDEX IF NOT EXISTS idx_harness_confirmations_session
  ON harness_confirmations (session_id, status, expires_at);

CREATE INDEX IF NOT EXISTS idx_harness_usage_session
  ON harness_usage (session_id, ts);

CREATE TABLE IF NOT EXISTS harness_tenant_repo_leases (
  tenant_id     TEXT PRIMARY KEY,
  owner_token   UUID NOT NULL,
  generation    BIGINT NOT NULL CHECK (generation > 0),
  acquired_at   TIMESTAMPTZ NOT NULL,
  heartbeat_at  TIMESTAMPTZ NOT NULL,
  expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_tenant_repo_leases_expiry
  ON harness_tenant_repo_leases (expires_at);
