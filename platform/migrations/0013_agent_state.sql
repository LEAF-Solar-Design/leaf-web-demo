-- 0013_agent_state.sql
-- Fleet-wide authority for the conversational agent security gate.

CREATE TABLE IF NOT EXISTS agent_approvals (
  confirmation_id TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  session_id       TEXT NOT NULL,
  turn_id          TEXT NOT NULL,
  action           TEXT NOT NULL,
  args             JSONB NOT NULL,
  args_hash        TEXT NOT NULL,
  policy           TEXT NOT NULL,
  rung             INTEGER NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at       TIMESTAMPTZ NOT NULL,
  granted          BOOLEAN NOT NULL DEFAULT FALSE,
  denied           BOOLEAN NOT NULL DEFAULT FALSE,
  decided_at       TIMESTAMPTZ,
  decided_by       TEXT,
  reason           TEXT,
  consumed_at      TIMESTAMPTZ,
  CHECK (NOT (granted AND denied))
);
CREATE INDEX IF NOT EXISTS idx_agent_approvals_pending
  ON agent_approvals (tenant_id, expires_at)
  WHERE granted = FALSE AND denied = FALSE;

CREATE TABLE IF NOT EXISTS agent_session_grants (
  tenant_id   TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  action      TEXT NOT NULL,
  target_key  TEXT NOT NULL,
  granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, session_id, action, target_key)
);

CREATE TABLE IF NOT EXISTS agent_rate_counters (
  namespace   TEXT NOT NULL,
  counter_key TEXT NOT NULL,
  value       BIGINT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace, counter_key),
  CHECK (value >= 0)
);

CREATE TABLE IF NOT EXISTS agent_fleet_state (
  state_key  TEXT PRIMARY KEY,
  active     BOOLEAN NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by TEXT
);
INSERT INTO agent_fleet_state (state_key, active, reason)
VALUES ('global_kill', FALSE, '')
ON CONFLICT (state_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_gate_audit_events (
  event_id   BIGSERIAL PRIMARY KEY,
  tenant_id  TEXT,
  session_id TEXT,
  turn_id    TEXT,
  kind       TEXT NOT NULL,
  event      JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_gate_audit_tenant_created
  ON agent_gate_audit_events (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_gate_audit_session_created
  ON agent_gate_audit_events (session_id, created_at);

CREATE TABLE IF NOT EXISTS agent_tenant_state (
  tenant_id      TEXT PRIMARY KEY,
  agent_disabled BOOLEAN NOT NULL DEFAULT FALSE,
  overlay        JSONB NOT NULL DEFAULT '{}'::JSONB,
  revision       BIGINT NOT NULL DEFAULT 1,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (jsonb_typeof(overlay) = 'object'),
  CHECK (revision > 0)
);

CREATE TABLE IF NOT EXISTS agent_usage_turns (
  usage_key   TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  turn_id     TEXT NOT NULL,
  ts          TIMESTAMPTZ NOT NULL,
  record      JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, session_id, turn_id),
  CHECK (jsonb_typeof(record) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_agent_usage_tenant_ts
  ON agent_usage_turns (tenant_id, ts);
