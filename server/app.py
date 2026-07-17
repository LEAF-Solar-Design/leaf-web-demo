"""
Leaf web demo — Lane D backend glue (FastAPI).

Implements the FROZEN HTTP API (contract section 4) that wires the frontend to
either mock/pure-python results (APS_LIVE=0, default) or the Lane A DA client
(APS_LIVE=1). The frontend never knows the difference.

Run:
    cd server && APS_LIVE=0 uvicorn app:app --port 8130
    # or: python app.py

Env:
    APS_LIVE=0  (default)  -> cached sample intake + pure-python tool logic
    APS_LIVE=1             -> requires ../da/client.py (Lane A); errors clearly if absent
    LEAF_AUTHOR_LLM=0      (default)  -> author endpoint uses templating only
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# paths + sibling-lane import wiring
# --------------------------------------------------------------------------- #
SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
DATA_FILE = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"
ENGINE_DIR = PROJECT_ROOT / "engine"
DA_DIR = PROJECT_ROOT / "da"
ENGINE_REGISTRY = ENGINE_DIR / "registry.json"
AUTHORED_STORE = SERVER_DIR / "authored_tools.json"  # our lane persists authored tools here

# make sibling lanes importable (engine.selfcheck, da.client)
for p in (str(PROJECT_ROOT), str(SERVER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

APS_LIVE = os.environ.get("APS_LIVE", "0") == "1"

# always-works fallback (this lane)
import tools_fallback as fb  # noqa: E402


# --------------------------------------------------------------------------- #
# optional sibling-lane modules (loaded lazily + gracefully)
# --------------------------------------------------------------------------- #
def _load_module_from(path: Path, mod_name: str):
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[leaf-demo] could not import {path}: {exc}", file=sys.stderr)
        return None


def get_engine_selfcheck():
    """Lane B's engine/selfcheck.py, if present AND it exposes run_op/run_tool.
    Optional enhancement only; the fallback is authoritative for the demo."""
    return _load_module_from(ENGINE_DIR / "selfcheck.py", "engine_selfcheck")


def get_da_client():
    """Lane A's da/client.py. Required only when APS_LIVE=1."""
    return _load_module_from(DA_DIR / "client.py", "da_client")


# --------------------------------------------------------------------------- #
# intake + registry loading
# --------------------------------------------------------------------------- #
def load_cached_intake() -> Dict[str, Any]:
    with open(DATA_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_engine_registry_tools() -> List[Dict[str, Any]]:
    """Prefer engine/registry.json (Lane B); fall back to built-in DEFAULT_TOOLS."""
    if ENGINE_REGISTRY.exists():
        try:
            data = json.loads(ENGINE_REGISTRY.read_text(encoding="utf-8"))
            tools = data.get("tools") if isinstance(data, dict) else None
            if isinstance(tools, list) and tools:
                return tools
        except Exception as exc:  # pragma: no cover
            print(f"[leaf-demo] bad engine/registry.json: {exc}", file=sys.stderr)
    return list(fb.DEFAULT_TOOLS)


def load_authored_tools() -> List[Dict[str, Any]]:
    if AUTHORED_STORE.exists():
        try:
            return json.loads(AUTHORED_STORE.read_text(encoding="utf-8")).get("tools", [])
        except Exception:
            return []
    return []


def save_authored_tools(tools: List[Dict[str, Any]]) -> None:
    AUTHORED_STORE.write_text(json.dumps({"tools": tools}, indent=2), encoding="utf-8")


def all_tools() -> List[Dict[str, Any]]:
    """Registry tools + authored tools, de-duplicated by name (authored wins)."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for t in load_engine_registry_tools():
        by_name[t["name"]] = t
    for t in _AUTHORED:  # in-memory authored list (also persisted)
        by_name[t["name"]] = t
    return list(by_name.values())


def find_tool(name: str) -> Optional[Dict[str, Any]]:
    for t in all_tools():
        if t.get("name") == name:
            return t
    return None


# in-memory authored registry (seeded from disk at startup)
_AUTHORED: List[Dict[str, Any]] = load_authored_tools()


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    tool: str
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"


class AuthorRequest(BaseModel):
    description: str


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
app = FastAPI(title="Leaf Web Demo — Lane D backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # permissive for localhost dev
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "aps_live": APS_LIVE,
        "data_file_present": DATA_FILE.exists(),
        "engine_registry_present": ENGINE_REGISTRY.exists(),
        "da_client_present": (DA_DIR / "client.py").exists(),
        "n_tools": len(all_tools()),
        "n_authored": len(_AUTHORED),
    }


@app.get("/api/session")
def session(dwg: str = "rooftop_demo") -> Dict[str, Any]:
    """Return the Intake JSON (contract section 1). APS_LIVE=0 -> cached sample."""
    if APS_LIVE:
        da = get_da_client()
        if da is None or not hasattr(da, "extract"):
            raise HTTPException(500, "APS_LIVE=1 but da/client.py (Lane A) is not importable")
        try:
            # root/Lane A owns the actual dwg path resolution; use the known sample path
            local = str(DATA_FILE.parent / f"{dwg}.dwg")
            return {"intake": da.extract(local)}
        except Exception as exc:
            raise HTTPException(502, f"DA extract failed: {exc}")
    if not DATA_FILE.exists():
        raise HTTPException(404, f"cached intake not found: {DATA_FILE}")
    return {"intake": load_cached_intake()}


@app.get("/api/tools")
def tools() -> Dict[str, Any]:
    return {"tools": all_tools()}


@app.post("/api/run")
def run(req: RunRequest) -> Dict[str, Any]:
    """Execute a tool and return the Result envelope (contract section 3)."""
    tool = find_tool(req.tool)
    if tool is None:
        raise HTTPException(404, f"unknown tool: {req.tool}")

    # merge authored default_params under caller params
    params = dict(tool.get("default_params", {}))
    params.update(req.params or {})

    t0 = time.perf_counter()

    if APS_LIVE:
        da = get_da_client()
        if da is None or not hasattr(da, "run_tool"):
            raise HTTPException(500, "APS_LIVE=1 but da/client.py (Lane A) is not importable")
        try:
            local = str(DATA_FILE.parent / f"{req.dwg}.dwg")
            env = da.run_tool(local, tool, params)
            # trust Lane A's envelope; ensure required keys exist
            return _normalize_envelope(env, tool, t0)
        except Exception as exc:
            raise HTTPException(502, f"DA run_tool failed: {exc}")

    # APS_LIVE=0 — pure python. Prefer engine.selfcheck if it exposes a runner.
    intake = load_cached_intake()
    engine_op = tool.get("engine_op", "")
    result: Dict[str, Any]
    overlay: Optional[Dict[str, Any]]

    sc = get_engine_selfcheck()
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


@app.post("/api/author")
def author(req: AuthorRequest) -> Dict[str, Any]:
    """Generate a tool package from a description, register it so it appears in
    /api/tools, and return {tool, code, preview} (contract section 4)."""
    use_llm = os.environ.get("LEAF_AUTHOR_LLM", "0") == "1"
    tool, code, preview = fb.author_tool(req.description)
    if use_llm:
        # Real LLM authoring is optional and gated. No key wired here; we fall
        # back to templating and note it in the preview rather than fail.
        preview = "[LLM gate on, but no provider wired — templated] " + preview

    # register (in-memory + persisted to our lane's store; additive)
    _AUTHORED[:] = [t for t in _AUTHORED if t["name"] != tool["name"]]
    _AUTHORED.append(tool)
    save_authored_tools(_AUTHORED)

    return {"tool": tool, "code": code, "preview": preview}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8130)
