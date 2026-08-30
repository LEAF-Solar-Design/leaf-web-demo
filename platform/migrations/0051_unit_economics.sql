-- Durable inputs for pricing and unit-economics decisions.
--
-- Both ledgers are append-only. Stripe identifiers and provider invoice
-- references are stored only as SHA-256 digests so the reporting surface can
-- prove idempotency without turning operational reports into identifier dumps.

CREATE TABLE IF NOT EXISTS billing_subscription_events (
  event_key                   TEXT PRIMARY KEY,
  org_id                      UUID NOT NULL,
  stripe_event_type           TEXT,
  stripe_event_ref_sha256     TEXT,
  subscription_ref_sha256     TEXT,
  plan                        TEXT,
  subscription_active         BOOLEAN,
  subscription_status         TEXT,
  current_period_start        TIMESTAMPTZ,
  current_period_end          TIMESTAMPTZ,
  previous_tier               TEXT NOT NULL,
  derived_tier                TEXT NOT NULL,
  tier_changed                BOOLEAN NOT NULL,
  payload_sha256              TEXT NOT NULL,
  observed_at                 TIMESTAMPTZ NOT NULL,
  recorded_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT billing_subscription_events_event_key_check
    CHECK (event_key ~ '^[0-9a-f]{64}$'),
  CONSTRAINT billing_subscription_events_stripe_event_ref_check
    CHECK (stripe_event_ref_sha256 IS NULL OR stripe_event_ref_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT billing_subscription_events_subscription_ref_check
    CHECK (subscription_ref_sha256 IS NULL OR subscription_ref_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT billing_subscription_events_payload_check
    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT billing_subscription_events_period_check
    CHECK (
      current_period_start IS NULL
      OR current_period_end IS NULL
      OR current_period_start < current_period_end
    )
);

CREATE INDEX IF NOT EXISTS idx_billing_subscription_events_period
  ON billing_subscription_events(observed_at, event_key);
CREATE INDEX IF NOT EXISTS idx_billing_subscription_events_org_period
  ON billing_subscription_events(org_id, observed_at);

CREATE TABLE IF NOT EXISTS unit_economics_observations (
  observation_key             TEXT PRIMARY KEY,
  period_start                TIMESTAMPTZ NOT NULL,
  period_end                  TIMESTAMPTZ NOT NULL,
  kind                        TEXT NOT NULL,
  category                    TEXT NOT NULL,
  amount_usd                  NUMERIC(18,6) NOT NULL,
  quantity                    NUMERIC(18,6),
  unit                        TEXT,
  source                      TEXT NOT NULL,
  source_ref_sha256           TEXT,
  payload_sha256              TEXT NOT NULL,
  metadata                    JSONB NOT NULL DEFAULT '{}'::jsonb,
  recorded_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT unit_economics_observations_key_check
    CHECK (observation_key ~ '^[0-9a-f]{64}$'),
  CONSTRAINT unit_economics_observations_period_check
    CHECK (period_start < period_end),
  CONSTRAINT unit_economics_observations_kind_check
    CHECK (kind IN ('shared_fixed', 'usage_variable', 'revenue')),
  CONSTRAINT unit_economics_observations_category_check
    CHECK (char_length(btrim(category)) BETWEEN 1 AND 100),
  CONSTRAINT unit_economics_observations_amount_check
    CHECK (amount_usd >= 0 AND amount_usd <> 'Infinity'::numeric
      AND amount_usd <> 'NaN'::numeric),
  CONSTRAINT unit_economics_observations_quantity_check
    CHECK (quantity IS NULL OR (quantity >= 0 AND quantity <> 'Infinity'::numeric
      AND quantity <> 'NaN'::numeric)),
  CONSTRAINT unit_economics_observations_unit_check
    CHECK (unit IS NULL OR char_length(btrim(unit)) BETWEEN 1 AND 50),
  CONSTRAINT unit_economics_observations_source_check
    CHECK (char_length(btrim(source)) BETWEEN 1 AND 100),
  CONSTRAINT unit_economics_observations_source_ref_check
    CHECK (source_ref_sha256 IS NULL OR source_ref_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT unit_economics_observations_payload_check
    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT unit_economics_observations_metadata_check
    CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_unit_economics_observations_period
  ON unit_economics_observations(period_start, period_end, kind, category);

CREATE OR REPLACE FUNCTION leaf_reject_unit_economics_ledger_mutation()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable unit economics ledger record'
    USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER billing_subscription_events_immutable
  BEFORE UPDATE OR DELETE ON billing_subscription_events
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_unit_economics_ledger_mutation();

CREATE OR REPLACE TRIGGER unit_economics_observations_immutable
  BEFORE UPDATE OR DELETE ON unit_economics_observations
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_unit_economics_ledger_mutation();
