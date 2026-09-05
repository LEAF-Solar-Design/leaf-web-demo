-- Frozen remote request identity, not a budget or provider authority.
CREATE TABLE IF NOT EXISTS campaign_dispatch_bindings (
  attempt_id UUID PRIMARY KEY REFERENCES campaign_task_attempts(attempt_id),
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id) ON DELETE CASCADE,
  org_id UUID NOT NULL, project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  fence BIGINT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('implementation','build_test','publication','deployment','verification','cleanup')),
  request_id TEXT NOT NULL CHECK (request_id ~ '^cd-[0-9a-f]{48}$'),
  machine_id TEXT NOT NULL CHECK (char_length(machine_id) BETWEEN 1 AND 200),
  run_id TEXT NOT NULL CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  leaf_id TEXT NOT NULL CHECK (leaf_id ~ '^vmc-[0-9a-f]{48}$'),
  registration_id TEXT NOT NULL CHECK (registration_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  root_request_id TEXT NOT NULL CHECK (char_length(root_request_id) BETWEEN 1 AND 200),
  gateway_project_id TEXT NOT NULL CHECK (char_length(gateway_project_id) BETWEEN 1 AND 200),
  source_ref TEXT NOT NULL CHECK (source_ref ~ '^[0-9a-f]{40}$'),
  packet_digest TEXT NOT NULL CHECK (packet_digest ~ '^[0-9a-f]{64}$'),
  budget_class TEXT NOT NULL CHECK (budget_class IN ('explicit','daily')),
  reservation_micro_usd BIGINT NOT NULL CHECK (reservation_micro_usd > 0),
  submission_digest TEXT NOT NULL CHECK (submission_digest ~ '^[0-9a-f]{64}$'),
  binding_fingerprint TEXT NOT NULL CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
  state TEXT NOT NULL DEFAULT 'bound' CHECK (state IN ('bound','admitted','settled')),
  reservation_id TEXT CHECK (reservation_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  admitted_at TIMESTAMPTZ,
  remote_fencing_token BIGINT CHECK (remote_fencing_token >= 0),
  verdict_fingerprint TEXT CHECK (verdict_fingerprint ~ '^[0-9a-f]{64}$'),
  settled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT campaign_dispatch_bindings_request_unique UNIQUE (request_id),
  CONSTRAINT campaign_dispatch_bindings_leaf_unique UNIQUE (leaf_id),
  CONSTRAINT campaign_dispatch_bindings_task_fence_unique UNIQUE (task_id, fence),
  CHECK ((state = 'bound') = (admitted_at IS NULL)),
  CHECK (state <> 'settled' OR (verdict_fingerprint IS NOT NULL AND remote_fencing_token IS NOT NULL)),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
ALTER TABLE campaign_events DROP CONSTRAINT IF EXISTS campaign_events_event_type_check;
ALTER TABLE campaign_events ADD CONSTRAINT campaign_events_event_type_check CHECK (event_type IN (
  'task_submitted','question_linked','attempt_claimed','attempt_expired','stage_succeeded',
  'stage_failed','outcome_unknown','reconciled','task_retried',
  'remote_bound','remote_admitted','remote_settled'
));
