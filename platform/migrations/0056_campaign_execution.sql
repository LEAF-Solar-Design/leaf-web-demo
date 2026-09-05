-- Campaign execution authority. Provider authorization belongs to service adapters.
CREATE TABLE IF NOT EXISTS campaign_tasks (
  task_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  task_key TEXT NOT NULL CHECK (char_length(task_key) BETWEEN 1 AND 128 AND task_key ~ '^[A-Za-z0-9._-]+$'),
  kind TEXT NOT NULL DEFAULT 'task' CHECK (kind IN ('task', 'capability')),
  parent_task_id UUID REFERENCES campaign_tasks(task_id),
  title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  spec TEXT NOT NULL CHECK (char_length(spec) BETWEEN 1 AND 16384),
  capability TEXT NOT NULL CHECK (char_length(capability) BETWEEN 1 AND 64 AND capability ~ '^[a-z][a-z0-9._-]*$'),
  stages JSONB NOT NULL CHECK (CASE WHEN jsonb_typeof(stages)='array' THEN jsonb_array_length(stages) BETWEEN 1 AND 6 ELSE FALSE END),
  owned_paths JSONB NOT NULL CHECK (CASE WHEN jsonb_typeof(owned_paths)='array' THEN jsonb_array_length(owned_paths)<=64 ELSE FALSE END),
  declared_artifacts JSONB NOT NULL CHECK (CASE WHEN jsonb_typeof(declared_artifacts)='array' THEN jsonb_array_length(declared_artifacts)<=32 ELSE FALSE END),
  source_sha TEXT NOT NULL CHECK (source_sha ~ '^[0-9a-f]{40}$'),
  verify_command TEXT NOT NULL CHECK (char_length(verify_command) BETWEEN 1 AND 4096),
  idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
  payload_fingerprint TEXT NOT NULL CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','succeeded','failed','reconcile_required','cancelled')),
  current_stage TEXT NOT NULL CHECK (current_stage IN ('implementation','build_test','publication','deployment','verification','cleanup')),
  fence BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT campaign_tasks_key_unique UNIQUE (campaign_id, task_key),
  CONSTRAINT campaign_tasks_idempotency_unique UNIQUE (org_id, project_id, campaign_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS campaign_tasks_status ON campaign_tasks(campaign_id, status, created_at);

CREATE TABLE IF NOT EXISTS campaign_task_dependencies (
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id) ON DELETE CASCADE,
  depends_on_task_id UUID NOT NULL REFERENCES campaign_tasks(task_id) ON DELETE CASCADE,
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on_task_id),
  CHECK (task_id <> depends_on_task_id),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS campaign_task_questions (
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id) ON DELETE CASCADE,
  question_id UUID NOT NULL REFERENCES campaign_questions(question_id),
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, question_id),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS campaign_task_attempts (
  attempt_id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id) ON DELETE CASCADE,
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  fence BIGINT NOT NULL,
  attempt_token_hash TEXT NOT NULL CHECK (attempt_token_hash ~ '^[0-9a-f]{64}$'),
  worker_id TEXT NOT NULL CHECK (char_length(worker_id) BETWEEN 1 AND 128),
  stage TEXT NOT NULL CHECK (stage IN ('implementation','build_test','publication','deployment','verification','cleanup')),
  outward_operation_key TEXT CHECK (char_length(outward_operation_key) BETWEEN 1 AND 256),
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deadline_at TIMESTAMPTZ NOT NULL,
  settled_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('active','settled','expired')),
  budget_reservation_ref TEXT CHECK (char_length(budget_reservation_ref) BETWEEN 1 AND 256),
  CONSTRAINT campaign_task_attempts_fence_unique UNIQUE (task_id, fence),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS campaign_task_attempts_one_active ON campaign_task_attempts(task_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS campaign_stage_receipts (
  receipt_id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id),
  attempt_id UUID NOT NULL REFERENCES campaign_task_attempts(attempt_id),
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  stage TEXT NOT NULL CHECK (stage IN ('implementation','build_test','publication','deployment','verification','cleanup')),
  fence BIGINT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','unknown')),
  result JSONB NOT NULL CHECK (pg_column_size(result)<=65536),
  result_fingerprint TEXT NOT NULL CHECK (result_fingerprint ~ '^[0-9a-f]{64}$'),
  artifact_ref TEXT CHECK (char_length(artifact_ref) BETWEEN 1 AND 1024),
  outward_operation_key TEXT CHECK (char_length(outward_operation_key) BETWEEN 1 AND 256),
  resource_identity TEXT CHECK (char_length(resource_identity) BETWEEN 1 AND 1024),
  rollback_identity TEXT CHECK (char_length(rollback_identity) BETWEEN 1 AND 1024),
  verified BOOLEAN NOT NULL DEFAULT FALSE,
  reconciles_receipt_id UUID REFERENCES campaign_stage_receipts(receipt_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT campaign_stage_receipts_attempt_unique UNIQUE (attempt_id),
  CONSTRAINT campaign_stage_receipts_reconciliation_unique UNIQUE (reconciles_receipt_id),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS campaign_stage_receipts_one_success ON campaign_stage_receipts(task_id, stage) WHERE outcome='succeeded';
CREATE TABLE IF NOT EXISTS campaign_events (
  event_id UUID PRIMARY KEY, seq BIGSERIAL UNIQUE,
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  task_id UUID REFERENCES campaign_tasks(task_id),
  attempt_id UUID REFERENCES campaign_task_attempts(attempt_id),
  fence BIGINT,
  event_type TEXT NOT NULL CHECK (event_type IN ('task_submitted','question_linked','attempt_claimed','attempt_expired','stage_succeeded','stage_failed','outcome_unknown','reconciled','task_retried')),
  payload JSONB NOT NULL CHECK (pg_column_size(payload)<=16384),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='campaign_stage_receipts'::regclass AND tgname='campaign_stage_receipts_immutable' AND NOT tgisinternal) THEN
    CREATE TRIGGER campaign_stage_receipts_immutable BEFORE UPDATE OR DELETE ON campaign_stage_receipts FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='campaign_events'::regclass AND tgname='campaign_events_immutable' AND NOT tgisinternal) THEN
    CREATE TRIGGER campaign_events_immutable BEFORE UPDATE OR DELETE ON campaign_events FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
