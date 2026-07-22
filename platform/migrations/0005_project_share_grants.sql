-- Expiring, revocable project reviewer grants. Tokens are never stored in clear.
DO $$ BEGIN
  ALTER TABLE identity_bindings ADD CONSTRAINT identity_bindings_org_binding_unique
    UNIQUE (platform_tenant_id, binding_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS project_share_grants (
  grant_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  token_digest TEXT NOT NULL UNIQUE CHECK (token_digest ~ '^[0-9a-f]{64}$'),
  role TEXT NOT NULL CHECK (role IN ('reviewer', 'read_only')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_by_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  CHECK (expires_at > created_at),
  CHECK ((status = 'active' AND revoked_at IS NULL) OR
         (status = 'revoked' AND revoked_at IS NOT NULL)),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);

DO $$ BEGIN
  ALTER TABLE project_share_grants ADD CONSTRAINT project_share_grants_actor_org_fk
    FOREIGN KEY (org_id, created_by_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS project_share_grants_active_lookup_idx
  ON project_share_grants(token_digest, expires_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS project_share_grants_project_idx
  ON project_share_grants(org_id, project_id, created_at DESC);
