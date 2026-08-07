-- Durable exact binding for split-turn tenant MCP human approvals.
CREATE TABLE IF NOT EXISTS harness_tenant_mcp_approvals (
  approval_id TEXT CONSTRAINT harness_tenant_mcp_approvals_pkey PRIMARY KEY
    CONSTRAINT harness_tenant_mcp_approvals_id_check
    CHECK (approval_id ~ '^[A-Za-z0-9_-]{8,256}$'),
  tenant_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  authority_turn_id TEXT NOT NULL,
  subscription_mount_id TEXT NOT NULL,
  runner_profile_id TEXT NOT NULL
    CONSTRAINT harness_tenant_mcp_approvals_profile_check
    CHECK (runner_profile_id IN ('author', 'spine')),
  service_id TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  arguments JSONB NOT NULL
    CONSTRAINT harness_tenant_mcp_approvals_arguments_check
    CHECK (jsonb_typeof(arguments) = 'object'),
  argument_digest TEXT NOT NULL
    CONSTRAINT harness_tenant_mcp_approvals_digest_check
    CHECK (argument_digest ~ '^[a-f0-9]{64}$'),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  execution_state TEXT NOT NULL DEFAULT 'pending'
    CONSTRAINT harness_tenant_mcp_approvals_state_check
    CHECK (execution_state IN ('pending', 'approved', 'executing', 'completed', 'uncertain')),
  approved_at TIMESTAMPTZ,
  execution_claim_id TEXT
    CONSTRAINT harness_tenant_mcp_approvals_claim_check
    CHECK (execution_claim_id IS NULL OR execution_claim_id ~ '^[A-Za-z0-9_-]{16,256}$'),
  execution_started_at TIMESTAMPTZ,
  execution_deadline_at TIMESTAMPTZ,
  result JSONB CONSTRAINT harness_tenant_mcp_approvals_result_check CHECK (
    result IS NULL OR (
      jsonb_typeof(result) = 'object'
      AND jsonb_typeof(result->'content') = 'string'
      AND (result - 'content' - 'artifact_ids' - 'receipt_id') = '{}'::jsonb
      AND (NOT (result ? 'artifact_ids') OR jsonb_typeof(result->'artifact_ids') = 'array')
      AND (NOT (result ? 'receipt_id') OR jsonb_typeof(result->'receipt_id') = 'string')
    )
  ),
  completed_at TIMESTAMPTZ,
  uncertain_at TIMESTAMPTZ,
  CONSTRAINT harness_tenant_mcp_approvals_shape_check CHECK (
    (
      execution_state = 'pending'
      AND execution_claim_id IS NULL
      AND execution_started_at IS NULL
      AND execution_deadline_at IS NULL
      AND approved_at IS NULL
      AND result IS NULL
      AND completed_at IS NULL
      AND uncertain_at IS NULL
    ) OR (
      execution_state = 'approved'
      AND execution_claim_id IS NULL
      AND approved_at IS NOT NULL
      AND execution_started_at IS NULL
      AND execution_deadline_at IS NULL
      AND result IS NULL
      AND completed_at IS NULL
      AND uncertain_at IS NULL
    ) OR (
      execution_state = 'executing'
      AND execution_claim_id IS NOT NULL
      AND approved_at IS NOT NULL
      AND execution_started_at IS NOT NULL
      AND execution_deadline_at IS NOT NULL
      AND result IS NULL
      AND completed_at IS NULL
      AND uncertain_at IS NULL
    ) OR (
      execution_state = 'completed'
      AND execution_claim_id IS NOT NULL
      AND approved_at IS NOT NULL
      AND execution_started_at IS NOT NULL
      AND execution_deadline_at IS NOT NULL
      AND result IS NOT NULL
      AND completed_at IS NOT NULL
      AND uncertain_at IS NULL
    ) OR (
      execution_state = 'uncertain'
      AND execution_claim_id IS NOT NULL
      AND approved_at IS NOT NULL
      AND execution_started_at IS NOT NULL
      AND execution_deadline_at IS NOT NULL
      AND result IS NULL
      AND completed_at IS NULL
      AND uncertain_at IS NOT NULL
    )
  )
);

CREATE INDEX IF NOT EXISTS idx_harness_tenant_mcp_approvals_expiry
  ON harness_tenant_mcp_approvals (expires_at);
