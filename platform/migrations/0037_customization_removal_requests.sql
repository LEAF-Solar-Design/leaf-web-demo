-- Durable one-row removal intent and exact rollback predecessor binding.
CREATE TABLE IF NOT EXISTS customization_removal_requests (
  tenant_id TEXT NOT NULL,
  change_set_id TEXT NOT NULL REFERENCES customization_change_sets(change_set_id),
  target_tool_name TEXT NOT NULL,
  expected_catalog_digest TEXT NOT NULL,
  predecessor_change_set_id TEXT NOT NULL REFERENCES customization_change_sets(change_set_id),
  predecessor_catalog_commit TEXT NOT NULL,
  predecessor_catalog_digest TEXT NOT NULL,
  predecessor_platform_release TEXT NOT NULL,
  predecessor_workspace_contract_digest TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT to_char(
    clock_timestamp() AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
  ),
  PRIMARY KEY (tenant_id, change_set_id)
);
