"""
Shared dependencies / composition seam for the Leaf server (all routers import this).

Owns: path constants, sibling-lane import wiring, APS_LIVE flag, tool-registry
loading (engine registry + authored tools), and the tenant-identity stub.

Sibling-session ownership (see README.md): `auth0-identity-signup` owns
`require_tenant` and will replace the header stub with real identity.
"""
from __future__ import annotations

import hmac
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Header, Request

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

import platform as _stdlib_platform  # noqa: F401,E402  (cache stdlib before the
#   PROJECT_ROOT sys.path insert below makes the local platform/ package shadow it;
#   jsonschema imports `platform` lazily inside agent_policy's schema validation,
#   and the shadow turns every gate call into policy_load_failed. Same guard as
#   broker.py — this process needs it too.

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


# --------------------------------------------------------------------------- #
# per-tenant mushy-repo registry fold (UI wave 3 Contract 2; wave 4 multi-tenant)
# --------------------------------------------------------------------------- #
from tenant_paths import DEFAULT_TENANT as _DEFAULT_TENANT  # noqa: E402
from tenant_paths import resolve_tenant_repo_dir  # noqa: E402


def tenant_repo_dir(tenant_id: str = _DEFAULT_TENANT) -> Optional[Path]:
    """The mushy-codebase checkout root for ONE tenant, from ``$LEAF_TENANTS_DIR/<tid>``
    (wave 4), with ``$LEAF_TENANT_REPO`` as the demo-tenant back-compat override. Read
    at CALL time so a subprocess/test override applies. None => the tenant fold is OFF
    for this tenant and all_tools(tid) is byte-identical to the pre-wave-3 union."""
    return resolve_tenant_repo_dir(tenant_id)


def load_tenant_repo_tools(tenant_id: str = _DEFAULT_TENANT) -> List[Dict[str, Any]]:
    """Fold ONE tenant's registry.json tools when that tenant's repo resolves.

    These are the tools a real harness author session registered into the tenant's
    OWN repo (``registry.json`` at the repo root — see harness/registry/registerTool).
    Their ``entry`` stays RELATIVE to the repo root (e.g. ``tools/<name>/tool.py``);
    ``tool_loader.resolve_local_file`` resolves it against the SAME per-tenant root,
    so the execution chain no longer depends on the broker's cwd (the M1 hack), and
    tenant A's authored tools never leak into tenant B's catalog. Missing env / dir /
    file / bad JSON -> [] (never raises)."""
    root = tenant_repo_dir(tenant_id)
    if root is None:
        return []
    reg = root / "registry.json"
    if not reg.exists():
        return []
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[leaf-demo] bad tenant registry.json at {reg}: {exc}", file=sys.stderr)
        return []


# in-memory authored registry (seeded from disk at startup).
# IDENTITY MATTERS: routers mutate this list in place (`_AUTHORED[:] = ...`).
_AUTHORED: List[Dict[str, Any]] = load_authored_tools()


