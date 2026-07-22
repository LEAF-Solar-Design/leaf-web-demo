"""Every engine-registry tool must emit a non-empty live Activity script.

Follow-up to the leaf-web-demo PR #14 (round-2) review finding (2026-07-22):
da/client.py `tool_activity_spec` resolved a relative `.lsp` `script` path
against the project root ONLY, while engine/registry.json declares those paths
relative to the registry's own directory (`tools/<name>.lsp` ->
engine/tools/<name>.lsp — the convention engine/selfcheck.py resolves). Every
shipped engine-registry tool therefore emitted an EMPTY Activity script on the
APS-live path, and the read failure was swallowed silently.

These tests call the REAL da/client.py function (dependency-free file-path
import: no APS creds, no network) against the REAL engine/registry.json, so a
regression in either the resolution logic or the registry path convention
fails loudly here.
"""
import importlib.util
import json
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
ENGINE_REGISTRY = REPO_ROOT / "engine" / "registry.json"


def _load_real_da_client():
    """Load the REAL da/client.py module by file path (same dependency-free
    pattern as test_catalog_and_version_pin.py's `_load_real_da_client`)."""
    spec = importlib.util.spec_from_file_location(
        "da_client_engine_registry_scripts_test", REPO_ROOT / "da" / "client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry_tools():
    tools = json.loads(ENGINE_REGISTRY.read_text(encoding="utf-8")).get("tools", [])
    assert tools, "engine/registry.json must ship at least one tool"
    return tools


def _emitted_script(da_mod, tool):
    spec = da_mod.tool_activity_spec(tool)
    script = ((spec or {}).get("settings") or {}).get("script") or {}
    return script.get("value")


def test_every_engine_registry_tool_emits_a_nonempty_live_script():
    da_mod = _load_real_da_client()
    empty = [t.get("name") for t in _registry_tools()
             if not str(_emitted_script(da_mod, t) or "").strip()]
    assert not empty, (
        f"engine-registry tools emitting an EMPTY live Activity script: {empty} "
        f"(these would fail closed / silently no-op on the APS-live path)")


def test_lsp_scripts_resolve_to_the_declared_registry_relative_file():
    """The emitted script must be the exact bytes of the file the registry
    declares — proves the resolution found the registry-relative file, not
    some unrelated fallback. Mirrors tool_activity_spec's candidate order:
    project root first, then the engine dir."""
    da_mod = _load_real_da_client()
    checked = 0
    for tool in _registry_tools():
        sp = tool.get("script")
        if tool.get("engine_script") or not (sp and str(sp).endswith(".lsp")):
            continue
        candidates = [REPO_ROOT / sp, REPO_ROOT / "engine" / sp]
        on_disk = next((c for c in candidates if c.is_file()), None)
        assert on_disk is not None, f"{tool.get('name')}: dangling script path {sp!r}"
        assert _emitted_script(da_mod, tool) == on_disk.read_text(encoding="utf-8"), \
            tool.get("name")
        checked += 1
    assert checked, "expected at least one .lsp-declared engine-registry tool"


def test_unresolvable_lsp_path_still_emits_an_empty_script_not_a_raise():
    """Fail-soft contract the broker's live guard depends on: a dangling
    `.lsp` path yields an EMPTY script value, never an exception."""
    da_mod = _load_real_da_client()
    tool = {"name": "ghost-tool", "script": "tools/does_not_exist.lsp"}
    assert _emitted_script(da_mod, tool) == ""
