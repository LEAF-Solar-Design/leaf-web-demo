"""Database-free coverage of the opt-in Free plan admission allowance."""
from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER = Path(__file__).resolve().parent.parent
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import broker  # noqa: E402
import broker_pg_store  # noqa: E402
import free_allowance as allowance  # noqa: E402


@pytest.mark.parametrize("value", [None, "", "0", "true", " 1", "1 ", "01", "1"])
def test_enforcement_requires_exact_one(monkeypatch, value):
    monkeypatch.delenv("LEAF_FREE_ALLOWANCE_ENFORCE", raising=False)
    assert allowance.enforcement_enabled() is False
    if value is not None:
        monkeypatch.setenv("LEAF_FREE_ALLOWANCE_ENFORCE", value)
    assert allowance.enforcement_enabled() is (value == "1")
    assert allowance.enforcement_enabled({"LEAF_FREE_ALLOWANCE_ENFORCE": value}) is (value == "1")


def test_real_pricebook_limit():
    assert allowance.FREE_TIER == "hosted_free"
    assert allowance.load_monthly_limit() == 1


def test_limit_is_cached_per_path(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text('{"plans":{"free":{"runs_per_month":2}}}', encoding="utf-8")
    second.write_text('{"plans":{"free":{"runs_per_month":3}}}', encoding="utf-8")
    assert allowance.load_monthly_limit(first) == 2
    first.write_text("invalid", encoding="utf-8")
    assert allowance.load_monthly_limit(first) == 2
    assert allowance.load_monthly_limit(second) == 3


@pytest.mark.parametrize("limit", [0, 1, 12])
def test_nonnegative_limits(tmp_path, limit):
    path = tmp_path / "pricebook.json"
    path.write_text(json.dumps({"plans": {"free": {"runs_per_month": limit}}}), encoding="utf-8")
    assert allowance.load_monthly_limit(path) == limit


@pytest.mark.parametrize("text", [
    "{", "{}", '{"plans":{"free":{}}}',
    '{"plans":{"free":{"runs_per_month":null}}}',
    '{"plans":{"free":{"runs_per_month":-1}}}',
    '{"plans":{"free":{"runs_per_month":true}}}',
    '{"plans":{"free":{"runs_per_month":1.5}}}',
    '{"plans":{"free":{"runs_per_month":"1"}}}',
])
def test_invalid_config_raises(tmp_path, text):
    path = tmp_path / "pricebook.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(allowance.AllowanceConfigError):
        allowance.load_monthly_limit(path)


def test_missing_config_raises(tmp_path):
    with pytest.raises(allowance.AllowanceConfigError):
        allowance.load_monthly_limit(tmp_path / "missing.json")


def test_monthly_refusal_reuses_daily_shape():
    daily = allowance.usage.daily_quota_envelope("tenant-a", "hosted_free", 1, 1, tool="solve")
    monthly = allowance.monthly_refusal("tenant-a", 1, 1, "solve")
    assert set(monthly) == set(daily) | {"window"}
    assert monthly["window"] == monthly["error"]["window"] == "month"
    assert monthly["message"] == monthly["error"]["message"]
    assert "the Free plan includes 1 managed run(s) a month" in monthly["message"]
    assert "Upgrade" in monthly["message"]
    assert "bring your own account" in monthly["message"]
    for key in set(daily) - {"message", "error"}:
        assert monthly[key] == daily[key]


@pytest.fixture
def admission(monkeypatch):
    calls = []
    state = SimpleNamespace(monthly=0, daily=0, limit=1)

    def count(kind, tenant_id):
        calls.append((kind, tenant_id))
        return getattr(state, kind)

    usage = SimpleNamespace(
        daily_run_limit_for=lambda tier: None if tier == "hosted_pro" else 20,
        daily_quota_envelope=allowance.usage.daily_quota_envelope,
        daily_run_quota_check=lambda *_args: {"ok": True},
    )
    store = SimpleNamespace(
        daily_live_run_count=lambda tenant: count("daily", tenant),
        monthly_live_run_count=lambda tenant: count("monthly", tenant),
    )
    monkeypatch.setenv("LEAF_FREE_ALLOWANCE_ENFORCE", "1")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker, "_get_usage", lambda: usage)
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    monkeypatch.setattr(allowance, "usage", usage)
    monkeypatch.setattr(allowance, "load_monthly_limit", lambda: state.limit)
    monkeypatch.setattr(broker, "_free_allowance_config_logged", False)
    return state, calls


