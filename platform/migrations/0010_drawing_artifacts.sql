-- 0010_drawing_artifacts.sql
-- Give every DWG a stable identity distinct from its immutable versions.

CREATE TABLE IF NOT EXISTS drawing_artifacts (
  drawing_id  UUID PRIMARY KEY,
  project_id  UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  org_id      UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (project_id, name),
  UNIQUE (drawing_id, project_id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_drawing_artifacts_project
  ON drawing_artifacts(org_id, project_id, created_at);

ALTER TABLE drawing_versions ADD COLUMN IF NOT EXISTS drawing_id UUID;

-- Existing platform data represented one drawing chain per project. Preserve
-- every version by creating exactly one stable default artifact per project.
INSERT INTO drawing_artifacts (drawing_id, project_id, org_id, name)
SELECT gen_random_uuid(), project_id, org_id, 'Primary drawing'
FROM projects
ON CONFLICT (project_id, name) DO NOTHING;

UPDATE drawing_versions AS version
SET drawing_id = artifact.drawing_id
FROM drawing_artifacts AS artifact
WHERE version.drawing_id IS NULL
  AND artifact.project_id = version.project_id
  AND artifact.org_id = version.org_id
  AND artifact.name = 'Primary drawing';

ALTER TABLE drawing_versions ALTER COLUMN drawing_id SET NOT NULL;

-- Sequence numbers are monotonic within one drawing chain, not across every
-- drawing in a project.
ALTER TABLE drawing_versions
  DROP CONSTRAINT IF EXISTS drawing_versions_project_id_seq_key;

DO $$ BEGIN
  ALTER TABLE drawing_versions
    ADD CONSTRAINT drawing_versions_drawing_project_org_fk
    FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_drawing_versions_drawing_seq
  ON drawing_versions(drawing_id, seq);
