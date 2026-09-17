"""W1 graph bundles use the existing drawing authority, without network I/O."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import write_loop
import store
from test_w1_design_graph import graph  # noqa: F401, fixture

TENANT = "graph-tenant"
DRAWING = "graph-drawing"


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    backend = store.FilesystemBackend(str(tmp_path / "store"))
    source = tmp_path / "source.dwg"
    source.write_bytes(b"synthetic original DWG")
    store.ingest_drawing(backend, TENANT, str(source), drawing_id=DRAWING)
    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "writer", 300)
    return backend, fence


def request_for(backend, graph, *, apply_id="apply-1", dwg=b"synthetic output DWG"):
    version, _, entry = store.resolve_version_entry(backend, TENANT, DRAWING)
    previous_rev = None
    if (entry.get("note") or "").startswith("solar-bundle:"):
        previous_rev = store.read_graph_bundle(backend, TENANT, DRAWING)["graph"]["rev"]
    value = copy.deepcopy(graph)
    value["rev"] = previous_rev + 1 if previous_rev is not None else 0
    value["parent_rev"] = previous_rev
    value["source_hash"] = sha(dwg)
    return {
        "graph": value, "intake": {"layers": ["Panels"], "polylines": []},
        "mapping": {"application_to_handle": {value["panels"][0]["id"]: "A1"}},
        "project_id": value["project"]["id"], "apply_id": apply_id,
        "source_version": version, "source_sha256": entry["sha256"],
        "source_rev": previous_rev,
    }


def commit(drawing, request, dwg=b"synthetic output DWG"):
    backend, fence = drawing
    return write_loop.apply_graph_version(backend, TENANT, DRAWING, dwg, request,
                                          holder="writer", fence=fence)


def test_complete_bundle_reopens_and_duplicate_is_idempotent(drawing, graph):
    backend, _ = drawing
    request = request_for(backend, graph)
    assert commit(drawing, request) == 2
    assert commit(drawing, request) == 2
    reopened = store.FilesystemBackend(backend.root)
    bundle = store.read_graph_bundle(reopened, TENANT, DRAWING,
                                     project_id=request["project_id"])
    assert bundle["graph"] == request["graph"]
    assert bundle["mapping"] == request["mapping"]
    assert bundle["receipt"]["hashes"]["dwg"] == sha(b"synthetic output DWG")
    for name in ("graph", "intake", "mapping"):
        assert bundle["receipt"]["hashes"][name] == sha(store._bundle_json(request[name]))
    assert write_loop.read_intake(backend, TENANT, DRAWING) == (2, request["intake"])
    assert len(store.load_manifest(backend, TENANT, DRAWING)["versions"]) == 2


@pytest.mark.parametrize("field,value", [
    ("source_sha256", "0" * 64), ("source_rev", 5), ("source_version", 77),
    ("project_id", "another-project"),
])
def test_source_preconditions_cannot_move_head(drawing, graph, field, value):
    backend, _ = drawing
    request = request_for(backend, graph)
    request[field] = value
    with pytest.raises((ValueError, KeyError)):
        commit(drawing, request)
    assert store.resolve_version(backend, TENANT, DRAWING)[0] == 1


def test_stale_head_and_checkout_fence(drawing, graph):
    backend, fence = drawing
    stale = request_for(backend, graph, apply_id="stale")
    assert commit(drawing, request_for(backend, graph)) == 2
    with pytest.raises(ValueError):
        commit(drawing, stale)
    current = request_for(backend, graph, apply_id="next")
    store.acquire_checkout_fence(backend, TENANT, DRAWING, "writer", 300)
    with pytest.raises(store.CheckoutDenied):
        commit((backend, fence), current)
    assert store.resolve_version(backend, TENANT, DRAWING)[0] == 2


def test_reused_apply_id_with_different_content_is_refused(drawing, graph):
    backend, _ = drawing
    request = request_for(backend, graph)
    commit(drawing, request)
    changed = copy.deepcopy(request)
    changed["mapping"]["application_to_handle"] = {}
    with pytest.raises(ValueError, match="apply id"):
        commit(drawing, changed)
    assert store.resolve_version(backend, TENANT, DRAWING)[0] == 2


def test_concurrent_duplicate_apply_creates_one_version(drawing, graph):
    backend, _ = drawing
    request = request_for(backend, graph)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(commit, drawing, request) for _ in range(2)]
        assert [future.result(timeout=30) for future in futures] == [2, 2]
    assert len(store.load_manifest(backend, TENANT, DRAWING)["versions"]) == 2


def test_expired_checkout_and_changed_source_are_refused(drawing, graph):
    backend, fence = drawing
    request = request_for(backend, graph)
    store.release_checkout(backend, TENANT, DRAWING, "writer", expected_fence=fence)
    with pytest.raises(store.CheckoutDenied):
        commit(drawing, request)
    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "writer", 300)
    backend.put(store.drawing_version_key(TENANT, DRAWING, 1), b"corrupt source")
    with pytest.raises(ValueError, match="source hash"):
        commit((backend, fence), request)
    assert store.resolve_version(backend, TENANT, DRAWING)[0] == 1


@pytest.mark.parametrize("failure", ["bundle", "manifest"])
def test_partial_publication_preserves_head_and_can_retry(drawing, graph, monkeypatch, failure):
    backend, _ = drawing
    first = request_for(backend, graph)
    commit(drawing, first)
    request = request_for(backend, graph, apply_id="second", dwg=b"second DWG")
    if failure == "bundle":
        original = store._publish_graph_bundle
        def broken(*args):
            raise OSError("synthetic publication failure")
        monkeypatch.setattr(store, "_publish_graph_bundle", broken)
    else:
        original = store.save_manifest
        def broken(*args):
            raise OSError("synthetic publication failure")
        monkeypatch.setattr(store, "save_manifest", broken)
    with pytest.raises(OSError):
        commit(drawing, request, b"second DWG")
    assert store.read_graph_bundle(backend, TENANT, DRAWING)["graph"] == first["graph"]
    monkeypatch.setattr(store, "_publish_graph_bundle" if failure == "bundle" else "save_manifest", original)
    assert commit(drawing, request, b"second DWG") == 3


def test_corrupt_companion_fails_closed(drawing, graph):
    backend, _ = drawing
    request = request_for(backend, graph)
    commit(drawing, request)
    _, _, entry = store.resolve_version_entry(backend, TENANT, DRAWING)
    key = store._graph_bundle_key(TENANT, DRAWING, entry["note"].split(":")[1])
    backend.put(key, b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.read_graph_bundle(backend, TENANT, DRAWING)
    with pytest.raises(ValueError):
        write_loop.read_intake(backend, TENANT, DRAWING)


def test_undo_redo_restore_select_complete_bundle(drawing, graph):
    from routers.drawings import restore_drawing_version
    backend, fence = drawing
    first = request_for(backend, graph)
    commit(drawing, first)
    second = request_for(backend, graph, apply_id="second", dwg=b"second DWG")
    second["mapping"]["application_to_handle"] = {}
    commit(drawing, second, b"second DWG")
    assert store.undo(backend, TENANT, DRAWING, holder="writer", fence=fence) == 2
    assert store.read_graph_bundle(backend, TENANT, DRAWING)["mapping"] == first["mapping"]
    assert store.redo(backend, TENANT, DRAWING, holder="writer", fence=fence) == 3
    assert store.read_graph_bundle(backend, TENANT, DRAWING)["mapping"] == second["mapping"]
    restored = restore_drawing_version(TENANT, DRAWING, 2, actor="restore",
                                       holder="writer", fence=fence, backend=backend)
    assert restored.version == 4
    bundle = store.read_graph_bundle(backend, TENANT, DRAWING)
    for name in ("graph", "intake", "mapping"):
        assert bundle[name] == first[name]
    assert bundle["receipt"]["restore_version"] == 2
    assert backend.get(store.resolve_version(backend, TENANT, DRAWING)[1]) == b"synthetic output DWG"


def test_graph_mapping_routes_enforce_tenant_and_project(drawing, graph, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import drawings
    import deps
    backend, _ = drawing
    request = request_for(backend, graph)
    commit(drawing, request)
    app = FastAPI()
    app.include_router(drawings.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: TENANT
    monkeypatch.setattr(drawings, "_backend", lambda tenant: backend)
    with TestClient(app) as client:
        for resource in ("graph", "mapping"):
            path = f"/api/drawings/{DRAWING}/{resource}"
            assert client.get(path, params={"project_id": request["project_id"]}).status_code == 200
            assert client.get(path, params={"project_id": "other"}).status_code == 404
            app.dependency_overrides[deps.require_active_tenant] = lambda: "other-tenant"
            assert client.get(path, params={"project_id": request["project_id"]}).status_code == 404
            app.dependency_overrides[deps.require_active_tenant] = lambda: TENANT


def test_cutover_refusal_cannot_publish(drawing, graph, monkeypatch):
    backend, _ = drawing
    request = request_for(backend, graph)
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", lambda: "disabled")
    with pytest.raises(ValueError, match="disabled"):
        commit(drawing, request)
    assert store.resolve_version(backend, TENANT, DRAWING)[0] == 1


class PgAuthority:
    """Client-boundary double for reserve/finalize SQL, with no database I/O."""

    def __init__(self, fence):
        self.row = {"head": 1, "latest": 1, "checkout_holder": "writer",
                    "checkout_fence": fence,
                    "checkout_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}
        self.versions = {}
        self.result = None

    def run_transaction(self, operation, **kwargs):
        return operation(self)

    def connection(self):
        return nullcontext(self)

    def fetchone(self):
        return self.result

    def execute(self, sql, params):
        statement = " ".join(sql.split())
        self.result = None
        if statement.startswith("SELECT head"):
            self.result = dict(self.row)
        elif statement.startswith("SELECT version, note"):
            self.result = next(({"version": version, "note": value["note"]}
                                for version, value in self.versions.items()
                                if value["state"] == "ready"
                                and value["workitem"] == params["operation"]), None)
        elif "SET latest =" in statement:
            self.row["latest"] = params["version"]
        elif statement.startswith("INSERT INTO drawing_store_versions"):
            self.versions[params["version"]] = dict(params, state="reserved")
        elif "SET head =" in statement:
            if (self.row["head"] == params["expected"]
                    and self.row["checkout_fence"] == params["checkout_fence"]
                    and self.row["checkout_holder"] == params["checkout_holder"]):
                self.row["head"] = params["version"]
                self.result = {"tenant_id": TENANT}
        elif "SET state = 'ready'" in statement:
            self.versions[params["version"]]["state"] = "ready"
        elif "SET state = 'orphaned'" in statement:
            self.versions[params["version"]]["state"] = "orphaned"
        elif "SET state = %(state)s" in statement:
            self.versions[params["version"]]["state"] = params["state"]
        else:
            raise AssertionError(statement)
        return self


@pytest.mark.parametrize("failure", [None, "bundle", "fence"])
def test_postgres_reservation_publishes_bundle_before_ready(drawing, graph, monkeypatch, failure):
    backend, fence = drawing
    request = request_for(backend, graph)
    data = b"synthetic output DWG"
    bundle = store._prepare_graph_bundle(backend, TENANT, DRAWING, data, 1, request)
    meta = {"workitem_id": "solar:apply-1",
            "note": "solar-bundle:" + sha(store._bundle_json(bundle))}
    db = PgAuthority(fence)
    monkeypatch.setattr(store, "_db", lambda: db)
    publish = store._publish_graph_bundle

    def publication(*args):
        assert db.row["head"] == 1
        assert db.versions[2]["state"] == "reserved"
        if failure == "bundle":
            raise OSError("synthetic bundle failure")
        publish(*args)
        if failure == "fence":
            db.row["checkout_fence"] += 1

    monkeypatch.setattr(store, "_publish_graph_bundle", publication)
    if failure:
        with pytest.raises(OSError if failure == "bundle" else ValueError):
            store._pg_put(backend, TENANT, DRAWING, data, 1, meta,
                          holder="writer", fence=fence, bundle=bundle)
        assert db.row["head"] == 1
        assert db.versions[2]["state"] == "orphaned"
        assert backend.get(store.drawing_version_key(TENANT, DRAWING, 1)) == b"synthetic original DWG"
    else:
        assert store._pg_put(backend, TENANT, DRAWING, data, 1, meta,
                             holder="writer", fence=fence, bundle=bundle) == 2
        assert db.row["head"] == 2
        assert db.versions[2]["state"] == "ready"
        assert store._pg_put(backend, TENANT, DRAWING, data, 1, meta,
                             holder="writer", fence=fence, bundle=bundle) == 2
        assert len(db.versions) == 1
