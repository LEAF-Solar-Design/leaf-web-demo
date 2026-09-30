"""TCM-04 tenant direct use and the aps:engine usage observation.

Transparency of Leaf's real cost, never billing. Hermetic: rows are fixtures,
marathon runs live in tmp_path, and every store env var is set or cleared per test.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import direct_usage  # noqa: E402
from cost_meter.direct_usage import aps_usage_observation, tenant_direct_use  # noqa: E402
from cost_meter.ledger import ESTIMATED, MEASURED  # noqa: E402

PERIOD = "2026-09"
A = "tenant_a"
B = "tenant_b"


def _epoch(year, month, day):
    return datetime(year, month, day, 12, tzinfo=timezone.utc).timestamp()


def _turn(tenant, ts="2026-09-10T10:00:00.000Z", grant_kind="oauth", usd=0.1, **extra):
    row = {"kind": "turn", "ts": ts, "tenant_id": tenant, "session_id": "s", "turn_id": "t",
           "tokens_in": 100, "tokens_out": 20, "cache_read_tokens": 5,
           "cache_creation_tokens": 7, "usd_est": usd}
    if grant_kind is not None:
        row["grant_kind"] = grant_kind
    row.update(extra)
    return row


def _run(tenant, ts=None, seconds=10.0, usd=0.008, job_id="job-1", status="ok", aps_live=True):
    row = {"ts": _epoch(2026, 9, 12) if ts is None else ts, "tenant_id": tenant, "tool": "extract",
           "engine_op": "", "aps_endpoint": "https://developer.api.autodesk.com",
           "aps_live": aps_live, "engine_seconds": seconds, "usd_est": usd, "status": status}
    if job_id is not None:
        row["job_id"] = job_id
    return row


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in ("LEAF_MARATHON_RUNS_DIR", "LEAF_AGENT_STORE", "LEAF_BROKER_STORE",
                 "LEAF_AGENT_LEDGER", "BROKER_LEDGER"):
        monkeypatch.delenv(name, raising=False)


def test_llm_two_tenants_mixed_payers():
    rows = [
        _turn(A, grant_kind="oauth", usd=0.1),
        _turn(A, grant_kind="api_key", usd=0.2),
        _turn(A, grant_kind="leaf", usd=0.05),
        _turn(A, ts="2026-08-31T23:59:59.000Z", usd=9),  # other period
        _turn(B, grant_kind="leaf", usd=7),
        {"kind": "session", "tenant_id": A, "ts": "2026-09-10T00:00:00Z"},  # not a turn
    ]
    llm = tenant_direct_use(PERIOD, A, agent_rows=rows, broker_rows=[])["llm"]
    assert llm["coverage"] == "complete"
    assert llm["turns"] == 3
    assert llm["tokens"] == {"input": 300, "output": 60, "cache_read": 15, "cache_write": 21}
    assert llm["usd_est"] == "0.35"
    assert llm["payer"] == "mixed"
    assert llm["by_payer"] == {"leaf": {"turns": 1, "usd_est": "0.05"},
                               "tenant_api_key": {"turns": 1, "usd_est": "0.2"},
                               "tenant_plan": {"turns": 1, "usd_est": "0.1"}}


def test_llm_single_payer_and_missing_grant_kind_is_unknown_and_partial():
    only_tenant = tenant_direct_use(PERIOD, A, agent_rows=[_turn(A)], broker_rows=[])["llm"]
    assert only_tenant["payer"] == "tenant_plan"
    assert only_tenant["coverage"] == "complete"
    llm = tenant_direct_use(PERIOD, A, agent_rows=[_turn(A, grant_kind=None)], broker_rows=[])["llm"]
    assert llm["payer"] == "unknown"
    assert llm["coverage"] == "partial"
    assert llm["turns"] == 1


def test_cad_counts_legacy_row_without_job_id_and_skips_denials():
    rows = [
        _run(A, seconds=10.5, usd=0.008, job_id="job-1"),
        _run(A, seconds=4.5, usd=0.002, job_id=None),  # legacy row, predates #1490
        _run(A, seconds=None, usd=None, status="quota_exceeded"),  # denial: not a run
        _run(A, seconds=None, usd=None, aps_live=False),  # mock run: a run, no spend
        _run(A, ts=_epoch(2026, 10, 1), seconds=99),  # other period
    ]
    cad = tenant_direct_use(PERIOD, A, agent_rows=[], broker_rows=rows)["cad"]
    assert cad == {"coverage": "complete", "runs": 3, "engine_seconds": "15.0",
                   "usd_est": "0.010", "runs_without_job_id": 1, "source": "broker_ledger"}


def test_cad_live_run_without_engine_seconds_is_partial_not_zero():
    cad = tenant_direct_use(PERIOD, A, agent_rows=[], broker_rows=[_run(A, seconds=None)])["cad"]
    assert cad["coverage"] == "partial"
    assert cad["runs"] == 1


def test_missing_sources_are_null_not_zero():
    use = tenant_direct_use(PERIOD, A, agent_rows=None, broker_rows=None)
    assert use["llm"]["coverage"] == "unknown"
    assert use["llm"]["turns"] is None and use["llm"]["usd_est"] is None
    assert use["llm"]["tokens"] is None and use["llm"]["payer"] is None
    assert use["cad"]["coverage"] == "unknown"
    assert use["cad"]["runs"] is None and use["cad"]["engine_seconds"] is None
    assert use["marathon"]["coverage"] == "unknown"
    assert use["marathon"]["runs"] is None
    assert use["marathon"]["additive"] is False
    # A source that was read and holds nothing for the tenant is a real zero.
    read = tenant_direct_use(PERIOD, A, agent_rows=[], broker_rows=[])
    assert read["llm"]["turns"] == 0 and read["llm"]["coverage"] == "complete"
    assert read["cad"]["runs"] == 0 and read["cad"]["engine_seconds"] == "0"


def test_other_tenant_rows_never_counted():
    agent = [_turn(B, usd=5)] * 4 + [_turn(A, usd=1)]
    broker = [_run(B, seconds=100, usd=1)] * 3 + [_run(A, seconds=2, usd=0.5)]
    use = tenant_direct_use(PERIOD, A, agent_rows=agent, broker_rows=broker)
    assert use["llm"]["turns"] == 1 and use["llm"]["usd_est"] == "1"
    assert use["cad"]["runs"] == 1 and use["cad"]["engine_seconds"] == "2"
    assert use["cad"]["usd_est"] == "0.5"
    # A prefix of the tenant id is a different tenant.
    none = tenant_direct_use(PERIOD, "tenant", agent_rows=agent, broker_rows=broker)
    assert none["llm"]["turns"] == 0 and none["cad"]["runs"] == 0


def _marathon_run(root: Path, tenant: str, run_id: str, started_at=None):
    run_dir = root / tenant / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"round": 1}), encoding="utf-8")
    if started_at is not None:
        (run_dir / "run-manifest.json").write_text(
            json.dumps({"started_at": started_at}), encoding="utf-8")


def test_marathon_runs_in_period_are_not_additive(tmp_path):
    _marathon_run(tmp_path, A, "run-sep", "2026-09-03T08:00:00Z")
    _marathon_run(tmp_path, A, "run-sep-epoch", _epoch(2026, 9, 20))
    _marathon_run(tmp_path, A, "run-aug", "2026-08-03T08:00:00Z")
    _marathon_run(tmp_path, A, "run-undated")
    _marathon_run(tmp_path, B, "run-b", "2026-09-04T08:00:00Z")
    m = tenant_direct_use(PERIOD, A, agent_rows=[], broker_rows=[], marathon_root=tmp_path)["marathon"]
    assert m["additive"] is False
    assert m["runs"] == 2
    assert m["run_ids"] == ["run-sep", "run-sep-epoch"]
    assert m["undated_runs"] == 1
    assert m["coverage"] == "partial"
    other = tenant_direct_use(PERIOD, B, agent_rows=[], broker_rows=[], marathon_root=tmp_path)
    assert other["marathon"]["run_ids"] == ["run-b"]
    assert other["marathon"]["coverage"] == "complete"


def test_marathon_env_root_and_tenant_without_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_MARATHON_RUNS_DIR", str(tmp_path))
    _marathon_run(tmp_path, A, "run-sep", "2026-09-03T08:00:00Z")
    assert tenant_direct_use(PERIOD, A, agent_rows=[], broker_rows=[])["marathon"]["runs"] == 1
    empty = tenant_direct_use(PERIOD, B, agent_rows=[], broker_rows=[])["marathon"]
    assert empty["runs"] == 0 and empty["coverage"] == "complete" and empty["additive"] is False


def test_aps_observation_measured_per_tenant():
    rows = [_run(A, seconds=10.5), _run(A, seconds=1.5), _run(B, seconds=3),
            _run(B, seconds=None, status="TENANT_DISABLED"), _run(A, seconds=None, aps_live=False),
            _run(B, ts=_epoch(2026, 8, 30), seconds=50)]
    obs = aps_usage_observation(PERIOD, rows)
    assert obs == {"kind": "usage", "resource_id": "aps:engine", "period": PERIOD,
                   "unit": "engine-second", "source": "broker_ledger", "total_usage": "15.0",
                   "usages": {"tenant_a|": "12.0", "tenant_b|": "3"},
                   "status": MEASURED, "coverage": "complete"}


def test_aps_observation_estimated_when_a_row_lacks_tenant_or_seconds():
    no_tenant = _run(A, seconds=4)
    del no_tenant["tenant_id"]
    obs = aps_usage_observation(PERIOD, [_run(A, seconds=6), no_tenant])
    assert obs["status"] == ESTIMATED and obs["coverage"] == "partial"
    assert obs["total_usage"] == "10" and obs["usages"] == {"tenant_a|": "6"}
    no_seconds = aps_usage_observation(PERIOD, [_run(A, seconds=6), _run(B, seconds=None)])
    assert no_seconds["status"] == ESTIMATED and no_seconds["coverage"] == "partial"
    assert no_seconds["total_usage"] == "6"


def test_aps_observation_absent_source_is_unknown():
    obs = aps_usage_observation(PERIOD, None)
    assert obs["total_usage"] is None and obs["usages"] == {}
    assert obs["status"] == ESTIMATED and obs["coverage"] == "unknown"


def test_loaders_read_jsonl_missing_is_zero(tmp_path, monkeypatch):
    agent_path = tmp_path / "agent.jsonl"
    agent_path.write_text(json.dumps(_turn(A)) + "\nnot json\n\n", encoding="utf-8")
    broker_path = tmp_path / "broker.jsonl"
    broker_path.write_text(json.dumps(_run(A, job_id=None)) + "\n{broken\n", encoding="utf-8")
    agent_rows = direct_usage.load_agent_rows(agent_path)
    broker_rows = direct_usage.load_broker_rows(broker_path)
    assert len(agent_rows) == 1 and len(broker_rows) == 1
    use = tenant_direct_use(PERIOD, A, agent_rows=agent_rows, broker_rows=broker_rows)
    assert use["llm"]["turns"] == 1 and use["cad"]["runs_without_job_id"] == 1
    assert use["llm"]["coverage"] == use["cad"]["coverage"] == "partial"
    assert aps_usage_observation(PERIOD, broker_rows)["coverage"] == "partial"
    assert direct_usage.load_agent_rows(tmp_path / "absent.jsonl") == []
    assert direct_usage.load_broker_rows(tmp_path / "absent.jsonl") == []
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(agent_path))
    monkeypatch.setenv("BROKER_LEDGER", str(broker_path))
    assert len(direct_usage.load_agent_rows()) == 1
    assert len(direct_usage.load_broker_rows()) == 1
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    assert direct_usage.load_agent_rows() is None
    with pytest.raises(ValueError, match="period"):
        direct_usage.load_broker_rows()


@pytest.mark.parametrize("offset,utc,month", [
    ("2026-08-31T23:30:00-05:00", "2026-09-01T04:30:00Z", "2026-09"),
    ("2026-09-01T00:30:00+05:00", "2026-08-31T19:30:00Z", "2026-08"),
    ("2026-09-30T23:30:00-05:00", "2026-10-01T04:30:00Z", "2026-10"),
    ("2026-10-01T00:30:00+05:00", "2026-09-30T19:30:00Z", "2026-09"),
])
@pytest.mark.parametrize("representation", ["offset", "z", "aware", "naive", "epoch", "int_epoch"])
def test_direct_use_and_aps_bucket_timestamps_by_utc_month(offset, utc, month, representation):
    utc_dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    ts = {"offset": offset, "z": utc, "aware": datetime.fromisoformat(offset),
          "naive": utc_dt.replace(tzinfo=None).isoformat(), "epoch": utc_dt.timestamp(),
          "int_epoch": int(utc_dt.timestamp())}[representation]
    agent = [_turn(A, ts=ts), _turn(B, ts=ts, usd=99)]
    broker = [_run(A, ts=ts, seconds=2), _run(B, ts=ts, seconds=99)]
    for period in ("2026-08", "2026-09", "2026-10"):
        expected = int(period == month)
        use = tenant_direct_use(period, A, agent_rows=agent, broker_rows=broker)
        assert use["llm"]["coverage"] == use["cad"]["coverage"] == "complete"
        assert use["llm"]["turns"] == use["cad"]["runs"] == expected
        assert use["llm"]["usd_est"] == ("0.1" if expected else "0")
        assert use["cad"]["engine_seconds"] == ("2" if expected else "0")
        obs = aps_usage_observation(period, broker)
        assert obs["coverage"] == "complete" and obs["status"] == MEASURED
        assert obs["total_usage"] == ("101" if expected else "0")
        assert obs["usages"] == ({"tenant_a|": "2", "tenant_b|": "99"} if expected else {})


@pytest.mark.parametrize("ts", ["2026-09-garbage", "2026-09-30Tbad", "2026-09-31T00:00:00Z",
                              "2026-09", "", "not a timestamp"])
def test_malformed_timestamps_are_unattributed_and_partial(ts):
    use = tenant_direct_use(PERIOD, A, agent_rows=[_turn(A, ts=ts)], broker_rows=[_run(A, ts=ts)])
    assert use["llm"]["coverage"] == use["cad"]["coverage"] == "partial"
    assert use["llm"]["turns"] == use["cad"]["runs"] == 0
    assert use["llm"]["usd_est"] == use["cad"]["engine_seconds"] == "0"
    obs = aps_usage_observation(PERIOD, [_run(A, ts=ts)])
    assert obs["coverage"] == "partial" and obs["status"] == ESTIMATED
    assert obs["total_usage"] == "0" and obs["usages"] == {}
    foreign = tenant_direct_use(PERIOD, A, agent_rows=[_turn(B, ts=ts)], broker_rows=[_run(B, ts=ts)])
    assert foreign["llm"]["coverage"] == foreign["cad"]["coverage"] == "complete"
    assert foreign["llm"]["turns"] == foreign["cad"]["runs"] == 0


@pytest.mark.parametrize("bad_line", ["{broken", "[]", "null", "42", '"text"'])
@pytest.mark.parametrize("with_valid_rows", [False, True])
def test_jsonl_skipped_lines_preserve_coverage_and_tenant_isolation(tmp_path, bad_line, with_valid_rows):
    agent_path = tmp_path / "agent.jsonl"
    broker_path = tmp_path / "broker.jsonl"
    agent = [_turn(A), _turn(B, usd=99)] if with_valid_rows else []
    broker = [_run(A, seconds=2), _run(B, seconds=99)] if with_valid_rows else []
    for path, rows in ((agent_path, agent), (broker_path, broker)):
        path.write_text("\n".join([json.dumps(row) for row in rows] + [bad_line, ""]), encoding="utf-8")
    agent_rows = direct_usage.load_agent_rows(agent_path)
    broker_rows = direct_usage.load_broker_rows(broker_path)
    assert agent_rows == agent and broker_rows == broker
    assert agent_rows.skipped_count == broker_rows.skipped_count == 1
    use = tenant_direct_use(PERIOD, A, agent_rows=agent_rows, broker_rows=broker_rows)
    assert use["llm"]["coverage"] == use["cad"]["coverage"] == "partial"
    assert use["llm"]["turns"] == use["cad"]["runs"] == int(with_valid_rows)
    assert use["llm"]["usd_est"] == ("0.1" if with_valid_rows else "0")
    assert use["cad"]["engine_seconds"] == ("2" if with_valid_rows else "0")
    obs = aps_usage_observation(PERIOD, broker_rows)
    assert obs["coverage"] == "partial" and obs["status"] == ESTIMATED
    assert obs["total_usage"] == ("101" if with_valid_rows else "0")


@pytest.mark.parametrize("contents", ["", "\n  \n"])
def test_clean_empty_jsonl_is_complete_zero(tmp_path, contents):
    path = tmp_path / "empty.jsonl"
    path.write_text(contents, encoding="utf-8")
    agent = direct_usage.load_agent_rows(path)
    broker = direct_usage.load_broker_rows(path)
    assert agent == broker == []
    assert agent.skipped_count == broker.skipped_count == 0
    use = tenant_direct_use(PERIOD, A, agent_rows=agent, broker_rows=broker)
    assert use["llm"]["coverage"] == use["cad"]["coverage"] == "complete"
    assert use["llm"]["turns"] == use["cad"]["runs"] == 0
    obs = aps_usage_observation(PERIOD, broker)
    assert obs["coverage"] == "complete" and obs["status"] == MEASURED
    assert obs["total_usage"] == "0" and obs["usages"] == {}


def test_marathon_utc_month_and_malformed_start(tmp_path):
    _marathon_run(tmp_path, A, "run-oct", "2026-09-30T23:30:00-05:00")
    _marathon_run(tmp_path, A, "run-sep", "2026-10-01T00:30:00+05:00")
    _marathon_run(tmp_path, A, "run-bad", "2026-09-garbage")
    _marathon_run(tmp_path, B, "run-foreign", "2026-09-15T00:00:00Z")
    for period, run_id in (("2026-09", "run-sep"), ("2026-10", "run-oct")):
        marathon = tenant_direct_use(period, A, agent_rows=[], broker_rows=[],
                                     marathon_root=tmp_path)["marathon"]
        assert marathon["coverage"] == "partial"
        assert marathon["runs"] == 1 and marathon["run_ids"] == [run_id]
        assert marathon["undated_runs"] == 1


@pytest.mark.parametrize("truncated", [False, True])
def test_postgres_loader_shape_tenant_isolation_and_coverage(monkeypatch, caplog, truncated):
    import broker_pg_store

    expected = [_run(A), _run(B, seconds=99), _run(A, seconds=None, usd=None, aps_live=False)]
    stored = []
    for row in expected:
        entry = dict(row)
        entry["inserted_at"] = datetime.fromtimestamp(entry.pop("ts"), timezone.utc)
        stored.append(entry)

    class Store:
        def usage_rows_for_period(self, period):
            assert period == PERIOD
            return stored, truncated

    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker_pg_store, "get_store", lambda: Store())
    rows = direct_usage.load_broker_rows(period=PERIOD)
    assert rows == expected
    assert rows.truncated is truncated
    assert "inserted_at" in stored[0]  # the store's result was not mutated
    cad = tenant_direct_use(PERIOD, A, agent_rows=[], broker_rows=rows)["cad"]
    assert cad["runs"] == 2 and cad["engine_seconds"] == "10.0"
    assert cad["coverage"] == ("partial" if truncated else "complete")
    obs = aps_usage_observation(PERIOD, rows)
    assert obs["total_usage"] == "109.0"
    assert obs["status"] == (ESTIMATED if truncated else MEASURED)
    assert obs["coverage"] == ("partial" if truncated else "complete")
    assert ("truncated" in caplog.text) is truncated


def test_postgres_loader_unreachable_is_reported_unknown_no_jsonl_fallback(tmp_path, monkeypatch, caplog):
    import broker_pg_store

    path = tmp_path / "broker.jsonl"
    path.write_text(json.dumps(_run(A)), encoding="utf-8")
    monkeypatch.setenv("BROKER_LEDGER", str(path))
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")

    class Store:
        def usage_rows_for_period(self, period):
            raise ConnectionError("database unavailable")

    monkeypatch.setattr(broker_pg_store, "get_store", lambda: Store())
    assert direct_usage.load_broker_rows(period=PERIOD) is None
    assert "broker usage rows unavailable for 2026-09" in caplog.text
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    assert direct_usage.load_broker_rows(period=PERIOD) == [_run(A)]


def test_postgres_empty_month_is_complete_zero(monkeypatch):
    import broker_pg_store

    class Store:
        def usage_rows_for_period(self, period):
            return [], False

    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker_pg_store, "get_store", lambda: Store())
    rows = direct_usage.load_broker_rows(period=PERIOD)
    assert rows == []
    assert aps_usage_observation(PERIOD, rows)["coverage"] == "complete"
    assert aps_usage_observation(PERIOD, rows)["total_usage"] == "0"


def test_output_is_json_with_decimal_strings_never_floats(tmp_path):
    use = tenant_direct_use(PERIOD, A, agent_rows=[_turn(A)], broker_rows=[_run(A)],
                            marathon_root=tmp_path)
    obs = aps_usage_observation(PERIOD, [_run(A)])
    for payload in (use, obs):
        text = json.dumps(payload)

        def _no_floats(value):
            assert not isinstance(value, float), text
            if isinstance(value, dict):
                for v in value.values():
                    _no_floats(v)
            elif isinstance(value, list):
                for v in value:
                    _no_floats(v)

        _no_floats(json.loads(text))


@pytest.mark.parametrize("bad", ["2026-9", "2026-13", "", None, 202609])
def test_bad_period_is_refused(bad):
    with pytest.raises(ValueError):
        tenant_direct_use(bad, A, agent_rows=[], broker_rows=[])
    with pytest.raises(ValueError):
        aps_usage_observation(bad, [])


def test_blank_tenant_is_refused():
    with pytest.raises(ValueError):
        tenant_direct_use(PERIOD, "  ", agent_rows=[], broker_rows=[])


@pytest.mark.parametrize("period,start,end", [
    ("2026-09", datetime(2026, 9, 1, tzinfo=timezone.utc),
     datetime(2026, 10, 1, tzinfo=timezone.utc)),
    ("2026-12", datetime(2026, 12, 1, tzinfo=timezone.utc),
     datetime(2027, 1, 1, tzinfo=timezone.utc)),
])
@pytest.mark.parametrize("cap", [1, 2, 3])
def test_agent_postgres_month_reader_is_bounded_and_tenant_scoped(monkeypatch, period, start, end, cap):
    from contextlib import contextmanager
    from datetime import timedelta
    from types import SimpleNamespace

    import agent_pg_store

    stored = [
        {"tenant_id": tenant, "ts": ts,
         "record": _turn(tenant, ts="1999-01-01T00:00:00Z", usd="0.123456789",
                         grant_kind="api_key")}
        for tenant, ts in ((A, start - timedelta(microseconds=1)), (B, start),
                           (A, start), (A, end - timedelta(microseconds=1)), (A, end))
    ]

    class Cursor:
        def execute(self, sql, params):
            assert "SELECT tenant_id, ts, record FROM agent_usage_turns" in sql
            assert "tenant_id = %(tenant_id)s" in sql
            assert "record->>'kind' = 'turn'" in sql
            assert "ts >= %(start)s AND ts < %(end)s" in sql
            assert "ORDER BY ts, usage_key" in sql
            assert "LIMIT %(limit)s" in sql
            assert params == {"tenant_id": A, "start": start, "end": end, "limit": cap + 1}
            self.rows = [r for r in stored if r["tenant_id"] == params["tenant_id"]
                         and params["start"] <= r["ts"] < params["end"]][:params["limit"]]

        def fetchall(self):
            return self.rows

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (SimpleNamespace(cursor=cursor), None))
    monkeypatch.setattr(agent_pg_store, "MAX_USAGE_ROWS", cap)
    rows, truncated = agent_pg_store.usage_rows_for_period(A, period)
    assert len(rows) == min(cap, 2)
    assert truncated is (cap < 2)
    assert all(r["tenant_id"] == A and start <= r["ts"] < end for r in rows)
    assert rows[0]["ts"] == start  # indexed DB timestamp, not the JSON timestamp
    assert rows[0]["usd_est"] == "0.123456789"
    assert {k: rows[0][k] for k in ("tokens_in", "tokens_out", "cache_read_tokens", "cache_creation_tokens")} == {
        "tokens_in": 100, "tokens_out": 20, "cache_read_tokens": 5, "cache_creation_tokens": 7}
    assert rows[0]["grant_kind"] == "api_key"
    assert "session_id" not in rows[0]


@pytest.mark.parametrize("mode", ["complete", "partial", "unknown", "empty"])
def test_agent_postgres_cost_api_coverage_and_payer(monkeypatch, tmp_path, mode):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import agent_pg_store
    import deps
    from routers import cost as cost_router

    calls = []

    def read(tenant_id, period):
        calls.append((tenant_id, period))
        if mode == "unknown":
            raise ConnectionError("database unavailable")
        if mode == "empty":
            return [], False
        # The collector also rejects a foreign tenant should a source return one.
        return [_turn(A, grant_kind="api_key", usd="0.123456789"),
                _turn(B, usd="99")], mode == "partial"

    monkeypatch.setattr(agent_pg_store, "usage_rows_for_period", read)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setenv("LEAF_COST_LEDGER_DIR", str(tmp_path / "cost"))
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "broker.jsonl"))
    # A readable JSONL must never mask a Postgres outage.
    legacy = tmp_path / "agent.jsonl"
    legacy.write_text(json.dumps(_turn(A, usd="50")), encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(legacy))
    app = FastAPI()
    app.dependency_overrides[deps.require_tenant] = lambda: A
    app.include_router(cost_router.router)
    with TestClient(app) as client:
        response = client.get("/api/cost", params={"period": PERIOD, "tenant_id": B})
    assert response.status_code == 200
    assert calls == [(A, PERIOD)]
    assert B not in response.text
    llm = response.json()["own_use"]["llm"]
    assert llm["coverage"] == ("complete" if mode == "empty" else mode)
    if mode == "unknown":
        assert all(llm[key] is None for key in ("turns", "tokens", "usd_est", "payer", "by_payer"))
    elif mode == "empty":
        assert llm["turns"] == 0 and llm["usd_est"] == "0"
    else:
        assert llm["turns"] == 1 and llm["usd_est"] == "0.123456789"
        assert llm["tokens"] == {"input": 100, "output": 20, "cache_read": 5, "cache_write": 7}
        assert llm["payer"] == "tenant_api_key"
        assert llm["by_payer"] == {"tenant_api_key": {"turns": 1, "usd_est": "0.123456789"}}


@pytest.mark.parametrize("bad", ["2026-9", "2026-13", "", None, "0000-01"])
def test_agent_postgres_reader_rejects_invalid_month_before_io(monkeypatch, bad):
    import agent_pg_store

    def unexpected_io():
        pytest.fail("invalid month reached database")

    monkeypatch.setattr(agent_pg_store, "_load_platform", unexpected_io)
    with pytest.raises(ValueError):
        agent_pg_store.usage_rows_for_period(A, bad)
