-- Widen the add-on registry without changing existing rows or mode checks.
-- expand-contract: contract-of=0083
ALTER TABLE tenant_cost_events DROP CONSTRAINT IF EXISTS tenant_cost_events_addon_check;
ALTER TABLE tenant_cost_events ADD CONSTRAINT tenant_cost_events_addon_check CHECK (addon IN (
    'aps_solve', 'e2b_exec', 'marathon', 'forge_hosting', 'aws_resources',
    'storage', 'canonical_solve', 'tool_ci', 'gpu_inference', 'ios_build', 'app_auth',
    'kashbot_runner'
));

ALTER TABLE tenant_addon_settings DROP CONSTRAINT IF EXISTS tenant_addon_settings_addon_check;
ALTER TABLE tenant_addon_settings ADD CONSTRAINT tenant_addon_settings_addon_check CHECK (addon IN (
    'aps_solve', 'e2b_exec', 'marathon', 'forge_hosting', 'aws_resources',
    'storage', 'canonical_solve', 'tool_ci', 'gpu_inference', 'ios_build', 'app_auth',
    'kashbot_runner'
));
