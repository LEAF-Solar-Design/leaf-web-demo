"""GET /api/search — slice 10c binary acceptance.

Hermetic: in-process TestClient wrapping only routers/search.py, an isolated
LEAF_STORE_DIR / LEAF_TENANTS_DIR per test (tmp_path), APS_LIVE=0
(FilesystemBackend, no APS/da credential, no network), auth-live off (the
X-Tenant-Id header stub).

Run:  cd server && python -m pytest tests/test_search.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Cache the STDLIB `platform` module BEFORE SERVER_DIR lands on sys.path (a
# root-level platform/ package would otherwise shadow it for this process —
# server/tests' own repo-wide hazard, see test_wave3/wave4).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("APS_LIVE", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_TENANTS_DIR", raising=False)
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    from routers import search as search_router  # noqa: PLC0415 (import after env is set)
    from envelopes import install_error_handlers  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(search_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _bootstrap_version_note(tmp_path, monkeypatch, tenant: str, drawing_id: str, note: str) -> None:
    """Bootstrap `drawing_id` for `tenant` (the cached demo intake, same path
    every fresh id takes) then overwrite v1's note to a tenant-unique string,
    so a search hit on that string PROVES which tenant's store answered."""
    import write_loop  # imported first: its own top-level code puts da/ (store.py's home) on sys.path
    import store

    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    write_loop.ensure_demo_drawing(backend, tenant, drawing_id)
    manifest = store.load_manifest(backend, tenant, drawing_id)
    manifest["versions"][0]["note"] = note
    store.save_manifest(backend, tenant, drawing_id, manifest)


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #
def test_query_over_the_length_cap_is_400_bad_params(client):
    from routers.search import QUERY_MAX_LEN

    r = client.get("/api/search", params={"q": "x" * (QUERY_MAX_LEN + 1)}, headers=_h("acme"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_a_query_at_the_length_cap_is_accepted(client):
    from routers.search import QUERY_MAX_LEN

    r = client.get("/api/search", params={"q": "x" * QUERY_MAX_LEN}, headers=_h("acme"))
    assert r.status_code == 200, r.text
    assert r.json()["results"] == []


def test_response_never_exceeds_the_response_row_cap(client, tmp_path, monkeypatch):
    from routers.search import RESPONSE_ROW_CAP

    base = tmp_path / "tenants"
    root = base / "acme"
    root.mkdir(parents=True)
    many_tools = [
        {"name": f"match-tool-{i}", "version": "1.0.0", "description": "match",
         "kind": "script", "engine_op": f"match_tool_{i}",
         "params": {"type": "object", "properties": {}}, "returns": {"type": "object"},
         "capabilities": [], "provenance": {"author": "agent", "created": "2026-07-18T00:00:00Z"}}
        for i in range(RESPONSE_ROW_CAP + 10)
    ]
    (root / "registry.json").write_text(json.dumps({"tools": many_tools}), encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))

    r = client.get("/api/search", params={"q": "match"}, headers=_h("acme"))
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) <= RESPONSE_ROW_CAP


