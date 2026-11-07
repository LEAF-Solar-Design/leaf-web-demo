-- Expand only: legacy images can still read and settle legacy exchanges.
ALTER TABLE project_solve_exchanges ADD COLUMN IF NOT EXISTS grid_id UUID;
ALTER TABLE project_solve_exchanges ADD COLUMN IF NOT EXISTS send_protocol TEXT;
ALTER TABLE project_solve_exchanges ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE project_solve_exchanges ADD COLUMN IF NOT EXISTS send_principal_sha256 TEXT;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid='project_solve_exchanges'::regclass
      AND conname='project_solve_exchanges_async_shape_check') THEN
    ALTER TABLE project_solve_exchanges ADD CONSTRAINT project_solve_exchanges_async_shape_check CHECK (
      (send_protocol IS NULL OR send_protocol = 'async-idempotent-v1')
      AND (state NOT IN ('prepared', 'outcome_unknown') OR grid_id IS NULL)
      AND (grid_id IS NULL OR (send_protocol IS NOT NULL AND send_protocol = 'async-idempotent-v1'))
      AND (
        (send_protocol IS NULL AND sent_at IS NULL AND send_principal_sha256 IS NULL)
        OR (send_protocol IS NOT NULL AND state <> 'prepared' AND sent_at IS NOT NULL
            AND send_principal_sha256 IS NOT NULL AND send_principal_sha256 ~ '^[0-9a-f]{64}$')
      )
    );
  END IF;
END;
$$;

-- Keep 0078's guard, including all existing byte and provenance protections.
CREATE OR REPLACE FUNCTION guard_project_solve_async()
RETURNS TRIGGER AS $$
BEGIN
  IF ROW(NEW.send_protocol, NEW.sent_at, NEW.send_principal_sha256)
      IS DISTINCT FROM ROW(OLD.send_protocol, OLD.sent_at, OLD.send_principal_sha256)
      AND NOT (OLD.state = 'prepared' AND NEW.state = 'sending'
          AND OLD.send_protocol IS NULL AND OLD.sent_at IS NULL
          AND OLD.send_principal_sha256 IS NULL) THEN
    RAISE EXCEPTION 'solve send custody is immutable';
  END IF;
  IF NEW.grid_id IS DISTINCT FROM OLD.grid_id AND NOT (
      OLD.grid_id IS NULL AND NEW.grid_id IS NOT NULL
      AND OLD.state = 'sending' AND NEW.state = 'sending'
      AND OLD.send_protocol = 'async-idempotent-v1') THEN
    RAISE EXCEPTION 'solve grid is immutable outside sending custody';
  END IF;
  IF OLD.send_protocol = 'async-idempotent-v1' AND OLD.state = 'sending'
      AND NEW.state = 'outcome_unknown' AND NOT (
          OLD.grid_id IS NULL AND now() >= OLD.sent_at + interval '3 hours') THEN
    RAISE EXCEPTION 'async solve outcome remains recoverable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='project_solve_exchanges_async_guard'
      AND tgrelid='project_solve_exchanges'::regclass) THEN
    CREATE TRIGGER project_solve_exchanges_async_guard BEFORE UPDATE ON project_solve_exchanges
      FOR EACH ROW EXECUTE FUNCTION guard_project_solve_async();
  END IF;
END;
$$;
