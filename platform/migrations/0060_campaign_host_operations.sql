-- A host operation is subordinate to an existing async job, never a scheduler.
ALTER TABLE campaign_capability_links
  ADD COLUMN IF NOT EXISTS catalog_commit TEXT CHECK (catalog_commit ~ '^[0-9a-f]{40}$'),
  ADD COLUMN IF NOT EXISTS effective_catalog_digest TEXT CHECK (effective_catalog_digest ~ '^[0-9a-f]{64}$'),
  ADD COLUMN IF NOT EXISTS tool_name TEXT CHECK (tool_name = 'campaign-host-enrollment'),
  ADD COLUMN IF NOT EXISTS tool_manifest_sha256 TEXT CHECK (tool_manifest_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  ADD COLUMN IF NOT EXISTS tool_source_sha256 TEXT CHECK (tool_source_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS campaigns_host_scope_uq
  ON campaigns(campaign_id, org_id, project_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS campaign_enrollments_host_scope_uq
  ON campaign_host_enrollments(enrollment_id, org_id, project_id, campaign_id);
CREATE UNIQUE INDEX IF NOT EXISTS campaign_links_host_scope_uq
  ON campaign_capability_links(link_id, enrollment_id, org_id, project_id, campaign_id);
CREATE UNIQUE INDEX IF NOT EXISTS async_jobs_host_scope_uq
  ON async_jobs(job_id, tenant_id, org_id, project_id);

CREATE TABLE IF NOT EXISTS campaign_capability_invocations (
  job_id UUID PRIMARY KEY,
  async_job_id TEXT GENERATED ALWAYS AS (job_id::TEXT) STORED,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  job_org_id TEXT GENERATED ALWAYS AS (org_id::TEXT) STORED,
  job_project_id TEXT GENERATED ALWAYS AS (project_id::TEXT) STORED,
  campaign_id UUID NOT NULL,
  link_id UUID NOT NULL,
  enrollment_id UUID NOT NULL,
  tenant_id TEXT NOT NULL,
  context JSONB NOT NULL CHECK (jsonb_typeof(context) = 'object'),
  context_sha256 TEXT NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  counted_receipt_digest TEXT CHECK (counted_receipt_digest ~ '^[0-9a-f]{64}$'),
  counted_receipt_id TEXT,
  counted_at TIMESTAMPTZ,
  CHECK ((counted_at IS NULL AND counted_receipt_digest IS NULL AND counted_receipt_id IS NULL)
    OR (counted_at IS NOT NULL AND counted_receipt_digest IS NOT NULL AND counted_receipt_id IS NOT NULL)),
  UNIQUE (job_id, org_id, project_id, campaign_id, link_id, enrollment_id, tenant_id),
  FOREIGN KEY (async_job_id, tenant_id, job_org_id, job_project_id)
    REFERENCES async_jobs(job_id, tenant_id, org_id, project_id),
  FOREIGN KEY (campaign_id, org_id, project_id, tenant_id)
    REFERENCES campaigns(campaign_id, org_id, project_id, tenant_id),
  FOREIGN KEY (enrollment_id, org_id, project_id, campaign_id)
    REFERENCES campaign_host_enrollments(enrollment_id, org_id, project_id, campaign_id),
  FOREIGN KEY (link_id, enrollment_id, org_id, project_id, campaign_id)
    REFERENCES campaign_capability_links(link_id, enrollment_id, org_id, project_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS campaign_host_operations (
  operation_id UUID PRIMARY KEY,
  job_id UUID NOT NULL UNIQUE,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  campaign_id UUID NOT NULL,
  link_id UUID NOT NULL,
  enrollment_id UUID NOT NULL,
  tenant_id TEXT NOT NULL,
  machine_id TEXT NOT NULL CHECK (char_length(machine_id) BETWEEN 1 AND 200),
  service_subject TEXT NOT NULL,
  input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
  profile_selector TEXT NOT NULL CHECK (profile_selector = 'campaign-default-v1'),
  attempt BIGINT NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 9007199254740991),
  fence BIGINT NOT NULL DEFAULT 0 CHECK (fence BETWEEN 0 AND 9007199254740991),
  claim_sha256 TEXT CHECK (claim_sha256 ~ '^[0-9a-f]{64}$'),
  lease_expires_at TIMESTAMPTZ,
  stage TEXT NOT NULL DEFAULT 'apply' CHECK (stage IN ('apply','activate','readback')),
  outcome TEXT CHECK (outcome IN ('succeeded','held','failed')),
  stage_evidence JSONB NOT NULL DEFAULT '{}'::JSONB CHECK (jsonb_typeof(stage_evidence) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK ((claim_sha256 IS NULL) = (lease_expires_at IS NULL)),
  FOREIGN KEY (job_id, org_id, project_id, campaign_id, link_id, enrollment_id, tenant_id)
    REFERENCES campaign_capability_invocations(job_id, org_id, project_id, campaign_id, link_id, enrollment_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS campaign_host_operations_poll_idx
  ON campaign_host_operations(machine_id, created_at, operation_id) WHERE outcome IS NULL;

CREATE OR REPLACE FUNCTION campaign_invocation_identity_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - ARRAY['counted_receipt_digest','counted_receipt_id','counted_at',
                          'async_job_id','job_org_id','job_project_id'])
       IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['counted_receipt_digest','counted_receipt_id','counted_at',
                          'async_job_id','job_org_id','job_project_id'])
     OR (OLD.counted_at IS NOT NULL AND
         (NEW.counted_receipt_digest, NEW.counted_receipt_id, NEW.counted_at) IS DISTINCT FROM
         (OLD.counted_receipt_digest, OLD.counted_receipt_id, OLD.counted_at)) THEN
    RAISE EXCEPTION 'campaign invocation is immutable';
  END IF;
  RETURN NEW;
END
$$;
DO $$ BEGIN
  CREATE TRIGGER campaign_invocation_identity_immutable
    BEFORE UPDATE ON campaign_capability_invocations
    FOR EACH ROW EXECUTE FUNCTION campaign_invocation_identity_immutable();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
