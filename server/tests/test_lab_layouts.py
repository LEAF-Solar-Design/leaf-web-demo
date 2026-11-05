"""Content-keyed display state, bounded HTTP input and tenant isolation."""
import copy
import json
from pathlib import Path
import re
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import deps
import lab_artifacts as ledger
import lab_layouts as layouts
from lab_canonical import canonical_bytes
from routers import lab_artifacts as routes, lab_router

ROOT = Path(__file__).resolve().parents[2]
GRAPH = json.loads((ROOT / "contract/fixtures/lab/graphs/first-cut.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_LAB_STORE", "sqlite")
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    return tmp_path / "lab.db"


def layout(**positions):
    return {"schema": layouts.LAYOUT_SCHEMA, "positions": positions or {"market": {"x": 12, "y": -34}}}


def save(tenant="tenant-a", doc="strategy", graph=GRAPH, expected=None):
    return ledger.save(tenant, doc, graph, expected, "member")


def client(tenant="tenant-a"):
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        tenant, tier="hosted_pro", subject="member")
    return TestClient(app, raise_server_exceptions=False)


def test_round_trip_last_write_wins_and_positions_never_change_digest(local_store):
    first = save()
    digest = first["sha256"]
    assert layouts.get_layout("tenant-a", "strategy", digest) is None
    layouts.put_layout("tenant-a", "strategy", digest, layout())
    assert layouts.get_layout("tenant-a", "strategy", digest) == layout()
    moved = layout(market={"x": -1_000_000, "y": 1_000_000})
    second = save(expected=1)
    layouts.put_layout("tenant-a", "strategy", second["sha256"], moved)
    assert second["sha256"] == digest
    assert ledger.get("tenant-a", "strategy", 1) == first
    assert layouts.get_layout("tenant-a", "strategy", digest) == moved
    with sqlite3.connect(local_store) as conn:
        rows = conn.execute("SELECT layout_text,byte_count,updated_at FROM lab_layouts").fetchall()
        assert len(rows) == 1
        assert rows[0][0].encode() == canonical_bytes(moved)
        assert rows[0][1] == len(canonical_bytes(moved))
        assert rows[0][2] > 0
        assert conn.execute("SELECT COUNT(*) FROM lab_blobs").fetchone()[0] == 1


def test_unknown_block_is_refused_without_replacing_layout():
    digest = save()["sha256"]
    layouts.put_layout("tenant-a", "strategy", digest, layout())
    with pytest.raises(layouts.LayoutRefused) as error:
        layouts.put_layout("tenant-a", "strategy", digest, layout(foreign={"x": 0, "y": 0}))
    assert error.value.code == "lab_layout_unknown_block"
    assert layouts.get_layout("tenant-a", "strategy", digest) == layout()


def test_position_count_is_bounded():
    points = {f"block-{i}": {"x": 0, "y": 0} for i in range(201)}
    with pytest.raises(layouts.LayoutRefused) as error:
        layouts.validate_layout(layout(**points), set(points))
    assert error.value.code == "lab_layout_too_large"
    points.pop("block-200")
    assert layouts.validate_layout(layout(**points), set(points))


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), -float("inf"),
                                     1_000_001, -1_000_001, True, "1", None, 10**1000])
def test_coordinates_are_bounded_finite_numbers(coordinate):
    for axis in ("x", "y"):
        point = {"x": 0, "y": 0, axis: coordinate}
        with pytest.raises(layouts.LayoutRefused) as error:
            layouts.validate_layout(layout(market=point), {"market"})
        assert error.value.code == "lab_layout_invalid"


@pytest.mark.parametrize("value", [None, [], {}, {"schema": "other", "positions": {}},
    {**layout(), "extra": 1}, {"schema": layouts.LAYOUT_SCHEMA, "positions": []},
    layout(market={"x": 0}), layout(market={"x": 0, "y": 0, "z": 0})])
def test_layout_shape_is_exact(value):
    with pytest.raises(layouts.LayoutRefused) as error:
        layouts.validate_layout(value, {"market"})
    assert error.value.code == "lab_layout_invalid"


def test_canonical_byte_limit_includes_keys_and_envelope():
    overhead = len(canonical_bytes(layout(a={"x": 0, "y": 0}))) - 1
    key = "a" * (layouts.MAX_LAYOUT_BYTES - overhead)
    value = layout(**{key: {"x": 0, "y": 0}})
    assert len(layouts.validate_layout(value, {key})) == 32_768
    key += "a"
    value = layout(**{key: {"x": 0, "y": 0}})
    assert len(canonical_bytes(value)) == 32_769
    with pytest.raises(layouts.LayoutRefused) as error:
        layouts.validate_layout(value, {key})
    assert error.value.code == "lab_layout_too_large"


def test_unknown_revision_is_tenant_and_document_scoped():
    digest = save()["sha256"]
    for tenant, doc, sha in (("tenant-a", "strategy", "f" * 64),
                             ("tenant-b", "strategy", digest), ("tenant-a", "other", digest)):
        with pytest.raises(layouts.LayoutRefused) as error:
            layouts.put_layout(tenant, doc, sha, layout())
        assert error.value.code == "lab_layout_unknown_revision"


