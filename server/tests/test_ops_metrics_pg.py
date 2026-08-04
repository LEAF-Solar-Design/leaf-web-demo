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


def _insert_ledger(conn, event_key, tenant, status, aps_live, engine_seconds, usd_est, now,
                   tool="panel_layout"):
    conn.execute(
        "INSERT INTO broker_usage_ledger "
        "(event_key, ts, tenant_id, tool, engine_op, aps_endpoint, aps_live, "
        " engine_seconds, usd_est, status) "
        "VALUES (%(ek)s, %(ts)s, %(t)s, %(tool)s, 'SOLVE', %(ep)s, %(live)s, "
        " %(eng)s, %(usd)s, %(st)s)",
        {"ek": event_key, "ts": now, "t": tenant, "tool": tool, "ep": _APS_ENDPOINT,
         "live": aps_live, "eng": engine_seconds, "usd": usd_est, "st": status},
    )


def test_read_model_over_real_postgres():
    db = broker_pg_store._load_db()
    db.apply_migration()  # same one-shot chain staging uses
    store = broker_pg_store.PostgresBrokerStore(db)

    tenant = f"obs-{uuid.uuid4()}"
    job_running = str(uuid.uuid4())   # in-flight
    job_done = str(uuid.uuid4())      # terminal (failed but CHARGEABLE)
    orphan = str(uuid.uuid4())        # a ledger run with no owning job
    denied_key = str(uuid.uuid4())    # a denial — excluded from runs + cost
    now = time.time()

    with db.get_pool().connection() as conn:
        _insert_job(conn, job_running, tenant, "running", "executing", now)
        _insert_job(conn, job_done, tenant, "failed", None, now)

    # ledger rows (admission first to satisfy the FK, then the row):
    #   ok live+chargeable (owns job_running) · FAILED live+CHARGEABLE (owns job_done,
    #   must still be counted in cost) · ok mock orphan (no charge, no job) ·
    #   quota denial (must be excluded from runs and cost).
    ledger = [
        (f"{job_running}:broker-run", "ok", True, 4.0, 0.007),
        (f"{job_done}:broker-run", "WORKITEM_FAILED", True, 5.0, 0.01),
        (f"{orphan}:broker-run", "ok", False, 0.2, None),
        (f"{denied_key}:broker-run", "quota_exceeded", True, None, None),
    ]
    for event_key, status, aps_live, engine_seconds, usd in ledger:
        _admit(store, event_key, tenant)
        with db.get_pool().connection() as conn:
            _insert_ledger(conn, event_key, tenant, status, aps_live, engine_seconds, usd, now)

    # ---- fleet_metrics (tenant-scoped so a shared DB stays deterministic) ----
    m = ops_metrics_read.fleet_metrics(window_seconds=3600, tenant_id=tenant)
    assert m["throughput"]["attempts"] == 4          # all broker calls
    assert m["throughput"]["runs"] == 3              # executed (denial excluded)
    assert m["throughput"]["denied"] == 1
    assert m["throughput"]["live_runs"] == 2         # executed AND aps_live
    assert m["throughput"]["mock_runs"] == 1
    assert m["reliability"]["ok_runs"] == 2
    assert m["reliability"]["error_runs"] == 1       # executed and not ok (the failed run)
    assert m["reliability"]["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert any(e["status"] == "WORKITEM_FAILED" and e["count"] == 1
               for e in m["reliability"]["error_taxonomy"])
    # BLOCKER-1 regression: a chargeable FAILED run (0.01) counts; the denial does
    # not; the null-usd ok run does not -> 0.007 + 0.01.
    assert m["cost"]["usd_window"] == pytest.approx(0.017)
    # engine percentiles over the non-null values {4.0, 5.0, 0.2}
    assert m["engine_seconds"]["p50"] == pytest.approx(4.0, abs=1e-6)
    assert m["engine_seconds"]["sum"] == pytest.approx(9.2, abs=1e-6)

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


