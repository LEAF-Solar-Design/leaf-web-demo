-- 0002_deletion_columns.sql
-- Day-one deletion / compliance columns (DELETION-OFFBOARDING-DESIGN.md sec 4).
-- Adds the binding soft-delete + hard-PURGE audit columns to EVERY tenant-data
-- table so deletion-on-request is built in from the start and cannot be
-- retrofitted. Idempotent (ADD COLUMN IF NOT EXISTS): a re-apply is a clean no-op.
-- Applies on top of 0001_project_job.sql (the five tables must already exist).
--
-- BINDING COLUMN CONTRACT (both this lane and the billing/compliance sibling MUST
-- use these EXACT names on the org / Project / Job rows):
--   deleted_at          TIMESTAMPTZ NULL  soft-delete marker; NULL = live/visible
--   purge_requested_at  TIMESTAMPTZ NULL  hard-PURGE accepted; opens the audit window
--   purge_completed_at  TIMESTAMPTZ NULL  hard-PURGE cascade finished across all stores
--
-- deleted_at is the routine, reversible soft-delete (sec 2); every default store
-- read filters WHERE deleted_at IS NULL. purge_requested_at / purge_completed_at
-- bracket the gated, audited hard-PURGE-on-request exception (sec 3) -- the ONE
-- sanctioned override of the fleet never-hard-delete rule.

-- orgs (tenant anchor / tombstone row)
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS deleted_at         TIMESTAMPTZ;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS purge_requested_at TIMESTAMPTZ;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS purge_completed_at TIMESTAMPTZ;

-- projects
ALTER TABLE projects ADD COLUMN IF NOT EXISTS deleted_at         TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS purge_requested_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS purge_completed_at TIMESTAMPTZ;

-- drawing_versions
ALTER TABLE drawing_versions ADD COLUMN IF NOT EXISTS deleted_at         TIMESTAMPTZ;
ALTER TABLE drawing_versions ADD COLUMN IF NOT EXISTS purge_requested_at TIMESTAMPTZ;
ALTER TABLE drawing_versions ADD COLUMN IF NOT EXISTS purge_completed_at TIMESTAMPTZ;

-- jobs
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_at         TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS purge_requested_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS purge_completed_at TIMESTAMPTZ;

-- built_tools
ALTER TABLE built_tools ADD COLUMN IF NOT EXISTS deleted_at         TIMESTAMPTZ;
ALTER TABLE built_tools ADD COLUMN IF NOT EXISTS purge_requested_at TIMESTAMPTZ;
ALTER TABLE built_tools ADD COLUMN IF NOT EXISTS purge_completed_at TIMESTAMPTZ;

-- Partial indexes support the default reads' `WHERE org_id = ? AND deleted_at IS NULL`
-- (only live rows are indexed; soft-deleted rows drop out of the hot read path).
CREATE INDEX IF NOT EXISTS idx_projects_org_live
  ON projects(org_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_project_live
  ON jobs(project_id, created_at DESC) WHERE deleted_at IS NULL;
