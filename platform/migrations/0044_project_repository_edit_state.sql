-- P8 durable repository-edit coordination. Git bytes and instructions remain
-- outside PostgreSQL. These tables store only authority, immutable witnesses,
-- state, and content-free audit evidence.

CREATE TABLE IF NOT EXISTS project_repository_edits (
  edit_id UUID PRIMARY KEY,
  operation TEXT NOT NULL CHECK (operation IN ('edit','rollback')),
  source_edit_id UUID REFERENCES project_repository_edits(edit_id),
  tenant_id UUID NOT NULL,
  organization_id UUID NOT NULL,
  project_id UUID NOT NULL,
  repo_key UUID NOT NULL,
  actor_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  writer_lease_id UUID NOT NULL,
  writer_lease_generation BIGINT NOT NULL CHECK (writer_lease_generation > 0),
  base_commit TEXT NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
  staged_head_commit TEXT NOT NULL CHECK (staged_head_commit ~ '^[0-9a-f]{40}$'),
  staged_tree TEXT NOT NULL CHECK (staged_tree ~ '^[0-9a-f]{40}$'),
  expected_main_commit TEXT NOT NULL CHECK (expected_main_commit ~ '^[0-9a-f]{40}$'),
  receipt_json JSONB NOT NULL,
  receipt_digest TEXT NOT NULL CHECK (receipt_digest ~ '^[0-9a-f]{64}$'),
  changed_paths_digest TEXT NOT NULL CHECK (changed_paths_digest ~ '^[0-9a-f]{64}$'),
  diff_digest TEXT NOT NULL CHECK (diff_digest ~ '^[0-9a-f]{64}$'),
  instruction_digest TEXT NOT NULL CHECK (instruction_digest ~ '^[0-9a-f]{64}$'),
  state TEXT NOT NULL CHECK (state IN (
    'created','staging','staged','awaiting_confirmation','publishing','published',
    'rejected','conflicted','failed','superseded','rolled_back')),
  version BIGINT NOT NULL CHECK (version > 0),
  idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
  confirmation_id UUID,
  observed_private_ref_commit TEXT CHECK (
    observed_private_ref_commit IS NULL OR observed_private_ref_commit ~ '^[0-9a-f]{40}$'),
  observed_main_commit TEXT CHECK (
    observed_main_commit IS NULL OR observed_main_commit ~ '^[0-9a-f]{40}$'),
  observed_main_tree TEXT CHECK (
    observed_main_tree IS NULL OR observed_main_tree ~ '^[0-9a-f]{40}$'),
  published_at TIMESTAMPTZ,
  recovery_reason_code TEXT,
  recovered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT project_repository_edits_authority_fk
    FOREIGN KEY (tenant_id, organization_id, project_id)
    REFERENCES project_repository_authorities(tenant_id, organization_id, project_id),
  CONSTRAINT project_repository_edits_operation_link CHECK (
    (operation = 'edit' AND source_edit_id IS NULL) OR
    (operation = 'rollback' AND source_edit_id IS NOT NULL)),
  CONSTRAINT project_repository_edits_expected_base CHECK (expected_main_commit = base_commit),
  CONSTRAINT project_repository_edits_authority_key_uq
    UNIQUE (tenant_id, organization_id, project_id, repo_key, idempotency_key)
);

CREATE TABLE IF NOT EXISTS project_repository_edit_confirmations (
  confirmation_id UUID PRIMARY KEY,
  receipt_digest TEXT NOT NULL CHECK (receipt_digest ~ '^[0-9a-f]{64}$'),
  approver_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  tenant_id UUID NOT NULL,
  organization_id UUID NOT NULL,
  project_id UUID NOT NULL,
  repo_key UUID NOT NULL,
  edit_id UUID NOT NULL UNIQUE REFERENCES project_repository_edits(edit_id) ON DELETE CASCADE,
  writer_lease_id UUID NOT NULL,
  writer_lease_generation BIGINT NOT NULL CHECK (writer_lease_generation > 0),
  staged_tree TEXT NOT NULL CHECK (staged_tree ~ '^[0-9a-f]{40}$'),
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at > issued_at),
  idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
  consumed_at TIMESTAMPTZ,
  consumed_by_idempotency_key TEXT,
  consumed_edit_version BIGINT,
  CONSTRAINT project_repository_edit_confirmations_consumption_check CHECK (
    (consumed_at IS NULL AND consumed_by_idempotency_key IS NULL AND consumed_edit_version IS NULL)
    OR
    (consumed_at IS NOT NULL AND consumed_by_idempotency_key IS NOT NULL AND consumed_edit_version IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS project_repository_edit_audit_events (
  event_id BIGSERIAL PRIMARY KEY,
  edit_id UUID NOT NULL REFERENCES project_repository_edits(edit_id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL,
  organization_id UUID NOT NULL,
  project_id UUID NOT NULL,
  repo_key UUID NOT NULL,
  source_edit_id UUID,
  prior_state TEXT NOT NULL,
  next_state TEXT NOT NULL,
  actor_binding_id UUID,
  approver_binding_id UUID,
  writer_lease_id UUID NOT NULL,
  writer_lease_generation BIGINT NOT NULL CHECK (writer_lease_generation > 0),
  base_commit TEXT NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
  staged_head_commit TEXT NOT NULL CHECK (staged_head_commit ~ '^[0-9a-f]{40}$'),
  staged_tree TEXT NOT NULL CHECK (staged_tree ~ '^[0-9a-f]{40}$'),
  expected_main_commit TEXT NOT NULL CHECK (expected_main_commit ~ '^[0-9a-f]{40}$'),
  observed_private_ref_commit TEXT,
  observed_main_commit TEXT,
  observed_main_tree TEXT,
  changed_paths_digest TEXT NOT NULL CHECK (changed_paths_digest ~ '^[0-9a-f]{64}$'),
  receipt_digest TEXT NOT NULL CHECK (receipt_digest ~ '^[0-9a-f]{64}$'),
  idempotency_key TEXT NOT NULL,
  result TEXT NOT NULL,
  reason_code TEXT,
  event_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS project_repository_edits_authority_state_idx
  ON project_repository_edits (tenant_id, organization_id, project_id, repo_key, state);
CREATE INDEX IF NOT EXISTS project_repository_edit_audit_edit_idx
  ON project_repository_edit_audit_events (edit_id, event_id);

CREATE OR REPLACE FUNCTION reject_project_repository_edit_audit_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'project repository edit audit is append-only';
END;
$$;

CREATE OR REPLACE TRIGGER project_repository_edit_audit_no_update
BEFORE UPDATE OR DELETE ON project_repository_edit_audit_events
FOR EACH ROW EXECUTE FUNCTION reject_project_repository_edit_audit_mutation();
