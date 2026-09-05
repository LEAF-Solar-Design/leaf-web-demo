-- Synthetic normalized model inputs. No DWG upload or native evidence is implied.
CREATE TABLE IF NOT EXISTS arlo_lab_inputs (
  input_version_id UUID PRIMARY KEY REFERENCES drawing_versions(version_id) ON DELETE CASCADE,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  example_id TEXT NOT NULL,
  example_version TEXT NOT NULL,
  input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
  request_json TEXT NOT NULL CHECK (octet_length(request_json) <= 1048576),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
CREATE OR REPLACE FUNCTION leaf_reject_arlo_input_update() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable ARLO model input' USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='arlo_lab_inputs'::regclass
                 AND tgname='arlo_lab_inputs_immutable' AND NOT tgisinternal) THEN
    CREATE TRIGGER arlo_lab_inputs_immutable BEFORE UPDATE ON arlo_lab_inputs
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_arlo_input_update();
  END IF;
END $$;
