-- 0029_session_annex.sql
-- A PostgreSQL home for the two per-session annex tables that 0012 left on
-- SQLite: checkpoint metadata and the per-session approval policy.
--
-- Deliberately NO foreign key to app_sessions(session_id), unlike
-- app_session_events. These tables carry their OWN selector
-- (LEAF_SESSION_ANNEX_STORE), so `annex=postgres` with `sessions=legacy` is a
-- representable configuration, and under an FK every checkpoint write in it
-- would fail at INSERT time with a 500. The coupling that actually matters runs
-- the OTHER way -- a postgres sessions authority must not leave its annex on
-- task-local SQLite -- and that is declared as a selector dependency in
-- platform/authority-inventory.json, which fails at review time rather than in
-- a live request. Rows are per-session metadata, not the session's history:
-- orphans read as an empty checkpoint list and a default policy, both harmless.

CREATE TABLE IF NOT EXISTS app_session_checkpoints (
  checkpoint_id   TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,
  drawing_id      TEXT,
  drawing_version TEXT,
  transcript_seq  BIGINT NOT NULL DEFAULT 0 CHECK (transcript_seq >= 0),
  label           TEXT,
  created_at      DOUBLE PRECISION NOT NULL
);

-- Every read is (session_id, tenant_id) scoped at the STORAGE boundary and
-- ordered by (created_at, checkpoint_id); the cap count shares the leading
-- column. The tie-break column is in the index because `list_checkpoints`
-- orders by it, and a checkpoint list whose order depends on physical row
-- order is how two checkpoints created in the same clock tick swap places
-- between calls.
CREATE INDEX IF NOT EXISTS idx_app_session_checkpoints_scope
  ON app_session_checkpoints(session_id, tenant_id, created_at, checkpoint_id);

CREATE TABLE IF NOT EXISTS app_session_policies (
  session_id TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL,
  policy     TEXT NOT NULL
    CHECK (policy IN ('confirm_all', 'auto_approve_reads', 'plan_first')),
  updated_at DOUBLE PRECISION
);
