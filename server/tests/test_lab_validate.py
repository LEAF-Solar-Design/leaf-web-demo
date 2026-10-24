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
    value.update(kind="qa-sleep-count", effect="pure", residency="none")
    return value


def test_valid_manifest():
    assert validate_manifest(manifest()) is lookup("qa-sleep-count", "1.0.0")


@pytest.mark.parametrize("field", ["capability", "solar_adapter"])
@pytest.mark.parametrize("value", [
    "read.value\n", "simulate\n", "Read.value", "a" * 129, "", " read.value",
    "read..value", "read.Value", "read.-value", "read/value",
])
def test_K1_manifest_capability_grammar(field, value):
    data = manifest()
    data[field] = value
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.to_http() == (422, {
        "detail": "lab_manifest_invalid", "pointer": "/" + field, "block": data["id"],
    })


@pytest.mark.parametrize("capability,adapter", [
    ("read.engine-lab", None), ("a" * 128, "simulate"), ("read.0_value", "adapter.v2"),
])
def test_K1_manifest_valid_capability_text(capability, adapter):
    data = manifest()
    data.update(capability=capability, solar_adapter=adapter)
    assert validate_manifest(data) is lookup("qa-sleep-count", "1.0.0")


@pytest.mark.parametrize("field", ["id", "version"])
def test_J4_identity_requires_full_string_match(field):
    data = manifest()
    if field == "id":
        data["id"] = "example\n"
    else:
        data["version"]["semver"] += "\n"
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.to_http() == (422, {
        "detail": "lab_manifest_invalid", "pointer": "/" + field, "block": data["id"],
    })


@pytest.mark.parametrize("schema,pointer", [
    ({}, None), ({"type": "object"}, None),
    ({"$ref": "https://example.invalid/schema"}, "/$ref"),
    ({"description": "x" * 16384}, ""), ([], ""),
])
def test_C1_manifest_admits_and_bounds_returns_schema(schema, pointer):
    data = manifest()
    data["returns_schema"] = schema
    original = copy.deepcopy(data)
    trusted = lookup(data["kind"], data["version"]["semver"])
    if pointer is None:
        block = validate_manifest(data)
        assert block.returns_schema_digest == validate_returns_schema(schema)
        assert block.effect == trusted.effect
        assert block.residency == trusted.residency
        assert trusted.returns_schema_digest is None
    else:
        with pytest.raises(ManifestInvalid) as caught:
            validate_manifest(data)
        assert caught.value.to_http() == (422, {
            "detail": "lab_returns_schema_bounds", "pointer": "/returns_schema" + pointer,
            "block": data["id"],
        })
    assert data == original


@pytest.mark.parametrize("field,value", [
    ("effect", "read"), ("residency", "host"), ("residency", "tenant"),
    ("effect", "unknown"), ("residency", "unknown"),
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
