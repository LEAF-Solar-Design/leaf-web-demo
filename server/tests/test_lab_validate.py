"""Trusted admission and canonical schema bounds."""
import copy
import json
from pathlib import Path

import pytest

from lab_registry import lookup
from lab_validate import ManifestInvalid, validate_manifest, validate_returns_schema

ROOT = Path(__file__).resolve().parents[2]
PORT_DIGEST = "1093ae826c50fd89996c5e611dd184c7b6c3a3a0d662f4a477b73bec2829e264"


def manifest():
    value = json.loads((ROOT / "contract/fixtures/lab/blocks.json").read_text(encoding="utf-8"))["manifests"][0]
    value.update(kind="qa-sleep-count", effect="pure", residency="tenant")
    return value


def test_valid_manifest():
    assert validate_manifest(manifest()) is lookup("qa-sleep-count", "1.0.0")


@pytest.mark.parametrize("field,value", [
    ("effect", "read"), ("residency", "host"), ("effect", "unknown"), ("residency", "unknown"),
])
def test_spoof(field, value):
    data = manifest()
    data[field] = value
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.to_http() == (422, {"detail": "lab_manifest_spoof", "pointer": "/" + field, "block": data["id"]})


def test_unknown_block():
    data = manifest()
    data["kind"] = "missing"
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.code == "lab_manifest_unknown_block"


@pytest.mark.parametrize("bad", ["Bad", "has_underscore", "a" * 65, "a--b"])
def test_id_grammar(bad):
    data = manifest()
    data["id"] = bad
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.pointer == "/id"


@pytest.mark.parametrize("schema,pointer", [
    ({"description": "x" * 16384}, ""),
    ({"properties": {"a/b~": {"$ref": "https://example.invalid/schema"}}}, "/properties/a~1b~0/$ref"),
    ({"$ref": 2}, "/$ref"), ({"$ref": "#"}, "/$ref"),
    ({"value": float("nan")}, ""), ([], ""),
])
def test_returns_bounds(schema, pointer):
    with pytest.raises(ManifestInvalid) as caught:
        validate_returns_schema(schema)
    assert caught.value.code == "lab_returns_schema_bounds"
    assert caught.value.pointer == pointer


def test_depth_and_size_boundaries():
    schema = {}
    for _ in range(8):
        schema = {"a": schema}
    validate_returns_schema(schema)
    with pytest.raises(ManifestInvalid):
        validate_returns_schema({"a": schema})
    validate_returns_schema({"a": "x" * (16384 - 8)})
    with pytest.raises(ManifestInvalid):
        validate_returns_schema({"a": "x" * (16385 - 8)})


def test_local_ref_and_cycles():
    validate_returns_schema({"$ref": "#/definitions/value"})
    cycle = {}
    cycle["a"] = cycle
    with pytest.raises(ManifestInvalid):
        validate_returns_schema(cycle)


@pytest.mark.parametrize("value,digest", [
    ({}, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    ({"a": 1}, "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"),
])
def test_digest_vectors(value, digest):
    assert validate_returns_schema(value) == digest


def test_port_fixture_digest():
    data = json.loads((ROOT / "contract/fixtures/lab/port-compat.v1.json").read_text(encoding="utf-8"))
    assert validate_returns_schema(data) == PORT_DIGEST


def test_existing_canonical_vectors():
    vectors = json.loads((ROOT / "contract/fixtures/lab/digest-vectors.json").read_text(encoding="utf-8"))["vectors"]
    for vector in vectors:
        if isinstance(vector["input"], dict):
            assert validate_returns_schema(vector["input"]) == vector["sha256"]


def test_graph_manifest_and_pointer():
    from lab_doc_validators import InvalidGraph, validate_graph
    data = manifest()
    block = {"id": "example", "registry": {"id": "example", "version": "1.0.0", "code_sha256": "0" * 64},
             "settings": {}, "manifest": data}
    graph = {"schema": "leaf.lab-graph.v1", "blocks": [block], "pipes": [], "outputs": {}}
    original = copy.deepcopy(graph)
    validate_graph(graph)
    assert graph == original
    data["effect"] = "read"
    with pytest.raises(InvalidGraph) as caught:
        validate_graph(graph)
    assert caught.value.pointer == "/blocks/0/manifest/effect"
    assert caught.value.code == "lab_manifest_spoof"
    assert caught.value.block == data["id"]


def test_save_manifest_spoof_names_block(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import deps
    from routers import lab_router

    monkeypatch.setenv("LEAF_LAB_STORE", "sqlite")
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier="hosted_pro", subject="member")
    data = manifest()
    data.update(id="example", effect="read")
    graph = {"schema": "leaf.lab-graph.v1", "blocks": [{
        "id": "example", "registry": {"id": "example", "version": "1.0.0", "code_sha256": "0" * 64},
        "settings": {}, "manifest": data,
    }], "pipes": [], "outputs": {}}
    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.post("/api/lab/docs/strategy/save", json={"graph": graph, "expected_head": None})
    assert response.status_code == 422
    assert response.json() == {
        "detail": "lab_manifest_spoof", "pointer": "/blocks/0/manifest/effect", "block": "example",
    }
    assert not (tmp_path / "lab.db").exists()
