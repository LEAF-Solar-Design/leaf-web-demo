-- Expand only. Rollback stops using the store and retains its custody rows.
-- PostgreSQL text cannot contain NUL or surrogate code points. Reject all
-- remaining Cc characters and exactly the non-Cc whitespace Python strips.
CREATE OR REPLACE FUNCTION project_solve_exchange_worker_ok(worker TEXT)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
  SELECT worker IS NULL OR (
    char_length(worker) BETWEEN 1 AND 200
    AND worker !~ ('[' || chr(1) || '-' || chr(31) || chr(127) || '-' || chr(159) || ']')
    AND worker = btrim(worker,
      chr(32) || chr(160) || chr(5760) || chr(8192) || chr(8193) || chr(8194)
      || chr(8195) || chr(8196) || chr(8197) || chr(8198) || chr(8199) || chr(8200)
      || chr(8201) || chr(8202) || chr(8232) || chr(8233) || chr(8239) || chr(8287)
      || chr(12288))
  );
$$;

CREATE TABLE IF NOT EXISTS project_solve_exchanges (
  job_id UUID NOT NULL,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  drawing_id UUID NOT NULL,
  input_version_id UUID NOT NULL,
  parent_intake_sha256 TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  graph_sha256 TEXT NOT NULL,
  frame_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  grant_ref TEXT NOT NULL,
  classification TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'prepared',
  request_bytes BYTEA NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_bytes BYTEA,
  response_sha256 TEXT,
  solver_endpoint TEXT,
  solver_version TEXT,
  candidate_bytes BYTEA,
  candidate_sha256 TEXT,
  proof_bytes BYTEA,
  proof_sha256 TEXT,
  sender_worker TEXT,
  sender_attempt INTEGER,
  refusal_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

  CONSTRAINT project_solve_exchanges_pkey PRIMARY KEY (job_id),
  CONSTRAINT project_solve_exchanges_job_fk
    FOREIGN KEY (org_id, project_id, job_id)
    REFERENCES jobs(org_id, project_id, job_id) ON DELETE CASCADE,
  CONSTRAINT project_solve_exchanges_artifact_fk
    FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id) ON DELETE CASCADE,
  CONSTRAINT project_solve_exchanges_input_fk
    FOREIGN KEY (input_version_id) REFERENCES drawing_versions(version_id),
  CONSTRAINT project_solve_exchanges_grant_check
    CHECK (grant_ref ~ '^[a-zA-Z0-9_-]{1,64}$'),
  CONSTRAINT project_solve_exchanges_graph_check
    CHECK (graph_revision >= 0 AND char_length(frame_id) BETWEEN 1 AND 200
      AND project_solve_exchange_worker_ok(frame_id)),
  CONSTRAINT project_solve_exchanges_phase_check
    CHECK (phase IN ('initial', 'background')),
  CONSTRAINT project_solve_exchanges_worker_check
    CHECK (project_solve_exchange_worker_ok(sender_worker)),
  CONSTRAINT project_solve_exchanges_solver_check
    CHECK ((solver_endpoint IS NULL OR
        (char_length(solver_endpoint) BETWEEN 1 AND 2048
          AND solver_endpoint !~ '[[:cntrl:]]' AND solver_endpoint = btrim(solver_endpoint)))
      AND project_solve_exchange_worker_ok(solver_version)),
  CONSTRAINT project_solve_exchanges_classification_check
    CHECK (classification IN ('live', 'synthetic')),
  CONSTRAINT project_solve_exchanges_request_check
    CHECK (octet_length(request_bytes) BETWEEN 1 AND 262144),
  CONSTRAINT project_solve_exchanges_response_check
    CHECK (response_bytes IS NULL OR octet_length(response_bytes) BETWEEN 1 AND 1048576),
  CONSTRAINT project_solve_exchanges_candidate_check
    CHECK (candidate_bytes IS NULL OR octet_length(candidate_bytes) BETWEEN 1 AND 1048576),
  CONSTRAINT project_solve_exchanges_proof_check
    CHECK (proof_bytes IS NULL OR octet_length(proof_bytes) BETWEEN 1 AND 262144),
  CONSTRAINT project_solve_exchanges_hash_check
    CHECK (parent_intake_sha256 ~ '^[0-9a-f]{64}$'
      AND graph_sha256 ~ '^[0-9a-f]{64}$'
      AND request_sha256 ~ '^[0-9a-f]{64}$'
      AND (response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$')
      AND (candidate_sha256 IS NULL OR candidate_sha256 ~ '^[0-9a-f]{64}$')
      AND (proof_sha256 IS NULL OR proof_sha256 ~ '^[0-9a-f]{64}$')),
  CONSTRAINT project_solve_exchanges_digest_check
    CHECK (request_sha256 = encode(sha256(request_bytes), 'hex')
      AND (response_bytes IS NULL OR response_sha256 = encode(sha256(response_bytes), 'hex'))
      AND (candidate_bytes IS NULL OR candidate_sha256 = encode(sha256(candidate_bytes), 'hex'))
      AND (proof_bytes IS NULL OR proof_sha256 = encode(sha256(proof_bytes), 'hex'))),
  CONSTRAINT project_solve_exchanges_state_check
    CHECK (state IN ('prepared', 'sending', 'responded', 'completed', 'refused', 'outcome_unknown')),
  CONSTRAINT project_solve_exchanges_shape_check
    CHECK (
      ((response_bytes IS NULL) = (response_sha256 IS NULL))
      AND ((candidate_bytes IS NULL) = (candidate_sha256 IS NULL))
      AND ((proof_bytes IS NULL) = (proof_sha256 IS NULL))
      AND ((sender_worker IS NULL) = (sender_attempt IS NULL))
      AND (sender_attempt IS NULL OR sender_attempt > 0)
      AND (response_bytes IS NULL OR sender_worker IS NOT NULL)
      AND ((response_bytes IS NULL) = (solver_endpoint IS NULL))
      AND ((response_bytes IS NULL) = (solver_version IS NULL))
      AND ((state = 'completed') = (candidate_bytes IS NOT NULL))
      AND ((state = 'completed') = (proof_bytes IS NOT NULL))
      AND (
        (state = 'prepared' AND sender_worker IS NULL AND response_bytes IS NULL)
        OR (state IN ('sending', 'outcome_unknown') AND sender_worker IS NOT NULL AND response_bytes IS NULL)
        OR (state IN ('responded', 'completed') AND sender_worker IS NOT NULL AND response_bytes IS NOT NULL)
        OR state = 'refused'
      )
      AND (
        (state = 'refused' AND refusal_code IS NOT NULL AND refusal_code IN (
          'cloud_request_invalid', 'cloud_auth_missing', 'cloud_tenant_unauthorized',
          'cloud_upstream_failure', 'cloud_response_invalid'))
        OR (state = 'outcome_unknown' AND refusal_code IS NOT NULL AND refusal_code = 'SOLVE_OUTCOME_UNKNOWN')
        OR (state NOT IN ('refused', 'outcome_unknown') AND refusal_code IS NULL)
      )
    )
);

