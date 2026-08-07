-- Operator control plane foundations (contract/OPERATOR.md sections 1-2, 5).
-- Server-owned operator principals, PG-only operator sessions/events, one-use
-- execution authorities, budgets, and the fail-closed security audit.
-- Expand-only: no contract-phase statement.

CREATE TABLE IF NOT EXISTS operator_principals (
  subject        TEXT PRIMARY KEY,
  role           TEXT NOT NULL DEFAULT 'operator',
  role_revision  INTEGER NOT NULL DEFAULT 1,
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'suspended', 'revoked')),
  profiles       TEXT[] NOT NULL DEFAULT ARRAY['default'],
  environment    TEXT NOT NULL DEFAULT 'staging',
  granted_by     TEXT,
  granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS operator_sessions (
  session_id       TEXT PRIMARY KEY,
  subject          TEXT NOT NULL,
  profile          TEXT NOT NULL,
  environment      TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'idle',
  active_turn_id   TEXT,
  turn_started_at  TIMESTAMPTZ,
  turn_subject     TEXT,
  turn_role_revision INTEGER,
  turn_profiles    TEXT[],
  turn_environment TEXT,
  sdk_session_id   TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subject, profile, environment)
);

CREATE TABLE IF NOT EXISTS operator_events (
  session_id  TEXT NOT NULL REFERENCES operator_sessions(session_id),
  seq         BIGINT NOT NULL,
  turn_id     TEXT,
  type        TEXT NOT NULL,
  data        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS operator_authorities (
  authority_id     TEXT PRIMARY KEY,
  subject          TEXT NOT NULL,
  role_revision    INTEGER NOT NULL,
  profile          TEXT NOT NULL,
  session_id       TEXT NOT NULL,
  turn_id          TEXT,
  action           TEXT NOT NULL,
  args_hash        TEXT NOT NULL,
  target_revision  TEXT,
  policy_revision  TEXT NOT NULL,
  environment      TEXT NOT NULL,
  minted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at       TIMESTAMPTZ NOT NULL,
  nonce            TEXT NOT NULL,
  max_uses         INTEGER NOT NULL DEFAULT 1,
  used_count       INTEGER NOT NULL DEFAULT 0,
  idempotency_key  TEXT,
  status           TEXT NOT NULL DEFAULT 'granted'
                   CHECK (status IN ('granted', 'consumed', 'expired', 'revoked'))
);

CREATE TABLE IF NOT EXISTS operator_budgets (
  subject     TEXT NOT NULL,
  scope       TEXT NOT NULL,   -- 'principal_day' | 'session' | 'action_hour'
  scope_key   TEXT NOT NULL,   -- date, session_id, or action@hour
  used        BIGINT NOT NULL DEFAULT 0,
  ceiling     BIGINT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (subject, scope, scope_key)
);

CREATE TABLE IF NOT EXISTS operator_security_audit (
  audit_id    BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  subject     TEXT NOT NULL,
  session_id  TEXT,
  turn_id     TEXT,
  action      TEXT NOT NULL,
  decision    TEXT NOT NULL,
  reason      TEXT NOT NULL,
  authority_id TEXT,
  args_hash   TEXT,
  policy_revision TEXT,
  environment TEXT,
  extra       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS operator_audit_subject_ts
  ON operator_security_audit (subject, ts DESC);
