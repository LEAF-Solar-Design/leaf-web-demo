-- Persist the exact app-owned authority tuple used to admit an author stage job.
ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS authority_session_id TEXT,
  ADD COLUMN IF NOT EXISTS authority_turn_id TEXT;
