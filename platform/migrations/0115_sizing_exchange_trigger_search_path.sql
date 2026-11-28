-- expand-contract: expand-only
-- Pins the deferred sizing-exchange trigger function to its own schema so roles with a narrow search_path (the I01 decoder) can commit job updates. Rollback: ALTER FUNCTION ... RESET search_path.
DO $migration$
DECLARE
  s text := pg_catalog.current_schema();
BEGIN
  EXECUTE pg_catalog.format('ALTER FUNCTION %I.check_project_sizing_exchange_completion() SET search_path = %I, pg_catalog, pg_temp', s, s);
END;
$migration$;
