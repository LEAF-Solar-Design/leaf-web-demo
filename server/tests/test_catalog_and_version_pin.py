"""
Binary acceptance for two operator-decided goals (2026-07-22):

  GOAL 1  ``autofill-string-targets`` resolves via ``deps.find_tool`` from the
          GENERAL tool catalog (server/catalog_tools.json, folded by
          deps.all_tools) with the string_panels.py DEFAULTS as default_params.

  GOAL 2  version pinning: ``BrokerRunRequest.dwg_version`` lets a run execute
          against a SPECIFIC immutable drawing version instead of silently
          "head" — for both the read mock path (broker.py's pure-python
          branch) and the write mock path (write_loop.run_write_mock), with a
          clean fail-closed BAD_PARAMS on an unknown version and byte-identical
          behaviour when dwg_version is omitted.

All offline / in-process (direct function calls against the real broker
module + a real FilesystemBackend under tmp_path) — no subprocess boot,
mirroring tests/test_broker_boundary.py's style.

Run:  cd server && python -m pytest tests/test_catalog_and_version_pin.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import broker_client  # noqa: E402
import deps  # noqa: E402
import write_loop  # noqa: E402


# `builtins` is a stdlib name, so import server/builtins/string_panels.py by file
# path instead of `import builtins.string_panels` (which would shadow-fight the
# stdlib) — the same guard tests/test_site.py uses.
def _load_string_panels():
    import importlib.util

    path = SERVER_DIR / "builtins" / "string_panels.py"
    spec = importlib.util.spec_from_file_location("string_panels_catalog_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


string_panels = _load_string_panels()

READ_TOOL = {
    "name": "count-by-layer",
    "capabilities": ["drawing.read"],
    "engine_op": "count_by_layer",
    "params": {"type": "object", "properties": {}, "required": []},
}
WRITE_TOOL = {
    "name": "delete-marked-panel",
    "capabilities": ["drawing.write"],
    "entry": "builtins/delete_marked_panel.py",
    "params": {
        "type": "object",
        "properties": {
            "drawing_id": {"type": "string"},
            "handle": {"type": "string"},
            "layer": {"type": "string"},
        },
        "required": [],
    },
}


def _quiet_broker(monkeypatch, tmp_path):
    """Common broker preflight stubs so these tests exercise ONLY the
    version-pin logic, mirroring test_broker_boundary.py's pattern."""
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)


# --------------------------------------------------------------------------- #
# GOAL 1 — catalog registration
# --------------------------------------------------------------------------- #
def test_find_tool_resolves_autofill_string_targets_from_catalog():
    tool = deps.find_tool("autofill-string-targets")
    assert tool is not None, "autofill-string-targets must resolve via deps.find_tool"
    assert tool["entry"] == "builtins/string_panels.py"
    assert tool["capabilities"] == ["drawing.read"]

    default_params = tool.get("default_params") or {}
    expected_keys = (
        "voc", "temp_coeff_pct_per_c", "design_min_temp_c",
        "max_system_voltage", "panel_layer", "cluster_radius_factor",
    )
    for key in expected_keys:
        assert key in default_params, f"default_params missing {key!r}"
        assert default_params[key] == string_panels.DEFAULTS[key], (
            f"default_params[{key!r}]={default_params[key]!r} != "
            f"string_panels.DEFAULTS[{key!r}]={string_panels.DEFAULTS[key]!r}")

    # visible through the same all_tools() union find_tool uses
    names = {t["name"] for t in deps.all_tools()}
    assert "autofill-string-targets" in names


def test_autofill_string_targets_actually_runs_through_run_tool_dynamic():
    """The catalog entry must resolve to a REAL, runnable local file (not a
    dangling reference) — the same dynamic-loader path every other tool takes."""
    import tool_loader

    tool = deps.find_tool("autofill-string-targets")
    intake = deps.load_cached_intake()
    env = tool_loader.run_tool_dynamic(tool, intake, dict(tool["default_params"]),
                                       aps_live=False, da=None)
    assert env["ok"] is True, env.get("error")
    assert "electrical" in env["result"] and "strings" in env["result"]


