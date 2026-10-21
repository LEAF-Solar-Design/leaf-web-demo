from pathlib import Path
import pytest


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


def test_terminal_intent_migration_is_additive_and_write_once():
    sql = (MIGRATION.parent / "0081_broker_terminal_intent.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN terminal_intent_json JSONB" in sql
    assert "terminal_intent_json IS NULL OR jsonb_typeof(terminal_intent_json) = 'object'" in sql
    assert "OLD.state <> 'executing' OR NEW.state <> 'executing'" in sql
    assert "NEW.terminal_intent_json IS DISTINCT FROM OLD.terminal_intent_json" in sql
    assert "TG_OP = 'DELETE'" in sql and "OLD.terminal_intent_json IS NOT NULL" in sql
    assert "TG_OP = 'INSERT'" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE" in sql
    assert "DROP COLUMN" not in sql and "UPDATE broker_run_admissions" not in sql


@pytest.mark.parametrize("missing", ["terminal_intent_json", "broker_terminal_intent_object",
                                    "broker_terminal_intent_write_once"])
def test_terminal_intent_schema_readiness_requires_column_constraint_and_trigger(monkeypatch, missing):
    import broker_pg_store
    import test_broker_pg_store as fixtures
    db = fixtures._Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    store.validate_schema()
    execute = db.pool.conn.execute
    def catalog(sql, params=None):
        result = execute(sql, params)
        if sql.startswith(("SELECT table_name, column_name", "SELECT conname", "SELECT tgname")):
            return fixtures._Result(many=[row for row in result.fetchall() if missing not in row.values()])
        return result
    monkeypatch.setattr(db.pool.conn, "execute", catalog)
    with pytest.raises(RuntimeError, match=missing):
        store.validate_schema()
    source = (MIGRATION.parents[1] / "db.py").read_text(encoding="utf-8")
    assert f'"{missing}"' in source


def test_terminal_intent_migration_preserves_existing_money_constraints():
    test_broker_migration_has_shared_tenant_state_and_idempotent_ledger()
    source = (MIGRATION.parents[1] / "db.py").read_text(encoding="utf-8")
    for name in ["broker_usage_ledger_pkey", "broker_run_admissions_state_shape", "broker_usage_ledger_immutable",
                 "broker_usage_ledger_engine_seconds_nonnegative_finite",
                 "broker_usage_ledger_usd_est_nonnegative_finite"]:
        assert f'"{name}"' in source
