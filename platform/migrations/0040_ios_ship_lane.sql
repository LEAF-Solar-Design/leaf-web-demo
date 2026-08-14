-- 0040 Wave D one-shot iOS ship authority.
--
-- Apple credential material never enters these relations.  They contain only
-- tenant/project/source identity, sanitized provider readiness and terminal
-- TestFlight evidence.  The canonical jobs row remains the project timeline
-- anchor for every execution.

CREATE TABLE IF NOT EXISTS ios_ship_grants (
  grant_id       UUID PRIMARY KEY,
  org_id         UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  tenant_id      TEXT NOT NULL,
  status         TEXT NOT NULL,
  expires_at     TIMESTAMPTZ NOT NULL,
  observed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ios_ship_grants_status_check
    CHECK (status IN ('healthy', 'expired', 'revoked', 'unavailable')),
  CONSTRAINT ios_ship_grants_scope_unique UNIQUE (org_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS ios_ship_readiness (
  org_id              UUID NOT NULL,
  project_id          UUID NOT NULL,
  tenant_id           TEXT NOT NULL,
  record_kind         TEXT NOT NULL,
  healthy             BOOLEAN NOT NULL,
  dispatch_available  BOOLEAN NOT NULL,
  setup_action        TEXT,
  reported_at         TIMESTAMPTZ NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ios_ship_readiness_pkey PRIMARY KEY (org_id, project_id, tenant_id),
  CONSTRAINT ios_ship_readiness_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT ios_ship_readiness_kind_check
    CHECK (record_kind = 'leaf.ios-ship-readiness.v1'),
  CONSTRAINT ios_ship_readiness_setup_shape_check
    CHECK (dispatch_available OR setup_action IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS ios_ship_revision_approvals (
  approval_id        UUID PRIMARY KEY,
  org_id             UUID NOT NULL,
  project_id         UUID NOT NULL,
  revision           TEXT NOT NULL,
  source_revision    TEXT NOT NULL,
  source_sha256      TEXT NOT NULL,
  bundle_identifier  TEXT NOT NULL,
  marketing_version  TEXT NOT NULL,
  build_number       TEXT NOT NULL,
  approved           BOOLEAN NOT NULL DEFAULT TRUE,
  approved_by        TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  consumed_at        TIMESTAMPTZ,
  consumed_execution_id UUID,
  CONSTRAINT ios_ship_revision_approvals_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT ios_ship_revision_approvals_source_sha_check
    CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ios_ship_revision_approvals_values_check CHECK (
    char_length(revision) > 0
    AND char_length(source_revision) > 0
    AND char_length(bundle_identifier) > 0
    AND char_length(marketing_version) > 0
    AND char_length(build_number) > 0
  ),
  CONSTRAINT ios_ship_revision_approvals_consumed_shape_check CHECK (
    (consumed_at IS NULL AND consumed_execution_id IS NULL)
    OR (consumed_at IS NOT NULL AND consumed_execution_id IS NOT NULL)
  ),
  CONSTRAINT ios_ship_revision_approvals_scope_unique
    UNIQUE (org_id, project_id, revision)
);

CREATE TABLE IF NOT EXISTS ios_ship_executions (
  execution_id          UUID PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  org_id                UUID NOT NULL,
  project_id            UUID NOT NULL,
  tenant_id             TEXT NOT NULL,
  principal_id          TEXT NOT NULL,
  approval_id           UUID NOT NULL REFERENCES ios_ship_revision_approvals(approval_id),
  revision              TEXT NOT NULL,
  source_revision       TEXT NOT NULL,
  source_sha256         TEXT NOT NULL,
  bundle_identifier     TEXT NOT NULL,
  marketing_version     TEXT NOT NULL,
  build_number          TEXT NOT NULL,
  app_color             TEXT NOT NULL,
  status                TEXT NOT NULL DEFAULT 'queued',
  failed_stage          TEXT,
  idempotency_key       TEXT NOT NULL,
  submission_fingerprint TEXT NOT NULL,
  dispatch_result       JSONB,
  receipt_id            UUID,
  error                 JSONB,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ios_ship_executions_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT ios_ship_executions_source_sha_check
    CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ios_ship_executions_app_color_check
    CHECK (app_color IN ('primary', 'alternate')),
  CONSTRAINT ios_ship_executions_fingerprint_check
    CHECK (submission_fingerprint ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ios_ship_executions_status_check
    CHECK (status IN ('queued', 'dispatching', 'dispatched', 'running', 'succeeded', 'failed')),
  CONSTRAINT ios_ship_executions_scope_idempotency_unique
    UNIQUE (org_id, project_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_ios_ship_executions_scope
  ON ios_ship_executions(org_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ios_ship_receipts (
  receipt_id                UUID PRIMARY KEY,
  execution_id              UUID NOT NULL UNIQUE REFERENCES ios_ship_executions(execution_id),
  org_id                    UUID NOT NULL,
  project_id                UUID NOT NULL,
  tenant_id                 TEXT NOT NULL,
  kind                      TEXT NOT NULL,
  revision                  TEXT NOT NULL,
  source_revision           TEXT NOT NULL,
  source_sha256             TEXT NOT NULL,
  bundle_identifier         TEXT NOT NULL,
  marketing_version         TEXT NOT NULL,
  build_number              TEXT NOT NULL,
  image_identity            TEXT NOT NULL,
  toolchain_identity        TEXT NOT NULL,
  app_store_connect_result  JSONB NOT NULL,
  hash_algorithm            TEXT NOT NULL,
  hash_canonicalization     TEXT NOT NULL,
  hash_domain               TEXT NOT NULL,
  hash_value                TEXT NOT NULL,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ios_ship_receipts_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT ios_ship_receipts_kind_check
    CHECK (kind = 'leaf.ios-testflight-receipt.v1'),
  CONSTRAINT ios_ship_receipts_source_sha_check
    CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ios_ship_receipts_hash_check
    CHECK (hash_value ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ios_ship_receipts_result_check
    CHECK (jsonb_typeof(app_store_connect_result) = 'object'),
  CONSTRAINT ios_ship_receipts_build_unique
    UNIQUE (org_id, bundle_identifier, build_number)
);

CREATE INDEX IF NOT EXISTS idx_ios_ship_receipts_scope
  ON ios_ship_receipts(org_id, project_id, created_at DESC);

DO $$ BEGIN
  ALTER TABLE ios_ship_revision_approvals
    ADD CONSTRAINT ios_ship_revision_approvals_consumed_execution_fk
    FOREIGN KEY (consumed_execution_id)
    REFERENCES ios_ship_executions(execution_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE ios_ship_executions
    ADD CONSTRAINT ios_ship_executions_receipt_fk
    FOREIGN KEY (receipt_id)
    REFERENCES ios_ship_receipts(receipt_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'ios_ship_receipts'::regclass
      AND tgname = 'ios_ship_receipts_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER ios_ship_receipts_immutable
      BEFORE UPDATE OR DELETE ON ios_ship_receipts
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
