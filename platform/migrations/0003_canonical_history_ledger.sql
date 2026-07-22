-- PostgreSQL canonical ledger foundation.  This is additive: existing drawing
-- versions/jobs remain readable compatibility records and are never deleted here.

DO $$ BEGIN
  ALTER TABLE projects ADD CONSTRAINT projects_org_project_unique UNIQUE (org_id, project_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

-- Version identity is project-scoped.  These keys support a jobs FK which can
-- never point at a drawing version belonging to a different project/org.
DO $$ BEGIN
  ALTER TABLE drawing_versions ADD CONSTRAINT drawing_versions_org_project_version_unique
    UNIQUE (org_id, project_id, version_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

-- ``0001`` used version_id-only FKs.  Replace every such legacy FK on jobs
-- rather than leaving one unsafe constraint alongside the composite boundary.
DO $$ DECLARE constraint_name TEXT; BEGIN
  FOR constraint_name IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class tbl ON tbl.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
    WHERE tbl.relname = 'jobs' AND ns.nspname = current_schema()
      AND con.contype = 'f' AND array_length(con.conkey, 1) = 1
      AND con.confrelid = 'drawing_versions'::regclass
  LOOP
    EXECUTE format('ALTER TABLE jobs DROP CONSTRAINT %I', constraint_name);
  END LOOP;
END $$;
DO $$ BEGIN
  ALTER TABLE jobs ADD CONSTRAINT jobs_input_version_org_project_fk
    FOREIGN KEY (org_id, project_id, input_version_id)
    REFERENCES drawing_versions(org_id, project_id, version_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE jobs ADD CONSTRAINT jobs_output_version_org_project_fk
    FOREIGN KEY (org_id, project_id, output_version_id)
    REFERENCES drawing_versions(org_id, project_id, version_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_spine_ref_unique_when_present
  ON jobs(spine_ref) WHERE spine_ref IS NOT NULL;

DO $$ BEGIN
  ALTER TABLE drawing_versions ADD CONSTRAINT drawing_versions_org_project_fk
    FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE jobs ADD CONSTRAINT jobs_org_project_fk
    FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE built_tools ADD CONSTRAINT built_tools_org_project_fk
    FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS tenant_authority_modes (
  org_id UUID PRIMARY KEY REFERENCES orgs(org_id) ON DELETE CASCADE,
  authority_mode TEXT NOT NULL CHECK (authority_mode IN ('legacy_sqlite', 'postgres_canonical')),
  selected_by TEXT NOT NULL DEFAULT 'server',
  selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (selected_by = 'server')
);
CREATE TABLE IF NOT EXISTS project_authority_modes (
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  authority_mode TEXT NOT NULL CHECK (authority_mode IN ('legacy_sqlite', 'postgres_canonical')),
  selected_by TEXT NOT NULL DEFAULT 'server',
  selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (org_id, project_id),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CHECK (selected_by = 'server')
);

CREATE TABLE IF NOT EXISTS identity_bindings (
  binding_id UUID PRIMARY KEY,
  external_authority TEXT NOT NULL,
  external_subject TEXT NOT NULL,
  platform_tenant_id UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  platform_user_id UUID NOT NULL,
  role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'editor', 'reviewer', 'read_only')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at TIMESTAMPTZ,
  CHECK ((status = 'active' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS identity_bindings_active_subject_uq
  ON identity_bindings(external_authority, external_subject) WHERE status = 'active';
ALTER TABLE identity_bindings ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'owner';
DO $$ BEGIN
  ALTER TABLE identity_bindings ADD CONSTRAINT identity_bindings_role_check
    CHECK (role IN ('owner', 'editor', 'reviewer', 'read_only'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS history_operations (
  operation_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  operation_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  idempotency_key TEXT NOT NULL,
  hash_algorithm TEXT NOT NULL,
  hash_canonicalization TEXT NOT NULL,
  hash_domain TEXT NOT NULL,
  hash_value TEXT NOT NULL CHECK (hash_value ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  UNIQUE (org_id, project_id, idempotency_key),
  UNIQUE (org_id, project_id, operation_id)
);
CREATE TABLE IF NOT EXISTS history_edges (
  edge_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  parent_operation_id UUID NOT NULL,
  child_operation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, project_id, parent_operation_id)
    REFERENCES history_operations(org_id, project_id, operation_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, project_id, child_operation_id)
    REFERENCES history_operations(org_id, project_id, operation_id) ON DELETE CASCADE,
  UNIQUE (parent_operation_id, child_operation_id),
  CHECK (parent_operation_id <> child_operation_id)
);
CREATE TABLE IF NOT EXISTS branch_refs (
  ref_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  ref_name TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  operation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, project_id, operation_id)
    REFERENCES history_operations(org_id, project_id, operation_id) ON DELETE CASCADE,
  UNIQUE (org_id, project_id, ref_name, sequence)
);
CREATE TABLE IF NOT EXISTS solve_records (
  solve_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  payload JSONB NOT NULL,
  idempotency_key TEXT NOT NULL,
  hash_algorithm TEXT NOT NULL,
  hash_canonicalization TEXT NOT NULL,
  hash_domain TEXT NOT NULL,
  hash_value TEXT NOT NULL CHECK (hash_value ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  UNIQUE (org_id, project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS outbox_entries (
  outbox_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id UUID NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at TIMESTAMPTZ,
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  UNIQUE (aggregate_type, aggregate_id, event_type)
);

CREATE OR REPLACE FUNCTION leaf_reject_ledger_mutation() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND EXISTS (
    SELECT 1 FROM orgs WHERE org_id = OLD.org_id AND status = 'offboarding'
  ) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'immutable canonical ledger record: %', TG_TABLE_NAME USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS history_operations_immutable ON history_operations;
CREATE TRIGGER history_operations_immutable BEFORE UPDATE OR DELETE ON history_operations
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS history_edges_immutable ON history_edges;
CREATE TRIGGER history_edges_immutable BEFORE UPDATE OR DELETE ON history_edges
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS branch_refs_immutable ON branch_refs;
CREATE TRIGGER branch_refs_immutable BEFORE UPDATE OR DELETE ON branch_refs
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS solve_records_immutable ON solve_records;
CREATE TRIGGER solve_records_immutable BEFORE UPDATE OR DELETE ON solve_records
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
