"""Wave 0 fail-closed gate for tenant-authored broker execution."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

import broker
import tool_loader


def _safe_production(monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "test-broker-secret")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_QA_HOOKS", "0")


def _package(path, name: str) -> dict:
    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    return next(tool for tool in tools if tool["name"] == name)


def _request(tool: dict) -> broker.BrokerRunRequest:
    return broker.BrokerRunRequest(
        tenant_id="wave0-tenant",
        tool=tool,
        params={},
        dwg="rooftop_demo",
        aps_live=False,
    )


def _execute(monkeypatch, tool: dict):
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    return broker._execute(
        _request(tool),
        tool,
        str(tool.get("engine_op") or ""),
        time.perf_counter(),
        {},
    )


@pytest.mark.parametrize("capability", ["drawing.read", "drawing.write"])
def test_production_denies_authored_file_before_dynamic_runner(monkeypatch, capability):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.delenv("LEAF_AUTHORED_EXECUTION", raising=False)
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    monkeypatch.setattr(broker, "is_trusted_builtin_tool", lambda _tool, _tenant: False)

    called = False

    def forbidden_runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("tenant file runner must not be called")

    monkeypatch.setattr(broker, "run_tool_dynamic", forbidden_runner)
    env, status = _execute(monkeypatch, {
        "name": "tenant-probe",
        "entry": "tools/tenant-probe/tool.py",
        "params": {"type": "object", "properties": {}},
        "capabilities": [capability],
    })

    assert status == 403
    assert env["ok"] is False
    assert env["error"]["error_code"] == "TENANT_DISABLED"
    assert "authored execution is disabled" in env["error"]["message"]
    assert called is False


def test_production_keeps_tracked_builtin_available(monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "0")
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)

    tool = _package(
        tool_loader.SERVER_DIR.parent / "engine" / "registry.json",
        "measure-panel-area",
    )
    assert tool_loader.is_trusted_builtin_tool(tool, "wave0-tenant") is True
    env, status = _execute(monkeypatch, tool)

    assert status == 200
    assert env["ok"] is True
    assert env["result"]["total_area"] == pytest.approx(7015420.07)


def test_production_denies_agent_seed_that_only_resolves_to_builtins(
        monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "0")
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)

    tool = _package(tool_loader.SERVER_DIR / "write_tools.json", "delete-marked-panel")
    assert tool_loader.resolve_local_file(tool, "wave0-tenant") == (
        tool_loader.BUILTIN_DIR / "delete_marked_panel.py"
    ).resolve()

    called = False

    def forbidden_write(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("untrusted write package reached execution")

    monkeypatch.setattr(broker.write_loop, "run_write_mock", forbidden_write)
    env, status = _execute(monkeypatch, tool)

    assert tool_loader.is_trusted_builtin_tool(tool, "wave0-tenant") is False
    assert status == 403
    assert env["error"]["error_code"] == "TENANT_DISABLED"
    assert called is False


def test_local_demo_behavior_remains_enabled(monkeypatch):
    monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("LEAF_AUTHORED_EXECUTION", raising=False)
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)

    assert broker._production_runtime() is False
    assert broker._authored_execution_enabled() is True


def test_production_startup_rejects_enabled_authored_execution_without_sandbox(
        monkeypatch):
    _safe_production(monkeypatch)
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "1")
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)

    with pytest.raises(RuntimeError, match="requires LEAF_SANDBOX"):
        broker.validate_runtime_safety()


def test_fastapi_startup_runs_the_production_safety_check(monkeypatch):
    _safe_production(monkeypatch)
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "1")
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)

    with pytest.raises(RuntimeError, match="requires LEAF_SANDBOX"):
        with TestClient(broker.app):
            pass


@pytest.mark.parametrize("tier", ["e2b", "E2B-MICROVM"])
def test_production_startup_accepts_enabled_execution_with_approved_sandbox(
        monkeypatch, tier):
    _safe_production(monkeypatch)
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "1")
    monkeypatch.setenv("LEAF_SANDBOX", tier)

    assert broker.validate_runtime_safety() is None


def test_trusted_builtin_classification_rejects_tenant_shadow(monkeypatch, tmp_path):
    repo = tmp_path / "tenant"
    shadow = repo / "builtins" / "count_by_layer.py"
    shadow.parent.mkdir(parents=True)
    shadow.write_text(
        "def run(intake, params):\n    return {'shadowed': True}, None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda _tenant=None: repo)

    tool = {
        "name": "shadow",
        "entry": "builtins/count_by_layer.py",
        "params": {"type": "object", "properties": {}},
    }
    assert tool_loader.resolve_local_file(tool, "wave0-tenant") == shadow.resolve()
    assert tool_loader.is_trusted_builtin_tool(tool, "wave0-tenant") is False
