"""PostgreSQL-only authorization for demo broker work, in every store mode."""
from __future__ import annotations

import os
import re
from typing import NamedTuple


def is_demo_session_tenant(tenant_id: str) -> bool:
    """Match only the canonical lowercase UUID tenant ids minted by demo_sessions."""
    return re.fullmatch(
        r"demo-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        tenant_id,
    ) is not None


class DemoGuardResult(NamedTuple):
    authorized: bool


def _lookup_authorized(tenant_id: str) -> bool:
    # Use the existing lazy platform connection loader, never the legacy map.
    from broker_pg_store import _load_db

    with _load_db().get_pool().connection() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM broker_tenants AS tenant "
            "JOIN demo_sessions AS session ON session.tenant_id = tenant.tenant_id "
            "WHERE tenant.tenant_id = %(tenant_id)s "
            "AND tenant.tier = 'demo_live' AND tenant.disabled = FALSE "
            "AND session.revoked_at IS NULL AND session.expires_at > NOW()) "
            "AS authorized",
            {"tenant_id": tenant_id},
        ).fetchone()
    return row is not None and row.get("authorized") is True


def demo_tenant_authorized(tenant_id: str) -> DemoGuardResult:
    """Return an explicit decision; unavailable authority always denies demo work."""
    if not is_demo_session_tenant(tenant_id):
        return DemoGuardResult(True)
    if os.environ.get("LEAF_DEMO_LIVE_ENABLED") != "1":
        return DemoGuardResult(False)
    try:
        return DemoGuardResult(_lookup_authorized(tenant_id))
    except Exception:
        # Includes missing database/configuration/schema and lookup failures.
        return DemoGuardResult(False)
