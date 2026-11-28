CREATE TABLE IF NOT EXISTS lab_recovery_confirmations (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    reviewed_digest TEXT NOT NULL CHECK (reviewed_digest ~ '^[0-9a-f]{64}$'),
    actor TEXT NOT NULL,
    successor_doc_id TEXT NOT NULL,
    successor_run_id TEXT NOT NULL CHECK (successor_run_id <> source_run_id),
    successor_graph_sha256 TEXT NOT NULL CHECK (successor_graph_sha256 ~ '^[0-9a-f]{64}$'),
    lineage_json JSONB NOT NULL CHECK (jsonb_typeof(lineage_json) = 'object'),
    admission_json JSONB CHECK (jsonb_typeof(admission_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    admitted_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, source_run_id, project_id, reviewed_digest),
    UNIQUE (tenant_id, successor_doc_id),
    UNIQUE (tenant_id, successor_run_id),
    FOREIGN KEY (tenant_id, source_run_id) REFERENCES lab_runs (tenant_id, run_id) ON DELETE CASCADE,
    CHECK ((admitted_at IS NULL) = (admission_json IS NULL))
);
