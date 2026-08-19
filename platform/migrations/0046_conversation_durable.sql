-- 0046 Project-bound durable conversation records.
--
-- A conversation is a browser project's persistent chat thread. This is a
-- wholly new authority (nothing legacy to migrate), so this file creates one
-- fresh table and writes no backfill INSERT...SELECT -- a populated snapshot
-- gains zero conversation rows and orphans none, because none are written.
--
-- Project-bound and tenant-scoped exactly like 0038/0039's child tables:
-- CASCADE on project delete (matches project_member_bindings, project_files,
-- app_sessions, app_session_requests -- every other project-bound row in this
-- lane), and the same (org_id, created_by_binding_id) actor FK 0038 uses for
-- project_files_creator_fk / project_lifecycle_receipts_actor_fk.
--
-- Rows are MUTABLE (title rename), so this table does NOT reuse
-- leaf_reject_ledger_mutation: that trigger exists for append-only ledgers
-- (project_lifecycle_receipts) and attaching it here would turn every rename
-- into a 500.

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id       UUID PRIMARY KEY,
  org_id                UUID NOT NULL,
  project_id            UUID NOT NULL,
  title                 TEXT,
  created_by_binding_id UUID NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT conversations_project_fk
    FOREIGN KEY (org_id, project_id)
    REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT conversations_creator_fk
    FOREIGN KEY (org_id, created_by_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT conversations_title_check
    CHECK (title IS NULL OR char_length(title) BETWEEN 1 AND 200)
);

CREATE INDEX IF NOT EXISTS idx_conversations_scope
  ON conversations(org_id, project_id, created_at DESC, conversation_id);
