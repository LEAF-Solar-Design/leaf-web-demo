-- Expand only. Rollback stops using the store and retains its custody rows.
-- PostgreSQL text cannot contain NUL or surrogate code points. Reject all
-- remaining Cc characters and exactly the non-Cc whitespace Python strips.
CREATE OR REPLACE FUNCTION project_sizing_exchange_worker_ok(worker TEXT)
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

CREATE TABLE IF NOT EXISTS project_sizing_exchanges (
  job_id UUID NOT NULL,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  drawing_id UUID NOT NULL,
  actor_binding_id UUID NOT NULL,
  target_settings_id TEXT NOT NULL,
  admitted_tenant TEXT NOT NULL,
  grant_ref TEXT NOT NULL,
  classification TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'prepared',
  request_bytes BYTEA NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_bytes BYTEA,
  response_sha256 TEXT,
  evidence_bytes BYTEA,
  evidence_sha256 TEXT,
  sender_worker TEXT,
  sender_attempt INTEGER,
  refusal_code TEXT,
  output_version_id UUID,
  published_worker TEXT,
  published_attempt INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

  CONSTRAINT project_sizing_exchanges_pkey PRIMARY KEY (job_id),
  CONSTRAINT project_sizing_exchanges_job_fk
    FOREIGN KEY (org_id, project_id, job_id)
    REFERENCES jobs(org_id, project_id, job_id) ON DELETE CASCADE,
  CONSTRAINT project_sizing_exchanges_artifact_fk
    FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id) ON DELETE CASCADE,
  CONSTRAINT project_sizing_exchanges_actor_fk
    FOREIGN KEY (org_id, actor_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT project_sizing_exchanges_output_fk
    FOREIGN KEY (output_version_id) REFERENCES drawing_versions(version_id),
  CONSTRAINT project_sizing_exchanges_grant_check
    CHECK (grant_ref ~ '^[a-zA-Z0-9_-]{1,64}$'),
  CONSTRAINT project_sizing_exchanges_tenant_check
    CHECK (admitted_tenant = org_id::text),
  CONSTRAINT project_sizing_exchanges_target_check
    CHECK (char_length(target_settings_id) <= 128 AND target_settings_id ~
      '^leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
  CONSTRAINT project_sizing_exchanges_worker_check
    CHECK (project_sizing_exchange_worker_ok(sender_worker)
      AND project_sizing_exchange_worker_ok(published_worker)),
  CONSTRAINT project_sizing_exchanges_classification_check
    CHECK (classification IN ('live', 'synthetic')),
  CONSTRAINT project_sizing_exchanges_request_check
    CHECK (octet_length(request_bytes) BETWEEN 1 AND 65536),
  CONSTRAINT project_sizing_exchanges_response_check
    CHECK (response_bytes IS NULL OR octet_length(response_bytes) BETWEEN 1 AND 65536),
  CONSTRAINT project_sizing_exchanges_evidence_check
    CHECK (evidence_bytes IS NULL OR octet_length(evidence_bytes) BETWEEN 1 AND 262144),
  CONSTRAINT project_sizing_exchanges_digest_check
    CHECK (request_sha256 = encode(sha256(request_bytes), 'hex')
      AND (response_bytes IS NULL OR response_sha256 = encode(sha256(response_bytes), 'hex'))
      AND (evidence_bytes IS NULL OR evidence_sha256 = encode(sha256(evidence_bytes), 'hex'))),
  CONSTRAINT project_sizing_exchanges_state_check
    CHECK (state IN ('prepared', 'send_started', 'response_stored',
      'published', 'refused', 'outcome_unknown')),
  CONSTRAINT project_sizing_exchanges_shape_check
    CHECK (
      ((response_bytes IS NULL) = (response_sha256 IS NULL))
      AND ((evidence_bytes IS NULL) = (evidence_sha256 IS NULL))
      AND ((sender_worker IS NULL) = (sender_attempt IS NULL))
      AND (sender_attempt IS NULL OR sender_attempt > 0)
      AND (response_bytes IS NULL OR sender_worker IS NOT NULL)
      AND (evidence_bytes IS NULL OR response_bytes IS NOT NULL)
      AND (
        (state = 'prepared' AND sender_worker IS NULL AND response_bytes IS NULL AND evidence_bytes IS NULL)
        OR (state = 'send_started' AND sender_worker IS NOT NULL AND response_bytes IS NULL AND evidence_bytes IS NULL)
        OR (state IN ('response_stored', 'published') AND sender_worker IS NOT NULL AND response_bytes IS NOT NULL)
        OR (state = 'outcome_unknown' AND sender_worker IS NOT NULL AND response_bytes IS NULL AND evidence_bytes IS NULL)
        OR state = 'refused'
      )
      AND (
        (state = 'refused' AND refusal_code IS NOT NULL AND refusal_code IN (
          'cloud_request_invalid', 'cloud_auth_missing', 'cloud_tenant_unauthorized',
          'cloud_upstream_failure', 'cloud_response_invalid'))
        OR (state = 'outcome_unknown' AND refusal_code IS NOT NULL AND refusal_code = 'SIZING_OUTCOME_UNKNOWN')
        OR (state NOT IN ('refused', 'outcome_unknown') AND refusal_code IS NULL)
      )
      AND (
        (state = 'published' AND evidence_bytes IS NOT NULL AND output_version_id IS NOT NULL
          AND published_worker IS NOT NULL AND published_attempt IS NOT NULL AND published_attempt > 0)
        OR (state <> 'published' AND output_version_id IS NULL
          AND published_worker IS NULL AND published_attempt IS NULL)
      )
    )
);

CREATE INDEX IF NOT EXISTS idx_project_sizing_exchanges_scope
  ON project_sizing_exchanges(org_id, project_id, drawing_id, job_id);

CREATE OR REPLACE FUNCTION guard_project_sizing_exchange()
RETURNS TRIGGER AS $$
BEGIN
  IF ROW(NEW.job_id, NEW.org_id, NEW.project_id, NEW.drawing_id,
      NEW.actor_binding_id, NEW.target_settings_id, NEW.admitted_tenant,
      NEW.grant_ref, NEW.classification, NEW.request_bytes, NEW.request_sha256, NEW.created_at)
      IS DISTINCT FROM ROW(OLD.job_id, OLD.org_id, OLD.project_id, OLD.drawing_id,
      OLD.actor_binding_id, OLD.target_settings_id, OLD.admitted_tenant,
      OLD.grant_ref, OLD.classification, OLD.request_bytes, OLD.request_sha256, OLD.created_at) THEN
    RAISE EXCEPTION 'sizing exchange input is immutable';
  END IF;
  IF OLD.state IN ('published', 'refused', 'outcome_unknown') AND NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'sizing exchange terminal state is immutable';
  END IF;
  IF NEW.state <> OLD.state AND NOT (
      (OLD.state = 'prepared' AND NEW.state IN ('send_started', 'refused'))
      OR (OLD.state = 'send_started' AND NEW.state IN ('response_stored', 'refused', 'outcome_unknown'))
      OR (OLD.state = 'response_stored' AND NEW.state IN ('published', 'refused'))) THEN
    RAISE EXCEPTION 'invalid sizing exchange transition';
  END IF;
  IF OLD.sender_worker IS NOT NULL AND ROW(NEW.sender_worker, NEW.sender_attempt)
      IS DISTINCT FROM ROW(OLD.sender_worker, OLD.sender_attempt) THEN
    RAISE EXCEPTION 'sizing sender is immutable';
  END IF;
  IF OLD.sender_worker IS NULL AND NEW.sender_worker IS NOT NULL
      AND NOT (OLD.state = 'prepared' AND NEW.state = 'send_started') THEN
    RAISE EXCEPTION 'sizing sender requires send transition';
  END IF;
  IF OLD.response_bytes IS NOT NULL AND ROW(NEW.response_bytes, NEW.response_sha256)
      IS DISTINCT FROM ROW(OLD.response_bytes, OLD.response_sha256) THEN
    RAISE EXCEPTION 'sizing response is immutable';
  END IF;
  IF OLD.response_bytes IS NULL AND NEW.response_bytes IS NOT NULL
      AND NOT (OLD.state = 'send_started' AND NEW.state = 'response_stored') THEN
    RAISE EXCEPTION 'sizing response requires send custody';
  END IF;
  IF OLD.evidence_bytes IS NOT NULL AND ROW(NEW.evidence_bytes, NEW.evidence_sha256)
      IS DISTINCT FROM ROW(OLD.evidence_bytes, OLD.evidence_sha256) THEN
    RAISE EXCEPTION 'sizing evidence is immutable';
  END IF;
  IF OLD.evidence_bytes IS NULL AND NEW.evidence_bytes IS NOT NULL
      AND NOT (OLD.state = 'response_stored' AND NEW.state = 'response_stored') THEN
    RAISE EXCEPTION 'sizing evidence requires stored response';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'project_sizing_exchanges_guard'
      AND tgrelid = 'project_sizing_exchanges'::regclass) THEN
    CREATE TRIGGER project_sizing_exchanges_guard BEFORE UPDATE ON project_sizing_exchanges
      FOR EACH ROW EXECUTE FUNCTION guard_project_sizing_exchange();
  END IF;
