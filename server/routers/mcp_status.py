"""Tenant-scoped public status for the standard-services broker facade."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

import deps


router = APIRouter()


def _broker_descriptor(env: Any = os.environ) -> list[dict[str, str]]:
    """Expose only the fixed facade name and configured broker host."""
    value = env.get("LEAF_TENANT_MCP_BROKER_URL", "")
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return []
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        host = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
    except (TypeError, ValueError):
        return []
    return [{"name": "services", "host": host}]


@router.get("/api/converse/mcp")
def mcp_status(
    tenant=Depends(deps.require_active_tenant),
) -> dict[str, list[dict[str, str]]]:
    """Show the one broker facade. Never enumerate upstream or operator MCPs."""
    del tenant
    return {"servers": _broker_descriptor()}
