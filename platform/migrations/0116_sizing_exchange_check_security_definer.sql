-- expand-contract: expand-only
-- Runs the deferred sizing-exchange check with its owner's rights so a narrowly granted role (the I01 decoder) can commit job updates. Rollback: ALTER FUNCTION ... SECURITY INVOKER; GRANT EXECUTE ... TO PUBLIC.
DO $migration$
DECLARE
  s text := pg_catalog.current_schema();
BEGIN
  EXECUTE pg_catalog.format('ALTER FUNCTION %I.check_project_sizing_exchange_completion() SECURITY DEFINER', s);
  EXECUTE pg_catalog.format('ALTER FUNCTION %I.check_project_sizing_exchange_completion() SET search_path = %I, pg_catalog, pg_temp', s, s);
  EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %I.check_project_sizing_exchange_completion() FROM PUBLIC', s);
END;
$migration$;