def test_tool_and_tenant_aggregates_over_real_postgres():
    """Executes the GROUP BY aggregates the mocked router tests cannot cover.
    Two tools with denial-exclusion, plus the fleet per-tenant fan-out (scoped
    assertions only, so a shared DB stays deterministic)."""
    db = broker_pg_store._load_db()
    db.apply_migration()
    store = broker_pg_store.PostgresBrokerStore(db)

    tenant = f"obs-agg-{uuid.uuid4()}"
    now = time.time()
    # panel_layout: ok(2.0s, $0.01) + quota denial (excluded from runs + cost)
    # string_sizer: WORKITEM_FAILED but CHARGEABLE (4.0s, $0.02) — counts
    rows = [
        (str(uuid.uuid4()) + ":broker-run", "ok", True, 2.0, 0.01, "panel_layout"),
        (str(uuid.uuid4()) + ":broker-run", "quota_exceeded", True, None, None, "panel_layout"),
        (str(uuid.uuid4()) + ":broker-run", "WORKITEM_FAILED", True, 4.0, 0.02, "string_sizer"),
    ]
    for event_key, status, aps_live, engine_seconds, usd, tool in rows:
        _admit(store, event_key, tenant)
        with db.get_pool().connection() as conn:
            _insert_ledger(conn, event_key, tenant, status, aps_live,
                           engine_seconds, usd, now, tool=tool)

    # ---- per-tool (tenant-scoped) ----
    t = ops_metrics_read.tool_metrics(window_seconds=3600, tenant_id=tenant)
    by_tool = {row["tool"]: row for row in t["tools"]}
    assert by_tool["panel_layout"]["attempts"] == 2
    assert by_tool["panel_layout"]["runs"] == 1          # denial excluded
    assert by_tool["panel_layout"]["denied"] == 1
    assert by_tool["panel_layout"]["ok_runs"] == 1
    assert by_tool["panel_layout"]["usd_est"] == pytest.approx(0.01)
    assert by_tool["string_sizer"]["runs"] == 1
    assert by_tool["string_sizer"]["error_runs"] == 1
    # chargeable FAILED run counts toward cost (same rule as fleet_metrics)
    assert by_tool["string_sizer"]["usd_est"] == pytest.approx(0.02)
    assert by_tool["string_sizer"]["engine_seconds_p95"] == pytest.approx(4.0, abs=1e-6)

    # ---- the fleet-scope queries' ts-leading index exists (0024) ----
    with db.get_pool().connection() as conn:
        idx = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'broker_usage_ledger' "
            "AND indexname = 'broker_usage_ledger_ts_idx'"
        ).fetchone()
    assert idx is not None, "0024 ts-leading index missing: fleet windows would full-scan"

    # ---- per-tenant fleet fan-out: our tenant appears with reconciled sums ----
    f = ops_metrics_read.tenant_metrics(window_seconds=3600, limit=500)
    mine = next(row for row in f["tenants"] if row["tenant_id"] == tenant)
    assert mine["attempts"] == 3
    assert mine["runs"] == 2
    assert mine["denied"] == 1
    assert mine["error_runs"] == 1
    assert mine["live_runs"] == 2
    assert mine["usd_est"] == pytest.approx(0.03)
    assert mine["engine_seconds_sum"] == pytest.approx(6.0, abs=1e-6)
    assert mine["last_ts"] is not None


def test_timeseries_over_real_postgres():
    """Executes the bucketed GROUP BY: epoch-aligned buckets, per-bucket
    denial exclusion, tool filter, ascending order, and omitted empty buckets
    (absence means no rows, never a fabricated zero row)."""
    db = broker_pg_store._load_db()
    db.apply_migration()
    store = broker_pg_store.PostgresBrokerStore(db)

    tenant = f"obs-ts-{uuid.uuid4()}"
    now = time.time()
    bucket = 3600
    # Two adjacent hour buckets, timestamps pinned INSIDE each bucket and
    # STRICTLY IN THE PAST: the query bounds ts <= now, so rows two and one
    # hours back stay visible no matter where inside the current hour the
    # test runs (no first-30-seconds flake).
    b1 = (int(now) // bucket - 2) * bucket   # two hours back
    b2 = b1 + bucket                          # one hour back
    rows = [
        # bucket 1: one ok run, one quota denial (denial excluded from
        # runs/cost, counted in attempts/denied)
        (str(uuid.uuid4()) + ":broker-run", "ok", True, 2.0, 0.01, "panel_layout", b1 + 10),
        (str(uuid.uuid4()) + ":broker-run", "quota_exceeded", True, None, None, "panel_layout", b1 + 20),
        # bucket 2: one chargeable FAILED run on a different tool
        (str(uuid.uuid4()) + ":broker-run", "WORKITEM_FAILED", True, 4.0, 0.02, "string_sizer", b2 + 30),
    ]
    for event_key, status, aps_live, engine_seconds, usd, tool, ts in rows:
        _admit(store, event_key, tenant)
        with db.get_pool().connection() as conn:
            _insert_ledger(conn, event_key, tenant, status, aps_live,
                           engine_seconds, usd, ts, tool=tool)

    series = ops_metrics_read.timeseries(
        window_seconds=3 * bucket + 120, bucket_seconds=bucket, tenant_id=tenant)
    assert series["bucket_seconds"] == bucket
    assert series["scope"] == tenant
    got = {int(b["t"]): b for b in series["buckets"]}
    # Exactly the two populated buckets, ascending; no fabricated empties.
    assert [int(b["t"]) for b in series["buckets"]] == sorted(got.keys())
    assert set(got.keys()) == {b1, b2}
    assert got[b1]["attempts"] == 2
    assert got[b1]["runs"] == 1              # denial excluded
    assert got[b1]["denied"] == 1
    assert got[b1]["ok_runs"] == 1
    assert got[b1]["usd_est"] == pytest.approx(0.01)
    assert got[b2]["runs"] == 1
    assert got[b2]["error_runs"] == 1
    assert got[b2]["usd_est"] == pytest.approx(0.02)   # chargeable FAILED counts
    assert got[b2]["engine_seconds_sum"] == pytest.approx(4.0, abs=1e-6)

    # tool filter narrows to that tool's buckets only
    only_ps = ops_metrics_read.timeseries(
        window_seconds=3 * bucket + 120, bucket_seconds=bucket,
        tenant_id=tenant, tool="string_sizer")
    assert [int(b["t"]) for b in only_ps["buckets"]] == [b2]
    assert only_ps["tool"] == "string_sizer"
