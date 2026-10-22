"""APS adapter contract: pure fake-connection coverage and isolated DB rows."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import sys
from types import SimpleNamespace
import uuid

import pytest

from leaf_platform import aps_cost_sync as sync
from leaf_platform.tenant_cost_store import cost_event_key, validate_cost_event


ORG = uuid.UUID("b15f227e-ecb7-4c58-82ad-05360542a56c")


def row(**changes):
    return {"event_key": "broker-event", "ts": 10.0, "tenant_id": str(ORG),
            "org_id": ORG, "tool": "solve", "engine_op": "solve-op",
            "aps_live": True, "status": "ok", "engine_seconds": 2.0,
            "usd_est": 0.25, **changes}


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params):
        self.queries.append((sql, params))

    def fetchall(self):
        return self.rows


@pytest.fixture
def recorded(monkeypatch):
    events = []

    def record(conn, event):
        events.append(validate_cost_event(event))
        return "inserted"

    monkeypatch.setattr(sync, "record_cost_event", record)
    return events


def run(rows, **kwargs):
    return sync.sync_aps_cost_events(FakeConnection(rows), since_ts=0, until_ts=20, **kwargs)


def test_filters_count_each_skip_reason(recorded):
    result = run([row(aps_live=False, org_id=None), row(engine_seconds=None),
                  row(usd_est=None), row(status="QUOTA_EXCEEDED"),
                  row(org_id=None), row()])
    assert result == {"read": 6, "inserted": 1, "duplicate": 0,
                      "skipped_not_live": 1, "skipped_incomplete": 3,
                      "skipped_unresolved_tenant": 1, "next_since_ts": 10.0}
    assert len(recorded) == 1


def test_terminal_status_vocabulary(recorded):
    assert sync.ENGINE_TERMINAL_STATUSES == {"ok", "WORKITEM_FAILED"}
    for status in ("ok", "WORKITEM_FAILED"):
        assert run([row(status=status)])["inserted"] == 1
    for status in ("unknown", "error", "INTERNAL", "BAD_PARAMS", "APS_UNAVAILABLE",
                   "TENANT_DISABLED", "RECONCILED_FAILED_NO_CHARGE", "running"):
        assert run([row(status=status)])["skipped_incomplete"] == 1


def test_event_mapping_and_key(recorded):
    run([row()])
    assert recorded == [{
        "org_id": ORG, "addon": "aps_solve", "mode": "managed",
        "vendor": "autodesk-aps", "unit": "engine_seconds", "quantity": Decimal("2"),
        "cost_usd": Decimal("0.25"), "unit_cost_usd": Decimal("0.125"),
        "cost_basis": "estimated", "occurred_at": datetime.fromtimestamp(10, timezone.utc),
        "event_key": cost_event_key(ORG, "aps_solve", "broker_usage_ledger", "broker-event"),
        "provenance": {"source": "broker_usage_ledger", "broker_event_key": "broker-event",
                       "tool": "solve", "engine_op": "solve-op"},
    }]


def test_zero_engine_seconds_has_zero_unit_cost(recorded):
    assert run([row(engine_seconds=0, usd_est=0)])["inserted"] == 1
    assert recorded[0]["quantity"] == recorded[0]["unit_cost_usd"] == recorded[0]["cost_usd"] == 0


def test_money_is_normalized_to_destination_precision(recorded):
    run([row(engine_seconds=3, usd_est=0.0083)])
    assert recorded[0]["unit_cost_usd"] == Decimal("0.002767")
    assert recorded[0]["cost_usd"] == Decimal("0.008300")


def test_limit_bounds_precede_database_access():
    for limit in (0, -1, 5001, True, 1.5, None):
        with pytest.raises(ValueError, match="limit"):
            sync.sync_aps_cost_events(None, since_ts=0, until_ts=20, limit=limit)


def test_read_query_is_bounded_ordered_and_preserves_unresolved_rows():
    for limit in (1, 5000):
        conn = FakeConnection([])
        sync.sync_aps_cost_events(conn, since_ts=0, until_ts=20, limit=limit)
        sql, params = conn.queries[0]
        assert "l.ts >= %(since_ts)s AND l.ts < %(until_ts)s" in sql
        assert "ORDER BY l.ts, l.event_key LIMIT %(limit)s" in sql
        assert "LEFT JOIN orgs o ON o.org_id::text = l.tenant_id" in sql
        assert params == {"since_ts": 0, "until_ts": 20, "limit": limit}
    conn = FakeConnection([])
    sync.sync_aps_cost_events(conn, since_ts=0, until_ts=20)
    assert conn.queries[0][1]["limit"] == 5000


def test_watermark_tracks_last_read_even_when_skipped(recorded):
    assert run([])["next_since_ts"] == 0
    result = run([row(), row(ts=15, aps_live=False)])
    assert result["next_since_ts"] == 15
    assert result["read"] == 2


def test_duplicate_results_are_counted(monkeypatch):
    seen = set()

    def record(conn, event):
        key = event["event_key"]
        outcome = "duplicate" if key in seen else "inserted"
        seen.add(key)
        return outcome

    monkeypatch.setattr(sync, "record_cost_event", record)
    rows = [row(), row(event_key="another", ts=11)]
    assert run(rows)["inserted"] == 2
    result = run(rows)
    assert result["duplicate"] == result["read"] == 2
    assert result["inserted"] == 0


def test_timestamp_validation_precedes_database_access():
    for since, until in ((True, 20), (0, float("inf")), (float("nan"), 20),
                         ("0", 20), (20, 20), (21, 20)):
        with pytest.raises(ValueError):
            sync.sync_aps_cost_events(None, since_ts=since, until_ts=until)


def test_cli_prints_json_and_parses_aware_iso(monkeypatch, capsys):
    conn = FakeConnection([])

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setitem(sys.modules, "leaf_platform.db", SimpleNamespace(connection=connection))
    sync.main(["--since", "1970-01-01T00:00:00Z", "--until", "1970-01-01T01:00:00+00:00"])
    assert json.loads(capsys.readouterr().out) == run([])
    assert conn.queries[0][1]["until_ts"] == 3600
    for value in ("invalid", "2026-01-01T00:00:00"):
        with pytest.raises(argparse.ArgumentTypeError):
            sync._iso_epoch(value)


def seed_broker_row(conn, tenant_id, ts, **changes):
    facts = row(tenant_id=tenant_id, ts=ts, event_key=str(uuid.uuid4()), **changes)
    # The broker ledger requires an admission with matching event and tenant.
    conn.execute(
        "INSERT INTO broker_run_admissions "
        "(event_key, tenant_id, request_fingerprint, state, lease_token, aps_live, "
        "result_json, http_status, terminal_at) VALUES "
        "(%(event_key)s, %(tenant_id)s, repeat('a', 64), 'terminal', 'test', %(aps_live)s, "
        "'{}'::jsonb, 200, NOW())", facts)
    conn.execute(
        "INSERT INTO broker_usage_ledger "
        "(event_key, ts, tenant_id, tool, engine_op, aps_endpoint, aps_live, engine_seconds, usd_est, status) "
        "VALUES (%(event_key)s, %(ts)s, %(tenant_id)s, %(tool)s, %(engine_op)s, 'https://example.test', "
        "%(aps_live)s, %(engine_seconds)s, %(usd_est)s, %(status)s)", facts)
    return facts


@pytest.fixture
def db_period():
    # A random future second isolates period reads from siblings on a shared DB.
    return float(4102444800 + uuid.uuid4().int % 1000000000)


def test_db_live_row_becomes_one_event(make_org, db_period):
    from leaf_platform.db import connection

    org = make_org()
    with connection() as conn:
        facts = seed_broker_row(conn, str(org.org_id), db_period)
        result = sync.sync_aps_cost_events(conn, since_ts=db_period, until_ts=db_period + 1)
        assert result["read"] == result["inserted"] == 1
        event = conn.execute("SELECT * FROM tenant_cost_events WHERE event_key = %s", (
            cost_event_key(org.org_id, "aps_solve", "broker_usage_ledger", facts["event_key"]),)).fetchone()
        assert event["org_id"] == org.org_id
        assert event["cost_basis"] == "estimated"
        assert event["cost_usd"] == Decimal("0.25")
        assert event["quantity"] == 2


def test_db_rerun_is_all_duplicates(make_org, db_period):
    from leaf_platform.db import connection

    org = make_org()
    with connection() as conn:
        for status in ("ok", "WORKITEM_FAILED"):
            seed_broker_row(conn, str(org.org_id), db_period, status=status,
                            engine_seconds=3, usd_est=0.0083)
        assert sync.sync_aps_cost_events(conn, since_ts=db_period, until_ts=db_period + 1)["inserted"] == 2
    with connection() as conn:
        result = sync.sync_aps_cost_events(conn, since_ts=db_period, until_ts=db_period + 1)
        assert result["duplicate"] == result["read"] == 2
        assert result["inserted"] == 0


def test_db_not_live_and_unresolved_tenant_are_skipped(make_org, db_period):
    from leaf_platform.db import connection

    org = make_org()
    with connection() as conn:
        seed_broker_row(conn, str(org.org_id), db_period, aps_live=False)
        seed_broker_row(conn, "unresolved-" + str(uuid.uuid4()), db_period)
        seed_broker_row(conn, str(org.org_id), db_period, engine_seconds=None)
        result = sync.sync_aps_cost_events(conn, since_ts=db_period, until_ts=db_period + 1)
        assert result["read"] == 3
        assert result["skipped_not_live"] == result["skipped_unresolved_tenant"] == result["skipped_incomplete"] == 1
        assert result["inserted"] == result["duplicate"] == 0
