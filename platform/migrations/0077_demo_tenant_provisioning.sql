-- Demo classification is reserved for the live demo mint transaction.
-- expand-contract: contract-of=0075
ALTER TABLE orgs DROP CONSTRAINT IF EXISTS orgs_account_class_check;
ALTER TABLE orgs ADD CONSTRAINT orgs_account_class_check
  CHECK (account_class IN ('customer', 'production_acceptance', 'demo'));

ALTER TABLE unit_economics_observations
  DROP CONSTRAINT IF EXISTS unit_economics_observations_org_class_check;
ALTER TABLE unit_economics_observations
  ADD CONSTRAINT unit_economics_observations_org_class_check
  CHECK ((org_id IS NULL AND org_account_class IS NULL) OR
         (org_id IS NOT NULL AND org_account_class IS NOT NULL AND
          org_account_class IN ('customer', 'production_acceptance', 'demo')));
