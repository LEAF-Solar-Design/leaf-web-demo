from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "platform"
    / "migrations"
    / "0014_broker.sql"
)


def test_broker_migration_has_shared_tenant_state_and_idempotent_ledger():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS broker_tenants" in sql
    assert "disabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "CREATE TABLE IF NOT EXISTS broker_usage_ledger" in sql
    assert "event_key TEXT PRIMARY KEY" in sql
    assert "broker_usage_ledger_engine_seconds_nonnegative_finite" in sql
    assert "broker_usage_ledger_usd_est_nonnegative_finite" in sql
    assert "engine_seconds < 'Infinity'::DOUBLE PRECISION" in sql
    assert "usd_est < 'Infinity'::DOUBLE PRECISION" in sql
    assert "CREATE TABLE IF NOT EXISTS broker_run_admissions" in sql
    assert "state TEXT NOT NULL" in sql
    assert "request_fingerprint TEXT NOT NULL" in sql
    assert "accounted_work BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "'leased', 'executing', 'terminal'" in sql
    assert "execution_started_at TIMESTAMPTZ" in sql
    assert "result_json JSONB" in sql
    assert "broker_usage_ledger_admission_fk" in sql
    assert "REFERENCES broker_run_admissions(event_key, tenant_id)" in sql
    assert "CREATE TABLE IF NOT EXISTS broker_admission_resolution_audit" in sql
    assert "confirmed_failed_no_charge" in sql
    assert "verified_terminal" in sql
    assert "broker_admission_resolution_audit_immutable" in sql
    assert "CREATE TABLE IF NOT EXISTS broker_aps_slots" in sql
    assert "broker_aps_slots_state_allowed" in sql
    assert "CHECK (state IN ('held', 'released'))" in sql
    assert "pg_advisory" not in sql
    assert "broker_usage_ledger_immutable" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
