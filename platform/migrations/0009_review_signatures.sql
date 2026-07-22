-- Verified professional credentials and immutable evidence-root countersigns.
-- Private signing keys never enter PostgreSQL; provider_key_ref is opaque.
CREATE TABLE IF NOT EXISTS professional_credentials (
  credential_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  binding_id UUID NOT NULL,
  profession TEXT NOT NULL CHECK (profession = 'professional_engineer'),
  jurisdiction TEXT NOT NULL CHECK (length(trim(jurisdiction)) > 0),
  license_ref TEXT NOT NULL CHECK (length(trim(license_ref)) > 0),
  signature_algorithm TEXT NOT NULL
    CHECK (signature_algorithm IN ('ed25519', 'ecdsa-p256-sha256')),
  public_key BYTEA NOT NULL,
  provider_key_ref TEXT NOT NULL CHECK (length(trim(provider_key_ref)) > 0),
  verified_by TEXT NOT NULL CHECK (length(trim(verified_by)) > 0),
  verified_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (expires_at > verified_at),
  FOREIGN KEY (org_id, binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id) ON DELETE CASCADE,
  UNIQUE (jurisdiction, license_ref),
  UNIQUE (org_id, credential_id)
);

CREATE TABLE IF NOT EXISTS professional_credential_events (
  event_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, credential_id)
    REFERENCES professional_credentials(org_id, credential_id) ON DELETE CASCADE,
  UNIQUE (credential_id, sequence)
);

CREATE TABLE IF NOT EXISTS review_signatures (
  signature_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  bundle_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  actor_binding_id UUID NOT NULL,
  history_operation_id UUID NOT NULL,
  idempotency_key TEXT NOT NULL,
  root_sha256 TEXT NOT NULL CHECK (root_sha256 ~ '^[0-9a-f]{64}$'),
  signature_contract TEXT NOT NULL CHECK (signature_contract = 'leaf.review-signature.v1'),
  signature_algorithm TEXT NOT NULL
    CHECK (signature_algorithm IN ('ed25519', 'ecdsa-p256-sha256')),
  signed_payload JSONB NOT NULL,
  signature_bytes BYTEA NOT NULL,
  signed_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id, bundle_id)
    REFERENCES evidence_bundles(org_id, project_id, bundle_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, credential_id)
    REFERENCES professional_credentials(org_id, credential_id),
  FOREIGN KEY (org_id, actor_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  FOREIGN KEY (org_id, project_id, history_operation_id)
    REFERENCES history_operations(org_id, project_id, operation_id) ON DELETE CASCADE,
  UNIQUE (org_id, project_id, idempotency_key),
  UNIQUE (bundle_id, credential_id),
  UNIQUE (history_operation_id),
  UNIQUE (org_id, project_id, signature_id)
);

DROP TRIGGER IF EXISTS professional_credentials_immutable ON professional_credentials;
CREATE TRIGGER professional_credentials_immutable BEFORE UPDATE OR DELETE ON professional_credentials
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS professional_credential_events_immutable ON professional_credential_events;
CREATE TRIGGER professional_credential_events_immutable BEFORE UPDATE OR DELETE ON professional_credential_events
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS review_signatures_immutable ON review_signatures;
CREATE TRIGGER review_signatures_immutable BEFORE UPDATE OR DELETE ON review_signatures
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
