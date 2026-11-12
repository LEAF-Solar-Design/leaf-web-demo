-- Additive custody tables. No existing identity, binding, tier or grant changes.
CREATE TABLE IF NOT EXISTS service_login_challenges (
    id uuid PRIMARY KEY,
    nonce_hash text NOT NULL CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
    environment text NOT NULL CHECK (environment IN ('staging','production')),
    origin text NOT NULL,
    expires_at timestamptz NOT NULL,
    approved_at timestamptz,
    consumed_at timestamptz,
    sealed_token text,
    subject text,
    token_expires_at timestamptz,
    CHECK (origin = CASE environment WHEN 'staging' THEN 'https://platform-staging.leafdesign.ai'
                  ELSE 'https://platform.leafdesign.ai' END),
    CHECK ((approved_at IS NULL AND sealed_token IS NULL AND subject IS NULL AND token_expires_at IS NULL)
        OR (approved_at IS NOT NULL AND sealed_token IS NOT NULL AND subject IS NOT NULL AND token_expires_at IS NOT NULL)),
    CHECK (consumed_at IS NULL OR approved_at IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS service_browser_sessions (
    binding_id uuid PRIMARY KEY,
    secret_hash text NOT NULL UNIQUE CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    sealed_token text NOT NULL,
    subject text NOT NULL,
    environment text NOT NULL CHECK (environment IN ('staging','production')),
    origin text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (origin = CASE environment WHEN 'staging' THEN 'https://platform-staging.leafdesign.ai'
                  ELSE 'https://platform.leafdesign.ai' END)
);
CREATE INDEX IF NOT EXISTS service_login_challenges_expiry ON service_login_challenges(expires_at);
CREATE INDEX IF NOT EXISTS service_browser_sessions_expiry ON service_browser_sessions(expires_at);
