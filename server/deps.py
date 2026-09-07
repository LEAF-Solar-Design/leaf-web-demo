"""
Shared dependencies / composition seam for the Leaf server (all routers import this).

Owns: path constants, sibling-lane import wiring, APS_LIVE flag, tool-registry
loading (engine registry + authored tools), and the tenant-identity stub.

Sibling-session ownership (see README.md): `auth0-identity-signup` owns
`require_tenant` and will replace the header stub with real identity.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, Header, Request, HTTPException


def require_campaign_worker(request: Request):
    """Always verify the service bearer, including when tenant auth is disabled."""
    import auth
    payload = auth.verify_platform_token(request.headers.get('Authorization'))
    subject = os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT', '')
    if not subject or payload.get('sub') != subject:
        raise HTTPException(status_code=403, detail='Campaign worker is not authorized')
    return subject

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
CATALOG_TOOLS_STORE = SERVER_DIR / "catalog_tools.json"  # tracked server-lane seed for GENERAL
# (non-write) tools whose only reachable implementation is a server/builtins/*.py file that
# engine/registry.json (Lane B) does not itself carry — e.g. autofill-string-targets. Folded
# the SAME way as WRITE_TOOLS_STORE so /api/run resolves them by name regardless of what the
# engine registry happens to contain.

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


def load_seed_catalog_tools() -> List[Dict[str, Any]]:
    """Tracked server-lane seed (catalog_tools.json) for general read-class tools
    that need to resolve via deps.find_tool regardless of engine/registry.json's
    own contents (Lane B owns that file; this repo lane does not edit it). Kept
    separate from WRITE_TOOLS_STORE, which is documented + used for drawing.write
    tools specifically."""
    if CATALOG_TOOLS_STORE.exists():
        try:
            tools = json.loads(CATALOG_TOOLS_STORE.read_text(encoding="utf-8")).get("tools", [])
            return [t for t in tools if isinstance(t, dict) and t.get("name")]
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[leaf-demo] bad catalog_tools.json: {exc}", file=sys.stderr)
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
    file / bad JSON -> [] (never raises).

    The call-time read itself is the vendored mushy-fold core (mushy-code
    extraction, 2026-08-06); tenant resolution above it stays this repo's, and
    so does the malformed-registry stderr diagnostic (injected via on_error so
    operational log classification is unchanged — PR #474 review, P2)."""
    from _vendor.mushy_fold.registry import load_repo_registry_tools

    def _bad_registry(reg: Path, exc: Exception) -> None:
        print(f"[leaf-demo] bad tenant registry.json at {reg}: {exc}", file=sys.stderr)

    return load_repo_registry_tools(tenant_repo_dir(tenant_id), on_error=_bad_registry)


# --------------------------------------------------------------------------- #
# per-tenant surface-config overlay fold (standardization slice 7b)
# --------------------------------------------------------------------------- #

# 30s: long enough that a route-matrix burst against one tenant folds the
# file once, short enough that an author's just-committed surface-config.json
# is visible within one polling cycle with no deploy. Bounds the hot-path cost
# to one stat+parse per tenant per window instead of one per request.
SURFACE_CONFIG_CACHE_TTL_SECONDS = 30.0
_surface_config_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# One stderr warning per tenant per process, not per request: a tenant whose
# repo carries a permanently broken overlay must not spam the log on every
# poll. Bounded so an unbounded stream of distinct tenant ids (never expected
# in practice — tenants are provisioned, not attacker-controlled cardinality)
# cannot grow this set for the life of the process.
_MAX_SURFACE_CONFIG_WARNED_TENANTS = 10_000
_surface_config_warned_tenants: set = set()


def _contained_tenant_root(tenant_id: Any) -> Optional[str]:
    """The tenant's repo root for the surface-config reads, or None.

    tenant_repo_dir already runs tenant_paths._safe_component; the literal
    fullmatch here restates tenant_id_validator's canonical rule at this read
    site the way tenant_paths._safe_component does (pinned equal to it by
    server/tests/test_codeql_barrier_literals.py; it is not a second rule).
    The read itself is contained by _contained_surface_config_path below:
    the FILE, not only the tenant id, is measured against the root."""
    tid = str(tenant_id).strip() if tenant_id is not None else ""
    if not tid:
        tid = _DEFAULT_TENANT
    m = re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", tid)
    if m is None:
        return None
    root = tenant_repo_dir(m.group(0))
    return None if root is None else str(root)


_SURFACE_CONFIG_FILE = "surface-config.json"


def _contained_surface_config_path(root: str) -> Optional[str]:
    """The normalised real path of ``<root>/surface-config.json`` when that is
    exactly where the file lives INSIDE ``root``, else None.

    os.path.normpath over os.path.realpath for both the root and the file,
    then a prefix check against the root plus a separator (the shape of
    tool_loader._contained_published_path), then equality with the root's
    own ``surface-config.json`` path: a symlink under that name is refused
    whatever it points at, so nothing outside the tenant repo is ever read
    and nothing inside it is read under another name. Fails closed to None
    on any OS error. Absence is NOT refused: a missing file normalises to
    the root's own path and the callers fold "absent" themselves."""
    try:
        base = os.path.normpath(os.path.realpath(root))
        real = os.path.normpath(
            os.path.realpath(os.path.join(root, _SURFACE_CONFIG_FILE))
        )
    except OSError:
        return None
    if not real.startswith(base + os.sep):
        return None
    if real != os.path.join(base, _SURFACE_CONFIG_FILE):
        return None
    return real


