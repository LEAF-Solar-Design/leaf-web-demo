-- Conversation scope and title on durable sessions (standardization slice 6b).
--
-- `scope_kind` / `scope_handle` record the {kind, handle} envelope a client
-- attached with (server/routers/sessions.py SessionScope: kind is one of
-- project | drawing | entity, handle is bounded and charset-validated at the
-- wire before it can reach this column). Both NULL on a row written before
-- this migration; the read path derives the scope from (drawing_id,
-- project_id) for those rows, so nothing is backfilled and nothing is
-- invented. Session IDENTITY is unchanged: the (tenant_id, drawing_id) and
-- (org_id, project_id) uniqueness targets stay exactly as 0012 / 0039 wrote
-- them; the scope is an attribute of the one session those keys name.
--
-- `title` is the first user text of the conversation (bounded at 120 chars,
-- written once by session_store.append_event on the first `turn_started`
-- that carries text, never rewritten), so GET /api/sessions can list rows
-- with one query and no per-row transcript read.
ALTER TABLE app_sessions
  ADD COLUMN IF NOT EXISTS scope_kind TEXT,
  ADD COLUMN IF NOT EXISTS scope_handle TEXT,
  ADD COLUMN IF NOT EXISTS title TEXT;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_scope_kind_check
    CHECK (scope_kind IS NULL OR scope_kind IN ('project', 'drawing', 'entity'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_scope_shape_check
    CHECK ((scope_kind IS NULL) = (scope_handle IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_title_check
    CHECK (title IS NULL OR char_length(title) BETWEEN 1 AND 120);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- The list is newest-first per tenant, cursor-paged on (updated_at, session_id);
-- both storage tenancies get the index the ORDER BY needs.
CREATE INDEX IF NOT EXISTS idx_app_sessions_tenant_recent
  ON app_sessions(tenant_id, updated_at DESC, session_id DESC);

CREATE INDEX IF NOT EXISTS idx_app_sessions_org_recent
  ON app_sessions(org_id, updated_at DESC, session_id DESC)
  WHERE org_id IS NOT NULL;