# --------------------------------------------------------------------------- #
# tools index: filters, and reuses /api/tools' own per-tenant fold
# --------------------------------------------------------------------------- #
def test_tools_index_filters_by_name_and_description(client, tmp_path, monkeypatch):
    base = tmp_path / "tenants"
    root = base / "acme"
    root.mkdir(parents=True)
    (root / "registry.json").write_text(json.dumps({"tools": [
        {"name": "panel-cut", "version": "1.0.0", "description": "cuts roof panels",
         "kind": "script", "engine_op": "panel_cut", "params": {"type": "object", "properties": {}},
         "returns": {"type": "object"}, "capabilities": [],
         "provenance": {"author": "agent", "created": "2026-07-18T00:00:00Z"}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))

    r = client.get("/api/search", params={"q": "roof"}, headers=_h("acme"))
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert any(row["id"] == "tool:panel-cut" for row in results)

    r2 = client.get("/api/search", params={"q": "nomatch-xyz"}, headers=_h("acme"))
    assert r2.json()["results"] == []


def test_tenant_a_tool_is_invisible_to_tenant_b(client, tmp_path, monkeypatch):
    """Tenant scoping proof (hard rule): a tenant-repo tool is reachable only
    by its OWN tenant's search — never by another tenant's request for the
    same query text, even though both hit the identical /api/search route."""
    base = tmp_path / "tenants"
    for tid, tools in (("tenant-a", [{
        "name": "a-only-tool", "version": "1.0.0", "description": "a private catalog entry",
        "kind": "script", "engine_op": "a_only_tool", "params": {"type": "object", "properties": {}},
        "returns": {"type": "object"}, "capabilities": [],
        "provenance": {"author": "agent", "created": "2026-07-18T00:00:00Z"},
    }]), ("tenant-b", [])):
        root = base / tid
        root.mkdir(parents=True)
        (root / "registry.json").write_text(json.dumps({"tools": tools}), encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))

    a = client.get("/api/search", params={"q": "a-only"}, headers=_h("tenant-a")).json()["results"]
    b = client.get("/api/search", params={"q": "a-only"}, headers=_h("tenant-b")).json()["results"]
    assert any(row["id"] == "tool:a-only-tool" for row in a)
    assert b == []


# --------------------------------------------------------------------------- #
# version index: tenant-scoped drawing history
# --------------------------------------------------------------------------- #
def test_version_index_matches_on_note_and_is_absent_without_drawing_id(client, tmp_path, monkeypatch):
    _bootstrap_version_note(tmp_path, monkeypatch, "acme", "demo", "panel move west edge")

    r = client.get("/api/search", params={"q": "panel move", "drawing_id": "demo"}, headers=_h("acme"))
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert any(row["id"] == "version:1" and row["kind"] == "version" for row in results)

    # No drawing_id -> the version index contributes nothing (it is not a
    # cross-drawing scan; the caller must name which drawing it is searching).
    r2 = client.get("/api/search", params={"q": "panel move"}, headers=_h("acme"))
    assert all(row["kind"] != "version" for row in r2.json()["results"])


def test_tenant_a_version_note_is_invisible_to_tenant_b_for_the_same_drawing_id(client, tmp_path, monkeypatch):
    """Tenant scoping proof for the version index: the SAME drawing_id under
    two different tenants resolves to two different, isolated stores
    (write_loop.backend_for_tenant), so tenant B's request for tenant A's
    private note text returns nothing."""
    _bootstrap_version_note(tmp_path, monkeypatch, "tenant-a", "shared-id", "tenant-a-private-note-xyz")

    a = client.get("/api/search", params={"q": "tenant-a-private-note-xyz", "drawing_id": "shared-id"},
                   headers=_h("tenant-a")).json()["results"]
    b = client.get("/api/search", params={"q": "tenant-a-private-note-xyz", "drawing_id": "shared-id"},
                   headers=_h("tenant-b")).json()["results"]
    assert any(row["kind"] == "version" for row in a)
    assert all(row["kind"] != "version" for row in b)


# --------------------------------------------------------------------------- #
# session index: fails closed to zero rows for a non-operator caller
# --------------------------------------------------------------------------- #
def test_session_index_is_empty_for_a_non_operator_caller(client):
    r = client.get("/api/search", params={"q": ""}, headers=_h("acme"))
    assert r.status_code == 200, r.text
    assert all(row["kind"] != "session" for row in r.json()["results"])


# --------------------------------------------------------------------------- #
# empty query
# --------------------------------------------------------------------------- #
def test_empty_query_returns_the_envelope_shape_never_throws(client):
    r = client.get("/api/search", headers=_h("acme"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert body["query"] == ""
    assert isinstance(body["results"], list)