def all_tools(tenant_id: str = _DEFAULT_TENANT) -> List[Dict[str, Any]]:
    """Registry tools + THIS TENANT's repo tools + write seed + authored tools, de-duped
    by name. PRECEDENCE (last wins): engine registry < tenant-repo (per tenant) <
    write seed < authored_tools.json — so a tenant-repo tool overrides an engine tool
    of the same name, and an authored tool overrides both (CONTRACT-ADDENDUM §15).

    TENANT SCOPE (wave 4): only the tenant-repo fold is per-tenant — the engine
    registry, write seed, and the process-global authored store are GLOBAL (visible to
    every tenant). So tenant A's harness-authored tools (in A's repo) are invisible to
    tenant B, while shared globals are visible to both. With no tenant repo resolvable
    (neither $LEAF_TENANTS_DIR nor $LEAF_TENANT_REPO set) the tenant fold is empty and
    this is BYTE-IDENTICAL to the pre-wave-3 union."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for t in load_engine_registry_tools():
        by_name[t["name"]] = t
    for t in load_tenant_repo_tools(tenant_id):  # the REQUESTING tenant's OWN registry
        by_name[t["name"]] = t
    for t in load_seed_write_tools():  # tracked drawing.write seed (M2)
        by_name[t["name"]] = t
    for t in _AUTHORED:  # in-memory authored list (also persisted)
        by_name[t["name"]] = t
    return list(by_name.values())


def find_tool(name: str, tenant_id: str = _DEFAULT_TENANT) -> Optional[Dict[str, Any]]:
    for t in all_tools(tenant_id):
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


# --------------------------------------------------------------------------- #
# harness back-edge trust (wire contract §0): with LEAF_AUTH_LIVE=1 the spine's
# AppRunClient authenticates with X-Dispatch-Secret + X-Tenant-Id instead of a
# JWT — same trust model as the broker's X-Broker-Secret — but ONLY on the
# contract-listed read/dispatch routes. Secret unset in env => back-edge
# disabled => the ordinary JWT path (which 401s without a token).
# --------------------------------------------------------------------------- #
def _dispatch_backedge_route(method: str, path: str) -> bool:
    """True iff (method, path) is one of the §0 back-edge routes: POST /api/run,
    GET /api/jobs/{id}, GET /api/capabilities, GET /api/tools, GET /api/drawings/*."""
    if method == "POST":
        return path == "/api/run"
    if method != "GET":
        return False
    if path in ("/api/capabilities", "/api/tools"):
        return True
    if path.startswith("/api/drawings/"):
        return True
    if path.startswith("/api/jobs/"):
        rest = path[len("/api/jobs/"):]
        return bool(rest) and "/" not in rest  # the job read only, not /stream etc.
    return False


def backedge_tier(tenant_id: str) -> Optional[str]:
    """The back-edge caller's tier from the SAME broker-TRUSTED sources the broker's
    own re-check uses (broker.py:363-376): the persisted broker tenants record
    (`$BROKER_TENANTS`, default server/broker_tenants.json), then
    `$LEAF_BROKER_TENANT_TIERS` ({tenant_id: tier}). Never the request body — a
    back-edge caller could forge it. Read at call time. Returns None when the
    tenant has no provisioned tier at all; returns the raw (possibly empty) string
    when it does, so `entitlements.resolve_tier` applies its own fail-closed rule."""
    path = Path(os.environ.get("BROKER_TENANTS") or (SERVER_DIR / "broker_tenants.json"))
    # ABSENT primary -> the operator has not provisioned this source, so the env
    # map is a legitimate fallback. PRESENT-but-corrupt is NOT the same fact:
    # falling through there let a stale LEAF_BROKER_TENANT_TIERS promote a
    # downgraded tenant (truncated file + stale env -> hosted_pro instead of
    # restricted). A primary we cannot read means the tier is UNKNOWN, and an
    # unknown tier must resolve restricted rather than inherit a secondary.
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except OSError:
        return None  # present but unreadable -> unknown -> resolve_tier fails closed
    except UnicodeDecodeError:
        # NOT an OSError (it subclasses ValueError), so it escaped as a 500.
        # Non-UTF-8 bytes are just another unreadable primary: unknown tier.
        return None
    if text is not None:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return None  # present but invalid -> unknown, never a stale fallback
        if not isinstance(raw, dict):
            return None
        if tenant_id in raw:
            rec = raw[tenant_id]
            if not isinstance(rec, dict):
                return None  # present-but-corrupt record -> unknown, fail closed
            if "tier" in rec:
                return str(rec.get("tier") or "")
    raw = os.environ.get("LEAF_BROKER_TENANT_TIERS")
    if raw:
        try:
            m = json.loads(raw)
        except Exception:  # noqa: BLE001
            m = None
        if isinstance(m, dict) and tenant_id in m:
            return str(m.get(tenant_id) or "")
    return None


def backedge_tenant(tenant_id: str) -> Any:
    """The tenant identity for a verified back-edge call.

    OFF-auth: the plain str (byte-identical legacy — the whole platform is the
    open demo, so the gate resolving "demo" is the same answer every other route
    gives). LIVE: a `TenantContext` carrying the broker-trusted tier, so
    `entitlements.resolve_tier` sees a real rung instead of defaulting a bare
    string to "demo". No provisioned tier -> `tier=None` -> "restricted"
    (fail CLOSED: an unresolvable tier must never authorize as demo)."""
    if not auth_live():
        return tenant_id
    return TenantContext(tenant_id, tier=backedge_tier(tenant_id))


def _dispatch_secret_ok(presented: Optional[str]) -> bool:
    secret = os.environ.get("LEAF_APP_DISPATCH_SECRET", "").strip()
    if not secret or not presented:
        return False
    return hmac.compare_digest(presented, secret)


def require_tenant(
    request: Request,
    x_tenant_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_dispatch_secret: Optional[str] = Header(default=None),
):
    """FastAPI dependency: resolve the calling tenant.

    OFF: returns the plain `x_tenant_id or DEFAULT_TENANT` string (unchanged).
    ON:  returns a verified `TenantContext` (raises 401 no-token / 403 no-claim),
         EXCEPT the harness back-edge: a valid `X-Dispatch-Secret` on a §0-listed
         route trusts `X-Tenant-Id` as the resolved tenant, carried as a
         `TenantContext` whose tier comes from the broker-trusted source
         (`backedge_tenant`) — never a bare str, which would resolve to "demo"
         and hand a downgraded tenant every capability.
    """
    if not auth_live():
        # BYTE-IDENTICAL legacy path — X-Tenant-Id header stub, plain str.
        return x_tenant_id or DEFAULT_TENANT

    if (x_dispatch_secret is not None and x_tenant_id
            and _dispatch_backedge_route(request.method, request.url.path)
            and _dispatch_secret_ok(x_dispatch_secret)):
        return backedge_tenant(x_tenant_id)

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
        out["tier"] = tenant.tier
        return out
    return body
