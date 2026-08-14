-- P8 server-owned project repository authority. No caller-controlled path or
-- host root is stored. The opaque UUID repo_key selects a repository
-- through the later trusted provider adapter.

CREATE TABLE IF NOT EXISTS project_repository_authorities (
  tenant_id UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  organization_id UUID NOT NULL,
  project_id UUID NOT NULL,
  repo_key UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT project_repository_authorities_pkey
    PRIMARY KEY (tenant_id, organization_id, project_id),
  CONSTRAINT project_repository_authorities_tenant_org_match
    CHECK (tenant_id = organization_id),
  CONSTRAINT project_repository_authorities_project_fk
    FOREIGN KEY (organization_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT project_repository_authorities_repo_key_uq
    UNIQUE (tenant_id, repo_key)
);
