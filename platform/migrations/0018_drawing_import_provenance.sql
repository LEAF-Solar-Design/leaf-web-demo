-- 0018_drawing_import_provenance.sql
-- Server-derived provenance and replay protection for adopting a ready account
-- upload into the canonical project drawing ledger.

ALTER TABLE drawing_versions
  ADD COLUMN IF NOT EXISTS provenance JSONB;

ALTER TABLE drawing_versions
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

ALTER TABLE drawing_versions
  ADD COLUMN IF NOT EXISTS import_fingerprint TEXT;

DO $$ BEGIN
  ALTER TABLE drawing_versions
    ADD CONSTRAINT drawing_versions_import_fingerprint_format
    CHECK (
      import_fingerprint IS NULL
      OR import_fingerprint ~ '^[0-9a-f]{64}$'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE drawing_versions
    ADD CONSTRAINT drawing_versions_import_metadata_complete
    CHECK (
      (idempotency_key IS NULL AND import_fingerprint IS NULL)
      OR (
        idempotency_key IS NOT NULL
        AND CHAR_LENGTH(idempotency_key) BETWEEN 1 AND 200
        AND import_fingerprint IS NOT NULL
        AND provenance IS NOT NULL
        AND JSONB_TYPEOF(provenance) = 'object'
      )
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_drawing_versions_import_idempotency
  ON drawing_versions(org_id, project_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
