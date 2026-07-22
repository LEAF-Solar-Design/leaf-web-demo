-- Deterministic compliance runs, immutable findings, and replayable waiver events.
CREATE TABLE IF NOT EXISTS compliance_runs (
  run_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  solve_id UUID NOT NULL,
  standards_snapshot_id UUID NOT NULL,
  standards_kind TEXT NOT NULL DEFAULT 'standards' CHECK (standards_kind = 'standards'),
  ahj_snapshot_id UUID NOT NULL,
  ahj_kind TEXT NOT NULL DEFAULT 'ahj' CHECK (ahj_kind = 'ahj'),
  rule_pack_id TEXT NOT NULL,
  rule_pack_version TEXT NOT NULL,
  input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
  rule_pack_sha256 TEXT NOT NULL CHECK (rule_pack_sha256 ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id, solve_id)
    REFERENCES solve_records(org_id, project_id, solve_id) ON DELETE CASCADE,
  FOREIGN KEY (standards_snapshot_id, standards_kind)
    REFERENCES platform_snapshots(snapshot_id, snapshot_kind),
  FOREIGN KEY (ahj_snapshot_id, ahj_kind)
    REFERENCES platform_snapshots(snapshot_id, snapshot_kind),
  UNIQUE (org_id, project_id, solve_id, rule_pack_id, rule_pack_version),
  UNIQUE (org_id, project_id, run_id)
);

CREATE TABLE IF NOT EXISTS compliance_findings (
  finding_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  run_id UUID NOT NULL,
  rule_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  finding_sha256 TEXT NOT NULL CHECK (finding_sha256 ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id, run_id)
    REFERENCES compliance_runs(org_id, project_id, run_id) ON DELETE CASCADE,
  UNIQUE (run_id, rule_id),
  UNIQUE (org_id, project_id, finding_id)
);

CREATE TABLE IF NOT EXISTS compliance_waivers (
  waiver_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  finding_id UUID NOT NULL,
  requested_by_binding_id UUID NOT NULL,
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id, finding_id)
    REFERENCES compliance_findings(org_id, project_id, finding_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, requested_by_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  UNIQUE (org_id, project_id, waiver_id)
);

CREATE TABLE IF NOT EXISTS compliance_waiver_events (
  event_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  waiver_id UUID NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  state TEXT NOT NULL CHECK (state IN ('proposed', 'approved', 'rejected', 'revoked')),
  actor_binding_id UUID NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id, waiver_id)
    REFERENCES compliance_waivers(org_id, project_id, waiver_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, actor_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  UNIQUE (waiver_id, sequence)
);

DROP TRIGGER IF EXISTS compliance_runs_immutable ON compliance_runs;
CREATE TRIGGER compliance_runs_immutable BEFORE UPDATE OR DELETE ON compliance_runs
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS compliance_findings_immutable ON compliance_findings;
CREATE TRIGGER compliance_findings_immutable BEFORE UPDATE OR DELETE ON compliance_findings
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS compliance_waivers_immutable ON compliance_waivers;
CREATE TRIGGER compliance_waivers_immutable BEFORE UPDATE OR DELETE ON compliance_waivers
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
DROP TRIGGER IF EXISTS compliance_waiver_events_immutable ON compliance_waiver_events;
CREATE TRIGGER compliance_waiver_events_immutable BEFORE UPDATE OR DELETE ON compliance_waiver_events
  FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
