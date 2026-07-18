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
WRITE_TOOLS_STORE = SERVER_DIR / "write_tools.json"  # tracked server-lane seed for drawing.write tools (M2)

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


def load_seed_write_tools() -> List[Dict[str, Any]]:
    """Tracked server-lane seed tools (drawing.write, M2). Kept separate from the
    engine registry (Lane B) and the gitignored authored store so a fresh checkout
    always resolves the write tool by name via /api/run."""
    if WRITE_TOOLS_STORE.exists():
        try:
            tools = json.loads(WRITE_TOOLS_STORE.read_text(encoding="utf-8")).get("tools", [])
            return [t for t in tools if isinstance(t, dict) and t.get("name")]
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[leaf-demo] bad write_tools.json: {exc}", file=sys.stderr)
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
    for t in load_seed_write_tools():  # tracked drawing.write seed (M2)
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
# tenant identity  (owned by `auth0-identity-signup`)
#
# Two behaviors, selected by LEAF_AUTH_LIVE (read at call time, default off):
#
#   OFF (default) -> BYTE-IDENTICAL legacy: tenant identity is the `X-Tenant-Id`
#                    header stub (default `demo-tenant`) that the jobs/broker
#                    chain relies on. `require_tenant` returns the plain str.
#                    PyJWT is never imported; no `Authorization` is required.
#
#   ON (=1)       -> `require_tenant` VERIFIES an Auth0 RS256 Bearer token
#                    (auth.py), extracts the namespaced tenant claim, resolves a
#                    workspace (tenancy.py), and returns a `TenantContext`.
#                    The verified JWT claim SUPERSEDES the X-Tenant-Id header.
#                    No token -> 401; verified-but-no-tenant-claim -> 403.
#
# `TenantContext` subclasses `str` and its string value IS the tenant_id, so
# every legacy consumer (jobs.submit_job, the broker ledger, SQLite TEXT binds,
# `==`/dict-key/json.dumps) keeps working unchanged in live mode too. This is
# why adding `Depends(require_tenant)` to a router is a ONE-LINE change with no
# downstream ripple. See contract/AUTH.md.
# --------------------------------------------------------------------------- #
DEFAULT_TENANT = "demo-tenant"


class TenantContext(str):
    """Tenant identity that IS its `tenant_id` string (so legacy str consumers
    keep working byte-for-byte) while also carrying the resolved org_id / tier /
    workspace for the auth-live response echo. Only produced when auth is live."""

    tenant_id: str
    org_id: Optional[str]
    tier: Optional[str]
    workspace: Optional[str]

    def __new__(cls, tenant_id: str, org_id: Optional[str] = None,
                tier: Optional[str] = None, workspace: Optional[str] = None) -> "TenantContext":
        obj = super().__new__(cls, tenant_id)
        obj.tenant_id = str(tenant_id)
        obj.org_id = org_id
        obj.tier = tier
        obj.workspace = workspace
        return obj


def auth_live() -> bool:
    """LEAF_AUTH_LIVE gate. Read at call time so a single process can be toggled
    in tests and subprocess env overrides apply."""
    return os.environ.get("LEAF_AUTH_LIVE", "0") == "1"


def require_tenant(
    x_tenant_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """FastAPI dependency: resolve the calling tenant.

    OFF: returns the plain `x_tenant_id or DEFAULT_TENANT` string (unchanged).
    ON:  returns a verified `TenantContext` (raises 401 no-token / 403 no-claim).
    """
    if not auth_live():
        # BYTE-IDENTICAL legacy path — X-Tenant-Id header stub, plain str.
        return x_tenant_id or DEFAULT_TENANT

    # LEAF_AUTH_LIVE=1: verified JWT claims win over the header stub.
    # Imported lazily so PyJWT is only needed when auth is actually live.
    import auth  # noqa: PLC0415
    import tenancy  # noqa: PLC0415

    payload = auth.verify_platform_token(authorization)   # -> 401 on bad/absent token
    claims = auth.extract_tenant_claims(payload)           # -> 403 if tenant claim absent
    ws = tenancy.get_store().resolve_workspace(claims["tenant_id"])
    return TenantContext(
        claims["tenant_id"],
        org_id=claims.get("org_id"),
        tier=claims.get("tier"),
        workspace=ws.workspace_dir if ws is not None else None,
    )


def tenant_echo(body: Dict[str, Any], tenant: Any) -> Dict[str, Any]:
    """Additively echo the resolved tenant identity into a success response body
    WHEN auth is live. No-op when off (tenant is a plain str) -> byte-identical
    legacy body. Used by routers/session.py and, at wave integration, by
    routers/jobs.py for /api/run."""
    if isinstance(tenant, TenantContext):
        out = dict(body)
        out["tenant_id"] = tenant.tenant_id
        out["org_id"] = tenant.org_id
        return out
    return body