END;
$$;

-- Serialize cross-table completion checks on the job. Under READ COMMITTED,
-- the VOLATILE function's later statement sees a fresh snapshot after the lock.
CREATE OR REPLACE FUNCTION check_project_sizing_exchange_completion()
RETURNS TRIGGER LANGUAGE plpgsql VOLATILE AS $$
BEGIN
  PERFORM 1 FROM jobs WHERE job_id = NEW.job_id FOR UPDATE;
  IF EXISTS (
    SELECT 1 FROM project_sizing_exchanges AS e
    LEFT JOIN jobs AS j ON j.job_id = e.job_id
    WHERE e.job_id = NEW.job_id AND e.state = 'published'
      AND (j.job_id IS NULL OR j.status IS DISTINCT FROM 'succeeded'
        OR j.output_version_id IS DISTINCT FROM e.output_version_id)
  ) THEN
    RAISE EXCEPTION 'project sizing exchange completion rule violated'
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'project_sizing_exchange_completion'
      AND tgrelid = 'project_sizing_exchanges'::regclass) THEN
    CREATE CONSTRAINT TRIGGER project_sizing_exchange_completion
      AFTER INSERT OR UPDATE ON project_sizing_exchanges
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
      WHEN (NEW.state = 'published')
      EXECUTE FUNCTION check_project_sizing_exchange_completion();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'jobs_sizing_exchange_completion'
      AND tgrelid = 'jobs'::regclass) THEN
    CREATE CONSTRAINT TRIGGER jobs_sizing_exchange_completion
      AFTER UPDATE ON jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
      WHEN (OLD.status IS DISTINCT FROM NEW.status
        OR OLD.output_version_id IS DISTINCT FROM NEW.output_version_id)
      EXECUTE FUNCTION check_project_sizing_exchange_completion();
  END IF;
END;
$$;
