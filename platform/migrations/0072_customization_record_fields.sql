-- Additive customization metadata authority. Rollback retains these nullable
-- columns and their data so older binaries can ignore them without data loss.
ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS request_graph_input TEXT,
  ADD COLUMN IF NOT EXISTS base_catalog_change_set_id TEXT,
  ADD COLUMN IF NOT EXISTS catalog_record_fields_json TEXT;
