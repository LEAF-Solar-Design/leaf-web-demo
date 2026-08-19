-- Out-of-band operator principal roster (contract/OPERATOR.md §1).
-- Written ONLY by scripts/operator_principal_admin.py with a direct
-- DATABASE_URL; the app reads it (future consumers key authority checks on
-- role_revision) but never writes it. Every mutation bumps role_revision so
-- approvals/authorities bound to a prior revision deny at redemption.
-- granted_by is NOT NULL: an authorization row with no accountable grantor is
-- invalid by schema, not by convention.

CREATE TABLE IF NOT EXISTS operator_principals (
  subject       TEXT PRIMARY KEY CHECK (subject <> ''),
  role          TEXT NOT NULL DEFAULT 'operator' CHECK (role <> ''),
  role_revision INTEGER NOT NULL DEFAULT 1 CHECK (role_revision >= 1),
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'revoked')),
  profiles      TEXT[] NOT NULL DEFAULT ARRAY['default'],
  environment   TEXT NOT NULL DEFAULT 'staging'
                CHECK (environment IN ('staging', 'production')),
  granted_by    TEXT NOT NULL CHECK (granted_by <> ''),
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
