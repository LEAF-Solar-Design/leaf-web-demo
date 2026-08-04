-- 0028_overlay_tokens.sql
-- T1 runtime theme/copy overlay: immutable proposals + a versioned per-tenant
-- document. Additive and idempotent, like every migration in this directory.
--
-- WHY TWO TABLES AND NOT ONE MUTABLE ROW. An adversarial review of the design
-- flagged that "INSERT the approved overlay" leaves concurrent approvals
-- overwriting and resurrecting each other. So: proposals are written once and
-- never edited (the audit trail stays true), and the live overlay is a single
-- versioned document per tenant updated by compare-and-swap against the exact
-- version the operator's card was rendered against.
--
-- WHY PER-TENANT AND NOT PLATFORM-WIDE. Operator decision 2026-08-04: there is
-- no platform-wide fast path at all, so a mistaken or hostile overlay is
-- bounded to one tenant BY CONSTRUCTION rather than by a policy check that
-- could be wrong. The schema enforces it: there is no row shape that means
-- "everyone".

-- Immutable proposal records. State transitions produce a new row (superseding
-- by proposal_id + revision), never an UPDATE of a decided row.
CREATE TABLE IF NOT EXISTS overlay_proposals (
  proposal_id       UUID        NOT NULL,
  revision          INTEGER     NOT NULL DEFAULT 0,
  tenant_id         UUID        NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  -- The requester's converse session. Scopes the PREVIEW: only this session
  -- sees a pending overlay, which is what makes the demo safe to show live.
  session_id        TEXT        NOT NULL,
  -- Validated token map ({token_id: canonical_value}). Content is validated by
  -- overlay_registry BEFORE it reaches here; the column stores the canonical
  -- form only.
  tokens            JSONB       NOT NULL,
  state             TEXT        NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending','approved','denied','expired','reverted')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Lease deadline. Expiry is a function of TIME, not of a sweeper running:
  -- readers compare against this, so an un-swept row is still expired.
  lease_expires_at  TIMESTAMPTZ NOT NULL,
  decided_at        TIMESTAMPTZ,
  decided_by        TEXT,
  -- Idempotency/replay witness: the SAME key retried returns the original
  -- outcome; a DIFFERENT key against a decided proposal is refused.
  decision_key      TEXT,
  -- Tenant-document version this approval produced (NULL for non-approvals).
  applied_version   INTEGER,
  reason            TEXT,
  PRIMARY KEY (proposal_id, revision)
);

-- One live proposal per session at a time: a second pending preview would make
-- "what the user is looking at" ambiguous, and the revoke path would not know
-- which overlay to pull.
CREATE UNIQUE INDEX IF NOT EXISTS overlay_proposals_one_pending_per_session
  ON overlay_proposals (tenant_id, session_id)
  WHERE state = 'pending';

-- The sweeper and the operator queue both read pending-by-deadline.
CREATE INDEX IF NOT EXISTS overlay_proposals_pending_lease_idx
  ON overlay_proposals (lease_expires_at)
  WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS overlay_proposals_tenant_created_idx
  ON overlay_proposals (tenant_id, created_at DESC);

-- The live per-tenant overlay. Exactly one row per tenant; `version` is the CAS
-- witness carried on the operator's card.
CREATE TABLE IF NOT EXISTS overlay_documents (
  tenant_id   UUID        PRIMARY KEY REFERENCES orgs(org_id) ON DELETE CASCADE,
  version     INTEGER     NOT NULL DEFAULT 0,
  tokens      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by  TEXT
);

-- Append-only audit. Deliberately carries a token COUNT and never token
-- content: tenant copy must not reach logs or exports through the audit trail.
CREATE TABLE IF NOT EXISTS overlay_audit (
  audit_id     BIGSERIAL   PRIMARY KEY,
  proposal_id  UUID        NOT NULL,
  tenant_id    UUID        NOT NULL,
  from_state   TEXT        NOT NULL,
  to_state     TEXT        NOT NULL,
  at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor        TEXT,
  decision_key TEXT,
  token_count  INTEGER     NOT NULL DEFAULT 0,
  detail       JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS overlay_audit_tenant_at_idx
  ON overlay_audit (tenant_id, at DESC);
