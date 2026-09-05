"""server/mcp_tool_projection.py: the CONNECTED-servers -> tool projection stub
(standardization slice 8c, closing the ledger's Todo 8c: mcp_source on tool
records plus the link_service capability, the capability already landed with
#1027).

`projected_tools` always returns an empty list today — projecting an
upstream server's own tool list is a later slice — so the capability
catalog must be BYTE IDENTICAL whether or not the tenant has a connected MCP
server. This file proves that equality rather than asserting it as a
premise nobody could fail.

Run:  cd server && python -m pytest tests/test_mcp_tool_projection.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import catalog  # noqa: E402
import mcp_tool_projection  # noqa: E402
import tenant_mcp_store  # noqa: E402

_TENANT = "demo-tenant"


def _tool(**over):
    base = {
        "name": "count-by-layer",
        "version": "1.0.0",
        "description": "Counts entities per layer.",
        "kind": "script",
        "engine_op": "count_by_layer",
        "params": {"type": "object", "properties": {}},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent"},
    }
    base.update(over)
    return base


def _connect_one_server(tenant_id: str) -> None:
    record = tenant_mcp_store.register(
        tenant_id, url="https://mcp.example", label="Example", host="mcp.example")
    tenant_mcp_store.update_record(
        tenant_id, record["id"], state="connected", linked_at="2026-09-04T00:00:00Z")


def test_projected_tools_is_empty_with_no_servers_registered_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path))
    assert mcp_tool_projection.projected_tools(_TENANT) == []


def test_projected_tools_is_empty_with_a_connected_server(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path))
    _connect_one_server(_TENANT)
    assert mcp_tool_projection.projected_tools(_TENANT) == []


def test_a_registered_but_not_yet_connected_server_is_never_projected(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path))
    tenant_mcp_store.register(
        _TENANT, url="https://mcp.example", label="Example", host="mcp.example")
    assert mcp_tool_projection.projected_tools(_TENANT) == []


def test_the_catalog_is_byte_identical_with_or_without_a_connected_server(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path))
    base_tools = [_tool()]

    without_connected = catalog.build_catalog(
        base_tools + mcp_tool_projection.projected_tools(_TENANT))

    _connect_one_server(_TENANT)
    with_connected = catalog.build_catalog(
        base_tools + mcp_tool_projection.projected_tools(_TENANT))

    assert with_connected == without_connected
