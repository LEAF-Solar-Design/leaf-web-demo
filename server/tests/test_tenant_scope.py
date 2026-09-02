"""Tenant tool scope (server/tenant_scope.py): a scoped tenant sees, resolves,
and can run ONLY the tools the operator listed; unscoped tenants are untouched.

Binding acceptance (2026-09-02, the locked single-purpose client app):
  * unscoped tenant -> `filter_rows` hands back the SAME list object (byte-identical
    catalog, zero allocation), /api/tools and /api/capabilities unchanged;
  * scoped tenant -> only the listed names on /api/tools, /api/capabilities,
    `find_tool` (the /api/run resolver) and `effective_tools_with_provenance`;
  * a listed name the catalog does not serve is simply absent;
  * a malformed entry scopes to NOTHING; an absent file scopes nobody; a PRESENT
    but corrupt file fails closed (ScopePolicyError -> structured 503 on the list
    routes), never an unlocked catalog;
  * /api/entitlements carries `scope: {label, tools}` for a scoped tenant and
    `scope: null` otherwise;
  * the shipped policy file parses and every scoped name exists in the catalog.
Run:  cd server && python -m pytest tests/test_tenant_scope.py -q
"""
from __future__ import annotations

import platform as _stdlib_platform  # noqa: E402

_stdlib_platform.python_implementation()
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="scope-jobs-")) / "jobs.db"))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tenant_scope  # noqa: E402

SCOPED = "scoped-tenant"
OTHER = "other-tenant"
SHIPPED = SERVER_DIR / "tenant_tool_scopes.json"


@pytest.fixture(autouse=True)
def _fresh_cache():
    tenant_scope.reset_cache()
    yield
    tenant_scope.reset_cache()


def _policy(monkeypatch, tmp_path, raw) -> Path:
    p = tmp_path / "scopes.json"
    p.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANT_TOOL_SCOPES_FILE", str(p))
    tenant_scope.reset_cache()
    return p


def _scope_count_by_layer(monkeypatch, tmp_path, tools=("count-by-layer",), label="Counts"):
    return _policy(monkeypatch, tmp_path, {"scopes": {SCOPED: {"label": label, "tools": list(tools)}}})


def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# --------------------------------------------------------------------------- #
# pure policy
# --------------------------------------------------------------------------- #
def test_unscoped_tenant_gets_the_same_rows_object_back(monkeypatch, tmp_path):
    _scope_count_by_layer(monkeypatch, tmp_path)
    rows = [({"name": "a"}, "engine"), ({"name": "b"}, "seed")]
    assert tenant_scope.filter_rows(OTHER, rows) is rows
    assert tenant_scope.scope_for(OTHER) is None
    assert tenant_scope.public_view(OTHER) is None


def test_scoped_tenant_keeps_only_listed_rows_in_order(monkeypatch, tmp_path):
    _scope_count_by_layer(monkeypatch, tmp_path, tools=("b", "zzz-not-served"))
    rows = [({"name": "a"}, "engine"), ({"name": "b"}, "seed"), ({"name": "c"}, "seed")]
    assert tenant_scope.filter_rows(SCOPED, rows) == [({"name": "b"}, "seed")]
    assert tenant_scope.public_view(SCOPED) == {"label": "Counts", "tools": ["b", "zzz-not-served"]}


@pytest.mark.parametrize("entry", [
    "not-a-mapping", {"tools": "count-by-layer"}, {"tools": [1, 2]}, {"tools": [""]},
    {"tools": ["x"] * (tenant_scope.MAX_TOOLS + 1)}, {},
])
def test_malformed_entry_scopes_to_nothing_never_everything(monkeypatch, tmp_path, entry):
    _policy(monkeypatch, tmp_path, {"scopes": {SCOPED: entry}})
    rows = [({"name": "count-by-layer"}, "engine")]
    assert tenant_scope.filter_rows(SCOPED, rows) == []
    assert tenant_scope.filter_rows(OTHER, rows) is rows


def test_absent_file_scopes_nobody(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_TENANT_TOOL_SCOPES_FILE", str(tmp_path / "missing.json"))
    rows = [({"name": "count-by-layer"}, "engine")]
    assert tenant_scope.filter_rows(SCOPED, rows) is rows


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"scopes": []}', '{"scopes": {"../evil": {"tools": []}}}'])
def test_present_but_corrupt_file_fails_closed(monkeypatch, tmp_path, raw):
    _policy(monkeypatch, tmp_path, raw)
    with pytest.raises(tenant_scope.ScopePolicyError):
        tenant_scope.filter_rows(OTHER, [])


