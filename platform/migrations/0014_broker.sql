-- Shared broker tenant authority and immutable usage ledger.
-- The broker keeps its frozen nine-field ledger wire contract. event_key is
-- storage metadata used to make retries idempotent across broker tasks.

CREATE TABLE IF NOT EXISTS broker_tenants (
  tenant_id TEXT PRIMARY KEY,
  disabled BOOLEAN NOT NULL DEFAULT FALSE,
  tier TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broker_usage_ledger (
  event_key TEXT PRIMARY KEY,
  ts DOUBLE PRECISION NOT NULL,
  tenant_id TEXT NOT NULL,
  tool TEXT,
  engine_op TEXT NOT NULL,
  aps_endpoint TEXT NOT NULL,
  aps_live BOOLEAN NOT NULL,
  engine_seconds DOUBLE PRECISION,
  usd_est DOUBLE PRECISION,
  status TEXT NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (event_key <> ''),
  CONSTRAINT broker_usage_ledger_engine_seconds_nonnegative_finite
    CHECK (
      engine_seconds IS NULL
      OR (engine_seconds >= 0
        AND engine_seconds < 'Infinity'::DOUBLE PRECISION)
    ),
  CONSTRAINT broker_usage_ledger_usd_est_nonnegative_finite
    CHECK (
      usd_est IS NULL
      OR (usd_est >= 0 AND usd_est < 'Infinity'::DOUBLE PRECISION)
    )
);
CREATE INDEX IF NOT EXISTS broker_usage_ledger_tenant_ts_idx
  ON broker_usage_ledger(tenant_id, ts);

-- Admission state machine. A leased request can be reclaimed after its bounded
-- lease only while execution_started_at is NULL. Once state becomes executing,
-- automatic reclaim is forbidden because APS might already have accepted work.
CREATE TABLE IF NOT EXISTS broker_run_admissions (
  event_key TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
  state TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ,
  aps_live BOOLEAN NOT NULL,
  accounted_work BOOLEAN NOT NULL DEFAULT FALSE,
  reserved_usd DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
  execution_started_at TIMESTAMPTZ,
  result_json JSONB,
  http_status INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  terminal_at TIMESTAMPTZ,
  CHECK (event_key <> ''),
  CONSTRAINT broker_run_admissions_state_allowed
    CHECK (state IN ('leased', 'executing', 'terminal')),
  CONSTRAINT broker_run_admissions_state_shape CHECK (
    (state = 'leased' AND lease_expires_at IS NOT NULL
      AND execution_started_at IS NULL AND result_json IS NULL
      AND http_status IS NULL AND terminal_at IS NULL)
    OR
    (state = 'executing' AND lease_expires_at IS NULL
      AND execution_started_at IS NOT NULL AND result_json IS NULL
      AND http_status IS NULL AND terminal_at IS NULL)
    OR
    (state = 'terminal' AND lease_expires_at IS NULL
      AND result_json IS NOT NULL AND http_status IS NOT NULL
      AND terminal_at IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS broker_run_admissions_tenant_state_idx
  ON broker_run_admissions(tenant_id, state);

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'broker_run_admissions'::regclass
      AND conname = 'broker_run_admissions_event_tenant_uq'
  ) THEN
    ALTER TABLE broker_run_admissions
      ADD CONSTRAINT broker_run_admissions_event_tenant_uq
      UNIQUE (event_key, tenant_id);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS broker_aps_slots (
  event_key TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  state TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  lease_expires_at TIMESTAMPTZ NOT NULL,
  released_at TIMESTAMPTZ,
  release_reason TEXT,
  CONSTRAINT broker_aps_slots_admission_fk
    FOREIGN KEY (event_key, tenant_id)
    REFERENCES broker_run_admissions(event_key, tenant_id),
  CONSTRAINT broker_aps_slots_state_allowed
    CHECK (state IN ('held', 'released')),
  CONSTRAINT broker_aps_slots_state_shape CHECK (
    (state = 'held' AND released_at IS NULL AND release_reason IS NULL)
    OR
    (state = 'released' AND released_at IS NOT NULL AND release_reason IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS broker_aps_slots_held_idx
  ON broker_aps_slots(state, lease_expires_at);
DO $$ BEGIN
  ALTER TABLE broker_usage_ledger ADD CONSTRAINT broker_usage_ledger_admission_fk
    FOREIGN KEY (event_key, tenant_id)
    REFERENCES broker_run_admissions(event_key, tenant_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS broker_admission_resolution_audit (
  audit_id UUID PRIMARY KEY,
  event_key TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  resolution TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  prior_state TEXT NOT NULL,
  terminal_status INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT broker_admission_resolution_allowed
    CHECK (resolution IN ('confirmed_failed_no_charge', 'verified_terminal')),
  CONSTRAINT broker_admission_resolution_prior_state
    CHECK (prior_state = 'executing'),
  CONSTRAINT broker_admission_resolution_admission_fk
    FOREIGN KEY (event_key, tenant_id)
      REFERENCES broker_run_admissions(event_key, tenant_id)
);

CREATE OR REPLACE FUNCTION leaf_reject_broker_ledger_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable broker usage ledger record'
    USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS broker_usage_ledger_immutable ON broker_usage_ledger;
CREATE TRIGGER broker_usage_ledger_immutable
  BEFORE UPDATE OR DELETE ON broker_usage_ledger
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_broker_ledger_mutation();
DROP TRIGGER IF EXISTS broker_admission_resolution_audit_immutable
  ON broker_admission_resolution_audit;
CREATE TRIGGER broker_admission_resolution_audit_immutable
  BEFORE UPDATE OR DELETE ON broker_admission_resolution_audit
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_broker_ledger_mutation();
