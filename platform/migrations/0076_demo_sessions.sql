-- No org FK: LD1a allocates an identity only. Provisioning is a later slice.
CREATE TABLE IF NOT EXISTS demo_sessions (
    id UUID PRIMARY KEY,
    subject TEXT NOT NULL UNIQUE
        CONSTRAINT demo_sessions_subject_check CHECK (subject = 'demo:' || id::text),
    tenant_id TEXT NOT NULL UNIQUE
        CONSTRAINT demo_sessions_tenant_id_check CHECK (tenant_id = 'demo-' || id::text),
    org_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    client_ip_hash TEXT NOT NULL
        CONSTRAINT demo_sessions_client_ip_hash_check CHECK (client_ip_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT demo_sessions_check
        CHECK (expires_at > created_at AND expires_at <= created_at + INTERVAL '60 minutes')
);

CREATE TABLE IF NOT EXISTS demo_mint_counters (
    day DATE NOT NULL,
    scope TEXT NOT NULL
        CONSTRAINT demo_mint_counters_scope_check CHECK (scope = 'global' OR scope ~ '^ip:[0-9a-f]{64}$'),
    count INTEGER NOT NULL CONSTRAINT demo_mint_counters_count_check CHECK (count > 0),
    PRIMARY KEY (day, scope)
);
