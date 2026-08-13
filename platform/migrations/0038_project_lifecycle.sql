-- 0038 Durable browser-project lifecycle authority.
--
-- These records keep collaboration, browser-owned material, and destructive
-- lifecycle actions inside the canonical tenant/project boundary.  They never
-- store external subjects, bearer credentials, provider tokens, or secrets.

DO $$ BEGIN
  ALTER TABLE identity_bindings
    ADD CONSTRAINT identity_bindings_tenant_binding_unique
    UNIQUE (platform_tenant_id, binding_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS project_member_bindings (
  membership_id        UUID PRIMARY KEY,
  org_id                UUID NOT NULL,
  project_id            UUID NOT NULL,
  binding_id            UUID NOT NULL,
  role                  TEXT NOT NULL,
  status                TEXT NOT NULL DEFAULT 'active',
  invited_by_binding_id UUID NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at            TIMESTAMPTZ,
  CONSTRAINT project_member_bindings_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT project_member_bindings_binding_fk
    FOREIGN KEY (org_id, binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT project_member_bindings_inviter_fk
    FOREIGN KEY (org_id, invited_by_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT project_member_bindings_role_check
    CHECK (role IN ('owner', 'editor', 'reviewer', 'read_only')),
  CONSTRAINT project_member_bindings_status_check
    CHECK (status IN ('active', 'revoked')),
  CONSTRAINT project_member_bindings_revocation_shape_check
    CHECK (
      (status = 'active' AND revoked_at IS NULL)
      OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS project_member_bindings_one_active
  ON project_member_bindings(org_id, project_id, binding_id)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_project_member_bindings_scope
  ON project_member_bindings(org_id, project_id, created_at, membership_id);

CREATE TABLE IF NOT EXISTS project_files (
  file_id               UUID PRIMARY KEY,
  org_id                UUID NOT NULL,
  project_id            UUID NOT NULL,
  path                  TEXT NOT NULL,
  media_type            TEXT NOT NULL,
  content               TEXT NOT NULL,
  content_sha256        TEXT NOT NULL,
  revision              INTEGER NOT NULL DEFAULT 1,
  created_by_binding_id UUID NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT project_files_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT project_files_creator_fk
    FOREIGN KEY (org_id, created_by_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT project_files_path_check
    CHECK (char_length(path) BETWEEN 1 AND 512),
  CONSTRAINT project_files_media_type_check
    CHECK (char_length(media_type) BETWEEN 1 AND 200),
  CONSTRAINT project_files_content_sha256_check
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT project_files_revision_check
    CHECK (revision > 0),
  CONSTRAINT project_files_scope_path_unique
    UNIQUE (org_id, project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_project_files_scope
  ON project_files(org_id, project_id, path);

CREATE TABLE IF NOT EXISTS project_lifecycle_receipts (
  receipt_id       UUID PRIMARY KEY,
  org_id           UUID NOT NULL,
  project_id       UUID NOT NULL,
  action           TEXT NOT NULL,
  actor_binding_id UUID NOT NULL,
  idempotency_key  TEXT NOT NULL,
  input_digest     TEXT NOT NULL,
  result_json      JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT project_lifecycle_receipts_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT project_lifecycle_receipts_actor_fk
    FOREIGN KEY (org_id, actor_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT project_lifecycle_receipts_action_check
    CHECK (action IN (
      'project_created', 'member_invited', 'member_revoked', 'file_put', 'file_deleted',
      'project_cloned', 'project_exported', 'project_reset', 'project_deleted'
    )),
  CONSTRAINT project_lifecycle_receipts_idempotency_key_check
    CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
  CONSTRAINT project_lifecycle_receipts_input_digest_check
    CHECK (input_digest ~ '^[0-9a-f]{64}$'),
  CONSTRAINT project_lifecycle_receipts_idempotency_unique
    UNIQUE (org_id, project_id, action, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_project_lifecycle_receipts_scope
  ON project_lifecycle_receipts(org_id, project_id, created_at, receipt_id);

CREATE UNIQUE INDEX IF NOT EXISTS project_lifecycle_receipts_create_idempotency
  ON project_lifecycle_receipts(org_id, action, idempotency_key)
  WHERE action = 'project_created';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid = 'project_lifecycle_receipts'::regclass
      AND tgname = 'project_lifecycle_receipts_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER project_lifecycle_receipts_immutable
      BEFORE UPDATE OR DELETE ON project_lifecycle_receipts
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
