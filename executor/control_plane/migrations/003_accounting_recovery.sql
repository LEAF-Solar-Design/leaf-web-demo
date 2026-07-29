-- Mark terminal accounting synthesized after an executor or delivery failure.
-- Recovered rows contain no estimated usage and therefore cannot overcharge.
ALTER TABLE instant_accounting
  ADD COLUMN IF NOT EXISTS recovery_reason text;

ALTER TABLE instant_accounting
  DROP CONSTRAINT IF EXISTS instant_accounting_recovery_reason_check;
ALTER TABLE instant_accounting
  ADD CONSTRAINT instant_accounting_recovery_reason_check
  CHECK (recovery_reason IS NULL OR recovery_reason = 'executor_lost');

CREATE INDEX IF NOT EXISTS instant_invocations_stale_state_idx
  ON instant_invocations(updated_at, invocation_id)
  WHERE state IN ('accepted', 'started');
