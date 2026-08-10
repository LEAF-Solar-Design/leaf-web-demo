-- Persist the harness's terminal authoring-job failure reason so stage status
-- can tell the tenant WHY authoring failed, not only that it did.
ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS stage_error_message TEXT;
