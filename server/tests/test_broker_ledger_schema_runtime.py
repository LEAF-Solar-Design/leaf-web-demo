"""Runtime ledger-line schema gate (CONTRACT-ADDENDUM §8, census #4 fix lane).

The static suite (test_broker_ledger_schema_static.py) pins the schema file,
the entry literal, and the reader; it cannot prove what broker_run actually
WRITES. This suite drives the real FastAPI app via TestClient with adversarial
tool packages (`engine_op: null`, non-string `name`, empty package) and
json-validates every line that lands in the ledger file against the FROZEN
`leaf.broker-ledger-line.v1` schema — including denial and garbage-input lines.

Repo invariant honoured: broker endpoint tests ALWAYS monkeypatch _get_da (the
live loader resolves real APS creds even at aps_live=false request bodies).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402

SCHEMA = json.loads((SERVER_DIR / "broker_ledger.schema.json").read_text(encoding="utf-8"))


def _ok_env(*_a, **_k):
    return {"ok": True, "tool": "t", "version": "1.0.0", "result": {}, "overlay": None,
            "timing_ms": 1, "cost": None, "error": None, "degraded_mode": False}


@pytest.fixture()
def ledgered_client(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    monkeypatch.setattr(
        broker, "_get_da",
        lambda: pytest.fail("request reached the APS client"))
    return TestClient(broker.app), tmp_path / "ledger.jsonl"


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_no_name_denials_still_append_schema_valid_lines(ledgered_client):
    """`{"engine_op": null}` / `{}` -> BAD_PARAMS, but the appended line must
    conform: tool null, engine_op '' (string), all nine frozen keys present."""
    client, ledger = ledgered_client
    for tool_pkg in ({"engine_op": None}, {}):
        r = client.post("/broker/run", json={
            "tenant_id": "t1", "tool": tool_pkg, "params": {},
            "dwg": "rooftop_demo", "aps_live": False})
        assert r.status_code == 400
        assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    lines = _lines(ledger)
    assert len(lines) == 2  # exactly ONE line per run
    for line in lines:
        jsonschema.validate(line, SCHEMA)
        assert line["tool"] is None
        assert line["engine_op"] == ""


def test_non_string_name_and_engine_op_append_schema_valid_lines(ledgered_client):
    """Arbitrary JSON in the tool package (the wire model allows it) must never
    produce a schema-invalid ledger line, whatever envelope the run returns."""
    client, ledger = ledgered_client
    bodies = [
        {"name": 123, "engine_op": {"x": 1}},
        {"name": ["a"], "engine_op": 7},
    ]
    for tool_pkg in bodies:
        client.post("/broker/run", json={
            "tenant_id": "t1", "tool": tool_pkg, "params": {},
            "dwg": "rooftop_demo", "aps_live": False})
    lines = _lines(ledger)
    assert len(lines) == len(bodies)
    for line in lines:
        jsonschema.validate(line, SCHEMA)
        assert line["tool"] is None      # non-string name -> null
        assert line["engine_op"] == ""   # non-string engine_op -> ''


def test_malformed_error_envelope_still_appends_string_status(ledgered_client, monkeypatch):
    """A tool envelope is unchecked input: `{"ok": false, "error":
    {"error_code": null}}` would flow through .get("error_code", "error") as
    None (the default only covers a MISSING key) — the appended line must
    still carry a STRING status."""
    client, ledger = ledgered_client
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *_a, **_k: {
        "ok": False, "tool": "t", "version": "1.0.0", "result": {}, "overlay": None,
        "timing_ms": 1, "cost": None, "error": {"error_code": None}, "degraded_mode": False})
    client.post("/broker/run", json={
        "tenant_id": "t1",
        "tool": {"name": "t", "engine_op": "op", "params_schema": {"type": "object"}},
        "params": {}, "dwg": "rooftop_demo", "aps_live": False})
    line = _lines(ledger)[-1]
    jsonschema.validate(line, SCHEMA)
    assert line["status"] == "error"


class _FakeLiveDa:
    """Minimal stand-in satisfying broker's live-path guards: a real-looking
    tool_activity_spec (non-empty script) and a run_tool returning an envelope
    whose cost block carries unusable numbers."""

    def __init__(self, cost):
        self._cost = cost

    def tool_activity_spec(self, _tool):
        return {"settings": {"script": {"value": "(princ)"}}}

    def run_tool(self, _local, tool, _params):
        return {"ok": True, "tool": tool.get("name"), "version": "1.0.0",
                "result": {}, "overlay": None, "timing_ms": 1, "error": None,
                "degraded_mode": False, "cost": self._cost}


def test_live_path_cost_copy_conforms_non_finite_and_oversized_numbers(
        ledgered_client, monkeypatch, tmp_path):
    """The LIVE branch is the only path that copies the envelope's cost block
    into the ledger entry (mock runs are unmetered by design). Drive it with a
    fake da whose cost carries 10**400 and NaN: float()/math.isfinite raise
    OverflowError on the oversized int, and the append runs in broker_run's
    `finally` — a raise there would replace the response AND drop the line.
    Both numbers must conform to null and the line must stay strictly
    parseable."""
    client, ledger = ledgered_client
    store = tmp_path / "live-dwg"
    store.mkdir()
    (store / "rooftop_demo.dwg").write_bytes(b"dwg")
    monkeypatch.setattr(broker, "DATA_DIR", store)
    monkeypatch.setattr(broker, "_get_da", lambda: _FakeLiveDa(
        {"engine_seconds": 10 ** 400, "usd_est": float("nan")}))
    client.post("/broker/run", json={
        "tenant_id": "t1",
        "tool": {"name": "t", "engine_op": "op", "params_schema": {"type": "object"}},
        "params": {}, "dwg": "rooftop_demo", "aps_live": True})
    raw = ledger.read_text(encoding="utf-8").splitlines()[-1]
    assert "NaN" not in raw and "Infinity" not in raw
    line = json.loads(raw)
    jsonschema.validate(line, SCHEMA)
    assert line["engine_seconds"] is None
    assert line["usd_est"] is None
    # Not an early denial: the run must have reached the live execution branch
    # (ok, or INTERNAL from strict response serialization of the bad cost) —
    # otherwise the None fields above would be vacuously None.
    assert line["status"] in ("ok", "INTERNAL")


def test_ledger_append_chokepoint_never_raises_on_unusable_numbers(monkeypatch, tmp_path):
    """_ledger_append runs in broker_run's `finally`: for 10**400 (OverflowError
    from float()/isfinite), inf, and nan it must neither raise nor emit a bare
    NaN/Infinity token — every unusable number conforms to null."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(broker, "LEDGER_PATH", ledger)
    for bad in (10 ** 400, -(10 ** 400), float("inf"), float("nan")):
        broker._ledger_append({
            "ts": 1753222000.0, "tenant_id": "t1", "tool": "t", "engine_op": "op",
            "aps_endpoint": "x", "aps_live": True,
            "engine_seconds": bad, "usd_est": bad, "status": "ok"})
    lines = [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 4
    for line in lines:
        jsonschema.validate(line, SCHEMA)
        assert line["engine_seconds"] is None
        assert line["usd_est"] is None


def test_ok_line_validates_and_keeps_real_values(ledgered_client):
    client, ledger = ledgered_client
    r = client.post("/broker/run", json={
        "tenant_id": "t1",
        "tool": {"name": "t", "engine_op": "op", "params_schema": {"type": "object"}},
        "params": {}, "dwg": "rooftop_demo", "aps_live": False})
    assert r.status_code == 200
    line = _lines(ledger)[-1]
    jsonschema.validate(line, SCHEMA)
    assert line["tool"] == "t"
    assert line["engine_op"] == "op"
    assert line["status"] == "ok"
