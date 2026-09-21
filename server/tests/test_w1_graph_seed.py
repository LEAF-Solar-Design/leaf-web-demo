"""Offline checks for the first Solar graph and its graphless parent."""
import copy
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import store
import leaf_cloud_client as cloud
from solar_design_graph import GraphValidationError, serialize_graph, validate_graph
from solar_graph_seed import new_empty_graph, resolve_seed_context, seed_entity_id, seed_units
from solar_sizing_client import checked_graph
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import seed
from test_w1_graph_versions import drawing, request_for, commit, TENANT, DRAWING  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline network forbidden")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


@contextmanager
def refused(code):
    with pytest.raises(GraphValidationError) as excinfo:
        yield
    assert excinfo.value.code == code


@contextmanager
def held(backend):
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    try:
        yield fence
    finally:
        store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


def units():
    return {"drawing_units": "m", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0,
                                                0, 0, 1, 0, 0, 0, 0, 1],
            "elevation_datum": "local", "crs": None}


def empty_graph(**overrides):
    args = {"tenant_id": "fixture-tenant", "drawing_id": "solar", "source_hash": "b" * 64,
            "units": units(), "created_at": "2026-09-21T00:00:00Z"}
    args.update(overrides)
    return new_empty_graph(**args)


@pytest.mark.parametrize("tenant,drawing_id,kind,expected", [
    ("fixture-tenant", "solar", "project", "leaf:project:0c30002c-4ce6-4673-979a-dcb5f2d2c852"),
    ("fixture-tenant", "solar", "settings", "leaf:settings:984663e5-fa35-4647-a170-35c8b60e01d8"),
    ("another-tenant", "solar", "project", "leaf:project:6c8796fe-5e49-4e76-909a-627c6fca86b3"),
    ("fixture-tenant", "other", "project", "leaf:project:1088fb2a-ee66-49d2-bc60-7994761a1763"),
])
def test_entity_id_literals(tenant, drawing_id, kind, expected):
    assert seed_entity_id(tenant, drawing_id, kind) == expected
    assert seed_entity_id(tenant, drawing_id, kind) == expected
    assert re.fullmatch(
        r"leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        expected)


def test_unknown_entity_kind():
    with refused("UNKNOWN_ENTITY_KIND"):
        seed_entity_id("fixture-tenant", "solar", "panel")


def test_complete_empty_graph_and_determinism():
    value = empty_graph()
    provenance = {"created_by": "solar-seed", "created_at": "2026-09-21T00:00:00Z",
                  "last_writer": "solar-seed", "source_rev": 0, "source_hash": "b" * 64,
                  "tool_id": "solar-settings"}
    project = {
        "id": "leaf:project:0c30002c-4ce6-4673-979a-dcb5f2d2c852", "kind": "project",
        "rev": 0, "extra": {}, "provenance": provenance,
        "validity": {"state": "unknown", "reasons": ["seed_defaults"]},
        "name": "", "zip_code": "", "latitude": None, "longitude": None,
        "installation_design": "Roof", "graph_schema_version": 1,
        "site_revision": "seed-bbbbbbbbbbbbbbbb",
        "units": {"drawing_units": "m", "meters_per_unit": 1, "source": "explicit",
                  "compute_units": "m", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0,
                                                       0, 0, 1, 0, 0, 0, 0, 1],
                  "elevation_datum": "local", "crs": None,
                  "drawing_unit_is_feet": False, "warnings": []}}
    settings = {
        "id": "leaf:settings:984663e5-fa35-4647-a170-35c8b60e01d8", "kind": "settings",
        "rev": 0, "extra": {}, "provenance": provenance,
        "validity": {"state": "valid", "reasons": []},
        "panel_layer_contains": "Panel", "panel_group_layer": "Panel Group",
        "string_layer": "String", "home_run_layer": "HomeRun", "panels_in_sequence": 0,
        "num_mppt": 0, "strings_per_mppt": 0, "optimizer_ratio": 1, "use_l2_collectors": False,
        "panel_group_number": 1, "string_number": 1, "inverter_number": 1, "mppt_letter": "A",
        "global_string_sizing_confirmed": False,
        "voc_cold": {"passes": None, "override_accepted": False, "suggested_string_length": None,
                     "per_module": None, "string_voltage": None, "max_dc_voltage": None}}
    assert value["project"] == project
    assert value["settings"] == settings
    assert value == {
        "graph_schema_version": 1, "rev": 0, "parent_rev": None, "source_hash": "b" * 64,
        "catalog_versions": {}, "project": project, "settings": settings,
        "electrical_zones": [], "frames": [], "panels": [], "strings": [], "inverters": [],
        "routes": [], "schedules": [], "opaque_stores": {}, "orphaned_xdata": [],
        "extra": {"seed": {"schema_version": 1, "source_intake_sha256": "b" * 64}}}
    assert validate_graph(value) == value
    assert serialize_graph(value) == serialize_graph(empty_graph())


