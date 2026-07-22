-- Frozen, offline-verifiable evidence bundle manifests and included bytes.
CREATE TABLE IF NOT EXISTS evidence_bundles (
  bundle_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  solve_id UUID NOT NULL,
  idempotency_key TEXT NOT NULL,
  root_sha256 TEXT NOT NULL CHECK (root_sha256 ~ '^[0-9a-f]{64}$'),
  manifest JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id, solve_id)
    REFERENCES solve_records(org_id, project_id, solve_id) ON DELETE CASCADE,
  UNIQUE (org_id, project_id, idempotency_key),
  UNIQUE (org_id, project_id, bundle_id)
);

CREATE TABLE IF NOT EXISTS evidence_entries (
  entry_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  bundle_id UUID NOT NULL,
  path TEXT NOT NULL CHECK (length(trim(path)) > 0),
  content BYTEA NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
  content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  FOREIGN KEY (org_id, project_id, bundle_id)
    REFERENCES evidence_bundles(org_id, project_id, bundle_id) ON DELETE CASCADE,
  UNIQUE (bundle_id, path)
);

DROP TRIGGER IF EXISTS evidence_bundles_immutable ON evidence_bundles;
CREATE TRIGGER evidence_bundles_immutable BEFORE UPDATE OR DELETE ON evidence_bundles
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS evidence_entries_immutable ON evidence_entries;
CREATE TRIGGER evidence_entries_immutable BEFORE UPDATE OR DELETE ON evidence_entries
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
