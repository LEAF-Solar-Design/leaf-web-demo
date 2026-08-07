-- Durable exact binding for split-turn tenant MCP human approvals.
CREATE TABLE IF NOT EXISTS harness_tenant_mcp_approvals (
  approval_id TEXT PRIMARY KEY CHECK (approval_id ~ '^[A-Za-z0-9_-]{8,256}$'),
  tenant_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  authority_turn_id TEXT NOT NULL,
  subscription_mount_id TEXT NOT NULL,
  runner_profile_id TEXT NOT NULL CHECK (runner_profile_id IN ('author', 'spine')),
  service_id TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  arguments JSONB NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
  argument_digest TEXT NOT NULL CHECK (argument_digest ~ '^[a-f0-9]{64}$'),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_harness_tenant_mcp_approvals_expiry
  ON harness_tenant_mcp_approvals (expires_at);