# --------------------------------------------------------------------------- #
# GOAL 2 — version pinning
# --------------------------------------------------------------------------- #
def test_omitted_dwg_version_preserves_legacy_data_file_read(monkeypatch, tmp_path):
    """Regression guard: with dwg_version absent (None), the mock read path is
    BYTE-IDENTICAL to before this feature — it still reads DATA_FILE directly,
    never touching the versioned store."""
    _quiet_broker(monkeypatch, tmp_path)
    fake_intake = {"polylines": [{"layer": "OnlyLayer", "closed": True,
                                  "pts": [[0, 0], [1, 0], [1, 1]]}]}
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text(json.dumps(fake_intake), encoding="utf-8")

    def _fail_store_touch(*_a, **_k):
        pytest.fail("dwg_version omitted must never touch the versioned store")

    monkeypatch.setattr(write_loop, "ensure_demo_drawing", _fail_store_touch)

    req = broker.BrokerRunRequest(tenant_id="vpin-legacy", tool=READ_TOOL, params={},
                                  dwg="rooftop_demo", aps_live=False)  # dwg_version omitted
    resp = broker.broker_run(req)
    assert resp.status_code == 200, resp.body
    body = json.loads(resp.body)
    assert body["result"]["counts"] == {"OnlyLayer": 1}


def test_dwg_version_unknown_fails_closed(monkeypatch, tmp_path):
    _quiet_broker(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))

    req = broker.BrokerRunRequest(tenant_id="vpin-unknown", tool=READ_TOOL, params={},
                                  dwg="rooftop_demo", aps_live=False, dwg_version=999)
    resp = broker.broker_run(req)
    assert resp.status_code == 400, resp.body
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert "version" in body["error"]["message"].lower()
    assert body["error"]["retryable"] is False


