-- PostgreSQL is the durable authority for warm-slot allocation and leases.
CREATE TABLE IF NOT EXISTS executor_hosts (
  host_id text PRIMARY KEY,
  state text NOT NULL,
  host_epoch bigint NOT NULL,
  public_key_fingerprint text NOT NULL DEFAULT '',
  capacity_total integer NOT NULL,
  capacity_ready integer NOT NULL DEFAULT 0,
  endpoint text NOT NULL DEFAULT '',
  last_heartbeat_at timestamptz,
  drain_deadline_at timestamptz,
  revoked_at timestamptz,
  version bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS executor_slots (
  host_id text NOT NULL REFERENCES executor_hosts(host_id),
  slot_id text NOT NULL,
  state text NOT NULL,
  slot_epoch bigint NOT NULL DEFAULT 1,
  code_digest text,
  runtime_digest text,
  current_claim_id uuid,
  last_ready_at timestamptz,
  version bigint NOT NULL DEFAULT 1,
  PRIMARY KEY(host_id, slot_id)
);

CREATE TABLE IF NOT EXISTS capacity_claims (
  claim_id uuid PRIMARY KEY,
  host_id text NOT NULL,
  slot_id text NOT NULL,
  owner_id text NOT NULL,
  claim_epoch bigint NOT NULL,
  state text NOT NULL,
  expires_at timestamptz NOT NULL,
  released_at timestamptz,
  version bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS instant_sessions (
  session_id uuid PRIMARY KEY,
  assignment_id uuid,
  tenant_id text NOT NULL,
  catalog_version text NOT NULL DEFAULT '',
  catalog_digest text NOT NULL DEFAULT '',
  code_digest text NOT NULL,
  artifact_digest text NOT NULL DEFAULT '',
  runtime_digest text NOT NULL DEFAULT '',
  capability jsonb NOT NULL DEFAULT '{}'::jsonb,
  host_id text NOT NULL,
  slot_id text NOT NULL,
  claim_id uuid NOT NULL,
  binding_epoch bigint NOT NULL,
  state text NOT NULL,
  lease_id uuid,
  lease_sequence bigint NOT NULL DEFAULT 0,
  expires_at timestamptz,
  last_activity_at timestamptz,
  invalidated_at timestamptz,
  reason text,
  version bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS executor_leases (
  lease_id uuid PRIMARY KEY,
  host_id text NOT NULL,
  slot_id text NOT NULL,
  host_epoch bigint NOT NULL,
  slot_epoch bigint NOT NULL,
  claim_id uuid NOT NULL,
  session_id uuid NOT NULL,
  binding_epoch bigint NOT NULL DEFAULT 1,
  lease_sequence bigint NOT NULL,
  not_before timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  state text NOT NULL,
  revoked_at timestamptz,
  version bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS control_outbox (
  event_id uuid PRIMARY KEY,
  kind text NOT NULL,
  entity_id text NOT NULL,
  entity_version bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);

-- These additive clauses make the migration safe for databases created by an
-- earlier control-plane prototype. No extension or custom SQL function is
-- required by this migration or by PostgresStore.
ALTER TABLE executor_hosts ADD COLUMN IF NOT EXISTS endpoint text NOT NULL DEFAULT '';
ALTER TABLE executor_slots ADD COLUMN IF NOT EXISTS current_claim_id uuid;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS assignment_id uuid;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS catalog_version text NOT NULL DEFAULT '';
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS catalog_digest text;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS artifact_digest text;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS runtime_digest text;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS capability jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS lease_sequence bigint NOT NULL DEFAULT 0;
ALTER TABLE instant_sessions ADD COLUMN IF NOT EXISTS last_activity_at timestamptz;
ALTER TABLE executor_leases ADD COLUMN IF NOT EXISTS binding_epoch bigint NOT NULL DEFAULT 1;

-- Backfill renamed or newly required durable fields without relying on an
-- extension-provided UUID generator.
UPDATE instant_sessions SET catalog_digest=catalog_version
WHERE catalog_digest IS NULL AND catalog_version IS NOT NULL;
UPDATE instant_sessions SET assignment_id=session_id WHERE assignment_id IS NULL;
UPDATE instant_sessions SET artifact_digest=code_digest WHERE artifact_digest IS NULL;
UPDATE instant_sessions SET runtime_digest='' WHERE runtime_digest IS NULL;
UPDATE instant_sessions SET catalog_digest='' WHERE catalog_digest IS NULL;

ALTER TABLE instant_sessions ALTER COLUMN assignment_id SET NOT NULL;
ALTER TABLE instant_sessions ALTER COLUMN catalog_digest SET NOT NULL;
ALTER TABLE instant_sessions ALTER COLUMN artifact_digest SET NOT NULL;
ALTER TABLE instant_sessions ALTER COLUMN runtime_digest SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim_per_slot
  ON capacity_claims(host_id, slot_id) WHERE state = 'ACTIVE';
CREATE INDEX IF NOT EXISTS executor_slots_ready_idx
  ON executor_slots(state, host_id, slot_id);
CREATE INDEX IF NOT EXISTS capacity_claims_expiry_idx
  ON capacity_claims(expires_at) WHERE state = 'ACTIVE';
CREATE INDEX IF NOT EXISTS instant_sessions_host_state_idx
  ON instant_sessions(host_id, state);
CREATE INDEX IF NOT EXISTS instant_sessions_reclaim_idx
  ON instant_sessions(state, expires_at, last_activity_at);
CREATE INDEX IF NOT EXISTS control_outbox_pending_idx
  ON control_outbox(kind, created_at) WHERE published_at IS NULL;
