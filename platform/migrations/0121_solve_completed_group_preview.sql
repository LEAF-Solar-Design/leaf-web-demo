-- Optional, bounded preview on existing parent custody. Saved output is untouched.
ALTER TABLE project_solve_exchanges
  ADD COLUMN IF NOT EXISTS completed_group_preview JSONB
  CHECK (completed_group_preview IS NULL OR (
    jsonb_typeof(completed_group_preview) = 'object'
    AND octet_length(completed_group_preview::text) <= 131072
  ));