@pytest.mark.parametrize("used,limit,refused", [(0, 1, False), (1, 1, True), (0, 0, True)])
def test_monthly_preflight(admission, used, limit, refused):
    state, calls = admission
    state.monthly, state.limit = used, limit
    result = broker._run_quota_preflight("tenant-a", "hosted_free", {"name": "solve"})
    assert calls == [("daily", "tenant-a"), ("monthly", "tenant-a")]
    if not refused:
        assert result is None
        return
    env, status = result
    assert status == 429
    assert env["error_code"] == env["error"]["error_code"] == "quota_exceeded"
    assert env["window"] == "month"
    assert env["degraded_mode"] is False
    assert env["limit"] == limit and env["used"] == used
    assert env["tool"] == "solve"
    assert f"the Free plan includes {limit} managed run(s) a month" in env["message"]


def test_config_errors_fail_closed_and_log_once(admission, monkeypatch, caplog):
    def fail():
        raise allowance.AllowanceConfigError("private configuration detail")

    monkeypatch.setattr(allowance, "load_monthly_limit", fail)
    for _ in range(2):
        env, status = broker._run_quota_preflight("tenant-a", "hosted_free", {"name": "solve"})
        assert status == 429
        assert env["limit"] == 0
        assert env["error_code"] == "quota_exceeded"
        assert env["window"] == "month"
        assert env["degraded_mode"] is False
        assert "free allowance is unavailable" in env["message"]
        assert env["message"] == env["error"]["message"]
    logs = [record.getMessage() for record in caplog.records if record.name == broker.__name__]
    assert logs == ["Free allowance is unavailable; refusing hosted_free runs"]


@pytest.mark.parametrize("tier", ["hosted_starter", "hosted_pro"])
def test_other_tiers_never_read_monthly_allowance(admission, monkeypatch, tier):
    monkeypatch.setattr(allowance, "load_monthly_limit", lambda: pytest.fail("read free config"))
    assert broker._run_quota_preflight("tenant-b", tier, {}) is None
    assert admission[1] == [("daily", "tenant-b")]


@pytest.mark.parametrize("tier", ["hosted_free", "hosted_starter", "hosted_pro"])
def test_disabled_never_reads_monthly_allowance(admission, monkeypatch, tier):
    monkeypatch.delenv("LEAF_FREE_ALLOWANCE_ENFORCE")
    monkeypatch.setattr(allowance, "load_monthly_limit", lambda: pytest.fail("read free config"))
    assert broker._run_quota_preflight("tenant-a", tier, {}) is None
    assert admission[1] == [("daily", "tenant-a")]


def test_legacy_never_reads_monthly_allowance(admission, monkeypatch):
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setattr(allowance, "load_monthly_limit", lambda: pytest.fail("read free config"))
    assert broker._run_quota_preflight("tenant-a", "hosted_free", {}) is None
    assert admission[1] == []


def test_daily_refusal_stays_first(admission, monkeypatch):
    admission[0].daily = 20
    monkeypatch.setattr(allowance, "load_monthly_limit", lambda: pytest.fail("read free config"))
    env, status = broker._run_quota_preflight("tenant-a", "hosted_free", {})
    assert status == 429
    assert env["quota_kind"] == "daily_runs"
    assert "window" not in env
    assert env["degraded_mode"] is False
    assert admission[1] == [("daily", "tenant-a")]


def test_monthly_sql_matches_daily_exclusions_and_utc_month():
    def query(method):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "execute")
        return ast.literal_eval(call.args[0]), call.args[1]

    daily, _ = query(broker_pg_store.PostgresBrokerStore.daily_live_run_count)
    monthly, params = query(broker_pg_store.PostgresBrokerStore.monthly_live_run_count)
    assert monthly.split("AND status NOT IN ")[1].split(" AND ")[0] == daily.split(
        "AND status NOT IN ")[1].split(" AND ")[0]
    assert "FROM broker_usage_ledger WHERE tenant_id = %(tenant_id)s AND aps_live = TRUE" in monthly
    assert ("date_trunc('month', to_timestamp(ts) AT TIME ZONE 'UTC') = "
            "date_trunc('month', NOW() AT TIME ZONE 'UTC')") in monthly
    assert isinstance(params, ast.Dict)
    assert [ast.literal_eval(key) for key in params.keys] == ["tenant_id"]