@pytest.mark.parametrize("name,scale", [
    ("m", 1), ("mm", .001), ("cm", .01), ("km", 1000),
    ("in", .0254), ("ft", .3048), ("yd", .9144),
])
def test_all_units_accepted_by_builtins(name, scale):
    request = units()
    request["drawing_units"] = name
    value = empty_graph(units=request)
    assert value["project"]["units"]["meters_per_unit"] == scale
    assert value["project"]["units"]["drawing_unit_is_feet"] is (name == "ft")
    assert checked_graph(value, 0) == value


def test_settings_builtin_accepts_first_graph():
    spec = importlib.util.spec_from_file_location(
        "seed_test_solar_settings", SERVER / "builtins" / "solar_settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run(empty_graph(), {"expected_rev": 0, "changes": {"panels_in_sequence": 3}})
    assert result["rev"] == 1
    assert result["parent_rev"] == 0
    assert result["settings"]["panels_in_sequence"] == 3
    assert result["settings"]["rev"] == 1
    for collection in ("electrical_zones", "frames", "panels", "strings", "inverters",
                       "routes", "schedules"):
        assert result[collection] == []


@pytest.mark.parametrize("defect", [
    "not_dict", "unknown_unit", "short_matrix", "boolean", "nan", "extra", "missing",
    "empty_datum", "integer_crs", "infinity", "long_datum", "long_crs", "tuple_matrix",
])
def test_invalid_units(defect):
    request = units()
    if defect == "not_dict":
        request = []
    elif defect == "unknown_unit":
        request["drawing_units"] = "furlong"
    elif defect == "short_matrix":
        request["wcs_to_ucs"].pop()
    elif defect in ("boolean", "nan", "infinity"):
        request["wcs_to_ucs"][0] = {"boolean": True, "nan": float("nan"),
                                    "infinity": float("inf")}[defect]
    elif defect == "extra":
        request["meters_per_unit"] = 1
    elif defect == "missing":
        del request["crs"]
    elif defect == "empty_datum":
        request["elevation_datum"] = ""
    elif defect == "integer_crs":
        request["crs"] = 1
    elif defect == "long_datum":
        request["elevation_datum"] = "a" * 4097
    elif defect == "long_crs":
        request["crs"] = "a" * 4097
    else:
        request["wcs_to_ucs"] = tuple(request["wcs_to_ucs"])
    with refused("INVALID_SEED_REQUEST"):
        seed_units(request)


def test_units_are_isolated():
    request = units()
    before = copy.deepcopy(request)
    first = seed_units(request)
    second = seed_units(request)
    first["wcs_to_ucs"][0] = 7
    first["warnings"].append("changed")
    assert request == before
    assert second["wcs_to_ucs"] == before["wcs_to_ucs"]
    assert second["warnings"] == []


@pytest.mark.parametrize("overrides", [
    {"source_hash": "B" * 64}, {"source_hash": "b" * 63}, {"tenant_id": ""},
    {"drawing_id": ""}, {"created_at": ""}, {"created_at": "a" * 4097},
])
def test_invalid_graph_request(overrides):
    with refused("INVALID_SEED_REQUEST"):
        empty_graph(**overrides)


def graphless_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [],
              "faces3d": [], "blockdefs": [], "geodata": None}
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, "fixture-tenant", str(path), drawing_id="solar")
    return backend, intake


def parent_entry(backend, version=1):
    return store.resolve_version_entry(backend, "fixture-tenant", "solar", version)[2]


def read_context(backend, *, tenant="fixture-tenant", drawing_id="solar", version=1,
                 source_hash=None):
    if source_hash is None:
        source_hash = parent_entry(backend)["sha256"]
    before = copy.deepcopy(store.load_manifest(backend, "fixture-tenant", "solar"))
    try:
        return resolve_seed_context(backend, tenant, drawing_id, version,
                                    source_intake_sha256=source_hash)
    finally:
        after = store.load_manifest(backend, "fixture-tenant", "solar")
        assert len(after["versions"]) == len(before["versions"])
        assert after == before


