"""Postgres integration verifier for the APS observability read-model.

This is the test that actually EXECUTES the read-model SQL (PERCENTILE_CONT ...
FILTER, split_part join, EXTRACT(EPOCH ...)) against a real database — the thing
the mocked router tests in test_ops_metrics.py cannot cover.

Gated on DATABASE_URL exactly like the repo's other Postgres integration tests
(see test_broker_pg_store.py). It SKIPS with no DATABASE_URL, so run it against a
throwaway Postgres to close the SQL gap:

    DATABASE_URL=postgresql://... python -m pytest tests/test_ops_metrics_pg.py -q

Data is created via the proven path: `broker_pg_store` admissions satisfy the
`broker_usage_ledger` FK, then rows are inserted directly (mirrors the direct
INSERT block in test_postgres_migration_two_writer_admission_and_replay). Every
assertion is scoped to a unique tenant so a shared DB stays deterministic.
"""
from __future__ import annotations

import os
import time
import uuid

import broker_pg_store
import ops_metrics_read
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not configured",
)

_APS_ENDPOINT = "https://developer.api.autodesk.com"


def _admit(store, key, tenant):
    """Create the admission row the ledger FK points at (proven kwargs)."""
    return store.admit_run(
        key, tenant, aps_live=True, estimated_usd=0.02,
        spend_cap=1.0, daily_limit=100, request_fingerprint="a" * 64,
    )


def _insert_job(conn, job_id, tenant, status, progress, now):
    conn.execute(
        "INSERT INTO async_jobs "
        "(job_id, tenant_id, tool, params_json, dwg, status, progress, "
        " created_at, updated_at, execution_json, submission_fingerprint) "
        "VALUES (%(j)s, %(t)s, 'panel_layout', '{}'::jsonb, 'd.dwg', %(s)s, %(p)s, "
        " %(now)s, %(now)s, '{}'::jsonb, %(fp)s)",
        {"j": job_id, "t": tenant, "s": status, "p": progress, "now": now,
         "fp": str(uuid.uuid4())},
    )


def _insert_ledger(conn, event_key, tenant, status, aps_live, engine_seconds, usd_est, now):
    conn.execute(
        "INSERT INTO broker_usage_ledger "
        "(event_key, ts, tenant_id, tool, engine_op, aps_endpoint, aps_live, "
        " engine_seconds, usd_est, status) "
        "VALUES (%(ek)s, %(ts)s, %(t)s, 'panel_layout', 'SOLVE', %(ep)s, %(live)s, "
        " %(eng)s, %(usd)s, %(st)s)",
        {"ek": event_key, "ts": now, "t": tenant, "ep": _APS_ENDPOINT,
         "live": aps_live, "eng": engine_seconds, "usd": usd_est, "st": status},
    )


def test_read_model_over_real_postgres():
    db = broker_pg_store._load_db()
    db.apply_migration()  # same one-shot chain staging uses
    store = broker_pg_store.PostgresBrokerStore(db)

    tenant = f"obs-{uuid.uuid4()}"
    job_running = str(uuid.uuid4())   # in-flight
    job_done = str(uuid.uuid4())      # terminal
    orphan = str(uuid.uuid4())        # a ledger run with no owning job
    now = time.time()

    with db.get_pool().connection() as conn:
        _insert_job(conn, job_running, tenant, "running", "executing", now)
        _insert_job(conn, job_done, tenant, "complete", None, now)

    # ledger rows (admission first to satisfy the FK, then the row):
    #   ok live run (owns job_running) · failed live run (owns job_done) · ok mock orphan
    ledger = [
        (f"{job_running}:broker-run", "ok", True, 4.0, 0.007),
        (f"{job_done}:broker-run", "WORKITEM_FAILED", True, None, None),
        (f"{orphan}:broker-run", "ok", False, 0.2, None),
    ]
    for event_key, status, aps_live, engine_seconds, usd in ledger:
        _admit(store, event_key, tenant)
        with db.get_pool().connection() as conn:
            _insert_ledger(conn, event_key, tenant, status, aps_live, engine_seconds, usd, now)

    # ---- fleet_metrics (tenant-scoped so a shared DB stays deterministic) ----
    m = ops_metrics_read.fleet_metrics(window_seconds=3600, tenant_id=tenant)
    assert m["throughput"]["runs"] == 3
    assert m["throughput"]["live_runs"] == 2
    assert m["throughput"]["mock_runs"] == 1
    assert m["reliability"]["ok_runs"] == 2
    assert m["reliability"]["error_runs"] == 1
    assert m["reliability"]["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert any(e["status"] == "WORKITEM_FAILED" and e["count"] == 1
               for e in m["reliability"]["error_taxonomy"])
    # only non-null usd on ok rows counts -> just the 0.007 run
    assert m["cost"]["usd_window"] == pytest.approx(0.007)
    # engine percentiles over the two non-null values {4.0, 0.2}
    assert m["engine_seconds"]["p50"] == pytest.approx(2.1, abs=1e-6)
    assert m["engine_seconds"]["sum"] == pytest.approx(4.2, abs=1e-6)

    # ---- inflight: only submitted/running jobs ----
    tail = ops_metrics_read.inflight(tenant_id=tenant)
    ids = {j["job_id"] for j in tail}
    assert job_running in ids
    assert job_done not in ids
    row = next(j for j in tail if j["job_id"] == job_running)
    assert row["progress"] == "executing"
    assert row["age_seconds"] is not None

    # ---- drill-down: correlation via event_key prefix ----
    drill = {r["event_key"]: r for r in ops_metrics_read.run_drilldown(tenant_id=tenant, limit=50)}
    assert drill[f"{job_running}:broker-run"]["job_correlated"] is True
    assert drill[f"{job_running}:broker-run"]["job"]["status"] == "running"
    assert drill[f"{orphan}:broker-run"]["job_correlated"] is False
    assert drill[f"{orphan}:broker-run"]["job"] is None
