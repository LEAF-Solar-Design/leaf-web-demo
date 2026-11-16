-- expand-contract: expand-only
-- Rollback disables the new routes and retains archive timestamps and sweep rows.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at timestamptz NULL;

-- Preserve existing status policies, admitting archived if a status check exists.
DO $$ DECLARE item RECORD; BEGIN
  FOR item IN
    SELECT conname, pg_get_expr(conbin, conrelid) AS expression
    FROM pg_constraint
    WHERE conrelid = 'projects'::regclass AND contype = 'c'
      AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                         WHERE attrelid='projects'::regclass AND attname='status')]
  LOOP
    EXECUTE format('ALTER TABLE projects DROP CONSTRAINT %I', item.conname);
    EXECUTE format('ALTER TABLE projects ADD CONSTRAINT %I CHECK ((%s) OR status = ''archived'')',
                   item.conname, item.expression);
  END LOOP;
END $$;

ALTER TABLE project_lifecycle_receipts DROP CONSTRAINT IF EXISTS project_lifecycle_receipts_action_check;
ALTER TABLE project_lifecycle_receipts ADD CONSTRAINT project_lifecycle_receipts_action_check CHECK (action IN (
  'project_created', 'member_invited', 'member_revoked', 'file_put', 'file_deleted',
  'project_cloned', 'project_exported', 'project_reset', 'project_deleted',
  'project_renamed', 'project_archived', 'project_restored'
));

CREATE TABLE IF NOT EXISTS project_artifact_sweep (
  project_id UUID NOT NULL REFERENCES projects(project_id),
  object_key TEXT NOT NULL CHECK (length(object_key) > 0),
  object_version_or_sha256 TEXT NOT NULL CHECK (length(object_version_or_sha256) > 0),
  queued_at timestamptz NOT NULL DEFAULT NOW(),
  swept_at timestamptz NULL,
  PRIMARY KEY (project_id, object_key, object_version_or_sha256)
);
CREATE INDEX IF NOT EXISTS project_artifact_sweep_pending
  ON project_artifact_sweep(project_id) WHERE swept_at IS NULL;