def effective_surface_config(tenant_id: str = _DEFAULT_TENANT) -> Dict[str, Any]:
    """Fold ONE tenant's surface-config.json overlay when that tenant's repo
    resolves, through the SAME tenant_repo_dir this file's registry fold uses.

    Returns ONLY the validated overlay — never a server-side copy of the
    contract defaults, which stay owned by web/src/site/productSurfaces.js;
    the web merges. Fails closed to `{}` on any loader error (missing repo,
    missing file, oversize, bad JSON, unknown surface id/slot): the vendored
    reader (mushy-code extraction, slice 7b) already enforces the closed
    schema vocabulary and never raises past this call.

    Cached per tenant for SURFACE_CONFIG_CACHE_TTL_SECONDS (bounded TTL, see
    the constant above)."""
    tenant_key = str(tenant_id)
    now = time.monotonic()
    cached = _surface_config_cache.get(tenant_key)
    if cached is not None and now - cached[0] < SURFACE_CONFIG_CACHE_TTL_SECONDS:
        return cached[1]

    from _vendor.mushy_fold.surface_config import load_repo_surface_config

    def _bad_surface_config(cfg: Path, exc: Exception) -> None:
        if (tenant_key not in _surface_config_warned_tenants
                and len(_surface_config_warned_tenants) < _MAX_SURFACE_CONFIG_WARNED_TENANTS):
            _surface_config_warned_tenants.add(tenant_key)
            print(f"[leaf-demo] bad tenant surface-config.json at {cfg}: {exc}",
                  file=sys.stderr)

    root = _contained_tenant_root(tenant_id)
    real = None if root is None else _contained_surface_config_path(root)
    if root is not None and real is None:
        # A surface-config.json that resolves outside the tenant repo, or is
        # a symlink under that name, is refused BEFORE the vendored reader
        # is handed a root; it then sees no root and folds to {}.
        _bad_surface_config(
            Path(root) / _SURFACE_CONFIG_FILE,
            ValueError("surface-config.json resolves outside the tenant repo"),
        )
    overlay = load_repo_surface_config(
        None if real is None else Path(os.path.dirname(real)),
        on_error=_bad_surface_config,
    )
    _surface_config_cache[tenant_key] = (now, overlay)
    return overlay


