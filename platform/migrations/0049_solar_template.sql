-- 0049_solar_template.sql
-- Immutable versioned template store (card C2-1R, recut of the parked C2-1;
-- superseding ruling: this table is the AUTHORITATIVE source for any
-- (template_id, version) it has published. server/templates.py's C2-2
-- in-process catalog reads through this store when solar_template_beta is
-- on -- the catalog is a bootstrap default, never a rival source of truth,
-- because a version's content is immutable from the moment it lands here.
-- Expand-only: creates one table, its index, and its own dedicated
-- immutability trigger function. Drops nothing.

CREATE TABLE IF NOT EXISTS template_versions (
  version_id        UUID PRIMARY KEY,
  template_id       TEXT NOT NULL,
  version           TEXT NOT NULL,
  content           JSONB NOT NULL,
  content_sha256    TEXT NOT NULL,
  -- Provenance is mandatory: every published row names who/what published it
  -- and through which path -- never reconstructed after the fact.
  published_by      TEXT NOT NULL,
  provenance_source TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT template_versions_template_id_check
    CHECK (char_length(template_id) BETWEEN 1 AND 200),
  CONSTRAINT template_versions_version_check
    CHECK (char_length(version) BETWEEN 1 AND 50),
  CONSTRAINT template_versions_content_sha256_check
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT template_versions_published_by_check
    CHECK (char_length(published_by) BETWEEN 1 AND 200),
  CONSTRAINT template_versions_provenance_source_check
    CHECK (provenance_source IN ('seed', 'operator_publish')),
  -- One row per (template_id, version), forever: a duplicate publish is
  -- refused by this constraint, never a silent overwrite -- the app-level
  -- INSERT-only write path relies on this to make "publishing a new version
  -- never mutates a prior one" a database-enforced fact, not a convention.
  CONSTRAINT template_versions_template_version_unique
    UNIQUE (template_id, version)
);

CREATE INDEX IF NOT EXISTS idx_template_versions_template_id
  ON template_versions (template_id, created_at);

-- No UPDATE path exists on this table: this trigger rejects every UPDATE and
-- DELETE unconditionally. A dedicated function, not a reuse of
-- leaf_reject_ledger_mutation() -- that function's DELETE exception reads
-- OLD.org_id, and template_versions carries no org_id (templates are not
-- tenant-scoped), so reusing it would fail at trigger-fire time instead of
-- at review time.
CREATE OR REPLACE FUNCTION leaf_reject_template_version_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable template version record: %', TG_TABLE_NAME USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid = 'template_versions'::regclass
      AND tgname = 'template_versions_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER template_versions_immutable
      BEFORE UPDATE OR DELETE ON template_versions
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_template_version_mutation();
  END IF;
END
$$;
