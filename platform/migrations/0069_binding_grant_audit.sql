-- Expand-only first-binding authorization audit and fleet-wide rate buckets.
-- Rollback: disable issuance and retain these additive tables. No existing
-- entity or prior migration is changed. Audits prove decisions, not acceptance.
CREATE TABLE IF NOT EXISTS binding_grant_audit (
    request_id UUID PRIMARY KEY,
    actor_binding_id UUID NOT NULL,
    org_id UUID NOT NULL,
    project_id UUID NOT NULL,
    drawing_id UUID,
    version_id UUID NOT NULL,
    plugin_session_id UUID NOT NULL,
    fingerprint_sha256 TEXT NOT NULL CHECK (fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
    nonce_sha256 TEXT NOT NULL CHECK (nonce_sha256 ~ '^[0-9a-f]{64}$'),
    key_id TEXT NOT NULL CHECK (key_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    iat BIGINT NOT NULL,
    exp BIGINT NOT NULL CHECK (exp > iat AND exp - iat <= 300),
    policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    decision TEXT NOT NULL CHECK (decision IN ('authorized', 'refused')),
    refusal_reason TEXT CHECK (refusal_reason ~ '^[a-z0-9_]{1,100}$'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((decision = 'authorized' AND refusal_reason IS NULL AND drawing_id IS NOT NULL)
        OR (decision = 'refused' AND refusal_reason IS NOT NULL))
);
-- No target foreign keys: refusals can refer to nonexistent scoped targets,
-- and deleting material must not cascade away the decision record.
CREATE INDEX IF NOT EXISTS binding_grant_audit_scope
    ON binding_grant_audit (org_id, actor_binding_id, recorded_at);

CREATE TABLE IF NOT EXISTS binding_grant_counters (
    namespace TEXT NOT NULL,
    counter_key TEXT NOT NULL,
    value BIGINT NOT NULL CHECK (value >= 0),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace, counter_key)
);
