"""Broker-level regression coverage for live LISP script resolution.

The engine registry declares repository-root-relative ``engine/tools/*.lsp``
paths.  The real APS client must resolve those paths before the broker permits a
live read, while a missing script must remain a structured, fail-closed error.

Run: ``cd server && python -m pytest tests/test_live_lsp_resolution.py -q``
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
REGISTRY = REPO_ROOT / "engine" / "registry.json"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402


def _load_real_da_client():
    """Load da/client.py without credentials or a network call at import time."""
    spec = importlib.util.spec_from_file_location(
        "da_client_live_lsp_resolution_test", REPO_ROOT / "da" / "client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry_tool(name: str) -> dict:
    tools = json.loads(REGISTRY.read_text(encoding="utf-8"))["tools"]
    return next(tool for tool in tools if tool["name"] == name)


def _quiet_broker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate this suite from ledger, tenant, and quota policy state."""
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    monkeypatch.setattr(broker, "_run_quota_preflight",
                        lambda _tenant, _tier, _tool: None)


def test_live_read_resolves_registry_root_relative_lsp(monkeypatch, tmp_path):
    """A real registry LISP path passes the broker's live-read guard.

    Before the root-relative registry convention, this same read rejected the
    registry's ``tools/<name>.lsp`` value because the file lives under
    ``engine/tools``.  This test uses the real client resolver and the actual
    engine registry so that mismatch cannot return.
    """
    _quiet_broker(monkeypatch, tmp_path)
    monkeypatch.setenv("APS_LIVE", "0")
    da_mod = _load_real_da_client()
    tool = _registry_tool("count-by-layer")
    assert tool["script"] == "engine/tools/count_by_layer.lsp"

    calls = []
    monkeypatch.setattr(da_mod, "ensure_tool_activity", lambda _tool: {})
    monkeypatch.setattr(
        da_mod,
        "run_tool",
        lambda local, received_tool, params: calls.append(
            (local, received_tool, dict(params))) or {"ok": True, "result": {}},
    )
    monkeypatch.setattr(broker, "_get_da", lambda: da_mod)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda _dwg: tmp_path / "input.dwg")

    response = broker.broker_run(broker.BrokerRunRequest(
        tenant_id="lsp-resolution", tool=tool, params={}, dwg="rooftop_demo",
        aps_live=True))

    assert response.status_code == 200, response.body
    assert calls and calls[0][1]["script"] == tool["script"]
    assert da_mod.tool_activity_spec(tool)["settings"]["script"]["value"].strip()


def test_live_read_missing_lsp_fails_closed_with_structured_envelope(monkeypatch, tmp_path):
    """A dangling LISP path stays blocked before activity provisioning or run."""
    _quiet_broker(monkeypatch, tmp_path)
    monkeypatch.setenv("APS_LIVE", "0")
    da_mod = _load_real_da_client()
    tool = dict(_registry_tool("count-by-layer"))
    tool["script"] = "engine/tools/does_not_exist.lsp"

    def _unexpected(*_args, **_kwargs):
        pytest.fail("missing LISP must fail before any APS-capable call")

    monkeypatch.setattr(da_mod, "ensure_tool_activity", _unexpected)
    monkeypatch.setattr(da_mod, "run_tool", _unexpected)
    monkeypatch.setattr(broker, "_get_da", lambda: da_mod)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda _dwg: tmp_path / "input.dwg")

    response = broker.broker_run(broker.BrokerRunRequest(
        tenant_id="lsp-missing", tool=tool, params={}, dwg="rooftop_demo",
        aps_live=True))

    assert response.status_code == 400, response.body
    body = json.loads(response.body)
    assert body["ok"] is False
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert body["error"]["retryable"] is False
    assert "live" in body["error"]["message"].lower()
