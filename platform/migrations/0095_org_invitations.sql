-- Expand only: one-time editor invitations; raw credentials are never persisted.
CREATE TABLE IF NOT EXISTS org_invitations (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES orgs(org_id),
  token_hash TEXT UNIQUE NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  role TEXT NOT NULL CHECK (role = 'editor'),
  label TEXT CHECK (char_length(label) <= 80 AND label !~ '[[:cntrl:]]'),
  created_by_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  accepted_at TIMESTAMPTZ,
  accepted_binding_id UUID REFERENCES identity_bindings(binding_id),
  revoked_at TIMESTAMPTZ,
  revoked_by_binding_id UUID REFERENCES identity_bindings(binding_id),
  CHECK ((accepted_at IS NULL) = (accepted_binding_id IS NULL)),
  CHECK ((revoked_at IS NULL) = (revoked_by_binding_id IS NULL)),
  CHECK (accepted_at IS NULL OR revoked_at IS NULL)
);
CREATE INDEX IF NOT EXISTS org_invitations_pending_org_idx ON org_invitations(org_id)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS org_invitation_receipts (
  id UUID PRIMARY KEY,
  invitation_id UUID NOT NULL REFERENCES org_invitations(id),
  org_id UUID NOT NULL REFERENCES orgs(org_id),
  event TEXT NOT NULL CHECK (event IN ('created', 'accepted', 'revoked')),
  actor_binding_id UUID NOT NULL REFERENCES identity_bindings(binding_id),
  at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION reject_org_invitation_receipt_mutation()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'org invitation receipts are append-only';
END;
$$ LANGUAGE plpgsql;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
      WHERE tgname = 'org_invitation_receipts_immutable'
      AND tgrelid = 'org_invitation_receipts'::regclass) THEN
    CREATE TRIGGER org_invitation_receipts_immutable
      BEFORE UPDATE OR DELETE ON org_invitation_receipts
      FOR EACH ROW EXECUTE FUNCTION reject_org_invitation_receipt_mutation();
  END IF;
END;
$$;
