-- P7 drawing annotation authority. Annotation bytes live in immutable Git
-- objects. PostgreSQL owns which exact commit/tree is effective for one
-- tenant/project/drawing and records every preview decision.

CREATE TABLE IF NOT EXISTS annotation_targets (
  tenant_id   UUID        NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  org_id      UUID        NOT NULL,
  project_id  UUID        NOT NULL,
  drawing_id  UUID        NOT NULL,
  version     BIGINT      NOT NULL DEFAULT 0 CHECK (version >= 0),
  repository_id TEXT      NOT NULL,
  commit_sha  TEXT        NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  tree_sha    TEXT        NOT NULL CHECK (tree_sha ~ '^[0-9a-f]{40}$'),
  source_receipt_digest TEXT NOT NULL CHECK (source_receipt_digest ~ '^[0-9a-f]{64}$'),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by_binding_id UUID REFERENCES identity_bindings(binding_id),
  CONSTRAINT annotation_targets_pkey
    PRIMARY KEY (tenant_id, org_id, project_id, drawing_id),
  CONSTRAINT annotation_targets_tenant_org_match CHECK (tenant_id = org_id),
  CONSTRAINT annotation_targets_project_fk FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT annotation_targets_drawing_fk FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS annotation_batches (
  batch_id       UUID        NOT NULL,
  revision       INTEGER     NOT NULL DEFAULT 0 CHECK (revision >= 0),
  tenant_id      UUID        NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  org_id         UUID        NOT NULL,
  project_id     UUID        NOT NULL,
  drawing_id     UUID        NOT NULL,
  session_id     TEXT        NOT NULL REFERENCES app_sessions(session_id) ON DELETE CASCADE,
  kind           TEXT        NOT NULL CHECK (kind IN ('apply','undo')),
  retry_of_batch_id UUID,
  reverses_batch_id UUID,
  base_version   BIGINT      NOT NULL CHECK (base_version >= 0),
  base_commit    TEXT        NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
  base_tree      TEXT        NOT NULL CHECK (base_tree ~ '^[0-9a-f]{40}$'),
  preview_commit TEXT        NOT NULL CHECK (preview_commit ~ '^[0-9a-f]{40}$'),
  preview_tree   TEXT        NOT NULL CHECK (preview_tree ~ '^[0-9a-f]{40}$'),
  reverses_commit TEXT CHECK (reverses_commit IS NULL OR reverses_commit ~ '^[0-9a-f]{40}$'),
  reverses_tree  TEXT CHECK (reverses_tree IS NULL OR reverses_tree ~ '^[0-9a-f]{40}$'),
  repository_id TEXT         NOT NULL,
  source_receipt_digest TEXT NOT NULL CHECK (source_receipt_digest ~ '^[0-9a-f]{64}$'),
  payload_digest TEXT        NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
  payload_count  INTEGER     NOT NULL CHECK (payload_count > 0),
  request_key_digest TEXT    NOT NULL CHECK (request_key_digest ~ '^[0-9a-f]{64}$'),
  request_fingerprint TEXT   NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
  state          TEXT        NOT NULL DEFAULT 'pending'
                              CHECK (state IN ('pending','accepted','rejected','expired','stale')),
  created_by_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  lease_expires_at TIMESTAMPTZ NOT NULL,
  decided_at     TIMESTAMPTZ,
  decided_by_binding_id UUID REFERENCES identity_bindings(binding_id),
  decision_key_digest TEXT CHECK (
    decision_key_digest IS NULL OR decision_key_digest ~ '^[0-9a-f]{64}$'),
  applied_version BIGINT,
  reason         TEXT,
  superseded_at  TIMESTAMPTZ,
  CONSTRAINT annotation_batches_pkey PRIMARY KEY (batch_id, revision),
  CONSTRAINT annotation_batches_tenant_org_match CHECK (tenant_id = org_id),
  CONSTRAINT annotation_batches_kind_link_check CHECK (
    (kind = 'apply' AND reverses_batch_id IS NULL
      AND reverses_commit IS NULL AND reverses_tree IS NULL) OR
    (kind = 'undo' AND reverses_batch_id IS NOT NULL
      AND reverses_commit IS NOT NULL AND reverses_tree IS NOT NULL)
  ),
  CONSTRAINT annotation_batches_target_fk
    FOREIGN KEY (tenant_id, org_id, project_id, drawing_id)
    REFERENCES annotation_targets(tenant_id, org_id, project_id, drawing_id)
    ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS annotation_batches_request_key_uq
  ON annotation_batches (tenant_id, request_key_digest) WHERE revision = 0;

CREATE UNIQUE INDEX IF NOT EXISTS annotation_batches_one_pending_per_session_target
  ON annotation_batches (tenant_id, session_id, project_id, drawing_id)
  WHERE state = 'pending' AND superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS annotation_batches_pending_lease_idx
  ON annotation_batches (lease_expires_at)
  WHERE state = 'pending' AND superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS annotation_batches_target_created_idx
  ON annotation_batches (tenant_id, project_id, drawing_id, created_at DESC);

CREATE TABLE IF NOT EXISTS annotation_audit (
  audit_id       BIGSERIAL   PRIMARY KEY,
  batch_id      UUID        NOT NULL,
  batch_revision INTEGER     NOT NULL,
  tenant_id     UUID        NOT NULL,
  org_id        UUID        NOT NULL,
  project_id    UUID        NOT NULL,
  drawing_id    UUID        NOT NULL,
  from_state    TEXT        NOT NULL,
  to_state      TEXT        NOT NULL,
  actor_binding_id UUID REFERENCES identity_bindings(binding_id),
  decision_key_digest TEXT CHECK (
    decision_key_digest IS NULL OR decision_key_digest ~ '^[0-9a-f]{64}$'),
  source_receipt_digest TEXT NOT NULL CHECK (source_receipt_digest ~ '^[0-9a-f]{64}$'),
  payload_digest TEXT       NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
  payload_count INTEGER     NOT NULL CHECK (payload_count > 0),
  before_version BIGINT     NOT NULL,
  after_version  BIGINT     NOT NULL,
  before_commit  TEXT       NOT NULL CHECK (before_commit ~ '^[0-9a-f]{40}$'),
  before_tree    TEXT       NOT NULL CHECK (before_tree ~ '^[0-9a-f]{40}$'),
  after_commit   TEXT       NOT NULL CHECK (after_commit ~ '^[0-9a-f]{40}$'),
  after_tree     TEXT       NOT NULL CHECK (after_tree ~ '^[0-9a-f]{40}$'),
  at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT annotation_audit_tenant_org_match CHECK (tenant_id = org_id),
  CONSTRAINT annotation_audit_batch_revision_fk
    FOREIGN KEY (batch_id, batch_revision)
    REFERENCES annotation_batches(batch_id, revision) ON DELETE CASCADE,
  CONSTRAINT annotation_audit_project_fk FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT annotation_audit_drawing_fk FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS annotation_audit_target_at_idx
  ON annotation_audit (tenant_id, project_id, drawing_id, at DESC);
