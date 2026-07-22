-- Content-addressed catalog/standards/AHJ snapshots and immutable solve pins.
-- The bootstrap channel is deliberately degraded: it allows the execution
-- spine to prove pinning without claiming licensed or ratified source data.

CREATE TABLE IF NOT EXISTS platform_snapshots (
  snapshot_id UUID PRIMARY KEY,
  snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('catalog', 'standards', 'ahj')),
  content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  content JSONB NOT NULL,
  source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  provenance JSONB NOT NULL,
  review_state TEXT NOT NULL CHECK (review_state IN ('candidate', 'accepted', 'advisory')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (snapshot_kind, content_sha256),
  UNIQUE (snapshot_id, snapshot_kind)
);

CREATE TABLE IF NOT EXISTS snapshot_channels (
  snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('catalog', 'standards', 'ahj')),
  channel TEXT NOT NULL,
  snapshot_id UUID NOT NULL,
  selected_by TEXT NOT NULL,
  selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (snapshot_kind, channel),
  FOREIGN KEY (snapshot_id, snapshot_kind)
    REFERENCES platform_snapshots(snapshot_id, snapshot_kind)
);

DO $$ BEGIN
  ALTER TABLE jobs ADD CONSTRAINT jobs_org_project_job_unique
    UNIQUE (org_id, project_id, job_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE solve_records ADD CONSTRAINT solve_records_org_project_solve_unique
    UNIQUE (org_id, project_id, solve_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS job_snapshot_pins (
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  job_id UUID NOT NULL,
  snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('catalog', 'standards', 'ahj')),
  snapshot_id UUID NOT NULL,
  PRIMARY KEY (job_id, snapshot_kind),
  FOREIGN KEY (org_id, project_id, job_id)
    REFERENCES jobs(org_id, project_id, job_id) ON DELETE CASCADE,
  FOREIGN KEY (snapshot_id, snapshot_kind)
    REFERENCES platform_snapshots(snapshot_id, snapshot_kind)
);

CREATE TABLE IF NOT EXISTS solve_snapshot_pins (
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  solve_id UUID NOT NULL,
  snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('catalog', 'standards', 'ahj')),
  snapshot_id UUID NOT NULL,
  PRIMARY KEY (solve_id, snapshot_kind),
  FOREIGN KEY (org_id, project_id, solve_id)
    REFERENCES solve_records(org_id, project_id, solve_id) ON DELETE CASCADE,
  FOREIGN KEY (snapshot_id, snapshot_kind)
    REFERENCES platform_snapshots(snapshot_id, snapshot_kind)
);

INSERT INTO platform_snapshots
  (snapshot_id, snapshot_kind, content_sha256, content, source_sha256, provenance, review_state)
VALUES
  ('10000000-0000-4000-8000-000000000001', 'catalog',
   '453dfdc7c6a2c3037feb74066f0acb37c95c856d8ff085e2617521ad8aab06d1',
   '{"items":[],"state":"locked_planned"}'::jsonb,
   '453dfdc7c6a2c3037feb74066f0acb37c95c856d8ff085e2617521ad8aab06d1',
   '{"source":"platform-bootstrap","claim":"no product catalog has been accepted"}'::jsonb,
   'candidate'),
  ('10000000-0000-4000-8000-000000000002', 'standards',
   '9665152cde1c563909db0bb98c58bd4b91665cd32653afd472fc553addf69ac7',
   '{"edition":"unverified","rules":[],"state":"locked_planned"}'::jsonb,
   '9665152cde1c563909db0bb98c58bd4b91665cd32653afd472fc553addf69ac7',
   '{"source":"platform-bootstrap","claim":"no licensed or PE-ratified standards pack"}'::jsonb,
   'candidate'),
  ('10000000-0000-4000-8000-000000000003', 'ahj',
   '08b3ee068fb07142bbfc74ed8beca5df4ca322bf0aa92b17307d96433ffd2dde',
   '{"adoptions":[],"authority":"unknown","state":"advisory"}'::jsonb,
   '08b3ee068fb07142bbfc74ed8beca5df4ca322bf0aa92b17307d96433ffd2dde',
   '{"source":"platform-bootstrap","claim":"AHJ adoption is unknown and advisory"}'::jsonb,
   'advisory')
ON CONFLICT (snapshot_kind, content_sha256) DO NOTHING;

INSERT INTO snapshot_channels (snapshot_kind, channel, snapshot_id, selected_by)
VALUES
  ('catalog', 'local-candidate', '10000000-0000-4000-8000-000000000001', 'migration'),
  ('standards', 'local-candidate', '10000000-0000-4000-8000-000000000002', 'migration'),
  ('ahj', 'local-candidate', '10000000-0000-4000-8000-000000000003', 'migration')
ON CONFLICT (snapshot_kind, channel) DO NOTHING;

CREATE OR REPLACE FUNCTION leaf_reject_global_ledger_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable canonical ledger record: %', TG_TABLE_NAME USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS platform_snapshots_immutable ON platform_snapshots;
CREATE TRIGGER platform_snapshots_immutable BEFORE UPDATE OR DELETE ON platform_snapshots
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_global_ledger_mutation();
DROP TRIGGER IF EXISTS job_snapshot_pins_immutable ON job_snapshot_pins;
CREATE TRIGGER job_snapshot_pins_immutable BEFORE UPDATE OR DELETE ON job_snapshot_pins
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS solve_snapshot_pins_immutable ON solve_snapshot_pins;
CREATE TRIGGER solve_snapshot_pins_immutable BEFORE UPDATE OR DELETE ON solve_snapshot_pins
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
