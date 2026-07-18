"""
Dynamic tool loader — "the tool FILE is the tool".

`run_tool_dynamic(tool, intake, params, aps_live, da)` resolves execution from
the TOOL PACKAGE (§2), never from a hardcoded engine_op->handler table:

  * kind:script + a resolvable local .py file (tool['entry'] under authored/ or
    builtins/, a `.py` at tool['script'], or a known built-in engine_op) ->
    import the module and call its `run(intake, params) -> (result, overlay)`.
    This is the local "the FILE is the tool" path (APS_LIVE=0).
  * kind:script with only a .lsp/engine_script, or kind:appbundle -> APS path:
    `da.ensure_tool_activity(tool)` then `da.run_tool(...)` (live; operator-gated).

Every run PRE-validates params (fail => BAD_PARAMS envelope, body never runs)
and POST-validates the produced §3 envelope (broken tool => INTERNAL). Returns
the extended §3 envelope (ADDENDUM §10: adds `degraded_mode`).

This module NEVER imports `da.*` at top level — the credential-holding client is
passed in by the broker (the only process allowed to hold it).
"""
from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from envelopes import ErrorCode, err_envelope, ok_envelope
from tool_validate import validate_envelope, validate_params

SERVER_DIR = Path(__file__).resolve().parent
AUTHORED_DIR = SERVER_DIR / "authored"
BUILTIN_DIR = SERVER_DIR / "builtins"

# Compat lookup used ONLY to resolve the pre-existing built-in ops to their
# server/builtins/*.py file. This is NOT an authoritative dispatch table:
# authored tools resolve by their own `entry` file and NEVER through this map,
# so a newly authored tool needs no entry here (that is the whole point).
BUILTIN_OPS: Dict[str, str] = {
    "count_by_layer": "count_by_layer.py",
    "measure_area_by_layer": "measure_area_by_layer.py",
    "measure_panel_area": "measure_area_by_layer.py",
    "highlight_near_edge": "highlight_near_edge.py",
    "highlight_panels_near_edge": "highlight_near_edge.py",
    "list_layers": "list_layers.py",
}

_MOD_CACHE: Dict[str, Any] = {}


def _load_module(path: Path):
    """Import a tool file by path (reloaded when its mtime OR size changes)."""
    st = path.stat()
    key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    cached = _MOD_CACHE.get(key)
    if cached is not None:
        return cached
    mod_name = f"leaf_tool_{abs(hash(key)) & 0xFFFFFFFF}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _MOD_CACHE[key] = mod
    return mod


def _tenant_repo_root(tenant_id: Optional[str] = None) -> Optional[Path]:
    """The mushy-repo checkout root for a tenant (read at CALL time; the SAME resolver
    deps.load_tenant_repo_tools folds against — server/tenant_paths.py). Wave 4:
    ``$LEAF_TENANTS_DIR/<tenant_id>``, with ``$LEAF_TENANT_REPO`` as the demo-tenant
    back-compat override; a None/empty tenant_id resolves the demo tenant (legacy
    no-tenant callers). None => not configured for this tenant."""
    from tenant_paths import resolve_tenant_repo_dir  # local import: no import cycle
    return resolve_tenant_repo_dir(tenant_id)


def resolve_local_file(tool: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Path]:
    """Return the local .py file that IS this tool, or None (=> APS path).

    Resolution order: explicit `entry` (resolved against the REQUESTING tenant's repo
    root when configured, then authored/ or builtins/), a `.py` `script`, then the
    built-in-op compat lookup. Authored tools always resolve via their `entry`, so
    they run their OWN persisted body.

    Wave 3/4 (Contract 2/5): a tenant-repo tool carries a repo-RELATIVE entry (e.g.
    ``tools/<name>/tool.py``). We resolve it against the requesting tenant's repo root
    (absolute — ``$LEAF_TENANTS_DIR/<tenant_id>``, or ``$LEAF_TENANT_REPO`` for the demo
    tenant), so the broker resolves it regardless of its cwd AND scopes execution to the
    calling tenant's own files (tenant A's tool never resolves against tenant B's repo).
    """
    entry = tool.get("entry")
    if entry:
        name = Path(entry).name
        cands = [Path(entry), SERVER_DIR / entry, AUTHORED_DIR / name, BUILTIN_DIR / name]
        troot = _tenant_repo_root(tenant_id)
        if troot is not None:
            # resolve the tenant entry against the repo root (absolute), ahead of the
            # authored/builtins fallbacks so a tenant tool runs its OWN repo file.
            cands.insert(1, troot / entry)
        for cand in cands:
            if cand.suffix == ".py" and cand.exists():
                return cand
    script = tool.get("script")
    if script and str(script).endswith(".py"):
        for cand in (Path(script), SERVER_DIR / script):
            if cand.exists():
                return cand
    op = tool.get("engine_op") or ""
    fname = BUILTIN_OPS.get(op)
    if fname:
        cand = BUILTIN_DIR / fname
        if cand.exists():
            return cand
    return None


def _needs_aps(tool: Dict[str, Any], tenant_id: Optional[str] = None) -> bool:
    """True when the tool has no local .py and must run on APS DA."""
    return resolve_local_file(tool, tenant_id) is None


