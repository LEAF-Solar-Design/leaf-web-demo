-- Expand only. Applied by the native schema stage before adopting readers.
CREATE TABLE IF NOT EXISTS lab_runs (
    tenant_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    graph_revision INTEGER NOT NULL,
    graph_sha256 TEXT NOT NULL,
    registry_digest TEXT NOT NULL,
    ops_revision TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    adapter_revision TEXT NOT NULL,
    build_revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','running','finished','failed','cancelled')),
    lease_owner TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    next_seq INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS lab_runs_doc_created_idx
    ON lab_runs (tenant_id, doc_id, created_at DESC);
CREATE TABLE IF NOT EXISTS lab_run_events (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    block_id TEXT,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, seq),
    FOREIGN KEY (tenant_id, run_id) REFERENCES lab_runs (tenant_id, run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS lab_run_events_created_idx ON lab_run_events (created_at);
