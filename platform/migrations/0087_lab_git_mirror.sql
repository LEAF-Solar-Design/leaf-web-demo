-- Expand only: the ledger remains authoritative during mirror and replay.
CREATE TABLE IF NOT EXISTS lab_git_tenants (
    tenant_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'ledger' CHECK (mode IN ('ledger','replaying','mirror')),
    frozen BOOLEAN NOT NULL DEFAULT FALSE,
    remote_sha TEXT,
    local_sha TEXT,
    receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    notified JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lab_git_mirror (
    tenant_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    "commit" TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, doc_id, revision)
);
