-- Enrollment authorizes scoped recovery, not publication or deployment.
CREATE TABLE IF NOT EXISTS campaign_host_enrollments (
  enrollment_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  machine_id TEXT NOT NULL CHECK (char_length(machine_id) BETWEEN 1 AND 200),
  service_subject TEXT NOT NULL CHECK (char_length(service_subject) BETWEEN 1 AND 200),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','enabled','revoked')),
  enrolled_by_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  enabled_by_binding_id UUID REFERENCES identity_bindings(binding_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  enabled_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  CONSTRAINT campaign_host_enrollments_machine_unique UNIQUE (campaign_id, machine_id),
  CHECK ((state='pending' AND enabled_at IS NULL AND revoked_at IS NULL)
    OR (state='enabled' AND enabled_at IS NOT NULL AND revoked_at IS NULL)
    OR (state='revoked' AND revoked_at IS NOT NULL)),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS campaign_capability_links (
  link_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  task_id UUID NOT NULL REFERENCES campaign_tasks(task_id) ON DELETE CASCADE,
  enrollment_id UUID NOT NULL REFERENCES campaign_host_enrollments(enrollment_id) ON DELETE CASCADE,
  capability TEXT NOT NULL,
  author_stage_id TEXT,
  change_set_id TEXT,
  publication_id TEXT,
  effective_catalog_id TEXT,
  first_invocation_receipt_id TEXT,
  second_invocation_receipt_id TEXT,
  state TEXT NOT NULL DEFAULT 'pending_link' CHECK (state IN ('pending_link','published','invoked_once','completed')),
  CONSTRAINT campaign_capability_links_task_unique UNIQUE (campaign_id, task_id),
  CHECK (state='pending_link' OR publication_id IS NOT NULL),
  CHECK (state<>'completed' OR (first_invocation_receipt_id IS NOT NULL
    AND second_invocation_receipt_id IS NOT NULL
    AND first_invocation_receipt_id<>second_invocation_receipt_id)),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);

-- expand-contract: contract-of=0057
ALTER TABLE campaign_events DROP CONSTRAINT IF EXISTS campaign_events_event_type_check;
ALTER TABLE campaign_events ADD CONSTRAINT campaign_events_event_type_check CHECK (event_type IN (
  'task_submitted','question_linked','attempt_claimed','attempt_expired','stage_succeeded',
  'stage_failed','outcome_unknown','reconciled','task_retried',
  'remote_bound','remote_admitted','remote_settled',
  'enrollment_requested','enrollment_enabled','enrollment_revoked','capability_link_recorded'
));
