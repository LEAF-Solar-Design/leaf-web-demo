-- Trusted adapter source evidence, separate from immutable planning lineage.
CREATE TABLE IF NOT EXISTS campaign_attempt_input_sources (
  attempt_id UUID PRIMARY KEY REFERENCES campaign_task_attempts(attempt_id),
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id),
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id),
  fence BIGINT NOT NULL,
  repository_id TEXT NOT NULL CHECK (char_length(repository_id) BETWEEN 1 AND 200),
  commit_sha TEXT NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  tree_sha TEXT NOT NULL CHECK (tree_sha ~ '^[0-9a-f]{40}$'),
  bundle_sha256 TEXT NOT NULL CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$'),
  bundle_bytes BIGINT NOT NULL CHECK (bundle_bytes > 0),
  source_fingerprint TEXT NOT NULL CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id)
);
CREATE TABLE IF NOT EXISTS campaign_attempt_result_sources (
  attempt_id UUID PRIMARY KEY REFERENCES campaign_attempt_input_sources(attempt_id),
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id),
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id),
  fence BIGINT NOT NULL,
  repository_id TEXT NOT NULL CHECK (char_length(repository_id) BETWEEN 1 AND 200),
  commit_sha TEXT NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  tree_sha TEXT NOT NULL CHECK (tree_sha ~ '^[0-9a-f]{40}$'),
  publication_receipt JSONB NOT NULL CHECK (jsonb_typeof(publication_receipt)='object'
    AND publication_receipt <> '{}'::jsonb AND octet_length(publication_receipt::text)<=65536),
  publication_receipt_sha256 TEXT NOT NULL CHECK (publication_receipt_sha256 ~ '^[0-9a-f]{64}$'),
  result_fingerprint TEXT NOT NULL CHECK (result_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id)
);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='campaign_attempt_input_sources'::regclass AND tgname='campaign_attempt_input_sources_immutable' AND NOT tgisinternal) THEN
    CREATE TRIGGER campaign_attempt_input_sources_immutable BEFORE UPDATE OR DELETE ON campaign_attempt_input_sources FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='campaign_attempt_result_sources'::regclass AND tgname='campaign_attempt_result_sources_immutable' AND NOT tgisinternal) THEN
    CREATE TRIGGER campaign_attempt_result_sources_immutable BEFORE UPDATE OR DELETE ON campaign_attempt_result_sources FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
