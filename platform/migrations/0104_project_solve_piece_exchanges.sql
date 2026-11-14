-- Additive custody only. Rollback disables piece readers and retains these rows.
-- Reuse the existing byte bounds, digest and state CHECKs, without copying its
-- single-job primary key or modifying any existing exchange or caller.
CREATE TABLE IF NOT EXISTS project_solve_piece_exchanges (
  LIKE project_solve_exchanges INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  piece_index INTEGER NOT NULL CHECK (piece_index BETWEEN 0 AND 63),
  source_hash TEXT NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY (job_id, piece_index),
  FOREIGN KEY (org_id, project_id, job_id)
    REFERENCES jobs(org_id, project_id, job_id) ON DELETE CASCADE,
  FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id) ON DELETE CASCADE,
  FOREIGN KEY (input_version_id) REFERENCES drawing_versions(version_id),
  -- Piece dispatch uses the existing synchronous protocol. Unknown outcomes
  -- stay quarantined for reconciliation; no new upstream retry authority.
  CHECK (send_protocol IS NULL AND grid_id IS NULL
    AND sent_at IS NULL AND send_principal_sha256 IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_project_solve_piece_exchanges_scope
  ON project_solve_piece_exchanges
    (org_id, project_id, drawing_id, job_id, graph_revision, graph_sha256, piece_index);

CREATE OR REPLACE FUNCTION guard_project_solve_piece_identity()
RETURNS TRIGGER AS $$
BEGIN
  IF ROW(NEW.piece_index, NEW.source_hash)
      IS DISTINCT FROM ROW(OLD.piece_index, OLD.source_hash) THEN
    RAISE EXCEPTION 'solve piece identity is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
      WHERE tgname='project_solve_piece_exchanges_guard'
      AND tgrelid='project_solve_piece_exchanges'::regclass) THEN
    CREATE TRIGGER project_solve_piece_exchanges_guard
      BEFORE UPDATE ON project_solve_piece_exchanges
      FOR EACH ROW EXECUTE FUNCTION guard_project_solve_exchange();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
      WHERE tgname='project_solve_piece_exchanges_identity_guard'
      AND tgrelid='project_solve_piece_exchanges'::regclass) THEN
    CREATE TRIGGER project_solve_piece_exchanges_identity_guard
      BEFORE UPDATE ON project_solve_piece_exchanges
      FOR EACH ROW EXECUTE FUNCTION guard_project_solve_piece_identity();
  END IF;
END;
$$;
