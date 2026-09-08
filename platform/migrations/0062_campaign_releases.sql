-- Bounded releases belong to the existing campaign authority.
CREATE UNIQUE INDEX IF NOT EXISTS campaigns_release_scope
  ON campaigns(org_id, project_id, campaign_id);
CREATE TABLE IF NOT EXISTS campaign_releases (
  release_id UUID PRIMARY KEY,
  org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL,
  principal_id UUID NOT NULL,
  delivery_profile TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','queued','waiting','paused','finished','cancelled','needs_approach')),
  contract_version INTEGER NOT NULL CHECK (contract_version > 0),
  contract JSONB NOT NULL CHECK (jsonb_typeof(contract)='object' AND octet_length(contract::text)<=65536),
  idempotency_key TEXT NOT NULL,
  payload_fingerprint TEXT NOT NULL,
  next_action JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (org_id, project_id, campaign_id, release_id),
  UNIQUE (org_id, project_id, campaign_id, idempotency_key),
  FOREIGN KEY (org_id, project_id, campaign_id) REFERENCES campaigns(org_id, project_id, campaign_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS campaign_releases_active_org
  ON campaign_releases(org_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS campaign_release_contracts (
  org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL, release_id UUID NOT NULL,
  contract_version INTEGER NOT NULL CHECK (contract_version > 0),
  contract JSONB NOT NULL CHECK (jsonb_typeof(contract)='object' AND octet_length(contract::text)<=65536),
  reason TEXT NOT NULL, principal_id UUID NOT NULL,
  idempotency_key TEXT NOT NULL, payload_fingerprint TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (org_id, project_id, campaign_id, release_id, contract_version),
  UNIQUE (org_id, project_id, campaign_id, release_id, idempotency_key),
  FOREIGN KEY (org_id, project_id, campaign_id, release_id)
    REFERENCES campaign_releases(org_id, project_id, campaign_id, release_id)
);
CREATE TABLE IF NOT EXISTS campaign_release_decisions (
  decision_id UUID PRIMARY KEY,
  org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL, release_id UUID NOT NULL,
  decision_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('scope','capability_selection','revision','external_dependency','answer')),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
  decided_by TEXT NOT NULL, payload_fingerprint TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (org_id, project_id, campaign_id, release_id, decision_key),
  FOREIGN KEY (org_id, project_id, campaign_id, release_id)
    REFERENCES campaign_releases(org_id, project_id, campaign_id, release_id)
);
CREATE TABLE IF NOT EXISTS campaign_release_stages (
  stage_id UUID PRIMARY KEY, seq BIGSERIAL UNIQUE,
  org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL, release_id UUID NOT NULL,
  contract_version INTEGER NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('implementation','publication','deployment','user_verification','delivery')),
  status TEXT NOT NULL CHECK (status IN ('passed','failed','unavailable')),
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object' AND octet_length(evidence::text)<=65536),
  producer TEXT NOT NULL,
  operation_key TEXT NOT NULL, payload_fingerprint TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (org_id, project_id, campaign_id, release_id, contract_version, stage, operation_key),
  FOREIGN KEY (org_id, project_id, campaign_id, release_id, contract_version)
    REFERENCES campaign_release_contracts(org_id, project_id, campaign_id, release_id, contract_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS campaign_release_stage_success
  ON campaign_release_stages(org_id, project_id, campaign_id, release_id, contract_version, stage)
  WHERE status='passed';
DO $$
DECLARE tab TEXT;
BEGIN
  FOREACH tab IN ARRAY ARRAY['campaign_release_contracts','campaign_release_decisions','campaign_release_stages'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=tab::regclass AND tgname=tab || '_immutable' AND NOT tgisinternal) THEN
      EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation()', tab || '_immutable', tab);
    END IF;
  END LOOP;
END
$$;