def test_label_is_bounded_and_defaults_to_the_tenant_id(monkeypatch, tmp_path):
    _policy(monkeypatch, tmp_path, {"scopes": {
        SCOPED: {"label": "x" * 500, "tools": ["a"]},
        OTHER: {"label": "   ", "tools": ["a"]},
    }})
    assert len(tenant_scope.scope_for(SCOPED).label) == tenant_scope.MAX_LABEL
    assert tenant_scope.scope_for(OTHER).label == OTHER


def test_cache_follows_the_file(monkeypatch, tmp_path):
    p = _scope_count_by_layer(monkeypatch, tmp_path, tools=("a",))
    assert tenant_scope.scope_for(SCOPED).tools == frozenset({"a"})
    p.write_text(json.dumps({"scopes": {SCOPED: {"tools": ["b"]}}}), encoding="utf-8")
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000))
    assert tenant_scope.scope_for(SCOPED).tools == frozenset({"b"})


# --------------------------------------------------------------------------- #
# the catalog choke point and the HTTP surfaces
# --------------------------------------------------------------------------- #
def test_catalog_resolution_and_run_lookup_are_scoped(monkeypatch, tmp_path):
    import deps
    monkeypatch.setattr(deps, "_AUTHORED", [])
    _scope_count_by_layer(monkeypatch, tmp_path)
    scoped = {t["name"] for t in deps.all_tools(SCOPED)}
    other = {t["name"] for t in deps.all_tools(OTHER)}
    assert scoped == {"count-by-layer"}
    assert "count-by-layer" in other and len(other) > 1
    assert deps.find_tool("count-by-layer", SCOPED) is not None
    unlisted = next(name for name in other if name != "count-by-layer")
    assert deps.find_tool(unlisted, SCOPED) is None          # the /api/run resolver
    assert deps.find_tool(unlisted, OTHER) is not None
    assert {t["name"] for t, _s in deps.effective_tools_with_provenance(SCOPED)} == {"count-by-layer"}


def test_tools_capabilities_and_entitlements_routes_are_scoped(monkeypatch, tmp_path):
    import deps
    monkeypatch.setattr(deps, "_AUTHORED", [])
    _scope_count_by_layer(monkeypatch, tmp_path)
    c = _client()
    scoped = {t["name"] for t in c.get("/api/tools", headers=_h(SCOPED)).json()["tools"]}
    other = {t["name"] for t in c.get("/api/tools", headers=_h(OTHER)).json()["tools"]}
    assert scoped == {"count-by-layer"} and len(other) > 1

    def _cap_names(body: dict) -> set:
        return {cap["name"] for fam in body["families"] for cap in fam["capabilities"]}
    assert _cap_names(c.get("/api/capabilities", headers=_h(SCOPED)).json()) == {"count-by-layer"}
    assert len(_cap_names(c.get("/api/capabilities", headers=_h(OTHER)).json())) > 1

    ent_scoped = c.get("/api/entitlements", headers=_h(SCOPED)).json()
    ent_other = c.get("/api/entitlements", headers=_h(OTHER)).json()
    assert ent_scoped["scope"] == {"label": "Counts", "tools": ["count-by-layer"]}
    assert ent_other["scope"] is None


def test_corrupt_scope_file_is_a_structured_503_on_the_list_routes(monkeypatch, tmp_path):
    _policy(monkeypatch, tmp_path, "{not json")
    c = _client()
    for path in ("/api/tools", "/api/entitlements"):
        r = c.get(path, headers=_h(OTHER))
        assert r.status_code == 503, path
        assert r.json()["error"]["retryable"] is True


def test_shipped_scope_file_parses_and_names_only_served_tools(monkeypatch):
    import deps
    monkeypatch.delenv("LEAF_TENANT_TOOL_SCOPES_FILE", raising=False)
    monkeypatch.setattr(deps, "_AUTHORED", [])
    scopes = tenant_scope.load_scopes()
    assert scopes, "the shipped file must list at least the cut-list client tenant"
    served = {t["name"] for t in deps.all_tools("unscoped-probe")}
    for tid, scope in scopes.items():
        assert scope.tools, tid
        assert scope.tools <= served, (tid, scope.tools - served)
