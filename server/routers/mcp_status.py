"""Tenant-scoped, redacted MCP attachment status for the converse composer."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

import deps


router = APIRouter()
MAX_MCP_CONFIG_BYTES = 64 * 1024
MAX_SERVERS = 16
SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE | re.ASCII)
MIN_REDACTABLE_SECRET_LEN = 24
logger = logging.getLogger(__name__)


def _tenant_hash(tenant_id: str) -> str:
    # Harness mcpBridge.ts: `return createHash("sha256").update(tenantId).digest("hex");`
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()


def _read_capped(path: Path) -> str | None:
    try:
        with path.open("rb") as source:
            data = source.read(MAX_MCP_CONFIG_BYTES + 1)
        if len(data) > MAX_MCP_CONFIG_BYTES:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _host(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        return f"{hostname}:{port}" if port is not None else hostname
    except (TypeError, ValueError):
        return None


def _redacted_servers(tenant_id: str, env: Any = os.environ) -> list[dict[str, str]]:
    """Return only harmless server descriptors, or no descriptors on any fault."""
    directory = env.get("LEAF_MCP_BRIDGE_DIR", "")
    if not directory:
        return []
    source = _read_capped(Path(directory) / f"{_tenant_hash(tenant_id)}.json")
    if source is None:
        return []
    try:
        configs = json.loads(source)
    except (RecursionError, ValueError):
        return []
    if not isinstance(configs, list) or len(configs) > MAX_SERVERS:
        return []

    servers: list[dict[str, str]] = []
    for config in configs:
        if not isinstance(config, dict):
            return []
        name = config.get("name")
        if not isinstance(name, str) or not SERVER_NAME.fullmatch(name) or name.endswith("."):
            return []
        host = _host(config.get("url"))
        if host is None:
            return []
        entry = {"name": name, "host": host}
        auth_token = config.get("authToken")
        if (
            isinstance(auth_token, str)
            and len(auth_token) >= MIN_REDACTABLE_SECRET_LEN
            and auth_token in json.dumps(entry)
        ):
            logger.warning("Dropping MCP server descriptor because it contains authToken=<redacted>")
            continue
        servers.append(entry)
    return servers


@router.get("/api/converse/mcp")
def mcp_status(tenant=Depends(deps.require_active_tenant)) -> dict[str, list[dict[str, str]]]:
    """List the calling tenant's mounted servers without exposing credentials."""
    return {"servers": _redacted_servers(str(tenant))}
