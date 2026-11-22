-- expand-contract: expand-only
-- Rollback stops decoder workers; existing jobs and terminal evidence are retained.
DO $migration$
DECLARE
  s text := pg_catalog.current_schema();
  signature text;
BEGIN
  EXECUTE pg_catalog.format($ddl$
    CREATE OR REPLACE FUNCTION %1$I.video_decode_claim(p_worker text, p_lease_seconds integer)
    RETURNS TABLE(job_id uuid, org_id uuid, project_id uuid, request_tenant_id text,
                  attempt integer, execution_context jsonb, lease_expires_at timestamptz)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    BEGIN
      IF p_worker IS NULL OR pg_catalog.btrim(p_worker) = '' OR
         p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 15 AND 300 THEN
        RAISE EXCEPTION 'invalid decoder worker or lease';
      END IF;
      RETURN QUERY
      WITH candidate AS (
        SELECT j.job_id FROM %1$I.jobs j
        WHERE j.tool_name = 'video-decode'
          AND j.execution_context->>'schema' = 'leaf.video-decode-job.v1'
          AND j.deleted_at IS NULL AND j.request_tenant_id IS NOT NULL
          AND j.status = 'queued' AND j.attempt = 0 AND j.max_attempts = 1
          AND EXISTS (SELECT 1 FROM %1$I.live_projects lp
                      WHERE lp.org_id = j.org_id AND lp.project_id = j.project_id)
          AND COALESCE((SELECT a.authority_mode FROM %1$I.live_project_authority_modes a
                        WHERE a.org_id = j.org_id AND a.project_id = j.project_id),
                       (SELECT a.authority_mode FROM %1$I.tenant_authority_modes a
                        WHERE a.org_id = j.org_id), 'legacy_sqlite') = 'postgres_canonical'
        ORDER BY j.created_at, j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT 1
      )
      UPDATE %1$I.jobs j SET status = 'running', attempt = 1, lease_owner = p_worker,
        lease_expires_at = pg_catalog.now() + pg_catalog.make_interval(secs => p_lease_seconds),
        heartbeat_at = pg_catalog.now(), started_at = pg_catalog.now(), updated_at = pg_catalog.now()
      FROM candidate c WHERE j.job_id = c.job_id
      RETURNING j.job_id, j.org_id, j.project_id, j.request_tenant_id, j.attempt,
                j.execution_context, j.lease_expires_at;
    END; $fn$
  $ddl$, s);

  EXECUTE pg_catalog.format($ddl$
    CREATE OR REPLACE FUNCTION %1$I.video_decode_heartbeat(p_job uuid, p_worker text, p_lease_seconds integer)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    DECLARE changed integer;
    BEGIN
      IF p_worker IS NULL OR pg_catalog.btrim(p_worker) = '' OR
         p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 15 AND 300 THEN
        RAISE EXCEPTION 'invalid decoder worker or lease';
      END IF;
      UPDATE %1$I.jobs j SET heartbeat_at = pg_catalog.now(), updated_at = pg_catalog.now(),
        lease_expires_at = pg_catalog.now() + pg_catalog.make_interval(secs => p_lease_seconds)
      WHERE j.job_id = p_job AND j.tool_name = 'video-decode'
        AND j.execution_context->>'schema' = 'leaf.video-decode-job.v1'
        AND j.deleted_at IS NULL AND j.status = 'running' AND j.lease_owner = p_worker
        AND j.lease_expires_at >= pg_catalog.now();
      GET DIAGNOSTICS changed = ROW_COUNT;
      RETURN changed = 1;
    END; $fn$
  $ddl$, s);

  EXECUTE pg_catalog.format($ddl$
    CREATE OR REPLACE FUNCTION %1$I.video_decode_complete(p_job uuid, p_worker text, p_attempt integer, p_result jsonb)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    DECLARE r record; fingerprint text;
    BEGIN
      SELECT j.* INTO r FROM %1$I.jobs j WHERE j.job_id = p_job
        AND j.tool_name = 'video-decode' AND j.deleted_at IS NULL
        AND j.execution_context->>'schema' = 'leaf.video-decode-job.v1' FOR UPDATE;
      IF NOT FOUND THEN RETURN 'missing'; END IF;
      fingerprint := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_result::text, 'UTF8')), 'hex');
      IF r.status IN ('succeeded', 'failed', 'cancelled') THEN
        IF r.terminal_fingerprint = fingerprint THEN RETURN 'duplicate'; END IF;
        UPDATE %1$I.jobs SET terminal_conflict = p_result, updated_at = pg_catalog.now() WHERE job_id = p_job;
        RETURN 'conflict';
      END IF;
      IF (r.status = 'running' AND r.lease_owner = p_worker AND
          r.lease_expires_at >= pg_catalog.now() AND r.attempt = p_attempt) IS NOT TRUE THEN
        RETURN 'not_owner';
      END IF;
      IF (pg_catalog.jsonb_typeof(p_result) = 'object'
          AND p_result->>'request_sha256' = r.execution_context->>'request_sha256'
          AND pg_catalog.jsonb_typeof(p_result->'identity') = 'object'
          AND p_result->'identity' = r.execution_context->'identity'
          AND p_result->>'manifest_sha256' ~ '^[0-9a-f]{64}$'
          AND p_result->>'video_sha256' ~ '^[0-9a-f]{64}$'
          AND p_result->>'video_sha256' = r.execution_context->'material'->>'sha256'
          AND p_result->>'result_key' = r.execution_context->>'result_key') IS NOT TRUE THEN
        RETURN 'invalid';
      END IF;
      UPDATE %1$I.jobs SET status = 'succeeded', result = p_result, terminal_fingerprint = fingerprint,
        finished_at = pg_catalog.now(), updated_at = pg_catalog.now(), lease_owner = NULL,
        lease_expires_at = NULL WHERE job_id = p_job;
      RETURN 'applied';
    END; $fn$
  $ddl$, s);

  EXECUTE pg_catalog.format($ddl$
    CREATE OR REPLACE FUNCTION %1$I.video_decode_fail(p_job uuid, p_worker text, p_attempt integer, p_error jsonb)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    DECLARE r record; fingerprint text;
    BEGIN
      SELECT j.* INTO r FROM %1$I.jobs j WHERE j.job_id = p_job
        AND j.tool_name = 'video-decode' AND j.deleted_at IS NULL
        AND j.execution_context->>'schema' = 'leaf.video-decode-job.v1' FOR UPDATE;
      IF NOT FOUND THEN RETURN 'missing'; END IF;
      fingerprint := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_error::text, 'UTF8')), 'hex');
      IF r.status IN ('succeeded', 'failed', 'cancelled') THEN
        IF r.terminal_fingerprint = fingerprint THEN RETURN 'duplicate'; END IF;
        UPDATE %1$I.jobs SET terminal_conflict = p_error, updated_at = pg_catalog.now() WHERE job_id = p_job;
        RETURN 'conflict';
      END IF;
      IF (r.status = 'running' AND r.lease_owner = p_worker AND
          r.lease_expires_at >= pg_catalog.now() AND r.attempt = p_attempt) IS NOT TRUE THEN
        RETURN 'not_owner';
      END IF;
      IF (pg_catalog.jsonb_typeof(p_error->'code') = 'string'
          AND pg_catalog.length(pg_catalog.btrim(p_error->>'code')) BETWEEN 1 AND 64
          AND pg_catalog.length(p_error->>'code') <= 64) IS NOT TRUE THEN RETURN 'invalid'; END IF;
      UPDATE %1$I.jobs SET status = 'failed', error = p_error, terminal_fingerprint = fingerprint,
        finished_at = pg_catalog.now(), updated_at = pg_catalog.now(), lease_owner = NULL,
        lease_expires_at = NULL WHERE job_id = p_job;
      RETURN 'failed';
    END; $fn$
  $ddl$, s);

  FOREACH signature IN ARRAY ARRAY['video_decode_claim(text,integer)',
    'video_decode_heartbeat(uuid,text,integer)', 'video_decode_complete(uuid,text,integer,jsonb)',
    'video_decode_fail(uuid,text,integer,jsonb)'] LOOP
    EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %I.%s FROM PUBLIC', s, signature);
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'leaf_i01_decoder') THEN
      EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA %I TO leaf_i01_decoder', s);
      EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %I.%s TO leaf_i01_decoder', s, signature);
    END IF;
  END LOOP;
END; $migration$;
