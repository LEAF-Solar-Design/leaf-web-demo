"""TCM-09a: publish the monthly share ledger and serve the tenant-guarded GET /api/cost.

Transparency of Leaf's real cost, never billing. Hermetic: every store lives in
tmp_path, every ledger env var is set or cleared per test, and the app is a
minimal FastAPI with only the cost router mounted.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import publisher  # noqa: E402
from cost_meter.ledger import ESTIMATED, LEAF, MEASURED, SHARE_ONE, validate_shares  # noqa: E402
from cost_meter.store import CostLedgerStore  # noqa: E402

PERIOD = "2026-09"
A = "tenant_a"
B = "tenant_b"
EFS = "aws:amazon-elastic-file-system"


def _cost(resource_id, gross, credits="0", coverage="complete", batch="batch-1", period=PERIOD):
    return {"kind": "cost", "resource_id": resource_id, "period": period, "gross_cost_usd": gross,
            "credits_usd": credits, "coverage": coverage, "source_batch_id": batch, "source": "test"}


def _usage(resource_id, total, usages, status=MEASURED, unit="unit", coverage="complete", period=PERIOD):
    return {"kind": "usage", "resource_id": resource_id, "period": period, "unit": unit,
            "total_usage": total, "usages": usages, "status": status, "coverage": coverage,
            "source": "test"}


def _fixture_observations():
    return [
        _cost(EFS, "30.00", credits="3.00"),
        _usage(EFS, "3", {f"{A}|": "1", f"{B}|": "1", "leaf|development": "0.5"},
               status=ESTIMATED, unit="gb-month"),
        _cost("aps:engine", "50.00", batch="batch-2"),
        _usage("aps:engine", "100", {f"{A}|": "60", f"{B}|": "30"}, unit="engine-second"),
        _cost("vendor:figma", "15.00", batch="cost-vendors:2026-09:abc"),
    ]


def _by_id(pairs):
    return {rp.resource_id: (rp, shares) for rp, shares in pairs}


def _share_map(shares):
    return {(s.participant_id, s.dimension): s for s in shares}


# --------------------------------------------------------------------------- #
# the publisher
# --------------------------------------------------------------------------- #
def test_cost_only_resource_sums_costs_weakest_coverage_and_is_unattributed_leaf():
    pairs = publisher.build_period(PERIOD, [
        _cost("vendor:figma", "10.00", credits="1.00", coverage="complete", batch="b1"),
        _cost("vendor:figma", "5.50", credits="0.25", coverage="partial", batch="b2"),
    ])
    assert len(pairs) == 1
    rp, shares = pairs[0]
    assert rp.gross_cost_usd == Decimal("15.50")
    assert rp.credits_usd == Decimal("1.25")
    assert rp.coverage == "partial"
    assert set(rp.source_batch_ids) == {"b1", "b2"}
    assert rp.total_usage is None
    assert [(s.participant_id, s.dimension, s.usage_share, s.status) for s in shares] == [
        (LEAF, "unattributed", SHARE_ONE, ESTIMATED)]


def test_usage_only_resource_has_zero_gross_unknown_coverage_and_still_appears():
    pairs = publisher.build_period(PERIOD, [_usage("llm:anthropic-main", "10", {f"{A}|": "4"}, unit="token")])
    assert len(pairs) == 1
    rp, shares = pairs[0]
    assert rp.resource_id == "llm:anthropic-main"
    assert rp.gross_cost_usd == Decimal(0) and rp.credits_usd == Decimal(0)
    assert rp.coverage == "unknown"
    assert rp.unit == "token"
    got = _share_map(shares)
    assert got[(A, "")].usage_share == Decimal("0.4")
    assert got[(LEAF, "unattributed")].usage_share == Decimal("0.6")


def test_usage_and_cost_join_by_resource_id():
    rp, shares = _by_id(publisher.build_period(PERIOD, _fixture_observations()))["aps:engine"]
    assert rp.gross_cost_usd == Decimal("50.00")
    assert rp.total_usage == Decimal("100")
    assert rp.unit == "engine-second"
    assert rp.source_batch_ids == ("batch-2",)
    got = _share_map(shares)
    assert got[(A, "")].usage_share == Decimal("0.6")
    assert got[(B, "")].usage_share == Decimal("0.3")
    assert got[(LEAF, "unattributed")].usage_share == Decimal("0.1")
    assert {s.status for s in shares} == {MEASURED}


def test_measured_usage_is_preferred_over_estimated():
    pairs = publisher.build_period(PERIOD, [
        _usage("aps:engine", "10", {f"{A}|": "10"}, status=ESTIMATED),
        _usage("aps:engine", "10", {f"{B}|": "10"}, status=MEASURED),
        _usage("aps:engine", "10", {f"{A}|": "5"}, status=MEASURED),
    ])
    _, shares = pairs[0]
    assert [(s.participant_id, s.usage_share, s.status) for s in shares] == [(B, SHARE_ONE, MEASURED)]


def test_every_resource_shares_sum_to_exactly_one():
    pairs = publisher.build_period(PERIOD, _fixture_observations())
    assert [rp.resource_id for rp, _ in pairs] == sorted([EFS, "aps:engine", "vendor:figma"])
    for rp, shares in pairs:
        validate_shares(shares)
        assert sum((s.usage_share for s in shares), Decimal(0)) == SHARE_ONE, rp.resource_id


def test_an_observation_for_another_period_is_refused():
    with pytest.raises(ValueError):
        publisher.build_period(PERIOD, [_cost("vendor:figma", "1.00", period="2026-08")])
    with pytest.raises(ValueError):
        publisher.build_period(PERIOD, [{"kind": "invoice", "resource_id": "vendor:x", "period": PERIOD}])


def test_duplicate_costs_count_once_and_shared_batches_keep_each_resource(tmp_path):
    store = CostLedgerStore(tmp_path / "ledger")
    cost = _cost(EFS, "12.50", credits="1.25")
    pub = publisher.publish_period(store, PERIOD,
                                   [cost, dict(cost), _cost("vendor:figma", "7")], "snapshot")
    rows = {r.resource_id: r.resource_period for r in store.read_publication(pub)}
    assert rows[EFS].gross_cost_usd == Decimal("12.50")
    assert rows[EFS].credits_usd == Decimal("1.25")
    assert rows["vendor:figma"].gross_cost_usd == Decimal("7")


@pytest.mark.parametrize("changed", [{"gross_cost_usd": "11"}, {"credits_usd": "2"}])
def test_conflicting_batch_amounts_write_nothing(tmp_path, changed):
    root = tmp_path / "ledger"
    store = CostLedgerStore(root)
    first = publisher.publish_period(store, PERIOD, [_cost(EFS, "10")], "initial")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    cost = _cost(EFS, "10")
    with pytest.raises(ValueError, match="conflicting amounts"):
        publisher.publish_period(store, PERIOD,
                                 [_cost("aps:engine", "5"), cost, {**cost, **changed}], "bad")
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert publisher.latest_publication_id(store, PERIOD) == first


def test_same_batch_decimal_equal_amounts_count_once(tmp_path):
    store = CostLedgerStore(tmp_path / "ledger")
    pub = publisher.publish_period(store, PERIOD, [
        _cost(EFS, "10", credits="1"),
        _cost(EFS, "10.00", credits="1.00"),
    ], "snapshot")
    row, = store.read_publication(pub)
    assert row.resource_period.gross_cost_usd == Decimal("10")
    assert row.resource_period.credits_usd == Decimal("1")
    assert row.resource_period.source_batch_ids == ("batch-1",)


@pytest.mark.parametrize("coverages", [("complete", "partial"), ("partial", "complete")])
def test_same_batch_duplicate_keeps_weakest_coverage(tmp_path, coverages):
    store = CostLedgerStore(tmp_path / "ledger")
    pub = publisher.publish_period(store, PERIOD, [
        _cost(EFS, "10", coverage=coverage) for coverage in coverages
    ], "snapshot")
    row, = store.read_publication(pub)
    assert row.resource_period.gross_cost_usd == Decimal("10")
    assert row.resource_period.coverage == "partial"


@pytest.mark.parametrize("changed", [{"gross_cost_usd": "11"}, {"credits_usd": "2"}])
def test_same_batch_conflict_leaves_new_ledger_empty(tmp_path, changed):
    root = tmp_path / "ledger"
    store = CostLedgerStore(root)
    cost = _cost(EFS, "10")
    with pytest.raises(ValueError, match="conflicting amounts"):
        publisher.publish_period(store, PERIOD,
                                 [_cost("aps:engine", "5"), cost, {**cost, **changed}], "bad")
    assert not any(p.is_file() for p in root.rglob("*"))


def test_snapshot_removes_absent_resources_and_partial_snapshot_carries_previous(tmp_path):
    store = CostLedgerStore(tmp_path / "ledger")
    first = publisher.publish_period(store, PERIOD, _fixture_observations(), "initial")
    frozen = store.read_publication(first)
    partial = publisher.publish_period(store, PERIOD, [_cost(EFS, "31")], "outage",
                                       missing_sources=["vendors"])
    assert {r.resource_id for r in store.read_publication(partial)} == {
        EFS, "aps:engine", "vendor:figma"}
    assert publisher.publication_info(store, partial)["carried_forward"] == ["aps:engine", "vendor:figma"]
    complete = publisher.publish_period(store, PERIOD, [_cost(EFS, "31")], "recovered")
    assert [r.resource_id for r in store.read_publication(complete)] == [EFS]
    assert publisher.publication_info(store, complete)["carried_forward"] == []
    again = publisher.publish_period(store, PERIOD, [], "another outage", missing_sources=["vendors"])
    assert [r.resource_id for r in store.read_publication(again)] == [EFS]
    assert publisher.publication_info(store, again)["carried_forward"] == [EFS]
    empty = publisher.publish_period(store, PERIOD, [], "empty snapshot")
    assert store.read_publication(empty) == []
    assert store.read_publication(first) == frozen


def test_source_health_changes_identity_without_changing_revisions(tmp_path):
    store = CostLedgerStore(tmp_path / "ledger")
    observations = [_cost(EFS, "10")]
    first = publisher.publish_period(store, PERIOD, observations, "outage",
                                     sources=["test"], missing_sources=["vendors"])
    old_info = publisher.publication_info(store, first)
    second = publisher.publish_period(store, PERIOD, observations, "recovered", sources=["test"])
    assert second != first
    assert publisher.publication_info(store, second)["missing_sources"] == []
    assert publisher.publication_info(store, first) == old_info
    assert store.read_publication(first) == store.read_publication(second)
    assert len(store.history(EFS, PERIOD)) == 1


@pytest.mark.parametrize("coverage", ["partial", "unknown"])
def test_usage_coverage_caps_resource_and_keeps_previous_publication(tmp_path, coverage):
    store = CostLedgerStore(tmp_path / "ledger")
    cost = _cost(EFS, "10")
    usage = _usage(EFS, "10", {f"{A}|": "10"})
    first = publisher.publish_period(store, PERIOD, [cost, usage], "complete")
    second = publisher.publish_period(store, PERIOD, [cost, {**usage, "coverage": coverage}], "limited")
    assert store.read_publication(second)[0].resource_period.coverage == coverage
    assert store.read_publication(first)[0].resource_period.coverage == "complete"


def test_republishing_identical_input_adds_no_revision(tmp_path):
    store = CostLedgerStore(tmp_path / "ledger")
    first = publisher.publish_period(store, PERIOD, _fixture_observations(), "first")
    second = publisher.publish_period(store, PERIOD, _fixture_observations(), "again")
    assert first == second
    for resource_id in (EFS, "aps:engine", "vendor:figma"):
        assert len(store.history(resource_id, PERIOD)) == 1
    lines = (tmp_path / "ledger" / "revisions" / f"{PERIOD}.jsonl").read_bytes().splitlines()
    assert len(lines) == 3
    assert publisher.latest_publication_id(store, PERIOD) == first


def test_publish_records_missing_sources_and_points_latest_at_the_newest(tmp_path):
    store = CostLedgerStore(tmp_path / "ledger")
    first = publisher.publish_period(store, PERIOD, _fixture_observations()[:2], "partial run",
                                     sources=["storage-snapshots"], missing_sources=["aws-cost-explorer"])
    assert publisher.publication_info(store, first)["missing_sources"] == ["aws-cost-explorer"]
    second = publisher.publish_period(store, PERIOD, _fixture_observations(), "full run",
                                      sources=["aws-cost-explorer", "storage-snapshots"])
    assert second != first
    assert publisher.latest_publication_id(store, PERIOD) == second
    info = publisher.publication_info(store, second)
    assert info["missing_sources"] == []
    assert info["sources"] == ["aws-cost-explorer", "storage-snapshots"]
    assert isinstance(info["published_at"], str)
    # without the pointer the bounded manifest scan still finds a publication of the month
    (tmp_path / "ledger" / publisher.LATEST_DIR / f"{PERIOD}.json").unlink()
    assert publisher.latest_publication_id(store, PERIOD) in {first, second}
    assert publisher.latest_publication_id(store, "2026-08") is None


# --------------------------------------------------------------------------- #
# GET /api/cost
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ledger_dir(tmp_path, monkeypatch):
    root = tmp_path / "ledger"
    for name in ("LEAF_AUTH_LIVE", "LEAF_BROKER_STORE", "LEAF_AGENT_STORE", "LEAF_MARATHON_RUNS_DIR",
                 "LEAF_USAGE_LEDGER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEAF_COST_LEDGER_DIR", str(root))
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "broker_ledger.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(tmp_path / "agent_ledger.jsonl"))
    return root


@pytest.fixture()
def published(ledger_dir):
    publisher.publish_period(CostLedgerStore(ledger_dir), PERIOD, _fixture_observations(), "fixture",
                             sources=["test"], missing_sources=["aws-cost-explorer"])
    return ledger_dir


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import cost as cost_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(cost_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _get(as_tenant=None, **params):
    headers = {"X-Tenant-Id": as_tenant} if as_tenant else {}
    return _client().get("/api/cost", params={"period": PERIOD, **params}, headers=headers)


def _rows(body):
    return {row["resource_id"]: row for row in body["resources"]}


@pytest.mark.parametrize("truncated", [False, True])
def test_postgres_broker_rows_reach_publisher_and_tenant_api(published, monkeypatch, truncated):
    from datetime import datetime, timezone

    import broker_pg_store
    from cost_meter import publish_main

    calls = []

    class Store:
        def usage_rows_for_period(self, period):
            calls.append(period)
            assert period == PERIOD
            return [
                {"tenant_id": tenant, "tool": "extract", "engine_seconds": seconds,
                 "usd_est": 1.0, "status": "ok", "job_id": "job-1", "aps_live": True,
                 "inserted_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}
                for tenant, seconds in ((A, 10.0), (B, 90.0))
            ], truncated

    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker_pg_store, "get_store", lambda: Store())
    observation, = publish_main._collect_broker(PERIOD, datetime.now(timezone.utc))
    assert observation["total_usage"] == "100.0"
    assert observation["usages"] == {f"{A}|": "10.0", f"{B}|": "90.0"}
    assert observation["coverage"] == ("partial" if truncated else "complete")
    assert observation["status"] == (ESTIMATED if truncated else MEASURED)
    response = _get(A, tenant_id=B)
    assert response.status_code == 200
    assert B not in response.text
    cad = response.json()["own_use"]["cad"]
    assert cad["runs"] == 1 and cad["engine_seconds"] == "10.0"
    assert cad["coverage"] == observation["coverage"]
    assert calls == [PERIOD, PERIOD]


def test_postgres_broker_outage_is_missing_for_publish_and_unknown_for_api(ledger_dir, monkeypatch):
    from datetime import datetime, timezone

    import broker_pg_store
    from cost_meter import publish_main

    def unavailable():
        raise ConnectionError("database unavailable")

    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker_pg_store, "get_store", unavailable)
    with pytest.raises(publish_main.SourceMissing, match="unreadable"):
        publish_main._collect_broker(PERIOD, datetime.now(timezone.utc))
    response = _get(A)
    assert response.status_code == 200
    cad = response.json()["own_use"]["cad"]
    assert cad["coverage"] == "unknown" and cad["engine_seconds"] is None


def test_a_tenant_sees_its_own_share_and_never_another_tenants_id(published):
    r = _get(A)
    assert r.status_code == 200
    body = r.json()
    assert B not in r.text
    assert body["period"] == PERIOD
    assert body["publication_id"].startswith(f"pub-{PERIOD}-")
    assert body["missing_sources"] == ["aws-cost-explorer"]
    assert body["own_use"]["tenant_id"] == A
    aps = _rows(body)["aps:engine"]
    assert aps["your_share"] == "0.600000000000"
    assert aps["your_implied_cost_usd"] == "30.00"
    assert aps["other_customers_share"] == "0.300000000000"
    assert aps["leaf_share"] == {"ci": "0.000000000000", "development": "0.000000000000",
                                 "fleet": "0.000000000000", "unattributed": "0.100000000000"}
    assert aps["status"] == MEASURED
    efs = _rows(body)[EFS]
    assert efs["your_share"] == "0.333333333333"
    assert efs["other_customers_share"] == "0.333333333333"
    assert efs["status"] == ESTIMATED
    assert efs["display_name"] == "Amazon Elastic File System"
    figma = _rows(body)["vendor:figma"]
    assert figma["your_share"] == "0.000000000000" and figma["your_implied_cost_usd"] == "0.00"
    assert figma["leaf_share"]["unattributed"] == "1.000000000000"


def test_two_tenants_see_identical_totals_and_rows_apart_from_their_own_share(published):
    a, b = _get(A).json(), _get(B).json()
    assert a["totals"] == b["totals"] == {"gross_cost_usd": "95.00", "credits_usd": "3.00"}
    assert a["coverage_summary"] == b["coverage_summary"]
    assert a["publication_id"] == b["publication_id"]
    assert _rows(b)["aps:engine"]["your_share"] == "0.300000000000"
    assert _rows(b)["aps:engine"]["other_customers_share"] == "0.600000000000"
    assert A not in json.dumps(b["resources"]) and B not in json.dumps(a["resources"])

    def strip(body):
        return [{k: v for k, v in row.items()
                 if k not in ("your_share", "your_implied_cost_usd", "other_customers_share")}
                for row in body["resources"]]

    assert strip(a) == strip(b)


def test_the_tenant_id_cannot_come_from_the_query(published):
    own = _get(A).json()
    spoofed = _get(A, tenant_id=B, tenant=B, x_tenant_id=B)
    assert spoofed.status_code == 200
    assert B not in spoofed.text
    assert spoofed.json()["own_use"]["tenant_id"] == A
    assert _rows(spoofed.json()) == _rows(own)
    anonymous = _get(None, tenant_id=A).json()  # no header: the default tenant, not the query's
    assert anonymous["own_use"]["tenant_id"] != A
    assert _rows(anonymous)["aps:engine"]["your_share"] == "0.000000000000"


def test_the_response_carries_no_floats(published):
    def refuse(text):
        raise AssertionError(f"float in /api/cost JSON: {text}")

    body = json.loads(_get(A).text, parse_float=refuse)
    for row in body["resources"]:
        for key in ("total_usage", "gross_cost_usd", "credits_usd", "your_share",
                    "your_implied_cost_usd", "other_customers_share"):
            assert row[key] is None or isinstance(row[key], str), key
        assert all(isinstance(v, str) for v in row["leaf_share"].values())


def test_an_empty_ledger_returns_no_resources(ledger_dir, monkeypatch):
    r = _get(A)
    assert r.status_code == 200
    body = r.json()
    assert body["resources"] == [] and body["publication_id"] is None
    assert body["totals"] == {"gross_cost_usd": "0", "credits_usd": "0"}
    monkeypatch.delenv("LEAF_COST_LEDGER_DIR")
    disabled = _get(A).json()
    assert disabled["resources"] == [] and disabled["publication_id"] is None


def test_a_malformed_period_is_refused(published):
    assert _client().get("/api/cost", params={"period": "2026-13"},
                         headers={"X-Tenant-Id": A}).status_code == 400
    other_month = _client().get("/api/cost", params={"period": "2026-08"}, headers={"X-Tenant-Id": A})
    assert other_month.status_code == 200 and other_month.json()["resources"] == []


# --------------------------------------------------------------------------- #
# scripts/publish-cost-ledger.py
# --------------------------------------------------------------------------- #
def _script():
    spec = importlib.util.spec_from_file_location(
        "publish_cost_ledger", PROJECT_ROOT / "scripts" / "publish-cost-ledger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _broken(period, now):
    raise RuntimeError("no credentials")


def test_package_publish_dry_run_defaults_to_utc_month_with_fixture_file(tmp_path):
    from datetime import datetime, timedelta, timezone
    from cost_meter import publish_main

    observations = tmp_path / "observations.jsonl"
    observations.write_text("\n".join(json.dumps(o) for o in _fixture_observations()) + "\n",
                            encoding="utf-8")
    out = io.StringIO()
    calls = []
    code = publish_main.main(
        ["--dry-run", "--observations", str(observations)],
        collectors={"aws-cost-explorer": lambda p, n: calls.append(p)},
        environ={"LEAF_COST_DISABLE_AWS": "1"}, stdout=out,
        now=datetime(2026, 10, 1, 1, tzinfo=timezone(timedelta(hours=2))))
    assert code == 0 and calls == []
    assert len(out.getvalue().splitlines()) == 1
    summary = json.loads(out.getvalue())
    assert summary["period"] == PERIOD
    assert summary["publication_id"] is None
    assert summary["missing_sources"] == ["aws-cost-explorer"]
    assert {r["resource_period"]["resource_id"] for r in summary["resources"]} == {
        EFS, "aps:engine", "vendor:figma"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["observations.jsonl"]


def test_python_module_dispatches_publish_without_network(monkeypatch, capsys):
    import runpy
    from cost_meter import publish_main

    monkeypatch.setattr(publish_main, "DEFAULT_COLLECTORS",
                        {"fixture": lambda p, n: _fixture_observations()})
    monkeypatch.setattr(sys, "argv", ["cost_meter", "publish", "--period", PERIOD, "--dry-run"])
    with pytest.raises(SystemExit) as result:
        runpy.run_module("cost_meter", run_name="__main__")
    assert result.value.code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["period"] == PERIOD and len(summary["resources"]) == 3


def test_package_publish_failure_returns_nonzero(tmp_path, monkeypatch):
    from cost_meter import publish_main

    def fail(*args, **kwargs):
        raise OSError("publication unavailable")

    monkeypatch.setattr(publisher, "publish_period", fail)
    out = io.StringIO()
    assert publish_main.main(
        ["--period", PERIOD],
        collectors={"fixture": lambda p, n: _fixture_observations()},
        environ={"LEAF_COST_LEDGER_DIR": str(tmp_path / "ledger")}, stdout=out) == 1
    assert out.getvalue() == ""


def test_script_dry_run_merges_observation_files_and_reports_failed_collectors(tmp_path):
    extra = tmp_path / "pooled.jsonl"
    extra.write_text("\n".join(json.dumps(o) for o in [
        _usage("aps:engine", "100", {f"{A}|": "60"}),
        _cost("vendor:other-month", "9.00", period="2026-08"),
    ]) + "\n", encoding="utf-8")
    out = io.StringIO()
    code = _script().main(
        ["--period", PERIOD, "--dry-run", "--observations", str(extra)],
        collectors={"aws-cost-explorer": _broken, "fixture": lambda p, n: [_cost("aps:engine", "50.00")]},
        environ={}, stdout=out)
    assert code == 0
    printed = json.loads(out.getvalue())
    assert printed["missing_sources"] == ["aws-cost-explorer"]
    assert printed["sources"] == ["fixture", "observations:pooled.jsonl"]
    (resource,) = printed["resources"]
    assert resource["resource_period"]["resource_id"] == "aps:engine"
    assert resource["resource_period"]["gross_cost_usd"] == "50.00"
    assert not (tmp_path / "ledger").exists()


def test_script_publishes_and_the_publication_records_missing_sources(tmp_path):
    out = io.StringIO()
    root = tmp_path / "ledger"
    code = _script().main(
        ["--period", PERIOD],
        collectors={"aws-cost-explorer": _broken, "fixture": lambda p, n: _fixture_observations()},
        environ={"LEAF_COST_LEDGER_DIR": str(root)}, stdout=out)
    assert code == 0
    summary = json.loads(out.getvalue())
    publication_id = summary["publication_id"]
    assert summary["period"] == PERIOD and summary["resources"] == 3
    assert len(out.getvalue().splitlines()) == 1
    store = CostLedgerStore(root)
    assert publisher.latest_publication_id(store, PERIOD) == publication_id
    assert publisher.publication_info(store, publication_id)["missing_sources"] == ["aws-cost-explorer"]
    assert {r.resource_id for r in store.read_publication(publication_id)} == {
        EFS, "aps:engine", "vendor:figma"}
    assert _script().main(["--period", PERIOD], collectors={}, environ={}, stdout=io.StringIO()) == 2
