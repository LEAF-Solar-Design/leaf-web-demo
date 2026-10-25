-- Expand only. Apply forward-normal before adopting an activation reader.
-- Rollback retains these tables; disable the worker or pause it globally.
CREATE TABLE IF NOT EXISTS lab_activations (
    tenant_id TEXT NOT NULL, activation_id TEXT NOT NULL, doc_id TEXT NOT NULL,
    revision INTEGER NOT NULL, graph_sha256 TEXT NOT NULL, registry_digest TEXT NOT NULL,
    ops_revision TEXT NOT NULL, input_digest TEXT NOT NULL, principal TEXT NOT NULL,
    interval_s INTEGER NOT NULL CHECK (interval_s BETWEEN 60 AND 86400),
    state TEXT NOT NULL CHECK (state IN ('active','paused','expired','stopped','off')),
    reason TEXT, lease_expires_at TIMESTAMPTZ NOT NULL, next_due_at TIMESTAMPTZ NOT NULL,
    in_flight_run_id TEXT, coalesced_ticks INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0, last_started_at TIMESTAMPTZ,
    last_output JSONB, last_summary JSONB, created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, activation_id)
);
CREATE INDEX IF NOT EXISTS lab_activations_due_idx ON lab_activations (state,next_due_at);
CREATE TABLE IF NOT EXISTS lab_activation_transitions (
    tenant_id TEXT NOT NULL, activation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL, reason TEXT, actor TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, activation_id, seq),
    FOREIGN KEY (tenant_id, activation_id) REFERENCES lab_activations (tenant_id, activation_id)
);
