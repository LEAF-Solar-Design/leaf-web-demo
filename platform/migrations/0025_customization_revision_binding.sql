-- Bind an authored-tool revision to one durable target before model execution.
ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS change_kind TEXT NOT NULL DEFAULT 'create';

ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS target_tool_name TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'customization_change_kind_check'
  ) THEN
    ALTER TABLE customization_change_sets
      ADD CONSTRAINT customization_change_kind_check
      CHECK (change_kind IN ('create', 'revise'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'customization_revision_target_check'
  ) THEN
    ALTER TABLE customization_change_sets
      ADD CONSTRAINT customization_revision_target_check
      CHECK (
        (change_kind = 'create' AND target_tool_name IS NULL)
        OR (change_kind = 'revise' AND target_tool_name IS NOT NULL)
      );
  END IF;
END
$$;
