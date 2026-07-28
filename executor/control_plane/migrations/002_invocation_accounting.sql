-- Durable accounting authority for instant executor invocations.  These rows
-- deliberately contain identifiers, state, and bounded metering only.  Request
-- and result payloads must remain outside the control-plane database.
CREATE TABLE IF NOT EXISTS instant_invocations (
  invocation_id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  session_id uuid NOT NULL,
  lease_id uuid NOT NULL,
  code_digest text NOT NULL,
  state text NOT NULL CHECK (state IN ('accepted', 'started', 'succeeded', 'failed')),
  accepted_at timestamptz NOT NULL,
  started_at timestamptz,
  terminal_at timestamptz,
  version bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'accepted' AND started_at IS NULL AND terminal_at IS NULL)
      OR (state = 'started' AND started_at IS NOT NULL AND terminal_at IS NULL)
      OR (state IN ('succeeded', 'failed') AND started_at IS NOT NULL AND terminal_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS instant_accounting (
  accounting_id uuid PRIMARY KEY,
  invocation_id uuid NOT NULL UNIQUE REFERENCES instant_invocations(invocation_id),
  tenant_id text NOT NULL,
  session_id uuid NOT NULL,
  lease_id uuid NOT NULL,
  code_digest text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
  recorded_at timestamptz NOT NULL,
  cpu_ms bigint NOT NULL CHECK (cpu_ms BETWEEN 0 AND 86400000),
  wall_ms bigint NOT NULL CHECK (wall_ms BETWEEN 0 AND 86400000),
  memory_peak_bytes bigint NOT NULL CHECK (memory_peak_bytes BETWEEN 0 AND 68719476736),
  input_bytes bigint NOT NULL CHECK (input_bytes BETWEEN 0 AND 67108864),
  output_bytes bigint NOT NULL CHECK (output_bytes BETWEEN 0 AND 67108864)
);

CREATE INDEX IF NOT EXISTS instant_accounting_tenant_time_idx
  ON instant_accounting(tenant_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS instant_accounting_session_time_idx
  ON instant_accounting(session_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS instant_accounting_outbox_pending_idx
  ON control_outbox(created_at, event_id)
  WHERE published_at IS NULL AND kind = 'instant.accounting.recorded';
CREATE INDEX IF NOT EXISTS instant_invocations_terminal_state_idx
  ON instant_invocations(state, terminal_at DESC)
  WHERE state IN ('succeeded', 'failed');
