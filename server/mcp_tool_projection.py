"""Tool projection from a tenant's CONNECTED MCP servers (standardization
slice 8c, the last piece of the tenant MCP registry from slice 8b/#1027).

`projected_tools` reads the tenant's CONNECTED servers from
`tenant_mcp_store` and returns the tools they project onto the capability
catalog. It always returns an empty list today: projecting an upstream
server's actual tool list is a later slice, so this function's whole job
right now is to be the live wiring point `routers/capabilities.py` fans
into `build_catalog`'s tool list, ahead of the slice that gives it
something to return. Because it is empty, calling it changes no response
byte for any tenant (test_mcp_tool_projection.py proves the catalog is the
same with or without a connected server) and it holds the invariant
test_mcp_status.py enforces for the sibling broker facade: no list response
may ever carry an upstream MCP tool name.
"""
from __future__ import annotations

from typing import Any, Dict, List

import tenant_mcp_store


def projected_tools(tenant_id: str) -> List[Dict[str, Any]]:
    """Tools projected from ``tenant_id``'s CONNECTED MCP servers.

    Reads the connected servers now (rather than waiting for the slice that
    gives them something to project) so `build_catalog`'s tool list already
    has a live seam: a later slice fills in the loop body below with each
    connected server's own tool list, stamping `mcp_source: {server_id,
    tool}` on each, and no second wiring point is ever needed.
    """
    connected = [
        record for record in tenant_mcp_store.list_records(tenant_id)
        if record.get("state") == "connected"
    ]
    tools: List[Dict[str, Any]] = []
    for _server in connected:
        # Upstream tool projection ships in a later slice; today a connected
        # server contributes nothing to the catalog.
        continue
    return tools
