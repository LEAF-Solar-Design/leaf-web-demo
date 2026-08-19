-- 0047 Immutable versioned Solar CAD template store.
--
-- This card's contract names ``server/migrations/0040_solar_template.sql``,
-- but the real migration tree lives at ``platform/migrations`` and is
-- glob-enumerated by ``leaf_platform.db.apply_migration()`` (MIGRATION_GLOB),
-- pinned by ``platform/authority-inventory.json``'s scope.migration_ids and
-- by ``server/tests/test_postgres_authority_inventory_contract.py``. 0040 is
-- already ``ios_ship_lane.sql`` and the highest shipped slot at claim time is
-- 0046, so this lands at 0047 (next free slot), matching the precedent set
-- by card B-C1 (platform/migrations/0046_conversation_durable.sql).
--
-- A template version is a permanent, append-only fact: "org X published
-- template key Y, version N, with this exact content, from this exact
-- actor". Every field a caller might want to change later (content, source,
-- provenance note) belongs to a NEW row with a higher version instead, so
-- there is deliberately no mutable "head" or "latest" pointer table here --
-- the latest version of a template is always `MAX(version)` over this one
-- table, computed at read time, never stored and updated.
--
-- Wholly new authority (nothing legacy to migrate), so this file creates one
-- fresh table and writes no backfill INSERT...SELECT: a populated snapshot
-- gains zero template rows and orphans none, because none are written.
--
-- Project-independent and org-scoped: templates are shared catalog-like
-- assets published once per org, not per project, so this table's tenant
-- boundary is `orgs(org_id)` directly rather than the
-- `projects(org_id, project_id)` composite the project-bound tables in
-- 0038/0046 use.

CREATE TABLE IF NOT EXISTS solar_template_versions (
  template_version_id     UUID PRIMARY KEY,
  org_id                   UUID NOT NULL,
  template_key             TEXT NOT NULL,
  version                  INTEGER NOT NULL,
  content                  JSONB NOT NULL,
  content_sha256           TEXT NOT NULL,
  source                   TEXT NOT NULL,
  provenance_note          TEXT,
  published_by_binding_id  UUID NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT solar_template_versions_org_fk
    FOREIGN KEY (org_id)
    REFERENCES orgs(org_id) ON DELETE CASCADE,
  CONSTRAINT solar_template_versions_publisher_fk
    FOREIGN KEY (org_id, published_by_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT solar_template_versions_template_key_check
    CHECK (char_length(template_key) BETWEEN 1 AND 200),
  CONSTRAINT solar_template_versions_version_check
    CHECK (version >= 1),
  CONSTRAINT solar_template_versions_content_sha256_check
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT solar_template_versions_source_check
    CHECK (source IN ('author', 'import', 'system')),
  CONSTRAINT solar_template_versions_scope_version_unique
    UNIQUE (org_id, template_key, version)
);

CREATE INDEX IF NOT EXISTS idx_solar_template_versions_scope
  ON solar_template_versions(org_id, template_key, version DESC, template_version_id);

-- Immutability: no UPDATE or DELETE path exists on this table, in code or in
-- the database. Reuses the shared ``leaf_reject_ledger_mutation`` trigger
-- function (defined in 0003, already governing
-- project_lifecycle_receipts (0038) and other append-only ledgers), so a
-- publish-new-version call can never mutate or remove a prior row -- only
-- INSERT a new one with a higher `version`. The idempotent
-- IF-NOT-EXISTS-guarded trigger creation (rather than DROP TRIGGER IF EXISTS
-- ... CREATE TRIGGER) matches 0038's pattern precisely because a bare DROP
-- would itself trip this repo's expand-only migration gate
-- (scripts/migration_expand_contract_gate.py), which this migration is not
-- grandfathered against.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid = 'solar_template_versions'::regclass
      AND tgname = 'solar_template_versions_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER solar_template_versions_immutable
      BEFORE UPDATE OR DELETE ON solar_template_versions
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
