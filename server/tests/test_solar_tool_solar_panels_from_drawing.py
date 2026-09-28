"""Solar graph origin from real stored outlines, with replay-bound commit proof."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import UUID

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import product_capability_availability as availability
import solar_local_graph as local
import solar_panel_group_kernel as kernel
import solar_tools
import store
import write_loop
from solar_graph_seed import new_empty_graph
from solar_sizing_client import digest
from test_solar_registry import package, load_package  # noqa: F401
from test_w1_local_graph_seed import (
    ORDINARY_KEYS, held, refused, rewrite_intake, request as seed_request,
    no_network,  # noqa: F401
)

TOOL = "solar-panels-from-drawing"
TENANT = "fixture-tenant"
DRAWING = "solar"
JOB = "panel-import"
UNITS = {"drawing_units": "ft", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0,
                                                    0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unknown", "crs": None}
P1 = {"layer": "Panels", "closed": True, "handle": "1A",
      "pts": [[0, 0], [77, 0], [77, 38.5], [0, 38.5]]}
P2 = {"layer": "roof PANEL layout", "closed": True, "handle": "1B",
      "pts": [[100, 0], [100, 77], [61.5, 77], [61.5, 0]]}
P3 = dict(P1, layer="Roof", handle="1C")
P4 = dict(P1, closed=False, handle="1D")
P5 = dict(P1, handle="1E", pts=[[0, 0], [77, 0], [70, 38.5], [0, 38.5]])
P6 = {"layer": "Panels", "closed": False, "handle": "2F",
      "pts": [[200, 0], [277, 0], [277, 38.5], [200, 38.5], [200, 0]]}
FIX = [P1, P2, P3, P4, P5, P6]


@pytest.fixture
def builtin():
    return local._load_builtin(TOOL)


def empty_graph(source_hash="a" * 64):
    return new_empty_graph(tenant_id=TENANT, drawing_id=DRAWING, source_hash=source_hash,
                           units=copy.deepcopy(UNITS), created_at="2026-09-26T00:00:00Z")


@pytest.fixture
def g1():
    return local._load_builtin("solar-settings").run(
        empty_graph(), {"expected_rev": 0, "changes": {"panel_layer_contains": "Panel"}})


def upload(tmp_path, monkeypatch, polylines):
    # Real upload and mutation admission, with no adapter or publication stubs.
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    intake = {"dwg": {}, "layers": [], "polylines": copy.deepcopy(polylines),
              "inserts": [], "faces3d": [], "blockdefs": [], "geodata": None,
              "custom": {"keep": [1, 2, 3]}}
    path = tmp_path / "drawing.json"
    path.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, TENANT, str(path), drawing_id=DRAWING)
    return backend


def seed_uploaded(backend):
    params = seed_request(backend)
    with held(backend) as fence:
        local.run_local_graph_commit(
            backend, TENANT, "solar-settings", params, drawing_id=DRAWING,
            source_version=1, holder="fixture-owner", fence=fence, job_id="panel-seed")


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    backend = upload(tmp_path, monkeypatch, FIX)
    seed_uploaded(backend)
    return backend


def run(backend, params=None, *, source_version=2, job_id=JOB):
    if params is None:
        params = {"drawing_id": DRAWING, "expected_rev": 1}
    with held(backend) as fence:
        return local.run_local_graph_commit(
            backend, TENANT, TOOL, params, drawing_id=DRAWING,
            source_version=source_version, holder="fixture-owner", fence=fence, job_id=job_id)


def proof(backend, result):
    return local.graph_commit_provenance(
        result, {"drawing_id": DRAWING, "expected_rev": 1}, TENANT, JOB, TOOL, 2,
        backend=backend)


def stored(backend, version):
    _, key = store.resolve_version(backend, TENANT, DRAWING, version)
    return json.loads(backend.get(key))


def preserved(intake):
    return {key: value for key, value in intake.items()
            if key not in ("solar_design_graph", "solar_design_graph_sha256")}


def version_count(backend, count):
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    assert len(manifest["versions"]) == count
    assert manifest["head"] == count


def expected_panel(graph, handle, centre, angle):
    normalized = handle.upper().lstrip("0") or "0"
    raw = hashlib.sha256((graph["source_hash"] + ":panel:" + normalized).encode()).digest()[:16]
    return {
        "id": "leaf:panel:" + str(UUID(bytes=raw, version=4)), "kind": "panel",
        "rev": graph["rev"] + 1, "extra": {}, "validity": {"state": "valid", "reasons": []},
        "provenance": {
            "created_by": TOOL, "created_at": graph["project"]["provenance"]["created_at"],
            "last_writer": TOOL, "source_rev": graph["rev"],
            "source_hash": graph["source_hash"], "source_handle": handle, "tool_id": TOOL,
        },
        "frame_ref": None, "matrix_cell": None, "centre": centre, "angle": angle,
        "assignment": {"string_ref": None, "seq": None},
    }


def assert_panels(after, before, handles=("1A", "1B", "2F")):
    geometry = {"1A": ([38.5, 19.25], 0.0), "1B": ([80.75, 38.5], 90.0),
                "2F": ([238.5, 19.25], 0.0)}
    assert after["panels"] == [expected_panel(before, h, *geometry[h]) for h in handles]
    assert after["rev"] == before["rev"] + 1
    assert after["parent_rev"] == before["rev"]
    assert {k: v for k, v in after.items() if k not in ("panels", "rev", "parent_rev")} == {
        k: v for k, v in before.items() if k not in ("panels", "rev", "parent_rev")}


def test_declaration_pins_the_capability():
    record = {
        "name": TOOL, "version": "1.0.0",
        "description": "Create the drawing's panels from closed rectangular polylines on the panel layer.",
        "kind": "script", "family_id": "placement", "engine_op": "solar_panels_from_drawing",
        "entry": "builtins/solar_panels_from_drawing.py",
        "params": {"type": "object", "properties": {
            "drawing_id": {"type": "string", "maxLength": 128},
            "expected_rev": {"type": "integer", "minimum": 0}, "cancel": {"type": "boolean"}},
            "required": ["expected_rev"], "additionalProperties": False},
        "returns": {"type": "object"}, "capabilities": ["drawing.write"],
        "allow_local_fallback": False,
    }
    expected = {
        "schema": "leaf.solar-tool.v1", "name": TOOL,
        "builtin": "builtins/solar_panels_from_drawing.py", "family": "placement",
        "adapter": "local-graph-commit", "entitlement": "run_write",
        "requires_persisted_graph": True, "seedable": False,
        "invalid_request_code": "INVALID_PANEL_IMPORT_REQUEST", "readiness": {"kind": "hook"},
        "engine": "server-builtin", "interaction": {"mode": "form"},
        "record_store": "registry", "record": record, "ledger": [],
        "trusted_inputs": ["source_intake"], "maturity": "preview", "wave": 1,
        "order": 15, "scenario": "graph-origin",
    }
    path = SERVER / "solar_tools" / "solar_panels_from_drawing.json"
    assert json.loads(path.read_text(encoding="utf-8")) == expected == solar_tools.get(TOOL)
    assert solar_tools.trusted_record(TOOL) == record
    assert TOOL not in availability.W1_CAPABILITIES
    assert len(availability.W1_CAPABILITIES) == 9


@pytest.mark.parametrize("adapter,seedable", [
    ("local-graph-commit", True), (None, False),
    ("cloud-proposal", False), ("local-graph-read", False),
])
def test_registry_refuses_source_intake_on_seed_or_non_local_tools(package, adapter, seedable):
    declaration = solar_tools.get(TOOL)
    declaration.update(adapter=adapter, seedable=seedable)
    (package[1] / "solar_panels_from_drawing.json").write_text(
        json.dumps(declaration), encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError,
                       match="^source_intake requires a non-seed local graph commit$"):
        load_package(package)


def test_trusted_resolvers_match_the_registry():
    assert solar_tools.TRUSTED_INPUTS == ("source_intake",)
    assert set(local._TRUSTED_RESOLVERS) == set(solar_tools.TRUSTED_INPUTS)
    assert local._TRUSTED_RESOLVERS["source_intake"] is local._source_intake


@pytest.mark.parametrize("row", list(range(1, 23)) + [27])
def test_builtin_case_table(builtin, g1, monkeypatch, row):
    graph, params, intake = copy.deepcopy(g1), {"expected_rev": 1}, {"polylines": copy.deepcopy(FIX)}
    errors = {
        3: "INVALID_PANEL_IMPORT_REQUEST", 4: "INVALID_PANEL_IMPORT_REQUEST",
        5: "INVALID_PANEL_IMPORT_REQUEST", 6: "STALE_GRAPH_REVISION",
        7: "STALE_GRAPH_REVISION", 8: "UNRESOLVED_UNITS", 9: "PANELS_ALREADY_PRESENT",
        10: "PANEL_LAYER_FILTER_REQUIRED", 11: "PANEL_LAYER_FILTER_REQUIRED",
        13: "SOURCE_INTAKE_REQUIRED", 14: "INVALID_SOURCE_INTAKE",
        15: "NO_PANELS_RECOGNISED", 16: "NO_PANELS_RECOGNISED",
        17: "AMBIGUOUS_PANEL_HANDLE", 18: "AMBIGUOUS_PANEL_HANDLE",
        19: "AMBIGUOUS_PANEL_HANDLE", 20: "INVALID_PANEL_HANDLE",
        21: "UNSUPPORTED_PANEL_OUTLINE", 22: "PANEL_LIMIT_EXCEEDED",
    }
    if row == 2:
        params["cancel"] = True
    elif row == 3:
        params = []
    elif row == 4:
        params["layer"] = "Panels"
    elif row == 5:
        params["cancel"] = "yes"
    elif row == 6:
        params["expected_rev"] = 0
    elif row == 7:
        params = {}
    elif row == 8:
        graph["project"]["units"]["meters_per_unit"] = 1
    elif row == 9:
        graph = builtin.run(graph, params, source_intake=intake)
        params["expected_rev"] = 2
    elif row in (10, 11, 12):
        graph["settings"]["panel_layer_contains"] = {10: "  ", 11: "", 12: "roof panel"}[row]
    elif row == 13:
        intake = None
    elif row == 14:
        intake["polylines"] = {"a": 1}
    elif row == 15:
        intake["polylines"] = copy.deepcopy([P3, P4, P5])
    elif row == 16:
        intake = {}
    elif row == 17:
        intake["polylines"] = [dict(P1, handle="1a"), copy.deepcopy(P1)]
    elif row == 18:
        intake["polylines"] = [dict(P1, handle="0A"), dict(P1, handle="A")]
    elif row == 19:
        intake["polylines"] = [copy.deepcopy(P1), dict(P3, handle="1a")]
    elif row == 20:
        intake["polylines"] = [dict(P1, handle="ZZ")]
    elif row == 21:
        intake["polylines"] = [dict(P1, handle="3A", pts=[
            [0, 0], [38.5, 0], [77, 0], [77, 38.5], [0, 38.5]])]
        assert [p["handle"] for p in kernel.panels_from_intake(intake)] == ["3A"]
    elif row == 22:
        monkeypatch.setattr(builtin, "MAX_PANELS", 2)
    elif row == 27:
        intake["polylines"] = [dict(P1, pts=[[str(x), str(y)] for x, y in P1["pts"]])]
    if row in errors:
        with refused(errors[row]):
            builtin.run(graph, params, source_intake=intake)
    else:
        after = builtin.run(graph, params, source_intake=intake)
        if row == 2:
            assert after == graph
        else:
            handles = {12: ("1B",), 27: ("1A",)}.get(row, ("1A", "1B", "2F"))
            assert_panels(after, graph, handles)


@pytest.mark.parametrize("row", [23, 24, 25, 26])
def test_builtin_refusal_precedence(builtin, g1, row):
    graph, params, intake = copy.deepcopy(g1), {"expected_rev": 1}, None
    if row == 23:
        graph = builtin.run(graph, params, source_intake={"polylines": copy.deepcopy(FIX)})
        assert builtin.run(graph, {"expected_rev": 2, "cancel": True}) == graph
        return
    graph["settings"]["panel_layer_contains"] = ""
    if row in (24, 25):
        graph["project"]["units"]["meters_per_unit"] = 1
    if row == 24:
        params["expected_rev"] = 0
    if row == 25:
        intake = {"polylines": copy.deepcopy(FIX)}
    with refused({24: "STALE_GRAPH_REVISION", 25: "UNRESOLVED_UNITS",
                  26: "PANEL_LAYER_FILTER_REQUIRED"}[row]):
        builtin.run(graph, params, source_intake=intake)


def test_run_publishes_the_recognised_panels(seeded):
    parent = stored(seeded, 2)
    result = run(seeded)
    assert set(result) == ORDINARY_KEYS
    assert result["before_rev"] == 1 and result["after_rev"] == 2
    assert result["new_version"] == {"drawing_id": DRAWING, "version": 3, "parent": 2}
    assert result["request_sha256"] == local.request_digest(TOOL, DRAWING, 2, {"expected_rev": 1})
    after = stored(seeded, 3)
    assert_panels(after["solar_design_graph"], parent["solar_design_graph"])
    assert preserved(after) == preserved(parent)
    version_count(seeded, 3)


def test_terminal_proof_accepts_the_commit(seeded):
    result = run(seeded)
    assert proof(seeded, result) == {
        "execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
        "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
        "intake_sha256": result["intake_sha256"], "source_version": 2, "new_version": 3,
    }


def test_proof_rejects_a_consistently_rewritten_graph(seeded):
    result = run(seeded)
    intake = stored(seeded, 3)
    intake["solar_design_graph"]["panels"][0]["centre"][0] += 1
    intake["solar_design_graph_sha256"] = digest(intake["solar_design_graph"])
    result["graph_sha256"] = intake["solar_design_graph_sha256"]
    result["intake_sha256"] = rewrite_intake(seeded, 3, intake)
    assert local.resolve_graph_context(seeded, TENANT, DRAWING, 3)["graph_sha256"] == result["graph_sha256"]
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(seeded, result)


def test_proof_rejects_rewritten_parent_geometry(seeded):
    result = run(seeded)
    parent = stored(seeded, 2)
    parent["polylines"] = [p for p in parent["polylines"] if p["handle"] != "1B"]
    rewrite_intake(seeded, 2, parent)
    assert local.resolve_graph_context(seeded, TENANT, DRAWING, 2)["graph_sha256"] == result["before_graph_sha256"]
    assert local.resolve_graph_context(seeded, TENANT, DRAWING, 3)["graph_sha256"] == result["graph_sha256"]
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(seeded, result)


@pytest.mark.parametrize("change", ["injected", "bool", "float"])
def test_proof_rejects_changed_preserved_content(seeded, change):
    result = run(seeded)
    intake = stored(seeded, 3)
    if change == "injected":
        intake["injected"] = 1
    else:
        intake["custom"]["keep"][0] = True if change == "bool" else 1.0
    result["intake_sha256"] = rewrite_intake(seeded, 3, intake)
    assert local.resolve_graph_context(seeded, TENANT, DRAWING, 3)["graph_sha256"] == result["graph_sha256"]
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(seeded, result)


def test_parent_bytes_mismatch_is_source_intake_unavailable(seeded):
    _, key = store.resolve_version(seeded, TENANT, DRAWING, 2)
    seeded.put(key, seeded.get(key) + b" ")
    local.resolve_graph_context(seeded, TENANT, DRAWING, 2)
    with refused("SOURCE_INTAKE_UNAVAILABLE"):
        run(seeded)
    version_count(seeded, 2)


def test_graphless_head_is_refused(tmp_path, monkeypatch):
    backend = upload(tmp_path, monkeypatch, FIX)
    with refused("GRAPH_NOT_EMBEDDED"):
        run(backend, {"expected_rev": 0}, source_version=1)
    version_count(backend, 1)


def test_seed_request_is_refused(seeded):
    with refused("INVALID_SEED_REQUEST"):
        run(seeded, {"expected_rev": 1, "initialize": {}})
    version_count(seeded, 2)


def test_cancel_publishes_nothing(seeded):
    before = stored(seeded, 2)
    with refused("GRAPH_COMMIT_CANCELLED"):
        run(seeded, {"drawing_id": DRAWING, "expected_rev": 1, "cancel": True})
    version_count(seeded, 2)
    assert stored(seeded, 2) == before


def test_second_run_is_refused(seeded):
    run(seeded)
    with refused("PANELS_ALREADY_PRESENT"):
        run(seeded, {"expected_rev": 2}, source_version=3, job_id="second-import")
    version_count(seeded, 3)


def test_readiness_hook_and_availability(tmp_path, monkeypatch, builtin, g1):
    backend = upload(tmp_path, monkeypatch, FIX)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    ready = availability.w1_input_readiness(TENANT, DRAWING)
    assert ready[TOOL] == {"input_ready": False, "input_reason": "persisted_graph_unavailable"}
    assert ready["solar-settings"] == {"input_ready": False, "input_reason": "graph_seed_required"}
    seed_uploaded(backend)
    assert availability.w1_input_readiness(TENANT, DRAWING)[TOOL] == {
        "input_ready": True, "input_reason": None}
    assert builtin.input_readiness(g1) == {"input_ready": True, "input_reason": None}
    for value in ("", "  ", None, 3):
        graph = copy.deepcopy(g1)
        graph["settings"]["panel_layer_contains"] = value
        assert builtin.input_readiness(graph) == {
            "input_ready": False, "input_reason": "panel_layer_filter_required"}
    run(backend)
    assert availability.w1_input_readiness(TENANT, DRAWING)[TOOL] == {
        "input_ready": False, "input_reason": "panels_already_present"}
    intake = stored(backend, 3)
    graph = intake["solar_design_graph"]
    graph["settings"]["panel_layer_contains"] = ""
    assert builtin.input_readiness(graph) == {
        "input_ready": False, "input_reason": "panels_already_present"}
    graph["project"]["units"]["meters_per_unit"] = 1
    intake["solar_design_graph_sha256"] = digest(graph)
    rewrite_intake(backend, 3, intake)
    assert availability.w1_input_readiness(TENANT, DRAWING)[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


def test_catalog_row_view_and_entitlement(monkeypatch):
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    families = catalog.build_catalog(deps.all_tools(TENANT))
    rows = [row for family in families for row in family["capabilities"] if row["name"] == TOOL]
    assert len(rows) == 1
    record = solar_tools.trusted_record(TOOL)
    assert rows[0]["solar"] == solar_tools.catalog_view(record)
    assert rows[0]["solar"]["family"] == "placement"
    assert rows[0]["solar"]["maturity"] == "preview"
    assert entitlements.tool_required_capability(record) == "run_write"
    annotated = availability.annotate_w1_availability(families, SimpleNamespace(tier="restricted"))
    row = next(row for family in annotated for row in family["capabilities"] if row["name"] == TOOL)
    assert row["availability"]["entitled"] is False
    assert row["availability"]["runnable"] is False


@pytest.mark.parametrize("drawing,count", [("rooftop_demo", 2345), ("rooftop_unsplit", 882)])
def test_rooftop_oracle_panel_sets(builtin, tmp_path, monkeypatch, drawing, count):
    started = time.perf_counter()
    oracle = json.loads((SERVER / "tests/fixtures/w1_rooftop_panel_groups.json").read_text(encoding="utf-8"))
    fixture = next(row for row in oracle["fixtures"] if row["drawing"] == drawing)
    intake = json.loads((SERVER.parent / fixture["intake"]).read_text(encoding="utf-8"))
    base = empty_graph(hashlib.sha256(json.dumps(intake).encode()).hexdigest())
    after = builtin.run(base, {"expected_rev": 0}, source_intake=intake)
    expected = {h.upper() for group in fixture["groups"] for h in group["members"]}
    assert len(after["panels"]) == count
    assert {p["provenance"]["source_handle"].upper() for p in after["panels"]} == expected
    backend = upload(tmp_path, monkeypatch, intake["polylines"])
    seed_uploaded(backend)
    result = run(backend)
    assert proof(backend, result)["new_version"] == 3
    panels = stored(backend, 3)["solar_design_graph"]["panels"]
    assert len(panels) == count
    assert {p["provenance"]["source_handle"].upper() for p in panels} == expected
    print(f"{drawing}: {time.perf_counter() - started:.3f}s including upload, seed, import and proof")


def test_inputs_are_not_mutated(builtin, g1):
    intake, params = {"polylines": copy.deepcopy(FIX)}, {"expected_rev": 1}
    before = copy.deepcopy((g1, params, intake))
    first = builtin.run(g1, params, source_intake=intake)
    second = builtin.run(g1, params, source_intake=intake)
    assert (g1, params, intake) == before
    assert first == second and digest(first) == digest(second)


@pytest.mark.parametrize("params", [[], {"expected_rev": 1, "source_intake": {}},
                                   {"expected_rev": 1, "panels": []},
                                   {"expected_rev": 1, "panel_layer_contains": "Roof"}])
def test_dispatcher_rejects_caller_geometry(seeded, params):
    with refused("INVALID_PANEL_IMPORT_REQUEST"):
        run(seeded, params)
    version_count(seeded, 2)


@pytest.mark.parametrize("source", [[], "intake", {"polylines": None}, {"polylines": {}}])
def test_invalid_source_intake(builtin, g1, source):
    with refused("INVALID_SOURCE_INTAKE"):
        builtin.run(g1, {"expected_rev": 1}, source_intake=source)


def test_other_kernel_error_is_invalid_source_intake(builtin, g1, monkeypatch):
    def fail(*args, **kwargs):
        raise kernel.PanelGroupKernelError("malformed input")
    monkeypatch.setattr(kernel, "panels_from_intake", fail)
    with refused("INVALID_SOURCE_INTAKE"):
        builtin.run(g1, {"expected_rev": 1}, source_intake={})


@pytest.mark.parametrize("damage", ["missing_graph", "missing_digest", "wrong_digest", "not_object", "not_json"])
def test_trusted_source_requires_both_companions_and_matching_digest(seeded, damage):
    intake = stored(seeded, 2)
    graph_sha256 = intake["solar_design_graph_sha256"]
    if damage == "missing_graph":
        del intake["solar_design_graph"]
    elif damage == "missing_digest":
        del intake["solar_design_graph_sha256"]
    elif damage == "wrong_digest":
        intake["solar_design_graph_sha256"] = "0" * 64
    elif damage == "not_object":
        intake = []
    if damage == "not_json":
        _, key = store.resolve_version(seeded, TENANT, DRAWING, 2)
        data = b"not json"
        seeded.put(key, data)
        manifest = store.load_manifest(seeded, TENANT, DRAWING)
        next(row for row in manifest["versions"] if row["v"] == 2)["sha256"] = hashlib.sha256(data).hexdigest()
        store.save_manifest(seeded, TENANT, DRAWING, manifest)
    else:
        rewrite_intake(seeded, 2, intake)
    with refused("SOURCE_INTAKE_UNAVAILABLE"):
        local._source_intake(seeded, TENANT, DRAWING, 2, graph_sha256)
