-- Expand only. Ephemeral admission leases; retain the table on rollback.
CREATE TABLE IF NOT EXISTS lab_stream_leases (
    principal TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (principal, stream_id)
);