def _coerce(ret: Any) -> Union[Tuple[dict, Any], dict]:
    """Normalize a tool's return into (result, overlay) — or a full envelope."""
    if isinstance(ret, tuple) and len(ret) == 2:
        return ret[0], ret[1]
    if isinstance(ret, dict) and "ok" in ret and "result" in ret:
        return ret  # the tool returned a full envelope
    if isinstance(ret, dict):
        return ret, None
    return {"value": ret}, None


def _normalize_aps_envelope(raw: Dict[str, Any], name: Optional[str], version: str,
                            t0: float) -> Dict[str, Any]:
    raw.setdefault("ok", True)
    raw.setdefault("tool", name)
    raw.setdefault("version", version)
    raw.setdefault("result", {})
    raw.setdefault("overlay", None)
    raw.setdefault("timing_ms", int((time.perf_counter() - t0) * 1000))
    raw.setdefault("cost", None)
    raw.setdefault("error", None)
    raw.setdefault("degraded_mode", False)
    if not raw.get("ok") and raw.get("error") is None:
        raw["error"] = {"error_code": ErrorCode.WORKITEM_FAILED,
                        "message": "WorkItem did not succeed", "retryable": True}
    return raw


def run_tool_dynamic(tool: Dict[str, Any], intake: Dict[str, Any], params: Dict[str, Any],
                     aps_live: bool, da: Any = None, *,
                     dwg_path: Optional[str] = None,
                     t0: Optional[float] = None,
                     tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute the tool the registry entry references. Returns an extended §3 envelope.

    ``tenant_id`` scopes entry resolution to the requesting tenant's repo (wave 4). A
    None tenant_id keeps the legacy demo-tenant behaviour (honours $LEAF_TENANT_REPO)."""
    t0 = t0 if t0 is not None else time.perf_counter()
    name = (tool or {}).get("name")
    version = (tool or {}).get("version", "1.0.0")
    params = dict(params or {})

    def _ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    # 1) PRE-VALIDATE params — fail => body NEVER runs (§8.4 step 1)
    perrs = validate_params(tool, params)
    if perrs:
        return err_envelope(ErrorCode.BAD_PARAMS, "params schema: " + "; ".join(perrs),
                            retryable=False, tool=name, version=version, timing_ms=_ms())

    local = resolve_local_file(tool, tenant_id)

    # 2) APS path (kind:appbundle OR kind:script with only .lsp/engine_script)
    if aps_live and da is not None and hasattr(da, "run_tool") and local is None:
        try:
            if hasattr(da, "ensure_tool_activity"):
                da.ensure_tool_activity(tool)
            raw = dict(da.run_tool(dwg_path, tool, params) or {})
            return _normalize_aps_envelope(raw, name, version, t0)
        except FileNotFoundError as exc:  # APS creds missing
            return err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                                tool=name, version=version, timing_ms=_ms())
        except Exception as exc:  # noqa: BLE001
            return err_envelope(ErrorCode.WORKITEM_FAILED, f"{type(exc).__name__}: {exc}",
                                retryable=True, tool=name, version=version, timing_ms=_ms())

    # 3) LOCAL "the FILE is the tool" path (APS_LIVE=0, or degraded live fallback)
    degraded = bool(aps_live)  # requested live but running locally => degraded
    if local is None:
        return err_envelope(
            ErrorCode.BAD_PARAMS,
            f"no local implementation resolvable for tool {name!r} "
            f"(engine_op={tool.get('engine_op')!r}) at APS_LIVE=0",
            retryable=False, tool=name, version=version, timing_ms=_ms())
    try:
        mod = _load_module(local)
    except Exception as exc:  # noqa: BLE001
        return err_envelope(ErrorCode.INTERNAL,
                            f"failed to load tool file {local.name}: {type(exc).__name__}: {exc}",
                            retryable=False, tool=name, version=version, timing_ms=_ms())
    if not hasattr(mod, "run"):
        return err_envelope(ErrorCode.INTERNAL,
                            f"tool file {local.name} has no run(intake, params)",
                            retryable=False, tool=name, version=version, timing_ms=_ms())
    try:
        ret = mod.run(intake, params)
    except Exception as exc:  # noqa: BLE001
        return err_envelope(ErrorCode.INTERNAL,
                            f"tool {name!r} raised {type(exc).__name__}: {exc}",
                            retryable=False, tool=name, version=version, timing_ms=_ms())

    coerced = _coerce(ret)
    if isinstance(coerced, dict):  # the tool returned a full envelope
        env = coerced
        env.setdefault("degraded_mode", degraded)
        return env
    result, overlay = coerced
    env = ok_envelope(name, version, result, overlay, timing_ms=_ms(), cost=None,
                      degraded_mode=degraded)

    # 4) POST-VALIDATE the §3 envelope — a broken tool surfaces as INTERNAL
    everrs = validate_envelope(env)
    if everrs:
        return err_envelope(ErrorCode.INTERNAL,
                            "tool output failed §3 envelope schema: " + "; ".join(everrs),
                            retryable=False, tool=name, version=version, timing_ms=env["timing_ms"])
    return env
