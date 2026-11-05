-- Expand only. Native forward-normal schema release applies this migration.
CREATE TABLE IF NOT EXISTS device_enrollments (
    enrollment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    device_id TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    agent_version TEXT NOT NULL,
    platform JSONB NOT NULL,
    hostname TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT,
    packages JSONB NOT NULL DEFAULT '[]',
    free_mem_mb INTEGER,
    UNIQUE (tenant_id, device_id)
);

CREATE TABLE IF NOT EXISTS device_commands (
    command_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('pkg.install','pkg.upgrade','pkg.uninstall','pkg.health','http.proxy','ha.onboard','ha.flow.start','ha.flow.step','ha.flow.list','agent.pause','agent.update')),
    args JSONB NOT NULL,
    issued_by TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    leased_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    state TEXT NOT NULL CHECK (state IN ('queued','leased','done','expired')),
    result JSONB,
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, device_id)
);
CREATE INDEX IF NOT EXISTS device_commands_device_state_issued_idx
    ON device_commands (device_id, state, issued_at);

CREATE TABLE IF NOT EXISTS device_nonces (
    device_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (device_id, nonce)
);

CREATE TABLE IF NOT EXISTS device_relay_tokens (
    token_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    package TEXT NOT NULL,
    label TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, device_id)
);

CREATE TABLE IF NOT EXISTS device_audit (
    event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    actor TEXT NOT NULL,
    op TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','error','refused')),
    request_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS device_audit_device_at_idx ON device_audit (device_id, at DESC);
