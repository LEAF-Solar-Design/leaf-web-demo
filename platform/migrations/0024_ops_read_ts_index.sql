-- ts-leading index for the APS observability read-model's fleet-scope queries
-- (ops_metrics_read: fleet_metrics / tool_metrics / tenant_metrics when no
-- tenant filter is given). The existing broker_usage_ledger_tenant_ts_idx
-- (0014_broker.sql) leads on tenant_id and cannot serve a bare ts range, so a
-- fleet window degrades to a full-table scan as the ledger grows.
CREATE INDEX IF NOT EXISTS broker_usage_ledger_ts_idx
    ON broker_usage_ledger (ts);
