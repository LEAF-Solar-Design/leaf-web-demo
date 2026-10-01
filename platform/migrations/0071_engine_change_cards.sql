-- Expand only: durable controller acceptance cards and per-operator read receipts.
-- Rollback: stop publishing/serving cards and retain these additive tables.
CREATE TABLE IF NOT EXISTS engine_change_cards (
  card_id UUID PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE CHECK (char_length(operation_id) BETWEEN 1 AND 256),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  state TEXT NOT NULL CHECK (state IN ('accepted', 'landed', 'live', 'reverted', 'held')),
  incident_fingerprint TEXT NOT NULL CHECK (char_length(incident_fingerprint) BETWEEN 1 AND 256),
  feature_id TEXT NOT NULL CHECK (char_length(feature_id) BETWEEN 1 AND 256),
  title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  summary TEXT NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 4000),
  change JSONB NOT NULL CHECK (jsonb_typeof(change) = 'object' AND octet_length(change::text) <= 32768),
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object' AND octet_length(evidence::text) <= 32768),
  acceptance JSONB NOT NULL CHECK (jsonb_typeof(acceptance) = 'object' AND octet_length(acceptance::text) <= 32768),
  deployment_identity TEXT CHECK (char_length(deployment_identity) BETWEEN 1 AND 256),
  hold_requested_at TIMESTAMPTZ,
  hold_requested_by TEXT CHECK (char_length(hold_requested_by) BETWEEN 1 AND 256),
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK ((hold_requested_at IS NULL) = (hold_requested_by IS NULL))
);
CREATE INDEX IF NOT EXISTS engine_change_cards_newest
  ON engine_change_cards (created_at DESC, card_id DESC);

CREATE TABLE IF NOT EXISTS engine_change_card_reads (
  card_id UUID NOT NULL REFERENCES engine_change_cards(card_id),
  subject TEXT NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 256),
  read_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (card_id, subject)
);

-- Retain the accepted facts. Only lifecycle and the first hold request can advance.
CREATE OR REPLACE FUNCTION guard_engine_change_card() RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'engine change cards are retained';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['state', 'deployment_identity', 'updated_at',
       'hold_requested_at', 'hold_requested_by', 'payload_sha256']) IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['state', 'deployment_identity', 'updated_at',
       'hold_requested_at', 'hold_requested_by', 'payload_sha256']) THEN
    RAISE EXCEPTION 'engine change accepted facts are immutable';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
       (OLD.state = 'accepted' AND NEW.state IN ('landed', 'reverted', 'held')) OR
       (OLD.state = 'landed' AND NEW.state IN ('live', 'reverted', 'held')) OR
       (OLD.state = 'live' AND NEW.state IN ('reverted', 'held'))) THEN
    RAISE EXCEPTION 'illegal engine change state advance';
  END IF;
  IF NEW.state = OLD.state AND
     (NEW.deployment_identity IS DISTINCT FROM OLD.deployment_identity OR
      NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256) THEN
    RAISE EXCEPTION 'engine change payload requires a state advance';
  END IF;
  IF OLD.deployment_identity IS NOT NULL AND
     NEW.deployment_identity IS DISTINCT FROM OLD.deployment_identity THEN
    RAISE EXCEPTION 'engine change deployment identity is immutable';
  END IF;
  IF OLD.hold_requested_at IS NOT NULL AND
     (NEW.hold_requested_at IS DISTINCT FROM OLD.hold_requested_at OR
      NEW.hold_requested_by IS DISTINCT FROM OLD.hold_requested_by) THEN
    RAISE EXCEPTION 'engine change hold request is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'engine_change_cards_guard'
      AND tgrelid = 'engine_change_cards'::regclass
  ) THEN
    CREATE TRIGGER engine_change_cards_guard BEFORE UPDATE OR DELETE ON engine_change_cards
      FOR EACH ROW EXECUTE FUNCTION guard_engine_change_card();
  END IF;
END;
$$;