def surface_config_source(tenant_id: str = _DEFAULT_TENANT) -> Optional[Dict[str, Any]]:
    """`{sha256, authored_at}` of the tenant's RAW surface-config.json file,
    independent of whether it validated: identity of the committed file, not
    a claim about its content (the `submitSurfaceConfig` author-tool receipt
    stamps the same sha256 at commit time). `None` when the tenant's repo
    does not resolve, the file is absent or a symlink, or it cannot be read — the route
    folds all of these into its own "no source" branch."""
    root = _contained_tenant_root(tenant_id)
    if root is None:
        return None
    real = _contained_surface_config_path(root)
    if real is None:
        return None
    cfg = Path(real)
    try:
        if not cfg.exists():
            return None
        blob = cfg.read_bytes()
    except OSError:
        return None
    return {
        "sha256": hashlib.sha256(blob).hexdigest(),
        "authored_at": datetime.fromtimestamp(
            cfg.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def submit_surface_config(tenant_id: str, overlay: dict) -> Dict[str, Any]:
    """Commit a fold-valid overlay and return the fresh file provenance.

    Serialization is UTF-8 JSON, two-space indentation and a final LF. The
    vendored fold's file limit also applies so every accepted write is readable.
    """
    from _vendor.mushy_fold.surface_config import MAX_SURFACE_CONFIG_BYTES, _valid_overlay
    from tenant_id_validator import is_valid_tenant_id

    if not isinstance(tenant_id, str) or not is_valid_tenant_id(tenant_id):
        raise HTTPException(status_code=400, detail="Invalid tenant identity")
    if not _valid_overlay(overlay):
        raise HTTPException(status_code=400, detail="surface-config.json failed schema validation")
    try:
        blob = (json.dumps(overlay, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(blob) > MAX_SURFACE_CONFIG_BYTES:
        raise HTTPException(status_code=413, detail=f"surface-config.json exceeds {MAX_SURFACE_CONFIG_BYTES} bytes")
    root = _contained_tenant_root(tenant_id)
    target = None if root is None else _contained_surface_config_path(root)
    if target is None:
        raise HTTPException(status_code=400, detail="surface-config.json target is unavailable or not contained")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(target), delete=False) as fh:
            temporary = fh.name
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        if _contained_surface_config_path(root) != target:
            raise HTTPException(status_code=400, detail="surface-config.json target is not contained")
        os.replace(temporary, target)
        temporary = None
        _surface_config_cache.pop(tenant_id, None)
        receipt = surface_config_source(tenant_id)
        if receipt is None:
            raise HTTPException(status_code=503, detail="Surface config was written but its receipt is unavailable")
        return receipt
    finally:
        if temporary is not None:
            os.unlink(temporary)


# in-memory authored registry (seeded from disk at startup).
# IDENTITY MATTERS: routers mutate this list in place (`_AUTHORED[:] = ...`).
_AUTHORED: List[Dict[str, Any]] = load_authored_tools()


class ToolCatalogCollisionError(RuntimeError):
    """Raised when the SAME tool name is defined by more than one GLOBAL catalog
    tier that has no documented override relationship with the others."""


class ToolCatalogProvenanceError(RuntimeError):
    """Raised when source provenance cannot be established unambiguously."""


TOOL_SOURCE_OPERATOR_OWNED_ENGINE = "operator_owned_engine"
TOOL_SOURCE_CATALOG_SEED = "catalog_seed"
TOOL_SOURCE_TENANT_REPO = "tenant_repo"
TOOL_SOURCE_WRITE_SEED = "write_seed"
TOOL_SOURCE_AUTHORED = "authored"
EFFECTIVE_TOOL_SOURCE_PRECEDENCE = (
    TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
    TOOL_SOURCE_CATALOG_SEED,
    TOOL_SOURCE_TENANT_REPO,
    TOOL_SOURCE_WRITE_SEED,
    TOOL_SOURCE_AUTHORED,
)


def _check_global_seed_collisions(tiers: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Fail LOUDLY (raise) on a name collision across the GLOBAL, non-per-tenant,
    non-authored catalog tiers (engine registry, general-catalog seed, write seed).

    These three tiers are maintained independently (Lane B owns engine/registry.json;
    this lane owns catalog_tools.json / write_tools.json) and have NO documented
    override relationship with EACH OTHER — unlike tenant-repo and authored_tools.json,
    which are INTENTIONALLY the override tiers (CONTRACT-ADDENDUM §15; see
    tests/test_wave3.py's Contract 2 fold-precedence tests, which rely on a
    tenant-repo/authored tool DELIBERATELY shadowing a same-named engine tool). Those
    two tiers are therefore excluded from this check — this only guards the tiers
    where a same-name collision could only ever be an accident, and where Python
    dict-insertion order would otherwise silently decide which tool's capabilities
    (read vs write, params schema, entry) win.

    Review 2026-07-22 (LOW, cheap-to-fix): added so a future name clash cannot
    silently swap capabilities.
    """
    owner: Dict[str, str] = {}
    for tier_name, tools in tiers:
        for t in tools:
            name = t.get("name")
            if not name:
                continue
            prior = owner.get(name)
            if prior is not None and prior != tier_name:
                raise ToolCatalogCollisionError(
                    f"tool name collision across GLOBAL catalog tiers: {name!r} is "
                    f"defined in both {prior!r} and {tier_name!r}; these tiers have no "
                    f"documented override precedence over each other (unlike "
                    f"tenant-repo/authored_tools.json, which intentionally shadow) — "
                    f"rename one of them.")
            owner.setdefault(name, tier_name)


def _global_tool_tiers() -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Load and validate the process-global catalog tiers.

    Keep this separate from tenant resolution so process health can inspect the
    shipped catalog without selecting a tenant or requiring an effective tenant
    catalog to have been published.
    """
    tiers = [
        ("engine_registry", load_engine_registry_tools()),
        ("catalog_seed", load_seed_catalog_tools()),
        ("write_seed", load_seed_write_tools()),
    ]
    _check_global_seed_collisions(tiers)
    return tiers


def shared_tools() -> List[Dict[str, Any]]:
    """Return only the process-global catalog used by every tenant."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for _tier_name, tools in _global_tool_tiers():
        for tool in tools:
            by_name[tool["name"]] = tool
    return list(by_name.values())


def _ordered_effective_tool_tiers(
    tiers_by_source: Dict[str, List[Dict[str, Any]]],
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Apply the one precedence specification shared by every effective fold."""
    return [
        (source, tiers_by_source[source])
        for source in EFFECTIVE_TOOL_SOURCE_PRECEDENCE
    ]


def _fold_effective_tool_tiers(
    tiers: List[Tuple[str, List[Dict[str, Any]]]],
    tenant_id: str,
    *,
    strict_provenance: bool = False,
) -> List[Tuple[Dict[str, Any], str]]:
    """Fold effective rows once, retaining the winning tier for projection."""
    if tuple(source for source, _tools in tiers) != EFFECTIVE_TOOL_SOURCE_PRECEDENCE:
        raise ToolCatalogProvenanceError(
            "tool source order does not match the catalog fold contract")
    by_name: Dict[str, Tuple[Dict[str, Any], str]] = {}
    for source, tools in tiers:
        for tool in tools:
            if source == TOOL_SOURCE_AUTHORED:
                owner = (
                    tool["tenant_id"]
                    if "tenant_id" in tool
                    else _DEFAULT_TENANT
                )
                if strict_provenance and (
                    not isinstance(owner, str) or not owner.strip()
                ):
                    raise ToolCatalogProvenanceError(
                        "authored tool contains an invalid tenant_id")
                if owner != tenant_id:
                    continue
            by_name[tool["name"]] = (tool, source)
    return list(by_name.values())


def _compatibility_effective_tool_tiers(
    tenant_id: str,
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Build tiers through the existing forgiving loaders used by all_tools."""
    global_tiers = _global_tool_tiers()
    return _ordered_effective_tool_tiers({
        TOOL_SOURCE_OPERATOR_OWNED_ENGINE: global_tiers[0][1],
        TOOL_SOURCE_CATALOG_SEED: global_tiers[1][1],
        TOOL_SOURCE_TENANT_REPO: load_tenant_repo_tools(tenant_id),
        TOOL_SOURCE_WRITE_SEED: global_tiers[2][1],
        TOOL_SOURCE_AUTHORED: _AUTHORED,
    })


def all_tools(tenant_id: str = _DEFAULT_TENANT) -> List[Dict[str, Any]]:
    """Registry tools + general-catalog seed + THIS TENANT's repo tools + write seed +
    authored tools, de-duped by name. PRECEDENCE (last wins): engine registry <
    general-catalog seed < tenant-repo (per tenant) < write seed < authored_tools.json —
    so a tenant-repo tool overrides an engine/catalog-seed tool of the same name, and an
    authored tool overrides all of them (CONTRACT-ADDENDUM §15).

    TENANT SCOPE (wave 4): only the tenant-repo fold is per-tenant — the engine
    registry, general-catalog seed, write seed, and the process-global authored store
    are GLOBAL (visible to every tenant). So tenant A's harness-authored tools (in A's
    repo) are invisible to tenant B, while shared globals are visible to both. With no
    tenant repo resolvable (neither $LEAF_TENANTS_DIR nor $LEAF_TENANT_REPO set) the
    tenant fold is empty and this is BYTE-IDENTICAL to the pre-wave-3 union (plus the
    additive general-catalog seed).

    Raises ``ToolCatalogCollisionError`` if the SAME name is defined by more than one
    of the three GLOBAL, no-override-relationship tiers (engine registry / general-
    catalog seed / write seed) — see ``_check_global_seed_collisions``. Tenant-repo and
    authored_tools.json are excluded from that check: they are the DOCUMENTED override
    tiers and a same-name collision there is the intended shadowing behaviour, not a bug."""
    folded = _fold_effective_tool_tiers(
        _compatibility_effective_tool_tiers(tenant_id), tenant_id)
    return [tool for tool, _source in folded]


def _provenance_rows(
    source: str, tools: Any,
) -> List[Tuple[Dict[str, Any], str]]:
    """Validate one tier before it can make an authority claim.

    A duplicate inside one tier has no documented precedence. Reject it instead
    of letting list order silently decide which same-source row is authoritative.
    Cross-tier tenant/authored shadows remain valid and are resolved by the
    existing last-wins catalog order below.
    """
    if not isinstance(tools, list):
        raise ToolCatalogProvenanceError(
            f"tool source {source!r} is not a list")
    rows: List[Tuple[Dict[str, Any], str]] = []
    names = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ToolCatalogProvenanceError(
                f"tool source {source!r} contains a non-object row")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolCatalogProvenanceError(
                f"tool source {source!r} contains a row without a valid name")
        if name in names:
            raise ToolCatalogProvenanceError(
                f"tool source {source!r} contains duplicate name {name!r}")
        names.add(name)
        rows.append((tool, source))
    return rows


def _strict_store_tools(
    path: Path, source: str, *, missing: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Read an unfiltered catalog store for an authority decision.

    The compatibility loaders intentionally drop malformed rows or degrade a
    malformed file to an empty tier. That behavior remains correct for
    ``all_tools``. Provenance cannot use those projections because a dropped
    override would make a lower engine row appear operator-owned.
    """
    if not path.exists():
        return list(missing or [])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ToolCatalogProvenanceError(
            f"could not read tool source {source!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise ToolCatalogProvenanceError(
            f"tool source {source!r} does not contain a tools list")
    tools = data["tools"]
    if source == TOOL_SOURCE_OPERATOR_OWNED_ENGINE and not tools:
        return list(missing or [])
    return tools


def _strict_provenance_tiers(
    tenant_id: str,
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    engine_tools = _strict_store_tools(
        ENGINE_REGISTRY,
        TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
        missing=list(fb.DEFAULT_TOOLS),
    )
    catalog_tools = _strict_store_tools(
        CATALOG_TOOLS_STORE, TOOL_SOURCE_CATALOG_SEED)
    tenant_root = tenant_repo_dir(tenant_id)
    tenant_tools = (
        _strict_store_tools(
            tenant_root / "registry.json", TOOL_SOURCE_TENANT_REPO)
        if tenant_root is not None
        else []
    )
    write_tools = _strict_store_tools(
        WRITE_TOOLS_STORE, TOOL_SOURCE_WRITE_SEED)
    authored_tools = _strict_store_tools(
        AUTHORED_STORE,
        TOOL_SOURCE_AUTHORED,
        missing=_AUTHORED,
    )
    tiers_by_source = {
        TOOL_SOURCE_OPERATOR_OWNED_ENGINE: engine_tools,
        TOOL_SOURCE_CATALOG_SEED: catalog_tools,
        TOOL_SOURCE_TENANT_REPO: tenant_tools,
        TOOL_SOURCE_WRITE_SEED: write_tools,
        TOOL_SOURCE_AUTHORED: authored_tools,
    }
    return _ordered_effective_tool_tiers(tiers_by_source)


def effective_tools_with_provenance(
    tenant_id: str = _DEFAULT_TENANT,
) -> List[Tuple[Dict[str, Any], str]]:
    """Return the effective catalog rows paired with their winning source tier.

    This is the authority-safe companion to ``all_tools``. Its tool projection
    and last-wins order are identical for a valid catalog, but the source label
    records the row that actually won. Callers deciding whether a tool is an
    operator-owned engine tool must require
    ``TOOL_SOURCE_OPERATOR_OWNED_ENGINE``. They must not infer ownership from a
    digest or definition equality because an override may be an exact copy.

    Tenant repositories also carry published customization catalogs, so their
    rows are intentionally classified as tenant overrides. Malformed rows and
    ambiguous same-tier duplicates fail closed.
    """
    raw_tiers = _strict_provenance_tiers(tenant_id)
    validated_tiers = {
        source: [tool for tool, _label in _provenance_rows(source, tools)]
        for source, tools in raw_tiers
    }
    _check_global_seed_collisions([
        (source, validated_tiers[source])
        for source in (
            TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
            TOOL_SOURCE_CATALOG_SEED,
            TOOL_SOURCE_WRITE_SEED,
        )
    ])
    return _fold_effective_tool_tiers(
        _ordered_effective_tool_tiers(validated_tiers),
        tenant_id,
        strict_provenance=True,
    )


def find_tool(name: str, tenant_id: str = _DEFAULT_TENANT) -> Optional[Dict[str, Any]]:
    for t in all_tools(tenant_id):
        if t.get("name") == name:
            return t
    return None


def catalog_tool_digest(tool: Dict[str, Any]) -> str:
    """Strong canonical digest issued by GET and required again by POST /api/run."""
    definition = {key: value for key, value in tool.items() if key != "catalog_digest"}
    encoded = json.dumps(
        definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def catalog_tool_view(tool: Dict[str, Any]) -> Dict[str, Any]:
    digest = catalog_tool_digest(tool)
    return {**tool, "catalog_digest": digest, "tool_manifest_sha256": digest}


def base_catalog_pin(tools: List[Dict[str, Any]]) -> Dict[str, str]:
    """Issue a stable generation pin when no published customization exists."""
    generated = {
        "catalog_digest", "tool_manifest_sha256", "catalog_commit",
        "effective_catalog_digest",
    }
    definitions = [
        {key: value for key, value in tool.items() if key not in generated}
        for tool in tools
    ]
    definitions.sort(key=lambda item: str(item.get("name", "")))
    encoded = json.dumps(
        definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "catalog_commit": hashlib.sha1(encoded).hexdigest(),  # noqa: S324
        "effective_catalog_digest": hashlib.sha256(encoded).hexdigest(),
    }


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
    subject: Optional[str]
    roles: tuple
    elevated: bool

    def __new__(cls, tenant_id: str, org_id: Optional[str] = None,
                tier: Optional[str] = None, workspace: Optional[str] = None,
                subject: Optional[str] = None,
                backedge: bool = False,
                authority_resolved: bool = False,
                roles: tuple = (),
                elevated: bool = False) -> "TenantContext":
        obj = super().__new__(cls, tenant_id)
        obj.tenant_id = str(tenant_id)
        obj.org_id = org_id
        obj.tier = tier
        obj.workspace = workspace
        obj.subject = subject
        # Verified role names from the `.../roles` claim (§11.5) + whether the
        # subject passed the LEAF_PLATFORM_ADMIN_SUBJECTS second factor. Only
        # the live JWT path stamps these; guests and back-edge identities stay
        # `(), False` — roles never ride an unverified channel.
        obj.roles = tuple(roles or ())
        obj.elevated = elevated is True
        # True ONLY for a verified dispatch-secret back edge. Origin must be
        # marked, never inferred from a missing subject: a JWT that carries no
        # `sub` also resolves to subject=None, and inferring would let such a
        # caller borrow the active turn's identity.
        obj.backedge = backedge
        # True only when this request already resolved the current server-owned
        # identity binding. This avoids a second authority lookup in
        # require_active_tenant and keeps one request on one tenant snapshot.
        obj.authority_resolved = authority_resolved
        return obj


# Spellings of LEAF_AUTH_LIVE that mean "authentication is ON". THE canonical
# set: platform_link.py's startup assertion already blessed 1/true/yes/on while
# this gate accepted only the exact "1", so `LEAF_AUTH_LIVE=true` passed the
# boot check and then left EVERY runtime auth gate off — a green app serving
# unauthenticated, with the ops surface open (no secret configured) instead of
# fail-closed 503. Both sides now read through auth_live() so they cannot drift.
_AUTH_LIVE_ON = frozenset({"1", "true", "yes", "on"})


def auth_live() -> bool:
    """LEAF_AUTH_LIVE gate. Read at call time so a single process can be toggled
    in tests and subprocess env overrides apply."""
    return os.environ.get("LEAF_AUTH_LIVE", "0").strip().lower() in _AUTH_LIVE_ON


def resolve_active_platform_tenant_authority(subject: Optional[str]) -> tuple[str, str]:
    """Resolve tenant identity and tier through one current server authority.

    JWT tenant and org claims are hints that can outlive an account move. The
    canonical upload namespace therefore comes only from the current platform
    identity binding. Missing bindings and unavailable binding authority fail
    closed instead of falling back to a signed claim.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(
            status_code=403,
            detail="verified subject has no active platform identity binding",
        )
    try:
        import platform_link  # noqa: PLC0415
        platform_store = platform_link.platform_store()
        binding = platform_store.resolve_active_identity_binding("auth0", subject)
        org = (platform_store.get_org(binding.platform_tenant_id)
               if binding is not None else None)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - authority failures must fail closed
        raise HTTPException(
            status_code=503,
            detail="platform identity binding authority is unavailable",
        ) from exc
    if binding is None or org is None or org.status != "active":
        raise HTTPException(
            status_code=403,
            detail="verified subject has no active platform tenant authority",
        )
    tier = org.tier if isinstance(org.tier, str) and org.tier.strip() else "restricted"
    return str(binding.platform_tenant_id), tier


def resolve_active_platform_tenant_id(subject: Optional[str]) -> str:
    """Compatibility wrapper for callers that need only current tenant identity."""
    return resolve_active_platform_tenant_authority(subject)[0]


def require_matching_platform_tenant_claim(
    claims: Dict[str, Any], platform_tenant_id: str,
) -> None:
    """Fail closed when a signed tenant hint disagrees with current authority."""
    if claims.get("tenant_id") != str(platform_tenant_id):
        from fastapi import HTTPException  # noqa: PLC0415
        raise HTTPException(
            status_code=409,
            detail="verified tenant claim conflicts with the active platform binding",
        )


# --------------------------------------------------------------------------- #
# harness back-edge trust (wire contract §0): with LEAF_AUTH_LIVE=1 the spine's
# AppRunClient authenticates with X-Dispatch-Secret + X-Tenant-Id instead of a
# JWT — same trust model as the broker's X-Broker-Secret — but ONLY on the
# contract-listed read/dispatch routes. Secret unset in env => back-edge
# disabled => the ordinary JWT path (which 401s without a token).
# --------------------------------------------------------------------------- #
def _dispatch_backedge_route(method: str, path: str) -> bool:
    """Return whether this exact method and path is a harness back-edge."""
    if method == "POST":
        if path in (
            "/api/run",
            "/api/author",
            "/api/author/publication-requests",
            "/api/platform/customize",
            # T1 overlay: the harness's propose_overlay tool calls this over
            # the back-edge (X-Dispatch-Secret, no user JWT). Absent from this
            # list, the call fell through to the JWT leg and 401'd — found by
            # a live staging chat turn, not by tests, because every test
            # called the route directly with a tenant header.
            "/api/overlay/proposals",
        ):
            return True
        # R7 spine mount: land is per-change (one path segment, no deeper).
        # The /internal/platform-customize/* co-sign routes are DELIBERATELY
        # absent — the harness must never hold co-sign authority
        # (server/routers/platform_customize.py module docstring).
        if path.startswith("/api/platform/customize/") and path.endswith("/land"):
            change_id = path[len("/api/platform/customize/"):-len("/land")]
            return bool(change_id) and "/" not in change_id
        return False
    if method != "GET":
        return False
    if path in ("/api/capabilities", "/api/tools"):
        return True
    if path in (
        # R7 read side: the spine's read-only customize ops (change list,
        # source file read, source tree listing). Each route re-runs the full
        # R7 admission chain (admin tier + rollout allowlist) on dispatch, so
        # this entry widens reachability, never authority.
        "/api/platform/customize",
        "/api/platform/source",
        "/api/platform/source/tree",
    ):
        return True
    if path.startswith("/api/drawings/"):
        return True
    if path.startswith("/api/jobs/"):
        rest = path[len("/api/jobs/"):]
        return bool(rest) and "/" not in rest  # the job read only, not /stream etc.
    if path.startswith("/api/platform/customize/"):
        # R7 spine mount: the per-change status read only (one segment).
        rest = path[len("/api/platform/customize/"):]
        return bool(rest) and "/" not in rest
    return False


def backedge_tier(tenant_id: str) -> Optional[str]:
    """The back-edge caller's tier from the SAME broker-TRUSTED sources the broker's
    own re-check uses (broker.py:363-376): the persisted broker tenants record
    (`$BROKER_TENANTS`, default server/broker_tenants.json), then
    `$LEAF_BROKER_TENANT_TIERS` ({tenant_id: tier}). Never the request body — a
    back-edge caller could forge it. Read at call time. Returns None when the
    tenant has no provisioned tier at all; returns the raw (possibly empty) string
    when it does, so `entitlements.resolve_tier` applies its own fail-closed rule."""
    store_mode = os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower()
    if store_mode not in {"legacy", "postgres"}:
        return None
    if store_mode == "postgres":
        try:
            from broker_pg_store import get_store

            rec = get_store().tenant(tenant_id)
        except Exception:
            return None
        if rec is not None and rec.get("tier") is not None:
            return str(rec.get("tier") or "")
        return None
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
    return TenantContext(tenant_id, tier=backedge_tier(tenant_id), backedge=True)


import re as _re

_GUEST_GET_ROUTE_RE = _re.compile(
    r"^/api/drawings/[a-z0-9][a-z0-9_-]*/(intake|upload-status|versions)$")


def _guest_route_allowed(method: str, path: str) -> bool:
    """The COMPLETE surface a guest-session identity may touch (§19): its own
    uploads and reads of them, plus the telemetry ingest door (loss-tolerant,
    identity server-stamped, can never mutate product state). Everything else
    — run/author/sessions/tenant/undo/redo/checkout — needs a real account,
    full stop."""
    if method == "POST":
        return path in {"/api/drawings/upload", "/api/telemetry"}
    if method == "GET":
        return bool(_GUEST_GET_ROUTE_RE.match(path))
    return False


def _dispatch_secret_ok(presented: Optional[str]) -> bool:
    secret = os.environ.get("LEAF_APP_DISPATCH_SECRET", "").strip()
    if not secret or not presented:
        return False
    return hmac.compare_digest(presented, secret)


def backedge_author_identity(tenant: Any, authority_session_id: Optional[str],
                             authority_turn_id: Optional[str]) -> Any:
    """Attach the turn's authenticated subject to a back-edge tenant identity.

    The harness back-edge authenticates as a TENANT and can never assert a
    user, so a route that requires a verified owner/editor binding would always
    fail closed. This does NOT let the harness name an identity: it names the
    app-owned session and turn that authenticated it (the same authority tuple
    the gate already takes, routers/agent.py), and the subject is read from the
    app's own record of who opened that turn. A completed, superseded, or
    foreign turn yields nothing and the caller keeps failing closed.

    Deliberately narrow: only for the authoring route, only when the identity
    is a back-edge context that has no subject of its own. A caller that
    already authenticated as a user is returned untouched.
    """
    if not isinstance(tenant, TenantContext) or tenant.subject:
        return tenant
    if not getattr(tenant, "backedge", False):
        # Only a verified dispatch-secret back edge may borrow a turn's author.
        # A JWT without a `sub` claim is also subjectless, and it must not.
        return tenant
    if not authority_session_id or not authority_turn_id:
        return tenant
    try:
        import session_store
        import turn_runner
        subject = session_store.active_turn_subject(
            str(authority_session_id), str(authority_turn_id), str(tenant),
            turn_runner.turn_max_s())
        platform_tenant_id, platform_tier = (
            resolve_active_platform_tenant_authority(subject)
        )
    except Exception:  # noqa: BLE001 - an authority outage must not elevate
        return tenant
    if not subject or platform_tenant_id != str(tenant):
        return tenant
    # The broker provisioning record can lag the active platform binding.
    # Resolve the CURRENT server-owned tier from the turn's stored subject
    # instead of authorizing from either the stale broker tier or the
    # turn-start snapshot. A downgrade or account move during the turn then
    # takes effect before protected authoring.
    return TenantContext(str(tenant), org_id=tenant.org_id, tier=platform_tier,
                         workspace=tenant.workspace, subject=subject,
                         backedge=True)


def active_stage_author_subject(
    tenant_id: str,
    authority_session_id: Optional[str],
    authority_turn_id: Optional[str],
) -> Optional[str]:
    """Resolve one exact, currently active app turn without trusting the caller.

    The session store query binds tenant, session, and turn in one lookup and
    rejects completed, superseded, or stale turns. Any storage or runtime
    configuration failure returns no authority so callers fail closed.
    """
    if not authority_session_id or not authority_turn_id:
        return None
    try:
        import session_store
        import turn_runner
        return session_store.active_turn_subject(
            str(authority_session_id),
            str(authority_turn_id),
            str(tenant_id),
            turn_runner.turn_max_s(),
        )
    except Exception:  # noqa: BLE001 - authority lookup is fail closed
        return None


def stage_author_identity(
    tenant: Any,
    authority_session_id: Optional[str],
    authority_turn_id: Optional[str],
) -> Optional[Any]:
    """Bind protected authoring to the authenticated author of this app turn.

    A direct user must already carry the same subject as the active turn. A
    verified subjectless dispatch back edge may recover that subject from the
    app-owned turn and its current platform binding. No other subjectless
    caller can borrow turn authority.
    """
    if not isinstance(tenant, TenantContext):
        return None
    subject = active_stage_author_subject(
        str(tenant), authority_session_id, authority_turn_id
    )
    if not subject:
        return None
    if tenant.subject:
        return tenant if tenant.subject == subject else None
    if not getattr(tenant, "backedge", False):
        return None
    try:
        platform_tenant_id, platform_tier = (
            resolve_active_platform_tenant_authority(subject)
        )
    except Exception:  # noqa: BLE001 - binding outage cannot grant authority
        return None
    if platform_tenant_id != str(tenant):
        return None
    return TenantContext(
        str(tenant),
        org_id=tenant.org_id,
        tier=platform_tier,
        workspace=tenant.workspace,
        subject=subject,
        backedge=True,
        authority_resolved=True,
    )


def backedge_run_identity(tenant: Any, authority_session_id: Optional[str],
                          authority_turn_id: Optional[str]) -> Optional[Any]:
    """Resolve live conversational run authority from the app-owned turn.

    Direct JWT and auth-off callers keep their existing identity. A verified
    dispatch back edge must name an active, same-tenant app session and turn;
    the harness cannot supply a tier or subject. The shared resolver reads the
    turn subject and then re-resolves its current platform tenant and tier.
    Missing, stale, superseded, foreign, or unavailable authority returns None
    so /api/run refuses before catalog or entitlement evaluation.
    """
    if not auth_live() or not isinstance(tenant, TenantContext):
        return tenant
    if not getattr(tenant, "backedge", False):
        return tenant
    resolved = backedge_author_identity(
        tenant, authority_session_id, authority_turn_id
    )
    if (not isinstance(resolved, TenantContext)
            or not resolved.subject
            or str(resolved) != str(tenant)):
        return None
    return resolved


def require_tenant(
    request: Request,
    x_tenant_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_dispatch_secret: Optional[str] = Header(default=None),
    x_guest_session: Optional[str] = Header(default=None),
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

    # Guest-session leg (CONTRACT-ADDENDUM §19): a VALID HMAC guest token — for
    # a `guest-*` tenant only, verified constant-time against LEAF_GUEST_SECRET
    # with its own expiry — resolves the guest identity at tier "guest", and
    # ONLY on the guest lane's own routes (_guest_route_allowed): the tier
    # policy gates capabilities, but many routes (sessions, tenant grants,
    # undo/redo/checkout) carry no capability gate, so without this allowlist a
    # guest token would be a general-purpose identity (review round 1, MAJOR).
    # Off-list -> 403 naming the boundary. ANY token defect falls through to
    # the ordinary JWT path, which 401s honestly without a Bearer.
    # PRECEDENCE (round-2 review, MAJOR): a presented Bearer token ALWAYS
    # wins — a newly signed-in user with a stale guest header must resolve as
    # their account (or get the JWT path's honest 401), never the guest
    # allowlist's 403. The guest leg only exists for callers with no
    # Authorization at all.
    if x_guest_session and not authorization:
        import guest_uploads  # lazy: mirrors the auth/tenancy lazy-import discipline
        guest_tid = guest_uploads.verify_guest_session(x_guest_session)
        if guest_tid is not None:
            if not _guest_route_allowed(request.method, request.url.path):
                from fastapi import HTTPException  # noqa: PLC0415
                raise HTTPException(
                    status_code=403,
                    detail="guest sessions are upload-only: upload, upload-status, "
                           "intake and versions reads; create an account for "
                           "everything else")
            return TenantContext(guest_tid, tier="guest")

    # LEAF_AUTH_LIVE=1: verify the JWT and require its Leaf tenant claim, but
    # derive the authorization and storage namespace from the current
    # server-owned subject binding. Claims can outlive an account move and are
    # hints, not authority. Rollback is an immutable image rollback, never a
    # request-time split that sends some routes to claims and others to the
    # active binding.
    # Imported lazily so PyJWT is only needed when auth is actually live.
    import auth  # noqa: PLC0415
    import tenancy  # noqa: PLC0415

    payload = auth.verify_platform_token(authorization)   # -> 401 on bad/absent token
    claims = auth.extract_tenant_claims(payload)           # -> 403 if tenant claim absent
    subject = payload.get("sub") if isinstance(payload.get("sub"), str) else None
    platform_tenant_id, platform_tier = resolve_active_platform_tenant_authority(subject)
    require_matching_platform_tenant_claim(claims, platform_tenant_id)
    platform_tier = admin_elevated_tier(claims.get("tier"), subject, platform_tier)
    ws = tenancy.get_store().resolve_workspace(platform_tenant_id)
    # Role overlay identity (§11.5): the VERIFIED roles claim, normalized
    # fail-closed (unreadable -> no roles), plus the same server-owned
    # allowlist membership the admin tier elevation uses as its second factor.
    # `elevated` alone grants nothing — elevated_grants apply only to roles
    # the verified claim actually carries (roles.role_grants).
    import roles as roles_mod  # noqa: PLC0415 — lazy, live path only
    role_names = roles_mod.normalize_role_names(claims.get("roles"))
    elevated = bool(isinstance(subject, str) and subject.strip()
                    and subject in admin_subjects())
    return TenantContext(
        platform_tenant_id,
        org_id=platform_tenant_id,
        tier=platform_tier,
        workspace=ws.workspace_dir if ws is not None else None,
        subject=subject,
        authority_resolved=True,
        roles=role_names,
        elevated=elevated,
    )


def admin_subjects() -> frozenset:
    """The server-owned admin allowlist (W14): Auth0 subjects an operator
    listed in LEAF_PLATFORM_ADMIN_SUBJECTS (comma-separated). Read at call
    time like every other policy seam. Empty/unset = nobody."""
    raw = os.environ.get("LEAF_PLATFORM_ADMIN_SUBJECTS", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def admin_elevated_tier(claim_tier: Any, subject: Optional[str],
                        stored_tier: str) -> str:
    """W14 admin elevation — BOTH factors required, else the stored tier.

    The stored org tier stays the billing authority (claims are hints that can
    outlive an account move, and tier-sync would overwrite an org-row grant on
    the next billing event, so the org row can never carry `admin`). `admin`
    is a SUBJECT-level operator grant instead, and it takes two independent
    operator acts to hold: the VERIFIED token must carry tier="admin" (minted
    only from the operator-set root `app_metadata.leaf_admin === true` flag —
    the Action never derives it from a plan), AND the subject must appear on
    the server-owned LEAF_PLATFORM_ADMIN_SUBJECTS allowlist. Either alone
    grants nothing: a forged/stale claim fails the allowlist, and an
    allowlisted subject without the flag never presents the claim. Removal of
    either revokes.
    """
    if (claim_tier == "admin" and isinstance(subject, str)
            and subject.strip() and subject in admin_subjects()):
        return "admin"
    return stored_tier


def resolve_active_tenant_context(tenant: Any) -> Any:
    """Replace live account claims with the active server-owned binding.

    JWT tenant and org claims can outlive an account move. Account-owned
    resources are written under the active platform binding, so every later
    resource read or mutation must resolve that same binding. Guest sessions,
    auth-off callers, and trusted broker back-edges keep their existing
    identities.
    """
    if not auth_live() or not isinstance(tenant, TenantContext):
        return tenant
    if getattr(tenant, "authority_resolved", False):
        return tenant
    if tenant.subject is None and tenant.org_id is None:
        return tenant

    import tenancy  # noqa: PLC0415 - lazy, mirrors require_tenant

    platform_tenant_id, platform_tier = (
        resolve_active_platform_tenant_authority(tenant.subject))
    ws = tenancy.get_store().resolve_workspace(platform_tenant_id)
    return TenantContext(
        platform_tenant_id,
        org_id=platform_tenant_id,
        tier=platform_tier,
        workspace=ws.workspace_dir if ws is not None else None,
        subject=tenant.subject,
        authority_resolved=True,
        # Roles/elevation are SUBJECT-level identity (§11.5) — an account move
        # changes the workspace binding, not who the person is. Carried over
        # verbatim; both were verified when the original context was built.
        roles=getattr(tenant, "roles", ()),
        elevated=getattr(tenant, "elevated", False),
    )


def require_active_tenant(tenant: Any = Depends(require_tenant)) -> Any:
    """FastAPI dependency for resources stored under platform authority."""
    return resolve_active_tenant_context(tenant)


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
