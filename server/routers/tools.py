"""GET /api/tools + POST /api/run — moved verbatim from app.py.

Owned downstream by the `dynamic-tool-loader` session.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import deps
from deps import fb

router = APIRouter()


class RunRequest(BaseModel):
    tool: str
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"


@router.get("/api/tools")
def tools() -> Dict[str, Any]:
    return {"tools": deps.all_tools()}


@router.post("/api/run")
def run(req: RunRequest) -> Dict[str, Any]:
    """Execute a tool and return the Result envelope (contract section 3)."""
    tool = deps.find_tool(req.tool)
    if tool is None:
        raise HTTPException(404, f"unknown tool: {req.tool}")

    # merge authored default_params under caller params
    params = dict(tool.get("default_params", {}))
    params.update(req.params or {})

    t0 = time.perf_counter()

    if deps.APS_LIVE:
        da = deps.get_da_client()
        if da is None or not hasattr(da, "run_tool"):
            raise HTTPException(500, "APS_LIVE=1 but da/client.py (Lane A) is not importable")
        try:
            local = str(deps.DATA_FILE.parent / f"{req.dwg}.dwg")
            env = da.run_tool(local, tool, params)
            # trust Lane A's envelope; ensure required keys exist
            return _normalize_envelope(env, tool, t0)
        except Exception as exc:
            raise HTTPException(502, f"DA run_tool failed: {exc}")

    # APS_LIVE=0 — pure python. Prefer engine.selfcheck if it exposes a runner.
    intake = deps.load_cached_intake()
    engine_op = tool.get("engine_op", "")
    result: Dict[str, Any]
    overlay: Optional[Dict[str, Any]]

    sc = deps.get_engine_selfcheck()
    ran_via_engine = False
    if sc is not None:
        for fn_name in ("run_op", "run_tool", "run"):
            fn = getattr(sc, fn_name, None)
            if callable(fn):
                try:
                    out = fn(engine_op, intake, params)
                    if isinstance(out, tuple):
                        result, overlay = out[0], (out[1] if len(out) > 1 else None)
                    elif isinstance(out, dict) and "result" in out:
                        result, overlay = out["result"], out.get("overlay")
                    else:
                        result, overlay = out, None
                    ran_via_engine = True
                    break
                except Exception as exc:  # fall back on any engine mismatch
                    print(f"[leaf-demo] engine.selfcheck.{fn_name} failed: {exc}", file=sys.stderr)

    if not ran_via_engine:
        try:
            result, overlay = fb.run_op(engine_op, intake, params)
        except KeyError as exc:
            raise HTTPException(400, str(exc))

    timing_ms = int((time.perf_counter() - t0) * 1000)
    envelope: Dict[str, Any] = {
        "ok": True,
        "tool": tool["name"],
        "version": tool.get("version", "1.0.0"),
        "result": result,
        "overlay": overlay,
        "timing_ms": timing_ms,
        "cost": None,  # null until a real APS run
        "error": None,
    }
    return envelope


# --------------------------------------------------------------------------- #
def _normalize_envelope(env: Dict[str, Any], tool: Dict[str, Any], t0: float) -> Dict[str, Any]:
    env = dict(env or {})
    env.setdefault("ok", True)
    env.setdefault("tool", tool["name"])
    env.setdefault("version", tool.get("version", "1.0.0"))
    env.setdefault("result", {})
    env.setdefault("overlay", None)
    env.setdefault("timing_ms", int((time.perf_counter() - t0) * 1000))
    env.setdefault("cost", None)
    env.setdefault("error", None)
    return env
