-- Classification is supplied only by trusted provisioning code.
ALTER TABLE orgs
  ADD COLUMN IF NOT EXISTS account_class TEXT NOT NULL DEFAULT 'customer';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'orgs'::regclass AND conname = 'orgs_account_class_check'
  ) THEN
    ALTER TABLE orgs ADD CONSTRAINT orgs_account_class_check
      CHECK (account_class IN ('customer', 'production_acceptance'));
  END IF;
END; $$;

-- Immutable observations survive removal of their organization; no FK.
ALTER TABLE unit_economics_observations
  ADD COLUMN IF NOT EXISTS org_id UUID;

ALTER TABLE unit_economics_observations
  ADD COLUMN IF NOT EXISTS org_account_class TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'unit_economics_observations'::regclass
      AND conname = 'unit_economics_observations_org_class_check'
  ) THEN
    ALTER TABLE unit_economics_observations
      ADD CONSTRAINT unit_economics_observations_org_class_check
      CHECK ((org_id IS NULL AND org_account_class IS NULL) OR
             (org_id IS NOT NULL AND org_account_class IS NOT NULL AND
              org_account_class IN ('customer', 'production_acceptance')));
  END IF;
END; $$;