def test_run_pinned_to_older_version_loads_that_version_not_head(monkeypatch, tmp_path):
    """A read run pinned to v1 keeps seeing v1's content even after a write has
    advanced head to v2; a run pinned to v2 sees the write's effect."""
    _quiet_broker(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    tenant = "vpin-older"

    # bootstrap v1 + baseline read pinned to v1 (also proves the golden fixture
    # count matches tests/test_write_loop.py's known cached-intake shape)
    req_v1 = broker.BrokerRunRequest(tenant_id=tenant, tool=READ_TOOL, params={},
                                     dwg="rooftop_demo", aps_live=False, dwg_version=1)
    resp1 = broker.broker_run(req_v1)
    assert resp1.status_code == 200, resp1.body
    v1_counts = json.loads(resp1.body)["result"]["counts"]
    assert "LEAF_WRITE_PROBE" not in v1_counts

    # a write (unpinned -> branches off head=v1) creates v2
    write_req = broker.BrokerRunRequest(tenant_id=tenant, tool=WRITE_TOOL,
                                        params={"drawing_id": "demo"},
                                        dwg="rooftop_demo", aps_live=False)
    wresp = broker.broker_run(write_req)
    assert wresp.status_code == 200, wresp.body
    wbody = json.loads(wresp.body)
    assert wbody["result"]["new_version"] == {"drawing_id": "demo", "version": 2, "parent": 1}

    # read PINNED to v1 again -> unaffected by the write that advanced head
    resp1b = broker.broker_run(req_v1)
    assert json.loads(resp1b.body)["result"]["counts"] == v1_counts

    # read PINNED to v2 -> reflects the write (marker layer now present)
    req_v2 = broker.BrokerRunRequest(tenant_id=tenant, tool=READ_TOOL, params={},
                                     dwg="rooftop_demo", aps_live=False, dwg_version=2)
    resp2 = broker.broker_run(req_v2)
    assert resp2.status_code == 200, resp2.body
    v2_counts = json.loads(resp2.body)["result"]["counts"]
    assert "LEAF_WRITE_PROBE" in v2_counts


def test_write_branches_from_pinned_base_version(monkeypatch, tmp_path):
    """A WRITE pinned to an older base version parents its new version off THAT
    base (write_loop.py's hardcoded "head" is now the default, not the only
    option)."""
    _quiet_broker(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    tenant = "vpin-write-base"

    # bootstrap v1, then advance head to v2 via an unpinned write
    first = broker.broker_run(broker.BrokerRunRequest(
        tenant_id=tenant, tool=WRITE_TOOL, params={"drawing_id": "demo"},
        dwg="rooftop_demo", aps_live=False))
    assert json.loads(first.body)["result"]["new_version"]["version"] == 2

    # a SECOND write, pinned back to base version 1, must parent off v1 (not
    # silently off head=2) and land at v3 (monotonic `latest`, per da/store.py)
    second = broker.broker_run(broker.BrokerRunRequest(
        tenant_id=tenant, tool=WRITE_TOOL, params={"drawing_id": "demo"},
        dwg="rooftop_demo", aps_live=False, dwg_version=1))
    assert second.status_code == 200, second.body
    nv = json.loads(second.body)["result"]["new_version"]
    assert nv == {"drawing_id": "demo", "version": 3, "parent": 1}, nv


# --------------------------------------------------------------------------- #
# thread-through: RunRequest.dwg_version -> jobs.submit_job -> broker_client
# --------------------------------------------------------------------------- #
def test_run_request_threads_dwg_version_through_router_to_submit_job(monkeypatch):
    from routers import jobs as jobs_router

    captured = {}

    def fake_submit_job(tenant_id, tool, params, dwg, aps_live, org_id=None,
                        project_id=None, dwg_version=None):
        captured["dwg_version"] = dwg_version
        return "fake-job-id"

    monkeypatch.setattr(jobs_router.jobs, "submit_job", fake_submit_job)
    req = jobs_router.RunRequest(tool="count-by-layer", params={}, dwg="rooftop_demo",
                                 dwg_version=7)
    resp = jobs_router.run(req, wait=0, tenant_id="demo-tenant")
    assert resp.status_code == 202
    assert captured["dwg_version"] == 7

    # omitted dwg_version -> None threads through unchanged
    captured.clear()
    req2 = jobs_router.RunRequest(tool="count-by-layer", params={}, dwg="rooftop_demo")
    jobs_router.run(req2, wait=0, tenant_id="demo-tenant")
    assert captured["dwg_version"] is None


def test_submit_job_threads_dwg_version_to_broker_client(monkeypatch, tmp_path):
    import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "DB_PATH", tmp_path / "jobs.db")
    # force a fresh connection against the isolated DB_PATH; monkeypatch restores
    # the PRIOR `_conn` value on teardown so other test modules sharing the `jobs`
    # module singleton are unaffected after this test.
    monkeypatch.setattr(jobs_mod, "_conn", None)

    captured = {}

    def fake_run_via_broker(tenant_id, tool, params, dwg, aps_live, timeout_s=None,
                            dwg_version=None):
        captured["dwg_version"] = dwg_version
        return {"ok": True, "tool": tool["name"], "version": "1.0.0", "result": {},
                "overlay": None, "timing_ms": 1, "cost": None, "error": None,
                "degraded_mode": False}

    monkeypatch.setattr(jobs_mod.broker_client, "run_via_broker", fake_run_via_broker)

    job_id = jobs_mod.submit_job("t-thread", {"name": "count-by-layer"}, {},
                                 "rooftop_demo", aps_live=False, dwg_version=42)
    rec = jobs_mod.wait_for_terminal(job_id, timeout_s=10)
    assert rec is not None and rec["status"] == "complete", rec
    assert captured["dwg_version"] == 42
