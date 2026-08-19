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
                        project_id=None, dwg_version=None, **_platform_kwargs):
        captured["dwg_version"] = dwg_version
        return "fake-job-id"

    monkeypatch.setattr(jobs_router.jobs, "submit_job", fake_submit_job)
    catalog_tool = jobs_router.deps.find_tool("count-by-layer")
    req = jobs_router.RunRequest(
        tool="count-by-layer", params={}, dwg="rooftop_demo", dwg_version=7,
        catalog_digest=jobs_router.deps.catalog_tool_digest(catalog_tool))
    resp = jobs_router.run(req, wait=0, tenant_id="demo-tenant",
                           x_org_id=None, x_project_id=None,
                           idempotency_key=None, authorization=None)
    assert resp.status_code == 202
    assert captured["dwg_version"] == 7

    # omitted dwg_version -> None threads through unchanged
    captured.clear()
    req2 = jobs_router.RunRequest(
        tool="count-by-layer", params={}, dwg="rooftop_demo",
        catalog_digest=jobs_router.deps.catalog_tool_digest(catalog_tool))
    jobs_router.run(req2, wait=0, tenant_id="demo-tenant",
                    x_org_id=None, x_project_id=None,
                    idempotency_key=None, authorization=None)
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
                            dwg_version=None, ledger_event_key=None, job_id=None):
        captured["dwg_version"] = dwg_version
        captured["ledger_event_key"] = ledger_event_key
        captured["job_id"] = job_id
        return {"ok": True, "tool": tool["name"], "version": "1.0.0", "result": {},
                "overlay": None, "timing_ms": 1, "cost": None, "error": None,
                "degraded_mode": False}

    monkeypatch.setattr(jobs_mod.broker_client, "run_via_broker", fake_run_via_broker)

    job_id = jobs_mod.submit_job("t-thread", {"name": "count-by-layer"}, {},
                                 "rooftop_demo", aps_live=False, dwg_version=42)
    rec = jobs_mod.wait_for_terminal(job_id, timeout_s=10)
    assert rec is not None and rec["status"] == "complete", rec
    assert captured["dwg_version"] == 42
    assert captured["ledger_event_key"] == f"{job_id}:broker-run"


# =========================================================================== #
# Review round 1 (2026-07-22) fixes
# =========================================================================== #
class _FakeLiveDa:
    """Minimal live-capable APS client stand-in: has `run_tool` (so broker.py's
    `hasattr(da, "run_tool")` gate lets it through) and `ensure_tool_activity`, but
    deliberately has NO `tool_activity_spec` — this is the "can't verify the real
    resolution" case, which must ALSO fail closed (never guess). The REAL
    resolution case is exercised separately below via the actual da/client.py
    module (``_load_real_da_client``), per review round 2."""

    def __init__(self) -> None:
        self.ensure_tool_activity_calls: list = []
        self.run_tool_calls: list = []

    def ensure_tool_activity(self, tool):
        self.ensure_tool_activity_calls.append(tool.get("name"))
        return {}

    def run_tool(self, local, tool, params):
        self.run_tool_calls.append((local, tool.get("name"), dict(params)))
        return {"ok": True, "result": {"ran": True}, "overlay": None}


