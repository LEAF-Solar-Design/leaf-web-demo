"""kind:"appbundle" tools provision a real Activity (CONTRACT §2 declared the kind; this
wires it). Calls the REAL da/client.py resolution, dependency-free (no creds, no network):
the appbundle branch never needs the nickname when APS_NICKNAME is set, and the emitted
script is the bundle's command plus the mark-saved QUIT, so the broker's live-script
guard passes and the WorkItem cannot be submitted with an empty script.

The fixture tool is SYNTHETIC (no registry entry ships a kind:appbundle tool today), so
this suite pins the platform capability, not any one product tool.
"""
from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent

BUNDLE_TOOL = {
    "name": "sample-appbundle-tool",
    "version": "1.0.0",
    "description": "Synthetic kind:appbundle fixture for the Activity spec contract.",
    "kind": "appbundle",
    "appbundle": "LeafSampleTools",
    "command": "LEAFSAMPLE",
    "engine_op": "leaf_sample",
    "capabilities": ["drawing.read"],
    "aps_live": True,
    "params": {"type": "object", "properties": {"spacing_mm": {"type": "number", "default": 600}}},
}


def _load_real_da_client():
    spec = importlib.util.spec_from_file_location("da_client_appbundle_test", REPO_ROOT / "da" / "client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tool() -> dict:
    return json.loads(json.dumps(BUNDLE_TOOL))


def test_appbundle_activity_spec_loads_bundle_and_runs_command(monkeypatch):
    monkeypatch.setenv("APS_NICKNAME", "nick")
    da = _load_real_da_client()
    da.nickname._v = "nick"  # cached: the spec must not touch the network
    spec = da.tool_activity_spec(_tool())
    assert spec["id"] == "LeafTool_leaf_sample"
    assert spec["engine"] == da.ENGINE
    assert spec["appbundles"] == ["nick.LeafSampleTools+prod"]
    cmd = spec["commandLine"][0]
    assert '/al "$(appbundles[LeafSampleTools].path)"' in cmd
    assert '/i "$(args[HostDwg].path)"' in cmd and '/s "$(settings[script].path)"' in cmd
    script = spec["settings"]["script"]["value"]
    assert script.startswith('(setvar "CMDECHO" 0)\r\nLEAFSAMPLE\r\n')
    assert "vla-put-Saved" in script and script.endswith("\r\n")
    assert spec["parameters"]["Result"]["localName"] == "result.json"
    assert spec["parameters"]["Params"]["localName"] == "params.json"


def test_appbundles_entry_is_never_a_placeholder(monkeypatch):
    """Staging 2026-09-01: the broker had no cached nickname and no APS_NICKNAME, the spec
    emitted "$(nickname).<bundle>+prod", and DA answered 400 on POST /activities.
    The owner must come from APS_NICKNAME or the (cached) live lookup, never a literal."""
    da = _load_real_da_client()
    # 1) env wins without any network
    monkeypatch.setenv("APS_NICKNAME", "envnick")
    if hasattr(da.nickname, "_v"):
        delattr(da.nickname, "_v")
    spec = da.tool_activity_spec(_tool())
    assert spec["appbundles"] == ["envnick.LeafSampleTools+prod"]
    # 2) no env: the live lookup is used (cached on the function), never a placeholder
    monkeypatch.delenv("APS_NICKNAME", raising=False)
    calls = []

    def fake_nickname():
        calls.append(1)
        return "livenick"

    monkeypatch.setattr(da, "nickname", fake_nickname)
    spec = da.tool_activity_spec(_tool())
    assert spec["appbundles"] == ["livenick.LeafSampleTools+prod"]
    assert calls == [1]
    assert "$(nickname)" not in json.dumps(spec)


class _NicknameResponse:
    text = '"real-owner"'

    @staticmethod
    def raise_for_status():
        return None


def _cold_nickname_client(monkeypatch):
    """Load the real client with no configured or cached nickname (#894: the live callers
    must resolve the owner through the one lookup before any Activity is built)."""
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


def test_live_script_guard_accepts_an_appbundle_tool(monkeypatch):
    import broker  # noqa: PLC0415
    da, calls = _cold_nickname_client(monkeypatch)
    assert broker._live_script_is_nonempty(_tool(), da) is True
    assert calls[0][0].endswith("/forgeapps/me")
    assert da.nickname._v == "real-owner"
    spec = da.tool_activity_spec(_tool())
    assert spec["appbundles"] == ["real-owner.LeafSampleTools+prod"]
    assert "$(nickname)" not in json.dumps(spec)


def test_tool_loader_live_path_resolves_nickname_before_activity(monkeypatch):
    import tool_loader  # noqa: PLC0415
    da, calls = _cold_nickname_client(monkeypatch)
    monkeypatch.setattr(tool_loader, "validate_params", lambda _tool, _params: [])

    class LiveDa:
        spec = None

        def ensure_tool_activity(self, tool):
            self.spec = da.tool_activity_spec(tool)

        @staticmethod
        def run_tool(_dwg_path, _tool, _params):
            return {"ok": True, "result": {}}

    live_da = LiveDa()
    result = tool_loader.run_tool_dynamic(
        _tool(), {}, {}, aps_live=True, da=live_da, dwg_path="input.dwg"
    )
    assert result["ok"] is True
    assert calls[0][0].endswith("/forgeapps/me")
    assert live_da.spec["appbundles"] == ["real-owner.LeafSampleTools+prod"]
    assert "$(nickname)" not in json.dumps(live_da.spec)


@pytest.mark.parametrize("bad", [
    {"appbundle": "../evil", "command": "LEAFSAMPLE"},
    {"appbundle": "LeafSampleTools", "command": "leafsample; (command \"_.SAVEAS\")"},
    {"appbundle": "", "command": "LEAFSAMPLE"},
])
def test_appbundle_ids_and_commands_are_validated(bad, monkeypatch):
    monkeypatch.setenv("APS_NICKNAME", "nick")
    da = _load_real_da_client()
    da.nickname._v = "nick"
    tool = {**_tool(), **bad}
    with pytest.raises(ValueError):
        da.tool_activity_spec(tool)


def _sample_zip(tmp_path: Path) -> Path:
    path = tmp_path / "LeafSampleTools.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("LeafSampleTools.bundle/PackageContents.xml", "<ApplicationPackage />")
        zf.writestr("LeafSampleTools.bundle/Contents/LeafSampleTools.dll", b"\x00")
    return path


def test_ensure_appbundle_dry_run_validates_zip_without_network(tmp_path):
    da = _load_real_da_client()
    zip_path = _sample_zip(tmp_path)
    out = da.ensure_appbundle("LeafSampleTools", str(zip_path), dry_run=True)
    assert out["_dry_run"] is True and out["body"]["id"] == "LeafSampleTools"
    assert out["body"]["engine"] == da.ENGINE and out["zip_bytes"] > 0
    with pytest.raises(ValueError):
        da.ensure_appbundle("bad id", str(zip_path), dry_run=True)
    empty = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("Contents/only.dll", b"\x00")
    with pytest.raises(ValueError):
        da.ensure_appbundle("LeafSampleTools", str(empty), dry_run=True)
