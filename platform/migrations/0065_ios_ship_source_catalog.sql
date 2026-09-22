CREATE TABLE IF NOT EXISTS ios_ship_source_catalog (
  catalog_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  catalog_key TEXT NOT NULL,
  repository TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  bundle_identifier TEXT NOT NULL,
  marketing_version TEXT NOT NULL,
  build_number TEXT NOT NULL,
  producer_receipt_digest TEXT NOT NULL CHECK (producer_receipt_digest ~ '^[0-9a-f]{64}$'),
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ios_ship_source_catalog_project_fk FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT ios_ship_source_catalog_scope_unique UNIQUE (org_id, project_id, source_revision),
  CONSTRAINT ios_ship_source_catalog_values_check CHECK (
    char_length(btrim(catalog_key)) > 0
    AND char_length(btrim(repository)) > 0
    AND char_length(btrim(source_revision)) > 0
    AND char_length(btrim(source_sha256)) > 0
    AND char_length(btrim(bundle_identifier)) > 0
    AND char_length(btrim(marketing_version)) > 0
    AND char_length(btrim(build_number)) > 0
    AND char_length(btrim(producer_receipt_digest)) > 0
  )
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'ios_ship_source_catalog'::regclass
      AND tgname = 'ios_ship_source_catalog_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER ios_ship_source_catalog_immutable
      BEFORE UPDATE OR DELETE ON ios_ship_source_catalog
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
