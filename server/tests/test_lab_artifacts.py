"""Ledger invariants, HTTP admission, canonical bytes and real writer races."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import lab_artifacts as ledger
from lab_canonical import canonical_bytes, canonical_json, graph_digest
from lab_doc_validators import InvalidGraph, MAX_RAW_BYTES, TooLarge, parse_body, validate_graph
from routers import lab_router

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "contract/fixtures/lab/digest-vectors.json").read_text(encoding="utf-8"))["vectors"]
GRAPH = {"schema": "leaf.lab-graph.v1", "blocks": [], "pipes": [], "outputs": {}}


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_LAB_STORE", "sqlite")
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    return tmp_path / "lab.db"


def save(expected=None, graph=None, tenant="tenant-a", doc="strategy"):
    return ledger.save(tenant, doc, GRAPH if graph is None else graph, expected, "member")


def client(tenant="tenant-a", tier="hosted_pro"):
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        tenant, tier=tier, subject="member")
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_runtime_canonical_digest_vectors(vector):
    assert canonical_json(vector["input"]) == vector["canonical"]
    assert canonical_bytes(vector["input"]).hex() == vector["utf8_hex"]
    assert graph_digest(vector["input"]) == vector["sha256"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "\ud800", "\udfff"])
def test_runtime_canonical_refuses_non_json(value):
    with pytest.raises((ValueError, UnicodeError)):
        canonical_bytes(value)


def test_runtime_canonical_rejects_cycles_and_nonstring_keys():
    value = []
    value.append(value)
    for invalid in (value, {1: "value"}, object()):
        with pytest.raises(ValueError):
            canonical_bytes(invalid)


def test_save_read_exact_bytes_and_content_address(local_store):
    result = save()
    assert result["revision"] == 1
    assert result["parent_revision"] is None
    assert result["author"] == "member"
    assert result["created_at"] > 0
    assert result["canonical_text"].encode() == canonical_bytes(GRAPH)
    assert result["sha256"] == graph_digest(GRAPH)
    assert ledger.get("tenant-a", "strategy") == {**result, "published_revision": None}
    assert ledger.get("tenant-a", "strategy", 1) == result
    with sqlite3.connect(local_store) as conn:
        text, data, digest = conn.execute("SELECT canonical_text,canonical_bytes,sha256 FROM lab_blobs").fetchone()
    assert data == text.encode() == canonical_bytes(GRAPH)
    assert digest == hashlib.sha256(data).hexdigest()


def test_content_dedup_is_tenant_scoped(local_store):
    save()
    save(1)
    save(tenant="tenant-b")
    with sqlite3.connect(local_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lab_blobs").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM lab_revisions").fetchone()[0] == 3


def test_stale_head_never_appends_or_changes_head():
    first = save()
    with pytest.raises(ledger.Conflict):
        save()
    assert ledger.get("tenant-a", "strategy") == {**first, "published_revision": None}
    assert len(ledger.list_revisions("tenant-a", "strategy")) == 1


def test_save_parent_and_revision_listing():
    save()
    assert save(1)["parent_revision"] == 1
    save(2)
    assert [row["revision"] for row in ledger.list_revisions("tenant-a", "strategy", limit=1)] == [3]
    assert [row["revision"] for row in ledger.list_revisions("tenant-a", "strategy", before=3)] == [2, 1]


def test_restore_appends_and_keeps_old_revision():
    first = save()
    save(1)
    restored = ledger.restore("tenant-a", "strategy", 1, 2, "restorer")
    assert restored["revision"] == 3
    assert restored["parent_revision"] == 1
    assert restored["sha256"] == first["sha256"]
    assert ledger.get("tenant-a", "strategy", 1) == first
    with pytest.raises(ledger.Conflict):
        ledger.restore("tenant-a", "strategy", 1, 2, "restorer")
    assert ledger.get("tenant-a", "strategy")["revision"] == 3


def test_publish_receipt_cas_and_stale_failure(local_store):
    first = save()
    receipt = ledger.publish("tenant-a", "strategy", 1, "publisher")
    assert receipt["expected_head"] == receipt["new_head"] == 1
    assert receipt["sha256"] == first["sha256"]
    save(1)
    with pytest.raises(ledger.Conflict):
        ledger.publish("tenant-a", "strategy", 1, "publisher")
    with sqlite3.connect(local_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lab_publish_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT published_revision FROM lab_documents").fetchone()[0] == 1


def test_cross_tenant_all_reads_restore_publish_are_missing():
    save()
    for operation in (
        lambda: ledger.get("tenant-b", "strategy"),
        lambda: ledger.get("tenant-b", "strategy", 1),
        lambda: ledger.list_revisions("tenant-b", "strategy"),
        lambda: ledger.restore("tenant-b", "strategy", 1, 1, "other"),
        lambda: ledger.publish("tenant-b", "strategy", 1, "other"),
    ):
        with pytest.raises(ledger.NotFound):
            operation()


@pytest.mark.parametrize("table", ["lab_blobs", "lab_revisions", "lab_publish_receipts"])
def test_sqlite_bytes_revisions_and_receipts_are_immutable(local_store, table):
    save()
    ledger.publish("tenant-a", "strategy", 1, "publisher")
    with sqlite3.connect(local_store) as conn:
        for query in (f"UPDATE {table} SET tenant_id=tenant_id", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(query)


@pytest.mark.parametrize("cap", ["MAX_TENANT_DOCS", "MAX_TENANT_REVISIONS", "MAX_TENANT_BYTES"])
def test_tenant_caps_rollback_every_write(monkeypatch, local_store, cap):
    first = save()
    monkeypatch.setattr(ledger, cap, 1)
    if cap == "MAX_TENANT_DOCS":
        operation = lambda: save(doc="second")
    elif cap == "MAX_TENANT_REVISIONS":
        operation = lambda: save(1)
    else:
        graph = copy.deepcopy(GRAPH)
        graph["blocks"] = [{"id": "market", "registry": {"id": "market", "version": "1.0.0", "code_sha256": "a" * 64}, "settings": {}}]
        operation = lambda: save(1, graph)
    with pytest.raises(TooLarge):
        operation()
    assert ledger.get("tenant-a", "strategy") == {**first, "published_revision": None}
    with sqlite3.connect(local_store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lab_blobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM lab_documents").fetchone()[0] == 1


def test_publish_cap_is_atomic(monkeypatch, local_store):
    save()
    monkeypatch.setattr(ledger, "MAX_TENANT_PUBLISHES", 0)
    with pytest.raises(TooLarge):
        ledger.publish("tenant-a", "strategy", 1, "publisher")
    with sqlite3.connect(local_store) as conn:
        assert conn.execute("SELECT published_revision FROM lab_documents").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM lab_publish_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("runtime", ["staging", "production"])
def test_sqlite_refused_in_deployment(monkeypatch, local_store, runtime):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    with pytest.raises(ledger.StoreUnavailable, match="lab_postgres_required"):
        save()
    assert not local_store.exists()


def test_sqlite_refused_when_postgres_required(monkeypatch, local_store):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
    with pytest.raises(ledger.StoreUnavailable):
        save()
    assert not local_store.exists()


@pytest.mark.parametrize("value", [None, "", " \t\n"])
def test_sqlite_requires_explicit_db_path(monkeypatch, local_store, value):
    if value is None:
        monkeypatch.delenv("LEAF_LAB_DB", raising=False)
    else:
        monkeypatch.setenv("LEAF_LAB_DB", value)

    def forbidden(*args, **kwargs):
        pytest.fail("unset SQLite path reached persistence")

    monkeypatch.setattr(ledger.sqlite3, "connect", forbidden)
    with pytest.raises(ledger.StoreUnavailable, match="^lab_db_unset$"):
        save()
    assert not local_store.exists()


def test_sqlite_forbidden_volume(monkeypatch):
    for path in ("/data/state/lab.db", "/data/state"):
        monkeypatch.setenv("LEAF_LAB_DB", path)
        with pytest.raises(ledger.StoreUnavailable, match="lab_sqlite_path_forbidden"):
            save()


def test_unknown_selector_fails_closed(monkeypatch, local_store):
    monkeypatch.setenv("LEAF_LAB_STORE", "postgre")
    with pytest.raises(ledger.StoreUnavailable):
        save()
    assert not local_store.exists()


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.0])
def test_expected_head_rejects_coercion(value, local_store):
    with pytest.raises(InvalidGraph):
        save(value)
    assert not local_store.exists()


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'[]', b'{} {}'])
def test_raw_parser_refuses_ambiguous_json(body):
    with pytest.raises(InvalidGraph):
        parse_body(body)


def test_invalid_graph_before_store_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid graph reached persistence")

    monkeypatch.setattr(ledger, "_run", forbidden)
    for graph in ({}, {**GRAPH, "host": "private"}, {**GRAPH, "blocks": [{}]},
                  {**GRAPH, "pipes": [{"from": {"node": "missing", "port": "out"}, "to": {"node": "missing", "port": "in"}}]}):
        with pytest.raises(InvalidGraph):
            save(graph=graph)


def test_schema_bounds_depth_and_duplicate_nodes():
    fixture = json.loads((ROOT / "contract/fixtures/lab/graphs/first-cut.json").read_text(encoding="utf-8"))
    assert validate_graph(fixture)
    invalid = copy.deepcopy(fixture)
    invalid["blocks"].append(copy.deepcopy(invalid["blocks"][0]))
    with pytest.raises(InvalidGraph):
        validate_graph(invalid)
    with pytest.raises(InvalidGraph):
        validate_graph({**GRAPH, "blocks": [{}] * 201})
    nested = []
    for _ in range(34):
        nested = [nested]
    with pytest.raises(InvalidGraph):
        validate_graph({**GRAPH, "blocks": nested})


def test_two_sqlite_writers_compare_and_swap():
    save()
    barrier = threading.Barrier(2)

    def writer(_):
        barrier.wait(timeout=10)
        try:
            return save(1)["revision"]
        except ledger.Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    assert results.count(2) == results.count("conflict") == 1
    assert len(ledger.list_revisions("tenant-a", "strategy")) == 2


def test_http_save_restore_publish_diff_and_tenant_boundary():
    http = client()
    assert http.post("/api/lab/docs/strategy/save", json={"graph": GRAPH, "expected_head": None}).status_code == 200
    assert http.post("/api/lab/docs/strategy/save", json={"graph": GRAPH, "expected_head": None}).status_code == 409
    assert http.get("/api/lab/docs/strategy/head").json()["revision"] == 1
    assert http.get("/api/lab/docs/strategy/revisions").json()["revisions"][0]["revision"] == 1
    assert http.get("/api/lab/docs/strategy/revisions/1").status_code == 200
    assert http.post("/api/lab/docs/strategy/restore", json={"revision": 1, "expected_head": 1}).json()["revision"] == 2
    assert http.get("/api/lab/docs/strategy/diff?before=1&after=2").json()["changes"] == []
    assert http.post("/api/lab/docs/strategy/publish", json={"expected_head": 1}).status_code == 409
    assert http.post("/api/lab/docs/strategy/publish", json={"expected_head": 2}).json()["new_head"] == 2
    other = client("tenant-b")
    for path in ("head", "revisions", "revisions/1", "diff?before=1&after=2"):
        assert other.get("/api/lab/docs/strategy/" + path).status_code == 404
    assert other.post("/api/lab/docs/strategy/restore", json={"revision": 1, "expected_head": 1}).status_code == 404
    assert other.post("/api/lab/docs/strategy/publish", json={"expected_head": 1}).status_code == 404


@pytest.mark.parametrize("stored_kind", ["widget", "graph"])
@pytest.mark.parametrize("requested_kind", ["widget", "graph", None])
@pytest.mark.parametrize("expected_head", [None, 1])
def test_F4_kind_change_conflicts_before_payload_validation(stored_kind, requested_kind, expected_head):
    widget = json.loads((ROOT / "contract/fixtures/widget/parity/basic.json").read_text(
        encoding="utf-8"))["doc"]
    document = widget if stored_kind == "widget" else GRAPH
    first = ledger.save("tenant-a", "fixed", document, None, "member", kind=stored_kind)
    body = {"graph": document, "expected_head": expected_head}
    if requested_kind is not None:
        body["kind"] = requested_kind
    response = client().post("/api/lab/docs/fixed/save", json=body)
    if (requested_kind or "graph") != stored_kind:
        assert response.status_code == 409
        assert response.json() == {"detail": "lab_document_kind_fixed"}
        assert ledger.get("tenant-a", "fixed")["sha256"] == first["sha256"]
        assert len(ledger.list_revisions("tenant-a", "fixed")) == 1
    elif expected_head is None:
        assert response.status_code == 409
        assert response.json() == {"detail": "stale_expected_head"}
    else:
        assert response.status_code == 200
        assert response.json()["kind"] == stored_kind


def test_http_head_carries_published_revision_but_numbered_revision_does_not():
    http = client()
    save()
    head = http.get("/api/lab/docs/strategy/head")
    assert head.status_code == 200
    assert head.json()["published_revision"] is None
    save(1)
    published = http.post("/api/lab/docs/strategy/publish", json={"expected_head": 2})
    assert published.status_code == 200
    assert published.json()["new_head"] == 2
    head = http.get("/api/lab/docs/strategy/head")
    assert head.status_code == 200
    assert head.json()["revision"] == head.json()["published_revision"] == 2
    revision = http.get("/api/lab/docs/strategy/revisions/2")
    assert revision.status_code == 200
    assert revision.json()["revision"] == 2
    assert "published_revision" not in revision.json()


def test_http_status_completes_while_save_is_pending(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_save = ledger.save

    def blocked_save(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10), "save was not released"
        return original_save(*args, **kwargs)

    monkeypatch.setattr(ledger, "save", blocked_save)
    # A context-managed client shares one event loop across both requests.
    with client() as http, ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(http.post, "/api/lab/docs/strategy/save",
                              json={"graph": GRAPH, "expected_head": None})
        try:
            assert entered.wait(timeout=5), "save did not reach the store"
            status = pool.submit(http.get, "/api/lab/status").result(timeout=3)
            assert status.status_code == 200
            assert status.json()["ok"] is True
            assert not release.is_set()
            assert not pending.done()
        finally:
            release.set()
        saved = pending.result(timeout=5)
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1
        assert saved.json()["sha256"] == graph_digest(GRAPH)


def test_http_invalid_and_oversize_never_create_database(local_store):
    http = client()
    endpoint = "/api/lab/docs/strategy/save"
    assert http.post(endpoint, json={"graph": {}, "expected_head": None}).status_code == 422
    assert http.post(endpoint, json={"graph": GRAPH}).status_code == 422
    assert http.post(endpoint, content=b"x" * (MAX_RAW_BYTES + 1)).status_code == 413
    assert not local_store.exists()


@pytest.mark.parametrize("schema,pointer", [
    ({}, None), ({"$ref": "https://example.invalid/schema"}, "/$ref"),
    ({"description": "x" * 16384}, ""),
])
def test_C1_graph_save_validates_returns_schema(local_store, schema, pointer):
    from test_lab_validate import manifest

    data = manifest()
    data["returns_schema"] = schema
    graph = dict(GRAPH, blocks=[{
        "id": "example", "registry": {"id": "example", "version": "1.0.0", "code_sha256": "0" * 64},
        "settings": {}, "manifest": data,
    }])
    response = client().post("/api/lab/docs/strategy/save", json={"graph": graph, "expected_head": None})
    if pointer is None:
        assert response.status_code == 200
        assert response.json()["graph"] == graph
    else:
        assert response.status_code == 422
        assert response.json() == {
            "detail": "lab_returns_schema_bounds",
            "pointer": "/blocks/0/manifest/returns_schema" + pointer, "block": data["id"],
        }
        assert not local_store.exists()


def test_save_manifest_spoof_names_block(local_store):
    from test_lab_validate import manifest

    data = manifest()
    data.update(id="example", effect="read")
    graph = dict(GRAPH, blocks=[{
        "id": "example", "registry": {"id": "example", "version": "1.0.0", "code_sha256": "0" * 64},
        "settings": {}, "manifest": data,
    }])
    with client() as http:
        response = http.post("/api/lab/docs/strategy/save", json={"graph": graph, "expected_head": None})
    assert response.status_code == 422
    assert response.json() == {
        "detail": "lab_manifest_spoof", "pointer": "/blocks/0/manifest/effect", "block": "example",
    }
    assert not local_store.exists()


def test_J4_graph_save_refuses_trailing_newline_manifest_id(local_store):
    from test_lab_validate import manifest

    data = manifest()
    data["id"] = "example\n"
    graph = dict(GRAPH, blocks=[{
        "id": "example", "registry": {"id": "example", "version": "1.0.0", "code_sha256": "0" * 64},
        "settings": {}, "manifest": data,
    }])
    response = client().post("/api/lab/docs/strategy/save", json={"graph": graph, "expected_head": None})
    assert response.status_code == 422
    assert response.json() == {
        "detail": "invalid_lab_document", "pointer": "/blocks/0/manifest/id", "block": data["id"],
    }
    assert not local_store.exists()


@pytest.mark.parametrize("action", ["save", "restore"])
@pytest.mark.parametrize("raw", [
    b'{"expected_head":null,"expected_head":null}', b'{', b'[]',
    b'{"expected_head":NaN}',
])
def test_C2_parse_errors_have_document_shape(local_store, action, raw):
    response = client().post("/api/lab/docs/strategy/" + action, content=raw)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_lab_document", "pointer": "", "block": None}
    assert not local_store.exists()


@pytest.mark.parametrize("action,body", [
    ("save", {"graph": GRAPH, "expected_head": -1}),
    ("restore", {"revision": 1, "expected_head": -1}),
])
def test_C2_validation_errors_have_document_shape(local_store, action, body):
    response = client().post("/api/lab/docs/strategy/" + action, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_lab_document", "pointer": "", "block": None}
    assert not local_store.exists()


def test_http_chunked_body_bound(local_store):
    response = client().post("/api/lab/docs/strategy/save", content=iter([b"x" * 65536] * 5))
    assert response.status_code == 413
    assert not local_store.exists()


def test_http_build_and_deploy_entitlements(local_store):
    assert client(tier="restricted").post("/api/lab/docs/strategy/save", json={"graph": GRAPH, "expected_head": None}).status_code == 403
    assert not local_store.exists()
    starter = client(tier="hosted_starter")
    assert starter.post("/api/lab/docs/strategy/save", json={"graph": GRAPH, "expected_head": None}).status_code == 200
    assert starter.post("/api/lab/docs/strategy/publish", json={"expected_head": 1}).status_code == 403
    assert client(tier=None).get("/api/lab/docs/strategy/head").status_code == 403


def test_http_rail_and_auth_precede_every_write(monkeypatch, local_store):
    http = client()
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "0")
    for action in ("save", "restore", "publish"):
        assert http.post("/api/lab/docs/strategy/" + action, json={}).status_code == 404
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert http.post("/api/lab/docs/strategy/save", json={}).status_code == 503
    assert not local_store.exists()


def test_failures_emit_only_allowlisted_context(caplog, monkeypatch):
    save()
    with pytest.raises(ledger.Conflict):
        save()
    with pytest.raises(ledger.Conflict):
        ledger.publish("tenant-a", "strategy", 2, "member")

    def fail(*args, **kwargs):
        raise RuntimeError("postgres://private-host/token")

    monkeypatch.setattr(ledger, "_run", fail)
    response = client().post("/api/lab/docs/strategy/save", json={"graph": GRAPH, "expected_head": 1})
    assert response.status_code == 503
    assert "private-host" not in response.text + caplog.text
    events = [record for record in caplog.records if record.name == "lab_artifacts"]
    assert {record.msg for record in events} == {"lab.save_failed", "lab_save_conflict", "lab_publish_cas_fail"}
    for record in events:
        assert record.tenant == "tenant-a"
        assert record.doc_id == "strategy"
        assert record.reason in {"stale_expected_head", "save_refused"}
        assert record.args == () and record.exc_info is None


def test_postgres_two_writer_cas_and_immutable_bytes(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("platform DB unreachable: DATABASE_URL unset")
    import psycopg

    try:
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=8):
            pass
    except Exception as exc:
        pytest.skip(f"platform DB unreachable: {type(exc).__name__}: {exc}")
    db = ledger.platform_link.platform_db()
    db.apply_migration()
    monkeypatch.setenv("LEAF_LAB_STORE", "postgres")
    tenant = "lab-test-" + uuid.uuid4().hex
    barrier = threading.Barrier(2)

    def writer(_):
        barrier.wait(timeout=10)
        try:
            return save(tenant=tenant)["revision"]
        except ledger.Conflict:
            return "conflict"

    try:
        db.assert_schema_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, range(2)))
        assert results.count(1) == results.count("conflict") == 1
        first = ledger.get(tenant, "strategy")
        assert first["sha256"] == graph_digest(GRAPH)
        ledger.restore(tenant, "strategy", 1, 1, "restorer")
        receipt = ledger.publish(tenant, "strategy", 2, "publisher")
        assert receipt["sha256"] == first["sha256"]
        assert len(ledger.list_revisions(tenant, "strategy")) == 2
        with pytest.raises(ledger.NotFound):
            ledger.get("foreign-" + tenant, "strategy", 1)
        for table in ("lab_blobs", "lab_revisions", "lab_publish_receipts"):
            for statement in (f"UPDATE {table} SET tenant_id=tenant_id WHERE tenant_id=%s",
                              f"DELETE FROM {table} WHERE tenant_id=%s"):
                with pytest.raises(Exception) as caught:
                    with db.transaction() as conn:
                        conn.execute(statement, (tenant,))
                assert getattr(caught.value, "sqlstate", None) == "55000"
    finally:
        db.reset_pool()
