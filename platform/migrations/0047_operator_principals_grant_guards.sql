-- Grant-attribution invariants for operator_principals, ENFORCED at the schema
-- level (contract/OPERATOR.md §1.1: the audit trail is a schema invariant, not
-- a CLI courtesy). 0032 created the roster with granted_by nullable, so a
-- writer bug — or the admin CLI's own pre-guard upsert, whose --granted-by
-- defaulted to None and whose ON CONFLICT clause overwrote the original
-- attribution with it — could mint or launder an unattributed grant. The CLI
-- now requires --granted-by; this migration closes the schema side so no
-- future writer can reopen it. Expand-only: backfills, then tightens; no DROP,
-- nothing reader-visible removed.
-- expand-contract: contract-of=0032

UPDATE operator_principals
   SET granted_by = 'backfill-0047-unattributed'
 WHERE granted_by IS NULL OR granted_by = '';

ALTER TABLE operator_principals
  ALTER COLUMN granted_by SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'operator_principals_granted_by_nonempty') THEN
    ALTER TABLE operator_principals
      ADD CONSTRAINT operator_principals_granted_by_nonempty
      CHECK (granted_by <> '');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'operator_principals_environment_valid') THEN
    ALTER TABLE operator_principals
      ADD CONSTRAINT operator_principals_environment_valid
      CHECK (environment IN ('staging', 'production'));
  END IF;
END $$;
