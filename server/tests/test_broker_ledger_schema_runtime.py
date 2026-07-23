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
