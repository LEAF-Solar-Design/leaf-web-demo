-- 0041_workspace_project_name_uniqueness.sql
-- One active project name identifies one project inside a workspace org.
-- Fail closed if historical duplicates exist instead of choosing one UUID.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM projects
    WHERE status = 'active' AND deleted_at IS NULL
    GROUP BY org_id, lower(btrim(name))
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'active project names must be unique per workspace org';
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS projects_active_org_normalized_name_uq
  ON projects (org_id, lower(btrim(name)))
  WHERE status = 'active' AND deleted_at IS NULL;