def append_bytes(backend, data):
    with held(backend) as fence:
        write_loop._put_bytes_version(
            backend, "fixture-tenant", "solar", data, parent_version=1, meta={},
            holder="fixture-owner", fence=fence, require_parent_is_head=True)


def test_graphless_current_parent(tmp_path, monkeypatch):
    backend, intake = graphless_seed(tmp_path, monkeypatch)
    entry = parent_entry(backend)
    result = read_context(backend)
    assert result == {"resolved_version": 1, "current_head": 1, "intake": intake,
                      "intake_sha256": entry["sha256"], "created": entry["created"],
                      "seed_ready": True, "refusal_reason": None}


def test_old_graphless_parent(tmp_path, monkeypatch):
    backend, intake = graphless_seed(tmp_path, monkeypatch)
    entry = parent_entry(backend)
    append_bytes(backend, json.dumps(dict(intake, layers=["new"])).encode("utf-8"))
    result = read_context(backend, source_hash=entry["sha256"])
    assert result == {"resolved_version": 1, "current_head": 2, "intake": intake,
                      "intake_sha256": entry["sha256"], "created": entry["created"],
                      "seed_ready": False, "refusal_reason": "not_current_head"}


@pytest.mark.parametrize("wrong_hash", [False, True])
def test_embedded_graph_precedes_caller_hash(graph, tmp_path, monkeypatch, wrong_hash):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with refused("GRAPH_ALREADY_EMBEDDED"):
        read_context(backend, source_hash="0" * 64 if wrong_hash else None)


@pytest.mark.parametrize("companion", ["solar_design_graph", "solar_design_graph_sha256"])
def test_partial_companion_is_not_seedable(tmp_path, monkeypatch, companion):
    backend, intake = graphless_seed(tmp_path, monkeypatch)
    intake[companion] = None
    append_bytes(backend, json.dumps(intake).encode("utf-8"))
    with refused("INVALID_SEED_PARENT"):
        read_context(backend, version=2, source_hash=parent_entry(backend, 2)["sha256"])


@pytest.mark.parametrize("data", [b"raw DWG bytes", b"[]", b"\xff",
                                  b"[" * 5000 + b"0" + b"]" * 5000])
def test_invalid_intake_bytes(tmp_path, monkeypatch, data):
    backend, _ = graphless_seed(tmp_path, monkeypatch)
    append_bytes(backend, data)
    with refused("INVALID_SEED_PARENT"):
        read_context(backend, version=2, source_hash=parent_entry(backend, 2)["sha256"])


def test_stored_hash_must_match_bytes(tmp_path, monkeypatch):
    backend, _ = graphless_seed(tmp_path, monkeypatch)
    _, key, _ = store.resolve_version_entry(backend, "fixture-tenant", "solar", 1)
    backend.put(key, b'{"changed":true}')
    with refused("INVALID_SEED_PARENT"):
        read_context(backend)


@pytest.mark.parametrize("overrides,code", [
    ({"source_hash": "0" * 64}, "SOURCE_HASH_MISMATCH"),
    ({"drawing_id": "missing"}, "GRAPH_CONTEXT_UNAVAILABLE"),
    ({"tenant": "another-tenant"}, "GRAPH_CONTEXT_UNAVAILABLE"),
    ({"tenant": ""}, "GRAPH_CONTEXT_UNAVAILABLE"),
    ({"version": 0}, "INVALID_PARENT_VERSION"),
    ({"version": True}, "INVALID_PARENT_VERSION"),
    ({"version": "1"}, "INVALID_PARENT_VERSION"),
    ({"source_hash": "xyz"}, "INVALID_SEED_REQUEST"),
])
def test_context_refusals(tmp_path, monkeypatch, overrides, code):
    backend, _ = graphless_seed(tmp_path, monkeypatch)
    with refused(code):
        read_context(backend, **overrides)


def test_bundle_parent_refused(drawing, graph):
    backend, _ = drawing
    commit(drawing, request_for(backend, graph))
    _, _, entry = store.resolve_version_entry(backend, TENANT, DRAWING, 2)
    before = copy.deepcopy(store.load_manifest(backend, TENANT, DRAWING))
    with refused("INVALID_SEED_PARENT"):
        resolve_seed_context(backend, TENANT, DRAWING, 2,
                             source_intake_sha256=entry["sha256"])
    after = store.load_manifest(backend, TENANT, DRAWING)
    assert len(after["versions"]) == len(before["versions"])
    assert after == before
