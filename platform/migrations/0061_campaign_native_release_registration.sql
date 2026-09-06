-- Registration only. Native release has no host operation or published tool.
ALTER TABLE campaign_host_enrollments
  ADD COLUMN IF NOT EXISTS capability TEXT NOT NULL DEFAULT 'campaign.host-enrollment';
DO $$ BEGIN
  ALTER TABLE campaign_host_enrollments ADD CONSTRAINT campaign_enrollments_capability_check
    CHECK (capability IN ('campaign.host-enrollment', 'campaign.native-release'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE campaign_host_enrollments
  DROP CONSTRAINT IF EXISTS campaign_host_enrollments_machine_unique;
DO $$ BEGIN
  ALTER TABLE campaign_host_enrollments ADD CONSTRAINT campaign_enrollments_machine_capability_unique
    UNIQUE (campaign_id, machine_id, capability);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
