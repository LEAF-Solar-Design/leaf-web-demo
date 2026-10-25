-- Expand only. Retain all tables on rollback; no reader is enabled here.
CREATE TABLE IF NOT EXISTS lab_git_generations (
 tenant_id TEXT PRIMARY KEY, generation BIGINT NOT NULL CHECK (generation > 0)
);
CREATE FUNCTION leaf_retain_git_generation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'lab_git_generations_retained'; END;
$$;
CREATE TRIGGER lab_git_generations_retained BEFORE DELETE ON lab_git_generations
FOR EACH ROW EXECUTE FUNCTION leaf_retain_git_generation();
CREATE TABLE IF NOT EXISTS lab_git_authority (
 tenant_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK (mode IN ('git','draining')),
 generation BIGINT NOT NULL, base_head TEXT, verified_head TEXT, updated_at TIMESTAMPTZ NOT NULL
);
CREATE FUNCTION leaf_lab_git_parents_valid(parents JSONB) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE AS $$
 SELECT CASE WHEN jsonb_typeof(parents) = 'array' THEN
 jsonb_array_length(parents) <= 4 AND NOT EXISTS (
 SELECT 1 FROM jsonb_array_elements(parents) p
 WHERE jsonb_typeof(p) <> 'string' OR (p #>> '{}') !~ '^[0-9a-f]{40}$') ELSE false END
$$;
CREATE TABLE IF NOT EXISTS lab_git_pending (
 op_id TEXT PRIMARY KEY CHECK (op_id ~ '^[0-9a-f]{32}$'), tenant_id TEXT NOT NULL,
 doc_id TEXT NOT NULL, revision BIGINT NOT NULL, parent_revision BIGINT,
 kind TEXT NOT NULL CHECK (kind IN ('graph','widget')), canonical_text TEXT NOT NULL,
 sha256 TEXT NOT NULL, base_blob TEXT, expected_parents JSONB NOT NULL CHECK (leaf_lab_git_parents_valid(expected_parents)),
 rebase_count INTEGER NOT NULL DEFAULT 0 CHECK (rebase_count BETWEEN 0 AND 3),
 fence INTEGER NOT NULL DEFAULT 1, lease_owner TEXT NOT NULL, lease_expires_at TIMESTAMPTZ NOT NULL,
 state TEXT NOT NULL DEFAULT 'staged' CHECK (state IN ('staged','committing','ambiguous','abandoned')),
 reserved_bytes BIGINT NOT NULL CHECK (reserved_bytes >= 0), new_document BOOLEAN NOT NULL,
 created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX lab_git_pending_live_idx ON lab_git_pending (tenant_id,doc_id) WHERE state <> 'abandoned';
CREATE TABLE IF NOT EXISTS lab_git_commits (
 tenant_id TEXT NOT NULL, "commit" TEXT NOT NULL CHECK ("commit" ~ '^[0-9a-f]{40}$'),
 op_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision BIGINT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('graph','widget')), parent TEXT, recorded_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY (tenant_id,"commit")
);
CREATE FUNCTION leaf_reject_git_commit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'lab_git_commits_append_only'; END;
$$;
CREATE TRIGGER lab_git_commits_immutable BEFORE UPDATE OR DELETE ON lab_git_commits
FOR EACH ROW EXECUTE FUNCTION leaf_reject_git_commit_mutation();
CREATE TABLE IF NOT EXISTS lab_git_outbox (
 outbox_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('publish','notify')), identity TEXT NOT NULL,
 sequence BIGINT NOT NULL, payload JSONB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL, done_at TIMESTAMPTZ,
 UNIQUE (tenant_id,kind,identity)
);
CREATE TABLE IF NOT EXISTS lab_git_quarantine (
 tenant_id TEXT NOT NULL, op_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision BIGINT NOT NULL,
 canonical_text TEXT NOT NULL, sha256 TEXT NOT NULL, reason TEXT NOT NULL,
 detected_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (tenant_id,op_id)
);
CREATE TABLE IF NOT EXISTS lab_git_transitions (
 tenant_id TEXT NOT NULL, transition_id TEXT NOT NULL CHECK (transition_id ~ '^[0-9a-f]{32}$'),
 kind TEXT NOT NULL CHECK (kind IN ('cutover','rollback')), from_generation BIGINT NOT NULL,
 to_generation BIGINT NOT NULL, barrier_head TEXT, owner TEXT NOT NULL,
 lease_expires_at TIMESTAMPTZ NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('planned','fenced','resolved','drained','proven','done','reverted')),
 created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY (tenant_id,transition_id)
);
CREATE UNIQUE INDEX lab_git_transition_live_idx ON lab_git_transitions (tenant_id) WHERE state NOT IN ('done','reverted');
CREATE FUNCTION leaf_lab_git_guard_exit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM lab_git_authority WHERE tenant_id=OLD.tenant_id) THEN
  IF TG_OP = 'UPDATE' THEN
   IF NEW.tenant_id IS NOT DISTINCT FROM OLD.tenant_id AND NEW.frozen IS TRUE AND NEW.mode = 'mirror' THEN RETURN NEW; END IF;
   IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN RAISE EXCEPTION 'lab_git_exit_refused'; END IF;
  END IF;
  IF current_setting('leaf.lab_git_writer', true) IS DISTINCT FROM OLD.tenant_id
   OR NOT EXISTS (SELECT 1 FROM lab_git_transitions WHERE tenant_id=OLD.tenant_id
    AND transition_id=current_setting('leaf.lab_git_transition', true) AND state NOT IN ('done','reverted'))
  THEN RAISE EXCEPTION 'lab_git_exit_refused'; END IF;
 END IF;
 IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER lab_git_tenants_exit_guard BEFORE UPDATE OR DELETE ON lab_git_tenants
FOR EACH ROW EXECUTE FUNCTION leaf_lab_git_guard_exit();