def test_layout_reads_do_not_cross_tenants_or_documents_even_for_identical_content():
    digest = save()["sha256"]
    save(tenant="tenant-b")
    save(doc="other")
    layouts.put_layout("tenant-a", "strategy", digest, layout())
    assert layouts.get_layout("tenant-b", "strategy", digest) is None
    assert layouts.get_layout("tenant-a", "other", digest) is None
    assert layouts.get_layout("tenant-a", "strategy", "f" * 64) is None
    moved = layout(market={"x": 90, "y": 80})
    layouts.put_layout("tenant-b", "strategy", digest, moved)
    assert layouts.get_layout("tenant-b", "strategy", digest) == moved
    assert layouts.get_layout("tenant-a", "strategy", digest) == layout()


def test_restore_reuses_the_older_contents_layout():
    first = save()
    layouts.put_layout("tenant-a", "strategy", first["sha256"], layout())
    changed = copy.deepcopy(GRAPH)
    changed["blocks"][0]["settings"]["mean_fast_index"] = 7
    second = save(graph=changed, expected=1)
    assert second["sha256"] != first["sha256"]
    assert layouts.get_layout("tenant-a", "strategy", second["sha256"]) is None
    restored = ledger.restore("tenant-a", "strategy", 1, 2, "member")
    assert restored["revision"] == 3
    assert restored["sha256"] == first["sha256"]
    assert layouts.get_layout("tenant-a", "strategy", restored["sha256"]) == layout()


@pytest.mark.parametrize("scope", [("", "strategy", "a" * 64), ("a" * 201, "strategy", "a" * 64),
    ("tenant-a", "../other", "a" * 64), ("tenant-a", "a" * 129, "a" * 64),
    ("tenant-a", "strategy", "a" * 65), ("tenant-a", "strategy", None)])
def test_invalid_scope_is_refused_before_store_access(monkeypatch, scope):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid scope reached persistence")

    monkeypatch.setattr(ledger, "_run", forbidden)
    for operation in (lambda: layouts.get_layout(*scope), lambda: layouts.put_layout(*scope, layout())):
        with pytest.raises(layouts.LayoutRefused) as error:
            operation()
        assert error.value.code == "lab_layout_invalid"


def test_http_round_trip_no_store_and_foreign_revision_refusal():
    digest = save()["sha256"]
    http = client()
    path = f"/api/lab/docs/strategy/layout/{digest}"
    response = http.put(path, json=layout())
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    response = http.get(path)
    assert response.status_code == 200
    assert response.json() == {"layout": layout()}
    assert response.headers["cache-control"] == "no-store"
    assert client("tenant-b").get(path).json() == {"layout": None}
    response = client("tenant-b").put(path, json=layout())
    assert response.status_code == 400
    assert response.json() == {"code": "lab_layout_unknown_revision"}
    response = http.put(path, json=layout(foreign={"x": 0, "y": 0}))
    assert response.status_code == 400
    assert response.json() == {"code": "lab_layout_unknown_block"}


@pytest.mark.parametrize("raw", [b'[]', b'\xff', b'{"schema":1,"schema":2}', b'{"x":NaN}', b'{} {}'])
def test_http_malformed_input_has_a_layout_error(raw):
    digest = save()["sha256"]
    response = client().put(f"/api/lab/docs/strategy/layout/{digest}", content=raw)
    assert response.status_code == 400
    assert response.json() == {"code": "lab_layout_invalid"}
    assert response.headers["cache-control"] == "no-store"


def test_http_caps_declared_and_streamed_bytes_before_parsing(monkeypatch):
    digest = save()["sha256"]

    def forbidden(*args, **kwargs):
        pytest.fail("oversized body reached parser")

    monkeypatch.setattr(routes, "parse_body", forbidden)
    for content, headers in ((b"{}", {"content-length": "32769"}),
                             (b" " * 32_769, {"content-length": "1"}),
                             (iter([b" " * 16_384, b" " * 16_385]), {})):
        response = client().put(f"/api/lab/docs/strategy/layout/{digest}", content=content, headers=headers)
        assert response.status_code == 413
        assert response.json() == {"code": "lab_layout_too_large"}
        assert response.headers["cache-control"] == "no-store"


def test_layout_routes_require_the_tenant_dependency():
    http = client()

    def unauthenticated():
        raise HTTPException(401, detail="authentication_required")

    http.app.dependency_overrides[deps.require_tenant] = unauthenticated
    path = "/api/lab/docs/strategy/layout/" + "a" * 64
    assert http.get(path).status_code == 401
    assert http.put(path, json=layout()).status_code == 401


def test_sqlite_schema_and_unapplied_migration_define_the_same_table(local_store):
    save()
    migration = (ROOT / "platform/migrations/0101_lab_layouts.sql").read_text(encoding="utf-8")
    columns = ["tenant_id", "doc_id", "sha256", "layout_text", "byte_count", "updated_at"]
    definitions = []
    for source in (ledger._SQLITE_SCHEMA, migration):
        definition = re.search(r"CREATE TABLE IF NOT EXISTS lab_layouts \((.*?)\);", source, re.S).group(1)
        assert re.findall(r"\b(\w+)\s+(?:TEXT|INTEGER|REAL)\s+NOT NULL", definition) == columns
        definitions.append(" ".join(definition.split()))
    assert definitions[0] == definitions[1]
    assert migration.count("CREATE TABLE") == 1
    assert "TRIGGER" not in migration
    with sqlite3.connect(local_store) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(lab_layouts)")] == columns
