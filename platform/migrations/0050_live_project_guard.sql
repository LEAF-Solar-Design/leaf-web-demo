-- 0050_live_project_guard.sql
-- ONE definition of "this project is live", and a project_authority_modes
-- surface that cannot return a row whose project is gone.
--
-- WHY THIS EXISTS. `projects` is soft-deleted, and there are TWO soft-delete
-- writers with DIFFERENT column effects:
--   store.soft_delete_project        -> sets deleted_at, leaves status 'active'
--   project_lifecycle.delete_project -> sets status = 'deleted' AND deleted_at
-- A guard that checks only one of those columns therefore misses half the
-- deleted rows, which is why `p.status = 'active'` alone (annotation_store) and
-- `status <> 'deleted'` alone (project_lifecycle._project_row) were both holes.
--
-- project_authority_modes' foreign key is ON DELETE CASCADE, and a soft delete
-- never DELETEs, so its rows outlive their project. An unguarded join over them
-- selects dead projects. Confirmed against the live staging database on
-- 2026-08-28 (read-only ECS run-task a3fe22bd33a848a4a756620cb6fef5f7): 20+
-- orphan rows across orgs 6bc92878-... and 4d887d18-..., all
-- authority_mode = 'postgres_canonical'. On 2026-08-28 a fixture-mint script
-- discovered soft-deleted project 6ad97852-4c07-441a-a04e-9675e25a9a82 that way
-- and stalled a release lane for ~40 minutes on an opaque "project not found".
--
-- REJECTED: purging the pam row inside soft_delete_project. Soft delete is
-- documented as REVERSIBLE (recovery = UPDATE projects SET deleted_at = NULL,
-- see store.py's deletion section), so purging pam would silently downgrade a
-- restored project to 'legacy_sqlite' -- data loss dressed as a fix.
-- REJECTED: a pam.deleted_at column cascaded on delete. That moves the
-- forgetting problem to a second column instead of removing it, and leaves the
-- two writers free to disagree again.
--
-- CHOSEN: name the liveness predicate exactly once (live_projects), and expose
-- pam only through a view that has already applied it. Callers cannot write the
-- unguarded join by accident, and platform/tests/test_soft_delete_guard_static.py
-- fails CI on any NEW query that reads the base tables without the guard -- so
-- the unguarded query is unreachable, not merely discouraged.
--
-- The predicate is `deleted_at IS NULL AND status <> 'deleted'`: the UNION of
-- both delete markers. Deliberately not `status = 'active'`, which would also
-- drop 'archived' projects and silently change behaviour beyond deletion.
--
-- Expand-phase only: two additive views and one additive partial index. No
-- reader of the base tables is broken, so the previous image keeps working
-- through the deploy overlap window.

CREATE OR REPLACE VIEW live_projects AS
SELECT project_id, org_id, name, status, created_at, updated_at,
       deleted_at, purge_requested_at, purge_completed_at
FROM projects
WHERE deleted_at IS NULL AND status <> 'deleted';

COMMENT ON VIEW live_projects IS
  'THE liveness predicate for projects: not soft-deleted by either writer. '
  'Join this, never `projects`, when the caller means a live project.';

CREATE OR REPLACE VIEW live_project_authority_modes AS
SELECT pam.org_id, pam.project_id, pam.authority_mode,
       pam.selected_by, pam.selected_at
FROM project_authority_modes pam
JOIN projects p
  ON p.org_id = pam.org_id AND p.project_id = pam.project_id
WHERE p.deleted_at IS NULL AND p.status <> 'deleted';

COMMENT ON VIEW live_project_authority_modes IS
  'project_authority_modes with the live-project guard already applied. '
  'Selecting from it cannot return an authority row for a soft-deleted '
  'project; selecting from the base table can, and did (2026-08-28).';

-- Keeps the added liveness join an index-only probe rather than a heap lookup:
-- the authority resolve is on the request hot path (get_authority_mode runs on
-- every canonical write), so the guard must cost an index probe, not a scan.
CREATE INDEX IF NOT EXISTS idx_projects_live_identity
  ON projects (org_id, project_id)
  WHERE deleted_at IS NULL AND status <> 'deleted';
