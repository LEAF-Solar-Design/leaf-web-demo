-- 0039 Project-bound durable conversation and request identity.
--
-- Legacy sessions remain valid with both scope columns NULL and retain the
-- original (tenant_id, drawing_id) conflict target.  A project-bound session
-- carries both columns and uses a reserved storage tenant so an older image
-- fails its tenant ownership check during canary overlap or rollback.  The new
-- image projects the canonical org back to callers. Request rows repeat the
-- trusted scope so retries cannot move to another project or session.

ALTER TABLE app_sessions
  ADD COLUMN IF NOT EXISTS org_id UUID,
  ADD COLUMN IF NOT EXISTS project_id UUID;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_project_scope_shape_check
    CHECK ((org_id IS NULL) = (project_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_project_tenant_check
    CHECK (
      org_id IS NULL OR tenant_id =
        'project:' || org_id::TEXT || ':' || project_id::TEXT
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_project_scope_unique
    UNIQUE (session_id, org_id, project_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_app_sessions_one_project
  ON app_sessions(org_id, project_id)
  WHERE project_id IS NOT NULL;

ALTER TABLE app_session_requests
  ADD COLUMN IF NOT EXISTS org_id UUID,
  ADD COLUMN IF NOT EXISTS project_id UUID;

DO $$ BEGIN
  ALTER TABLE app_session_requests
    ADD CONSTRAINT app_session_requests_project_scope_shape_check
    CHECK ((org_id IS NULL) = (project_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_session_requests
    ADD CONSTRAINT app_session_requests_project_tenant_check
    CHECK (org_id IS NULL OR tenant_id = org_id::TEXT);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_session_requests
    ADD CONSTRAINT app_session_requests_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_session_requests
    ADD CONSTRAINT app_session_requests_session_project_fk
    FOREIGN KEY (session_id, org_id, project_id)
    REFERENCES app_sessions(session_id, org_id, project_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_app_session_requests_project_scope
  ON app_session_requests(org_id, project_id, session_id, created_at DESC)
  WHERE project_id IS NOT NULL;
