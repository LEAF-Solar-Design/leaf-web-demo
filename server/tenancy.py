"""
server/tenancy.py — token -> tenant-workspace mapping (Concern 1, storage seam).

Given the tenant_id extracted from a verified Auth0 token (see auth.py), resolve
the tenant's Workspace. This is the JSON-backed DEMO implementation of a seam
whose production impl backs onto the orchestration platform's Postgres tenancy
tables (`Deployment`, `DeploymentTier` in its tenancy types module). Swap
`TenantStore` for a Postgres impl without touching the
verifier or the routers.

The composed `require_tenant` FastAPI dependency lives in `deps.py` (the shared
router seam — routers already import `deps.require_tenant`); this module only
owns the store + resolution. `deps.require_tenant` calls `get_store()
.resolve_workspace(...)` and packs the result into a `deps.TenantContext`.

Stdlib-only (no PyJWT / no network) so it is safe to import even when
LEAF_AUTH_LIVE is off; nothing here runs on the toggle-off path.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# DeploymentTier enum mirrored from the orchestration platform. Surface-only here;
# ENTITLEMENT ENFORCEMENT is explicitly out of scope for this lane.
TIERS = ("self_hosted", "hosted_starter", "hosted_pro")
DEFAULT_TIER = "hosted_starter"


def _tenants_file() -> Path:
    return Path(os.environ.get("LEAF_TENANTS_FILE", str(PROJECT_ROOT / "data" / "tenants.sample.json")))


def _workspace_base() -> Path:
    return Path(os.environ.get("LEAF_WORKSPACE_BASE", str(PROJECT_ROOT / "data" / "workspaces")))


@dataclass(frozen=True)
class Workspace:
    """Resolved tenant workspace. `workspace_dir` is where the tenant's hosted
    agent harness (mushy git repo + renders) will live; this lane only records
    the mapping — provisioning the dir is a downstream (credential-broker) job."""
    tenant_id: str
    org_id: Optional[str]
    tier: str
    workspace_dir: str


class TenantStore:
    """Interface: map a tenant_id to a Workspace. Production impl -> Postgres."""

    def resolve_workspace(self, tenant_id: str) -> Workspace:  # pragma: no cover - interface
        raise NotImplementedError


class JsonTenantStore(TenantStore):
    """Default demo store: reads data/tenants.sample.json.

    Records: {"tenants": [{tenant_id, org_id, tier, workspace_dir}, ...]}.
    An unknown-but-authenticated tenant is AUTO-PROVISIONED a default workspace
    (base/<tenant_id>) rather than rejected — a verified identity always maps to
    a workspace in the demo. The Postgres impl decides its own provisioning
    policy (e.g. deny until a Deployment row exists).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _tenants_file()
        self._by_id: Dict[str, Workspace] = {}
        self._load()

    def _load(self) -> None:
        self._by_id = {}
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a malformed sample file just means "no seeded tenants"
            return
        for rec in data.get("tenants", []):
            tid = rec.get("tenant_id")
            if not tid:
                continue
            tier = rec.get("tier") if rec.get("tier") in TIERS else DEFAULT_TIER
            self._by_id[tid] = Workspace(
                tenant_id=tid,
                org_id=rec.get("org_id"),
                tier=tier,
                workspace_dir=rec.get("workspace_dir") or str(_workspace_base() / tid),
            )

    def resolve_workspace(self, tenant_id: str) -> Workspace:
        ws = self._by_id.get(tenant_id)
        if ws is not None:
            return ws
        # auto-provision a default workspace mapping for a verified new tenant
        return Workspace(
            tenant_id=tenant_id,
            org_id=None,
            tier=DEFAULT_TIER,
            workspace_dir=str(_workspace_base() / tenant_id),
        )


@lru_cache(maxsize=1)
def get_store() -> TenantStore:
    """Process-wide default store (JSON-backed). Cached; call `reset_store()` in
    tests after changing LEAF_TENANTS_FILE."""
    return JsonTenantStore()


def reset_store() -> None:
    get_store.cache_clear()