def _load_real_da_client():
    """Load the REAL da/client.py module by file path (dependency-free: no APS
    creds, no network call at import time — matches how the round-2 reviewer
    reproduced the finding). Used to exercise the ACTUAL
    `tool_activity_spec` resolution `_live_script_is_nonempty` now calls,
    instead of a re-implemented heuristic."""
    import importlib.util

    da_dir = SERVER_DIR.parent / "da"
    spec = importlib.util.spec_from_file_location("da_client_version_pin_test", da_dir / "client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# FIX 1 (HIGH, round 2 — sharper): the guard must verify the REAL
# da/client.py:tool_activity_spec resolution, not a heuristic that merely checks
# for the PRESENCE of an `engine_script`/`.lsp` `script` reference. Originally
# reproduced the reviewer's dependency-free finding (the shipped count-by-layer
# entry declared `tools/<name>.lsp` and emitted a ZERO-length script); PR #15
# fixed the registry path convention (root-relative `engine/tools/<name>.lsp`),
# so the REAL emitted script is now the actual LISP source.
# --------------------------------------------------------------------------- #
def test_real_tool_activity_spec_resolves_the_shipped_tool_nonempty():
    """Sanity check (no broker involved): the shipped count-by-layer entry
    declares a root-relative `.lsp` path the REAL resolution reads. Per-tool
    coverage for the whole registry lives in test_engine_registry_scripts.py."""
    da_mod = _load_real_da_client()
    tool = deps.find_tool("count-by-layer")
    assert tool is not None and tool.get("script") == "engine/tools/count_by_layer.lsp"
    spec = da_mod.tool_activity_spec(tool)
    assert spec["settings"]["script"]["value"].strip()


def test_live_read_fails_closed_when_real_resolution_emits_empty_script(monkeypatch, tmp_path):
    """aps_live=True + a tool whose declared `.lsp` path does not resolve must
    fail closed via the REAL da.client.tool_activity_spec call — never
    silently submit an empty-script WorkItem. (Since PR #15 the shipped
    registry tools resolve, so the fixture is a dangling-path tool.)"""
    _quiet_broker(monkeypatch, tmp_path)
    da_mod = _load_real_da_client()

    def _poison(name):
        def _fn(*_a, **_k):
            pytest.fail(f"da.{name} must never be reached when the REAL emitted "
                        f"live script is empty")
        return _fn

    monkeypatch.setattr(da_mod, "run_tool", _poison("run_tool"))
    monkeypatch.setattr(da_mod, "ensure_tool_activity", _poison("ensure_tool_activity"),
                        raising=False)
    monkeypatch.setattr(broker, "_get_da", lambda: da_mod)

    tool = dict(READ_TOOL)
    tool["script"] = "tools/does_not_exist.lsp"  # dangling: resolves nowhere
    assert broker._live_script_is_nonempty(tool, da_mod) is False

    req = broker.BrokerRunRequest(tenant_id="live-realmismatch", tool=tool, params={},
                                  dwg="rooftop_demo", aps_live=True)
    resp = broker.broker_run(req)

    assert resp.status_code == 400, resp.body
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert body["error"]["retryable"] is False
    assert "live" in body["error"]["message"].lower()


def test_live_read_fails_closed_when_da_cannot_verify_real_resolution(monkeypatch, tmp_path):
    """A `da` client that has no `tool_activity_spec` at all (can't verify the
    real resolution) must ALSO fail closed — never guess from a `script`/
    `engine_script` field's mere presence."""
    _quiet_broker(monkeypatch, tmp_path)
    fake_da = _FakeLiveDa()  # deliberately lacks tool_activity_spec
    monkeypatch.setattr(broker, "_get_da", lambda: fake_da)

    tool = dict(READ_TOOL)
    tool["script"] = "tools/count_by_layer.lsp"  # presence alone must NOT be enough
    assert broker._live_script_is_nonempty(tool, fake_da) is False

    req = broker.BrokerRunRequest(tenant_id="live-noverify", tool=tool, params={},
                                  dwg="rooftop_demo", aps_live=True)
    resp = broker.broker_run(req)

    assert resp.status_code == 400, resp.body
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert fake_da.ensure_tool_activity_calls == []
    assert fake_da.run_tool_calls == []


def test_live_read_proceeds_when_real_resolution_emits_a_nonempty_script(monkeypatch, tmp_path):
    """Regression guard: the guard must NOT block a tool whose REAL
    `tool_activity_spec` resolution genuinely produces a non-empty script.
    Uses `engine_script` (literal LISP source) so the case is independent of
    any file-path convention/bug elsewhere in the repo."""
    _quiet_broker(monkeypatch, tmp_path)
    da_mod = _load_real_da_client()
    calls = []

    def _fake_run_tool(local, tool, params):
        calls.append((local, tool.get("name")))
        return {"ok": True, "result": {}, "overlay": None}

    monkeypatch.setattr(da_mod, "run_tool", _fake_run_tool)
    monkeypatch.setattr(da_mod, "ensure_tool_activity", lambda *_a, **_k: {}, raising=False)
    monkeypatch.setattr(broker, "_get_da", lambda: da_mod)

    tool = dict(READ_TOOL)
    tool["engine_script"] = '(princ "leaf-test-ok")'
    assert broker._live_script_is_nonempty(tool, da_mod) is True

    req = broker.BrokerRunRequest(tenant_id="live-realok", tool=tool, params={},
                                  dwg="rooftop_demo", aps_live=True)
    resp = broker.broker_run(req)

    assert resp.status_code == 200, resp.body
    assert calls, "the real live path must be reached when the script genuinely resolves"


# --------------------------------------------------------------------------- #
# FIX 2 (HIGH): dwg_version must NEVER be silently ignored on a live read —
# reject before submission rather than execute the unpinned live DWG.
# --------------------------------------------------------------------------- #
def test_live_read_with_dwg_version_fails_closed_never_executes(monkeypatch, tmp_path):
    _quiet_broker(monkeypatch, tmp_path)
    fake_da = _FakeLiveDa()
    monkeypatch.setattr(broker, "_get_da", lambda: fake_da)

    tool = dict(READ_TOOL)
    req = broker.BrokerRunRequest(tenant_id="live-vpin", tool=tool, params={},
                                  dwg="rooftop_demo", aps_live=True, dwg_version=999)
    resp = broker.broker_run(req)

    assert resp.status_code == 400, resp.body
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert body["error"]["retryable"] is False
    assert "dwg_version" in body["error"]["message"]
    # the exact regression the review caught: a pin must never be silently
    # dropped and the unpinned live DWG executed instead. The version check fires
    # BEFORE the live-script check, so this holds regardless of script validity.
    assert fake_da.run_tool_calls == [], "dwg_version must never be silently ignored on a live read"


# --------------------------------------------------------------------------- #
# FIX 3 (MEDIUM): a live WRITE against an unknown BASE version must surface
# BAD_PARAMS/non-retryable (400-class), not WORKITEM_FAILED/retryable (502).
# --------------------------------------------------------------------------- #
def test_run_write_live_unknown_base_version_fails_closed_bad_params(tmp_path):
    import store

    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    tenant_id = "vpin-live-write"
    write_loop.ensure_demo_drawing(backend, tenant_id, write_loop.DEMO_DRAWING_ID)  # v1 only

    class _NeverCalledDa:
        """If code reached ANY da.* call for an unknown BASE version, that would
        itself be a bug (resolve_version must raise before any APS call)."""

        def __getattr__(self, name):
            raise AssertionError(f"da.{name} must never be reached for an unknown base version")

    env, status = write_loop.run_write_live(
        WRITE_TOOL, {"drawing_id": "demo"}, tenant_id,
        backend=backend, da=_NeverCalledDa(), t0=0.0, version=999)

    assert status == 400, env
    assert env["error"]["error_code"] == "BAD_PARAMS"
    assert env["error"]["retryable"] is False
    assert "version" in env["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# FIX 4 (LOW): duplicate tool names across the GLOBAL, no-override-relationship
# catalog tiers must raise loudly, never silently swap capabilities.
# --------------------------------------------------------------------------- #
def test_all_tools_raises_on_global_seed_name_collision(monkeypatch):
    monkeypatch.setattr(deps, "load_seed_catalog_tools",
                        lambda: [{"name": "count-by-layer", "params": {}}])  # collides w/ engine registry
    with pytest.raises(deps.ToolCatalogCollisionError):
        deps.all_tools()


def test_all_tools_no_collision_among_the_real_current_global_tiers():
    """Regression guard: today's REAL global tiers (engine registry,
    catalog_tools.json, write_tools.json) have zero name collisions with each
    other, so the new guard changes nothing for normal operation."""
    names = [t["name"] for t in deps.all_tools()]
    assert len(names) == len(set(names)), names


def test_tenant_repo_override_of_global_tool_is_not_flagged_as_a_collision(monkeypatch, tmp_path):
    """The documented precedence (tenant-repo intentionally overriding a
    same-named GLOBAL tool) must NOT raise — only the three GLOBAL tiers with
    no override relationship are guarded."""
    repo = tmp_path / "repo"
    (repo / "tools" / "count-by-layer").mkdir(parents=True)
    (repo / "tools" / "count-by-layer" / "tool.py").write_text(
        "def run(intake, params):\n    return {}, None\n", encoding="utf-8")
    (repo / "registry.json").write_text(json.dumps({"tools": [
        {"name": "count-by-layer", "entry": "tools/count-by-layer/tool.py",
         "params": {"type": "object", "properties": {}, "required": []}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANT_REPO", str(repo))

    tool = deps.find_tool("count-by-layer")  # must NOT raise
    assert tool is not None and tool.get("entry") == "tools/count-by-layer/tool.py"  # tenant tool WINS


def test_dwg_version_survives_local_fallback(monkeypatch, tmp_path):
    """The discriminating pin-through-fallback case (round-2 review finding):
    cloud fails, the local fallback broker call must still carry the pin."""
    import jobs

    class _InertExecutor:
        def submit(self, fn, *args, **kwargs):
            return None

    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: _InertExecutor(),
                                             jobs.LANE_SLOW: _InertExecutor()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_terminal", lambda *a, **k: None)
    captured = {}

    def broker_stub(_tenant, _tool, _params, _dwg, aps_live, **kwargs):
        if aps_live:
            captured["cloud_event_key"] = kwargs.get("ledger_event_key")
            return {
                "ok": False,
                "error": {
                    "error_code": "WORKITEM_FAILED",
                    "message": "known terminal cloud failure",
                    "retryable": False,
                },
            }
        captured["dwg_version"] = kwargs.get("dwg_version")
        captured["fallback_event_key"] = kwargs.get("ledger_event_key")
        return {"ok": True, "result": {"source": "local"}}

    monkeypatch.setattr(broker_client, "run_via_broker", broker_stub)
    tool = {"name": "count-by-layer", "capabilities": ["drawing.read"],
            "allow_local_fallback": True}
    job_id = jobs.submit_job("pin-tenant", tool, {}, "rooftop_demo", aps_live=True,
                             dwg_version=5)
    jobs._run_job(job_id, "pin-tenant", tool, {}, "rooftop_demo", True, dwg_version=5)
    assert captured["dwg_version"] == 5
    assert captured["cloud_event_key"] == f"{job_id}:broker-run"
    assert captured["fallback_event_key"] == f"{job_id}:broker-fallback"
    assert captured["cloud_event_key"] != captured["fallback_event_key"]
    if jobs._conn is not None:
        jobs._conn.close()


def test_idempotency_fingerprint_includes_dwg_version(monkeypatch, tmp_path):
    """Round-3 finding 2: same idempotency key + DIFFERENT pin must be rejected
    as different run input, not deduped to the prior job."""
    import jobs
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "idem.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)

    class _Inert:
        def submit(self, fn, *a, **k):
            return None

    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: _Inert(), jobs.LANE_SLOW: _Inert()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    tool = {"name": "count-by-layer", "capabilities": ["drawing.read"]}
    kw = dict(org_id="org-1", project_id="proj-1", idempotency_key="key-1")
    j1 = jobs.submit_job("t", tool, {}, "rooftop_demo", aps_live=False, dwg_version=1, **kw)
    # same key, same pin -> dedupes to the same job
    j1b = jobs.submit_job("t", tool, {}, "rooftop_demo", aps_live=False, dwg_version=1, **kw)
    assert j1b == j1
    # same key, DIFFERENT pin -> rejected
    with pytest.raises(ValueError, match="different run input"):
        jobs.submit_job("t", tool, {}, "rooftop_demo", aps_live=False, dwg_version=2, **kw)
    if jobs._conn is not None:
        jobs._conn.close()


def test_restart_recovery_recovers_the_version_pin(monkeypatch, tmp_path):
    """Round-3 finding 1: a restart-recovered pinned job must NOT rerun against
    head; the pin is persisted in execution_json and threaded back to _run_job."""
    import jobs
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "recover.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    captured = {}

    class _Capture:
        def submit(self, fn, *a, **k):
            # _run_job(job_id, tenant, tool, params, dwg, aps_live, dwg_version)
            captured["dwg_version"] = a[6] if len(a) > 6 else None
            return None

    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: _Capture(), jobs.LANE_SLOW: _Capture()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    tool = {"name": "count-by-layer", "capabilities": ["drawing.read"]}
    job_id = jobs.submit_job("t", tool, {}, "rooftop_demo", aps_live=False, dwg_version=7)
    captured.clear()
    # simulate a fresh process picking the durable row back up
    assert jobs._redispatch_record(job_id) is True
    assert captured["dwg_version"] == 7
    if jobs._conn is not None:
        jobs._conn.close()


def test_restart_recovery_fails_closed_on_row_predating_pin_persistence(monkeypatch, tmp_path):
    """Round-4 finding: a job row written before pin-persistence lacks the
    dwg_version key; recovery must fail closed, never default to head."""
    import jobs
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    dispatched = {"called": False}

    class _Capture:
        def submit(self, fn, *a, **k):
            dispatched["called"] = True
            return None

    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: _Capture(), jobs.LANE_SLOW: _Capture()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    tool = {"name": "count-by-layer", "capabilities": ["drawing.read"]}
    job_id = jobs.submit_job("t", tool, {}, "rooftop_demo", aps_live=False, dwg_version=7)
    # Simulate an OLD row: rewrite execution_json without the dwg_version key.
    with jobs._lock:
        jobs._db().execute(
            "UPDATE jobs SET execution_json = ? WHERE job_id = ?",
            (json.dumps({"tool": tool, "aps_live": False}), job_id))
        jobs._db().commit()
    dispatched["called"] = False
    assert jobs._redispatch_record(job_id) is False
    assert dispatched["called"] is False
    assert jobs.get_job(job_id)["status"] == "failed"
    if jobs._conn is not None:
        jobs._conn.close()
