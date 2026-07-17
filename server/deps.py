"""
Shared dependencies / composition seam for the Leaf server (all routers import this).

Owns: path constants, sibling-lane import wiring, APS_LIVE flag, tool-registry
loading (engine registry + authored tools), and the tenant-identity stub.

Sibling-session ownership (see README.md): `auth0-identity-signup` owns
`require_tenant` and will replace the header stub with real identity.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Header

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
    """Lane A's da/client.py. Required only when APS_LIVE=1.

    NOTE (broker boundary): only legacy sync paths may use this; the async job
    spine goes through broker_client and never touches da.* in this process.
    """
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


# in-memory authored registry (seeded from disk at startup).
# IDENTITY MATTERS: routers mutate this list in place (`_AUTHORED[:] = ...`).
_AUTHORED: List[Dict[str, Any]] = load_authored_tools()


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


# --------------------------------------------------------------------------- #
# tenant identity (v1 stub — `auth0-identity-signup` session replaces this)
# --------------------------------------------------------------------------- #
DEFAULT_TENANT = "demo-tenant"


def require_tenant(x_tenant_id: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: tenant identity from the X-Tenant-Id header stub."""
    return x_tenant_id or DEFAULT_TENANT
