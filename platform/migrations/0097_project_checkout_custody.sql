-- Private proof of browser possession for one approved confirmation.
-- No raw capability, capability digest, bearer, or replacement checkout is stored.
CREATE TABLE IF NOT EXISTS project_checkout_custody (
 delegation_id UUID PRIMARY KEY,
 tenant_id TEXT NOT NULL,
 confirmation_id TEXT NOT NULL,
 session_id TEXT NOT NULL,
 subject TEXT NOT NULL CHECK (char_length(subject) > 0),
 actor_binding_id UUID NOT NULL,
 input_binding JSONB NOT NULL CHECK (jsonb_typeof(input_binding) = 'object'),
 approved_args JSONB NOT NULL CHECK (jsonb_typeof(approved_args) = 'object'),
 fence BIGINT NOT NULL CHECK (fence > 0),
 expires_at TIMESTAMPTZ NOT NULL,
 revoked_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CONSTRAINT custody_one_confirmation UNIQUE (tenant_id, confirmation_id)
);
CREATE INDEX IF NOT EXISTS custody_session ON project_checkout_custody(tenant_id, session_id);
