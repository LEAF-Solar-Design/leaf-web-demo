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
--
-- `turn_count` is maintained incrementally by session_store.append_event
-- (incremented in the SAME transaction as the `turn_started` event insert,
-- review finding 3), never computed at list time: a correlated COUNT(*) over
-- app_session_events per listed row would scan every event of every session
-- on the page, dominated by text_delta rows. Starts at 0 so a pre-0053 row
-- reads 0 until its next turn, not NULL.
ALTER TABLE app_sessions
  ADD COLUMN IF NOT EXISTS scope_kind TEXT,
  ADD COLUMN IF NOT EXISTS scope_handle TEXT,
  ADD COLUMN IF NOT EXISTS title TEXT,
  ADD COLUMN IF NOT EXISTS turn_count INTEGER NOT NULL DEFAULT 0;

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

-- scope_handle carries the same wire bound (routers/sessions.py
-- SCOPE_HANDLE_MAX = 128) as a database-level contract of its own, matching
-- the title CHECK below rather than leaning on the wire alone (review
-- finding 7).
DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_scope_handle_check
    CHECK (scope_handle IS NULL OR char_length(scope_handle) BETWEEN 1 AND 128);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_title_check
    CHECK (title IS NULL OR char_length(title) BETWEEN 1 AND 120);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE app_sessions
    ADD CONSTRAINT app_sessions_turn_count_check
    CHECK (turn_count >= 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- The list is newest-first per tenant, cursor-paged on (updated_at, session_id).
-- Non-project rows are found by tenant_id; project rows carry their org as
-- org_id (UUID) with tenant_id set to the reserved `project:<org>:<project>`
-- marker (0039), so they need their OWN index on the bare (uncast) org_id
-- column -- review finding 2: `org_id::text = $1` on the query side would
-- defeat a plain `org_id` index, so the query casts the PARAMETER
-- (`org_id = $1::uuid`) instead and this index stays a bare-column btree.
CREATE INDEX IF NOT EXISTS idx_app_sessions_tenant_recent
  ON app_sessions(tenant_id, updated_at DESC, session_id DESC);

CREATE INDEX IF NOT EXISTS idx_app_sessions_org_recent
  ON app_sessions(org_id, updated_at DESC, session_id DESC)
  WHERE org_id IS NOT NULL;
