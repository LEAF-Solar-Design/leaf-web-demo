-- Lab graph ledger: additive expand migration. Never rewrite stored revisions.
-- expand-contract: expand-only
CREATE TABLE IF NOT EXISTS lab_blobs (
  tenant_id TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  canonical_text TEXT NOT NULL,
  canonical_bytes BYTEA NOT NULL,
  byte_count BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, sha256),
  CONSTRAINT lab_blobs_bytes_check CHECK (
    canonical_bytes = convert_to(canonical_text, 'UTF8')
    AND byte_count = octet_length(canonical_bytes) AND byte_count <= 262144
    AND sha256 = encode(sha256(canonical_bytes), 'hex'))
);
CREATE TABLE IF NOT EXISTS lab_revisions (
  tenant_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  revision BIGINT NOT NULL CHECK (revision > 0),
  parent_revision BIGINT,
  author TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY (tenant_id, doc_id, revision),
  FOREIGN KEY (tenant_id, sha256) REFERENCES lab_blobs (tenant_id, sha256),
  FOREIGN KEY (tenant_id, doc_id, parent_revision)
    REFERENCES lab_revisions (tenant_id, doc_id, revision),
  CONSTRAINT lab_revisions_parent_check CHECK (parent_revision IS NULL OR parent_revision < revision)
);
CREATE TABLE IF NOT EXISTS lab_documents (
  tenant_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  head BIGINT NOT NULL,
  published_revision BIGINT,
  PRIMARY KEY (tenant_id, doc_id),
  FOREIGN KEY (tenant_id, doc_id, head) REFERENCES lab_revisions (tenant_id, doc_id, revision),
  FOREIGN KEY (tenant_id, doc_id, published_revision) REFERENCES lab_revisions (tenant_id, doc_id, revision)
);
CREATE TABLE IF NOT EXISTS lab_publish_receipts (
  receipt_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  expected_head BIGINT NOT NULL,
  new_head BIGINT NOT NULL,
  sha256 TEXT NOT NULL,
  author TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  FOREIGN KEY (tenant_id, doc_id, new_head) REFERENCES lab_revisions (tenant_id, doc_id, revision),
  FOREIGN KEY (tenant_id, sha256) REFERENCES lab_blobs (tenant_id, sha256)
);
CREATE OR REPLACE FUNCTION leaf_reject_lab_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable lab ledger record' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'lab_blobs_immutable'
                 AND tgrelid = 'lab_blobs'::regclass) THEN
    CREATE TRIGGER lab_blobs_immutable BEFORE UPDATE OR DELETE ON lab_blobs
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_lab_mutation();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'lab_revisions_immutable'
                 AND tgrelid = 'lab_revisions'::regclass) THEN
    CREATE TRIGGER lab_revisions_immutable BEFORE UPDATE OR DELETE ON lab_revisions
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_lab_mutation();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'lab_publish_receipts_immutable'
                 AND tgrelid = 'lab_publish_receipts'::regclass) THEN
    CREATE TRIGGER lab_publish_receipts_immutable BEFORE UPDATE OR DELETE ON lab_publish_receipts
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_lab_mutation();
  END IF;
END $$;
