"""Database-free receipt metering contracts with fake transactions and sinks."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import ast
import sys
import uuid

import pytest

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import e2b_cost_meter as meter


ORG = "00000000-0000-4000-8000-000000000001"
RATE = Decimal("0.000014")


def receipt(**changes):
    return {
        "sandboxId": "sandbox-1",
        "startedAt": "2026-10-22T10:00:00.000Z",
        "stoppedAt": "2026-10-22T10:00:02.500Z",
        **changes,
    }


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.delenv("LEAF_COST_METERING", raising=False)
    monkeypatch.delenv("LEAF_E2B_USD_PER_SECOND", raising=False)
    for counter in ("skipped_unresolved", "skipped_invalid", "record_failed"):
        monkeypatch.setattr(meter, counter, 0)
    monkeypatch.setattr(meter, "_logged_error_classes", set())


@pytest.fixture
def store():
    # Load the actual shared key and validation functions, without opening a DB.
    return meter._platform()[1]


def enable(monkeypatch):
    monkeypatch.setenv("LEAF_COST_METERING", "1")
    monkeypatch.setenv("LEAF_E2B_USD_PER_SECOND", "0.000014")


def test_config_default():
    assert meter.meter_config({}) == {
        "enabled": False, "reason": "flag_not_enabled", "rate": None,
    }


@pytest.mark.parametrize("flag", ["", "0", "true", 1, " 1"])
def test_config_requires_exact_flag(flag):
    config = meter.meter_config({"LEAF_COST_METERING": flag,
                                "LEAF_E2B_USD_PER_SECOND": "0.000014"})
    assert not config["enabled"]
    assert config["rate"] is None


@pytest.mark.parametrize("rate", [None, "", "0", "-1", "0.0000001", "1e-6",
                                  "1.", "100000", "0.1\n"])
def test_config_rejects_invalid_rate(rate):
    config = meter.meter_config({"LEAF_COST_METERING": "1",
                                "LEAF_E2B_USD_PER_SECOND": rate})
    assert not config["enabled"]
    assert config["reason"] in ("invalid_rate", "nonpositive_rate")
    assert config["rate"] is None


def test_config_enabled():
    assert meter.meter_config({"LEAF_COST_METERING": "1",
                              "LEAF_E2B_USD_PER_SECOND": "0.000014"}) == {
        "enabled": True, "reason": "enabled", "rate": RATE,
    }


def test_exact_event(store):
    facts = receipt()
    event = meter.cost_event_from_receipt(ORG.upper(), facts, RATE)
    assert event == {
        "org_id": uuid.UUID(ORG), "addon": "e2b_exec", "mode": "managed",
        "vendor": "e2b", "unit": "sandbox_seconds", "quantity": Decimal("2.500"),
        "unit_cost_usd": RATE, "cost_usd": Decimal("0.000035"),
        "cost_basis": "estimated",
        "occurred_at": datetime(2026, 10, 22, 10, 0, 2, 500000, tzinfo=timezone.utc),
        "provenance": {"source": "broker-e2b", "run_ref": "sandbox-1"},
        "event_key": store.cost_event_key(ORG, "e2b_exec", "broker-e2b", "sandbox-1"),
    }
    assert str(event["quantity"]) == "2.500"
    assert str(event["cost_usd"]) == "0.000035"
    assert store.validate_cost_event(event) == event
    assert facts == receipt()
    assert meter.skipped_invalid == meter.skipped_unresolved == 0


def test_half_up_rounding(store):
    event = meter.cost_event_from_receipt(ORG, receipt(), Decimal("0.000001"))
    assert str(event["cost_usd"]) == "0.000003"


def test_shared_python_key_vector(store):
    assert store.cost_event_key(ORG, "e2b_exec", "harness-e2b", "run-1") == (
        "93d6941c0d1da035f8fafeb675cd2f575fc8bc5d1b5894c3d04f136c55e301f6"
    )
    event = meter.cost_event_from_receipt(ORG, receipt(), RATE)
    other = meter.cost_event_from_receipt(ORG, receipt(sandboxId="sandbox-2"), RATE)
    assert event["event_key"] != other["event_key"]


@pytest.mark.parametrize("org,changes,reason", [
    ("tenant-slug", {}, "unresolved_org"),
    (ORG, {"sandboxId": None}, "invalid_sandbox_id"),
    (ORG, {"sandboxId": "sandbox-\u00e9"}, "invalid_sandbox_id"),
    (ORG, {"sandboxId": "s" * 129}, "invalid_sandbox_id"),
    (ORG, {"startedAt": "bad"}, "invalid_timestamp"),
    (ORG, {"stoppedAt": "2026-10-22T10:00:02"}, "invalid_timestamp"),
    (ORG, {"stoppedAt": "2026-10-22T10:00:02+01:00"}, "invalid_timestamp"),
    (ORG, {"stoppedAt": "2026-10-22T09:59:59Z"}, "invalid_lifetime"),
    (ORG, {"stoppedAt": "2026-10-22T11:00:00.001Z"}, "invalid_lifetime"),
    (ORG, {"stoppedAt": "2026-02-30T10:00:02Z"}, "invalid_timestamp"),
])
def test_skip_and_count(monkeypatch, org, changes, reason):
    enable(monkeypatch)
    monkeypatch.setattr(meter, "_platform", lambda: pytest.fail("skip loaded platform"))
    facts = receipt(**changes)
    if changes.get("sandboxId", "present") is None:
        del facts["sandboxId"]
    assert meter.cost_event_from_receipt(org, facts, RATE) is None
    assert meter._event_and_reason(org, facts, RATE) == (None, reason)
    meter.record_e2b_cost(org, facts)
    assert meter.skipped_unresolved == int(reason == "unresolved_org")
    assert meter.skipped_invalid == int(reason != "unresolved_org")
    assert meter.record_failed == 0


def test_disabled_does_not_load_platform(monkeypatch):
    monkeypatch.setattr(meter, "_platform", lambda: pytest.fail("disabled loaded platform"))
    meter.record_e2b_cost(ORG, receipt())
    assert meter.record_failed == 0


def test_sink_failures_leave_result_unchanged_and_log_once(monkeypatch, caplog, store):
    enable(monkeypatch)
    errors = [ValueError("private facts"), ValueError("again"), RuntimeError("private")]
    exits = []

    @contextmanager
    def transaction():
        try:
            yield object()
        finally:
            exits.append(True)

    def fail(conn, event):
        raise errors.pop(0)

    monkeypatch.setattr(meter, "_platform", lambda: (
        SimpleNamespace(transaction=transaction),
        SimpleNamespace(cost_event_key=store.cost_event_key, record_cost_event=fail),
    ))
    result = {"ok": True, "value": [1, 2]}

    def caller():
        meter.record_e2b_cost(ORG, receipt())
        return result

    for _ in range(3):
        assert caller() is result
    assert meter.record_failed == 3
    assert len(exits) == 3
    messages = [r.getMessage() for r in caplog.records if r.name == meter.__name__]
    assert messages == ["E2B cost recording failed: ValueError",
                        "E2B cost recording failed: RuntimeError"]


def test_record_in_one_transaction_reads_env_at_call_time(monkeypatch, store):
    events = []
    steps = []
    connection = object()

    @contextmanager
    def transaction():
        steps.append("enter")
        yield connection
        steps.append("exit")

    def record(conn, event):
        assert conn is connection
        assert steps == ["enter"]
        events.append(store.validate_cost_event(event))

    monkeypatch.setattr(meter, "_platform", lambda: (
        SimpleNamespace(transaction=transaction),
        SimpleNamespace(cost_event_key=store.cost_event_key, record_cost_event=record),
    ))
    meter.record_e2b_cost(ORG, receipt())
    assert not events
    enable(monkeypatch)
    meter.record_e2b_cost(ORG, receipt())
    assert len(events) == 1
    assert steps == ["enter", "exit"]
    assert meter.record_failed == 0


def test_whole_milliseconds_and_lifetime_bounds(store):
    event = meter.cost_event_from_receipt(
        ORG, receipt(stoppedAt="2026-10-22T10:00:02.500999+00:00"), RATE)
    assert str(event["quantity"]) == "2.500"
    for stopped, quantity in [("2026-10-22T10:00:00Z", "0.000"),
                              ("2026-10-22T11:00:00Z", "3600.000")]:
        event = meter.cost_event_from_receipt(ORG, receipt(stoppedAt=stopped), RATE)
        assert str(event["quantity"]) == quantity


def test_call_placement_after_verified_hash_before_tool_error():
    source = (SERVER / "tool_loader.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "record_e2b_cost"]
    assert len(calls) == source.count("record_e2b_cost(") == 1
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                    and node.name == "_run_source_in_sandbox_e2b")
    assert calls[0] in list(ast.walk(function))
    assert [arg.id for arg in calls[0].args] == ["tenant_id", "receipt"]
    body = function.body
    position = next(i for i, node in enumerate(body)
                    if isinstance(node, ast.Expr) and node.value is calls[0])
    assert isinstance(body[position - 1], ast.ImportFrom)
    hash_check = body[position - 2]
    error_check = body[position + 1]
    assert isinstance(hash_check, ast.If)
    assert 'receipt.get("resultHash") != computed_result_hash' in ast.get_source_segment(
        source, hash_check.test)
    assert any(isinstance(node, ast.Return) for node in hash_check.body)
    assert isinstance(error_check, ast.If)
    assert ast.get_source_segment(source, error_check.test) == 'result.get("error")'