CREATE INDEX IF NOT EXISTS idx_project_solve_exchanges_scope
  ON project_solve_exchanges(org_id, project_id, drawing_id, job_id);

CREATE OR REPLACE FUNCTION guard_project_solve_exchange()
RETURNS TRIGGER AS $$
BEGIN
  IF ROW(NEW.job_id, NEW.org_id, NEW.project_id, NEW.drawing_id,
      NEW.input_version_id, NEW.parent_intake_sha256, NEW.graph_revision,
      NEW.graph_sha256, NEW.frame_id, NEW.phase,
      NEW.grant_ref, NEW.classification, NEW.request_bytes, NEW.request_sha256, NEW.created_at)
      IS DISTINCT FROM ROW(OLD.job_id, OLD.org_id, OLD.project_id, OLD.drawing_id,
      OLD.input_version_id, OLD.parent_intake_sha256, OLD.graph_revision,
      OLD.graph_sha256, OLD.frame_id, OLD.phase,
      OLD.grant_ref, OLD.classification, OLD.request_bytes, OLD.request_sha256, OLD.created_at) THEN
    RAISE EXCEPTION 'solve exchange input is immutable';
  END IF;
  IF OLD.state IN ('completed', 'refused', 'outcome_unknown') AND NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'solve exchange terminal state is immutable';
  END IF;
  IF NEW.state <> OLD.state AND NOT (
      (OLD.state = 'prepared' AND NEW.state IN ('sending', 'refused'))
      OR (OLD.state = 'sending' AND NEW.state IN ('responded', 'refused', 'outcome_unknown'))
      OR (OLD.state = 'responded' AND NEW.state IN ('completed', 'refused'))) THEN
    RAISE EXCEPTION 'invalid solve exchange transition';
  END IF;
  IF OLD.sender_worker IS NOT NULL AND ROW(NEW.sender_worker, NEW.sender_attempt)
      IS DISTINCT FROM ROW(OLD.sender_worker, OLD.sender_attempt) THEN
    RAISE EXCEPTION 'solve sender is immutable';
  END IF;
  IF OLD.sender_worker IS NULL AND NEW.sender_worker IS NOT NULL
      AND NOT (OLD.state = 'prepared' AND NEW.state = 'sending') THEN
    RAISE EXCEPTION 'solve sender requires send transition';
  END IF;
  IF OLD.response_bytes IS NOT NULL AND ROW(NEW.response_bytes, NEW.response_sha256, NEW.solver_endpoint, NEW.solver_version)
      IS DISTINCT FROM ROW(OLD.response_bytes, OLD.response_sha256, OLD.solver_endpoint, OLD.solver_version) THEN
    RAISE EXCEPTION 'solve response is immutable';
  END IF;
  IF OLD.response_bytes IS NULL AND NEW.response_bytes IS NOT NULL
      AND NOT (OLD.state = 'sending' AND NEW.state = 'responded') THEN
    RAISE EXCEPTION 'solve response requires send custody';
  END IF;
  IF OLD.candidate_bytes IS NOT NULL AND ROW(NEW.candidate_bytes, NEW.candidate_sha256)
      IS DISTINCT FROM ROW(OLD.candidate_bytes, OLD.candidate_sha256) THEN
    RAISE EXCEPTION 'solve candidate is immutable';
  END IF;
  IF OLD.candidate_bytes IS NULL AND NEW.candidate_bytes IS NOT NULL
      AND NOT (OLD.state = 'responded' AND NEW.state = 'completed') THEN
    RAISE EXCEPTION 'solve candidate requires stored response';
  END IF;
  IF OLD.proof_bytes IS NOT NULL AND ROW(NEW.proof_bytes, NEW.proof_sha256)
      IS DISTINCT FROM ROW(OLD.proof_bytes, OLD.proof_sha256) THEN
    RAISE EXCEPTION 'solve proof is immutable';
  END IF;
  IF OLD.proof_bytes IS NULL AND NEW.proof_bytes IS NOT NULL
      AND NOT (OLD.state = 'responded' AND NEW.state = 'completed') THEN
    RAISE EXCEPTION 'solve proof requires stored response';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'project_solve_exchanges_guard'
      AND tgrelid = 'project_solve_exchanges'::regclass) THEN
    CREATE TRIGGER project_solve_exchanges_guard BEFORE UPDATE ON project_solve_exchanges
      FOR EACH ROW EXECUTE FUNCTION guard_project_solve_exchange();
  END IF;
END;
$$;
