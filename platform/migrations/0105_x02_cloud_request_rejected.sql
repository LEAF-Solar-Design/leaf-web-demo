-- Expand only: retain every existing custody predicate and refusal value.
-- Source rollback may leave this additive permission installed safely.
DO $$
DECLARE
  table_name TEXT;
  shape_definition TEXT;
  old_literal CONSTANT TEXT := '''cloud_request_invalid''::text';
  new_literal CONSTANT TEXT := '''CLOUD_REQUEST_REJECTED''::text';
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
      'project_solve_exchanges', 'project_solve_piece_exchanges']
  LOOP
    SELECT pg_get_constraintdef(oid) INTO shape_definition
      FROM pg_constraint
      WHERE conrelid = format('%I', table_name)::regclass
        AND conname = 'project_solve_exchanges_shape_check'
        AND contype = 'c';
    IF shape_definition IS NULL THEN
      RAISE EXCEPTION 'missing solve shape constraint on %', table_name;
    END IF;
    -- PostgreSQL renders this text IN list as ANY (ARRAY[...]). Refuse
    -- unfamiliar producer syntax rather than reconstructing any predicates.
    IF strpos(shape_definition, 'refusal_code = ANY (ARRAY[' || old_literal) = 0
        OR (length(shape_definition) - length(replace(shape_definition, old_literal, '')))
          <> length(old_literal)
        OR strpos(shape_definition, new_literal) <> 0 THEN
      RAISE EXCEPTION 'unexpected solve refusal list on %', table_name;
    END IF;
    shape_definition := replace(shape_definition, old_literal,
      old_literal || ', ' || new_literal);
    EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', table_name,
      'project_solve_exchanges_shape_check');
    EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', table_name,
      'project_solve_exchanges_shape_check', shape_definition);
  END LOOP;
END;
$$;
