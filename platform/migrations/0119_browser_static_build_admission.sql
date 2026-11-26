-- expand-contract: expand-only
-- An admitted static build retains its App actor and exact published source.
CREATE TABLE IF NOT EXISTS browser_static_build_admissions (
  tenant_id UUID NOT NULL,
  organization_id UUID NOT NULL CHECK (organization_id = tenant_id),
  project_id UUID NOT NULL,
  repo_key UUID NOT NULL,
  job_id UUID NOT NULL,
  -- Keep the original binding after revocation so a retry cannot replace it.
  actor_binding_id UUID NOT NULL,
  source_request_id UUID NOT NULL REFERENCES project_repository_edits(edit_id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version > 0),
  receipt_digest TEXT NOT NULL CHECK (receipt_digest ~ '^[0-9a-f]{64}$'),
  source_commit TEXT NOT NULL CHECK (source_commit ~ '^[0-9a-f]{40}$'),
  source_tree TEXT NOT NULL CHECK (source_tree ~ '^[0-9a-f]{40}$'),
  PRIMARY KEY (tenant_id, project_id, job_id)
);

CREATE INDEX IF NOT EXISTS browser_static_build_admissions_source_idx
  ON browser_static_build_admissions(source_request_id);
