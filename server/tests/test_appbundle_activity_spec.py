"""kind:"appbundle" tools provision a real Activity (CONTRACT §2 declared the kind; this
wires it). Calls the REAL da/client.py resolution. Live callers resolve and cache the
actual APS owner nickname before they construct the Activity; only an explicit dry run
may emit the literal nickname placeholder. The emitted script is the bundle's command
plus the mark-saved QUIT, so the broker's live-script guard passes and the WorkItem
cannot be submitted with an empty script.
"""
from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
ENGINE_REGISTRY = REPO_ROOT / "engine" / "registry.json"
BUNDLE_ZIP = REPO_ROOT / "engine" / "appbundle-cutlist" / "LeafCutListTools.zip"


def _load_real_da_client():
    spec = importlib.util.spec_from_file_location("da_client_appbundle_test", REPO_ROOT / "da" / "client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cutlist_tool():
    tools = json.loads(ENGINE_REGISTRY.read_text(encoding="utf-8"))["tools"]
    return next(t for t in tools if t["name"] == "timber-cutlist")


class _NicknameResponse:
    text = '"real-owner"'

    @staticmethod
    def raise_for_status():
        return None


def _cold_nickname_client(monkeypatch):
    da = _load_real_da_client()
    monkeypatch.delenv("APS_NICKNAME", raising=False)
    if hasattr(da.nickname, "_v"):
        delattr(da.nickname, "_v")
    calls = []
    monkeypatch.setattr(da, "_auth_headers", lambda: {"Authorization": "Bearer test"})

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _NicknameResponse()

    monkeypatch.setattr(da.requests, "get", fake_get)
    return da, calls


def test_registry_declares_the_compiled_tool_completely():
    t = _cutlist_tool()
    assert t["kind"] == "appbundle"
    assert t["appbundle"] == "LeafCutListTools"
    assert t["command"] == "LEAFCUTLIST"
    assert t["engine_op"] == "leaf_cutlist"
    assert t["capabilities"] == ["drawing.read"]
    assert t["aps_live"] is True
    assert set(t["params"]["properties"]) == {
        "wall_mid_spacing_mm", "wall_end_spacing_mm", "joist_spacing_mm", "merge_tolerance_mm", "reconcile_views"}


def test_appbundle_activity_spec_loads_bundle_and_runs_command(monkeypatch):
    monkeypatch.setenv("APS_NICKNAME", "nick")
    da = _load_real_da_client()
    da.nickname._v = "nick"  # cached: the spec must not touch the network
    spec = da.tool_activity_spec(_cutlist_tool())
    assert spec["id"] == "LeafTool_leaf_cutlist"
    assert spec["engine"] == da.ENGINE
    assert spec["appbundles"] == ["nick.LeafCutListTools+prod"]
    cmd = spec["commandLine"][0]
    assert '/al "$(appbundles[LeafCutListTools].path)"' in cmd
    assert '/i "$(args[HostDwg].path)"' in cmd and '/s "$(settings[script].path)"' in cmd
    script = spec["settings"]["script"]["value"]
    assert script.startswith('(setvar "CMDECHO" 0)\r\nLEAFCUTLIST\r\n')
    assert "vla-put-Saved" in script and script.endswith("\r\n")
    assert spec["parameters"]["Result"]["localName"] == "result.json"
    assert spec["parameters"]["Params"]["localName"] == "params.json"


def test_live_script_guard_accepts_the_appbundle_tool(monkeypatch):
    import broker  # noqa: PLC0415
    da, calls = _cold_nickname_client(monkeypatch)
    assert broker._live_script_is_nonempty(_cutlist_tool(), da) is True
    assert calls[0][0].endswith("/forgeapps/me")
    assert da.nickname._v == "real-owner"
    assert da.tool_activity_spec(_cutlist_tool())["appbundles"] == [
        "real-owner.LeafCutListTools+prod"
    ]


def test_tool_loader_live_path_resolves_nickname_before_activity(monkeypatch):
    import tool_loader  # noqa: PLC0415
    da, calls = _cold_nickname_client(monkeypatch)

    class LiveDa:
        spec = None

        def ensure_tool_activity(self, tool):
            self.spec = da.tool_activity_spec(tool)

        @staticmethod
        def run_tool(_dwg_path, _tool, _params):
            return {"ok": True, "result": {}}

    live_da = LiveDa()
    result = tool_loader.run_tool_dynamic(
        _cutlist_tool(), {}, {}, aps_live=True, da=live_da, dwg_path="input.dwg"
    )
    assert result["ok"] is True
    assert calls[0][0].endswith("/forgeapps/me")
    assert live_da.spec["appbundles"] == ["real-owner.LeafCutListTools+prod"]
    assert "$(nickname)" not in json.dumps(live_da.spec)


def test_explicit_activity_dry_run_may_use_nickname_placeholder(monkeypatch):
    da, calls = _cold_nickname_client(monkeypatch)
    out = da.ensure_tool_activity(_cutlist_tool(), dry_run=True)
    assert calls == []
    assert out["body"]["appbundles"] == ["$(nickname).LeafCutListTools+prod"]


@pytest.mark.parametrize("bad", [
    {"appbundle": "../evil", "command": "LEAFCUTLIST"},
    {"appbundle": "LeafCutListTools", "command": "leafcutlist; (command \"_.SAVEAS\")"},
    {"appbundle": "", "command": "LEAFCUTLIST"},
])
def test_appbundle_ids_and_commands_are_validated(bad, monkeypatch):
    monkeypatch.setenv("APS_NICKNAME", "nick")
    da = _load_real_da_client()
    da.nickname._v = "nick"
    tool = {**_cutlist_tool(), **bad}
    with pytest.raises(ValueError):
        da.tool_activity_spec(tool)


def test_shipped_bundle_zip_is_a_da_appbundle():
    assert BUNDLE_ZIP.exists(), "engine/appbundle-cutlist/LeafCutListTools.zip must be committed"
    with zipfile.ZipFile(BUNDLE_ZIP) as zf:
        names = zf.namelist()
    assert any(n.endswith("LeafCutListTools.bundle/PackageContents.xml") for n in names)
    assert any(n.endswith("Contents/LeafCutListTools.dll") for n in names)
    assert any(n.endswith("Contents/CutLists.Core.dll") for n in names)
    # The engine supplies these; shipping them would fail the load on a version clash.
    assert not any(n.lower().endswith(("acdbmgd.dll", "accoremgd.dll", "acmgd.dll")) for n in names)


def test_ensure_appbundle_dry_run_validates_zip_without_network():
    da = _load_real_da_client()
    out = da.ensure_appbundle("LeafCutListTools", str(BUNDLE_ZIP), dry_run=True)
    assert out["_dry_run"] is True and out["body"]["id"] == "LeafCutListTools"
    assert out["body"]["engine"] == da.ENGINE and out["zip_bytes"] > 0
    with pytest.raises(ValueError):
        da.ensure_appbundle("bad id", str(BUNDLE_ZIP), dry_run=True)
