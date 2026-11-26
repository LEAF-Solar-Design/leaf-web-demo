-- expand-contract: expand-only
-- Recreates the deferred sizing-exchange check as SECURITY DEFINER with schema-qualified tables and a pg_catalog-only search path so a narrowly granted role (the I01 decoder) can commit job updates. Rollback: re-run 0074's definition and 0115.
DO $migration$
DECLARE
  s text := pg_catalog.current_schema();
BEGIN
  EXECUTE pg_catalog.format($ddl$
CREATE OR REPLACE FUNCTION %1$I.check_project_sizing_exchange_completion()
RETURNS TRIGGER LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
  PERFORM 1 FROM %1$I.jobs WHERE job_id = NEW.job_id FOR UPDATE;
  IF EXISTS (
    SELECT 1 FROM %1$I.project_sizing_exchanges AS e
    LEFT JOIN %1$I.jobs AS j ON j.job_id = e.job_id
    WHERE e.job_id = NEW.job_id AND e.state = 'published'
      AND (j.job_id IS NULL OR j.status IS DISTINCT FROM 'succeeded'
        OR j.output_version_id IS DISTINCT FROM e.output_version_id)
  ) THEN
    RAISE EXCEPTION 'project sizing exchange completion rule violated'
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END;
$fn$;
  $ddl$, s);
  EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %I.check_project_sizing_exchange_completion() FROM PUBLIC', s);
END;
$migration$;
