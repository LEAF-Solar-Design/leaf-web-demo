-- Mutable display state outside the immutable Lab graph ledger.
-- expand-contract: expand-only
CREATE TABLE IF NOT EXISTS lab_layouts (
  tenant_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  layout_text TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (tenant_id, doc_id, sha256),
  FOREIGN KEY (tenant_id, sha256) REFERENCES lab_blobs (tenant_id, sha256)
);
