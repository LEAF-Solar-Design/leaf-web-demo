-- Expand only. Keep this nullable summary when rolling back the application.
ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS compile_summary_json TEXT;
