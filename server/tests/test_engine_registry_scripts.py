"""Every live-Activity engine-registry tool must emit a non-empty script.

Follow-up to the leaf-web-demo PR #14 (round-2) review finding (2026-07-22):
engine/registry.json declared its `.lsp` `script` paths as `tools/<name>.lsp`
while the files live at engine/tools/<name>.lsp, and da/client.py
`tool_activity_spec` resolves relative paths against the project root only —
so every shipped engine-registry tool emitted an EMPTY Activity script on the
APS-live path, with the read failure swallowed silently.

The fix declares root-relative paths (`engine/tools/<name>.lsp`) in the
registry. Resolution stays single-root ON PURPOSE: a fallback root would let
a tool from one registry silently load another registry's script on a path
collision (sol-critic round-1 finding on the first attempted fix).

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


def _live_activity_tools():
    """Return registry tools that use the Design Automation Activity path.

    ``canonical_only`` identifies a solver that runs through the canonical
    worker rather than a live Activity. ``offline_only`` identifies the one
    public demo tool that is contractually submitted with APS_LIVE=0 and runs
    its tracked Python builtin. Neither can use a live Activity.
    """
    # ``local_only`` (2026-09-01) identifies a tool whose implementation IS its local
    # Python entry by design (timber-cutlist-preflight): it never takes the Activity
    # path and the loader does not mark its runs degraded (tool_loader.run_tool_dynamic).
    return [
        tool for tool in _registry_tools()
        if not tool.get("canonical_only") and not tool.get("offline_only") and not tool.get("local_only")
    ]


def test_explicit_live_aps_catalog_tools_are_exactly_the_pinned_set():
    """The live APS lane is an explicit allowlist, pinned here so a registry edit cannot
    widen it silently. Members: count-by-layer (LISP, the original), timber-cutlist
    (compiled AppBundle, 2026-09-01: the first kind:appbundle tool; it has no local
    implementation, so APS is its only execution path)."""
    live_eligible = {
        tool["name"]: tool for tool in _registry_tools() if tool.get("aps_live") is True
    }
    assert sorted(live_eligible) == ["count-by-layer", "timber-cutlist"]
    assert live_eligible["count-by-layer"]["capabilities"] == ["drawing.read"]
    assert live_eligible["count-by-layer"]["script"] == "engine/tools/count_by_layer.lsp"
    assert live_eligible["timber-cutlist"]["capabilities"] == ["drawing.read"]
    assert live_eligible["timber-cutlist"]["kind"] == "appbundle"
    assert live_eligible["timber-cutlist"]["appbundle"] == "LeafCutListTools"


def test_live_aps_catalog_authority_requires_an_exact_boolean():
    count_by_layer = next(
        tool for tool in _registry_tools() if tool.get("name") == "count-by-layer"
    )
    assert count_by_layer.get("aps_live") is True
    assert type(count_by_layer["aps_live"]) is bool


def test_offline_only_registry_scope_is_exact_and_immutable():
    offline = [tool for tool in _registry_tools() if tool.get("offline_only")]
    assert offline == [{
        **offline[0],
        "name": "string-panels",
        "engine_op": "string_panels",
        "entry": "builtins/string_panels.py",
        "capabilities": ["drawing.read"],
        "offline_only": True,
    }]


def _emitted_script(da_mod, tool):
    spec = da_mod.tool_activity_spec(tool)
    script = ((spec or {}).get("settings") or {}).get("script") or {}
    return script.get("value")


def test_every_live_activity_engine_registry_tool_emits_a_nonempty_script():
    da_mod = _load_real_da_client()
    live_tools = _live_activity_tools()
    assert live_tools, "expected at least one engine-registry live Activity tool"
    empty = [t.get("name") for t in live_tools
             if not str(_emitted_script(da_mod, t) or "").strip()]
    assert not empty, (
        f"engine-registry tools emitting an EMPTY live Activity script: {empty} "
        f"(these would fail closed / silently no-op on the APS-live path)")


def test_lsp_scripts_resolve_to_the_declared_root_relative_file():
    """The emitted script must be the exact contents of the file the registry
    declares, resolved against the project root ONLY (the tool_activity_spec
    contract; registries declare root-relative paths)."""
    da_mod = _load_real_da_client()
    checked = 0
    for tool in _registry_tools():
        sp = tool.get("script")
        if tool.get("engine_script") or not (sp and str(sp).endswith(".lsp")):
            continue
        on_disk = REPO_ROOT / sp
        assert on_disk.is_file(), (
            f"{tool.get('name')}: declared script path {sp!r} does not resolve "
            f"at the project root — registry paths must be root-relative")
        assert _emitted_script(da_mod, tool) == on_disk.read_text(encoding="utf-8"), \
            tool.get("name")
        checked += 1
    assert checked, "expected at least one .lsp-declared engine-registry tool"


def test_no_cross_registry_fallback_on_colliding_relative_paths():
    """sol-critic round-1 finding on the first attempted fix: a NON-engine
    tool declaring a relative path that does NOT resolve at the project root
    but DOES name an existing engine/tools file must emit an EMPTY script —
    never silently load the engine registry's script."""
    da_mod = _load_real_da_client()
    assert (REPO_ROOT / "engine" / "tools" / "count_by_layer.lsp").is_file()
    assert not (REPO_ROOT / "tools" / "count_by_layer.lsp").exists()
    foreign = {"name": "tenant-foreign-tool", "script": "tools/count_by_layer.lsp"}
    assert _emitted_script(da_mod, foreign) == ""


def test_unresolvable_lsp_path_still_emits_an_empty_script_not_a_raise():
    """Fail-soft contract the broker's live guard depends on: a dangling
    `.lsp` path yields an EMPTY script value, never an exception."""
    da_mod = _load_real_da_client()
    tool = {"name": "ghost-tool", "script": "tools/does_not_exist.lsp"}
    assert _emitted_script(da_mod, tool) == ""
