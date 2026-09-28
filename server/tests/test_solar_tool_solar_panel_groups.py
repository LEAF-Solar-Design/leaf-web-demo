"""Panel groups on Studio's intake graph, through the kernel and durable job rail."""
import copy
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path
from uuid import UUID

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import deps
import jobs
import product_capability_availability as availability
import solar_design_graph
import solar_local_graph as local
import solar_panel_group_kernel as kernel
import solar_tools
import store
import write_loop
from solar_design_graph import GraphValidationError, validate_graph
from solar_graph_context import resolve_graph_context
from solar_graph_seed import new_empty_graph
from solar_sizing_client import require_sizing
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT as BUNDLE_TENANT, DRAWING  # noqa: F401
from test_w1_local_graph_adapter import held, latest
from test_w1_local_graph_broker import rails, call  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, body
from test_w1_sizing_groups import confirm, sizing_params, passing, service  # noqa: F401
from test_w1_solve_commit import seed

TOOL = "solar-panel-groups"
TENANT = "fixture-tenant"
P1, P2, P3 = [app_id("panel", n) for n in (1, 2, 3)]
S1, S2 = [app_id("string", n) for n in (1, 2)]
INV = app_id("inverter", 1)
Z1 = app_id("zone-el", 1)
INVALID = "INVALID_GROUP_REQUEST"


def builtin():
    return local._load_builtin(TOOL)


def params(refs=None):
    return {"expected_rev": 1, "groups": [{
        "name": "Roof group", "panel_refs": [P3, P2, P1] if refs is None else list(refs),
        "alignment_tolerance": 0.5, "module_width_along_row": 1,
        "module_height_across_row": 2,
    }]}


def frame_id(graph, refs):
    identity = str(graph["rev"]) + ":" + ",".join(sorted(refs))
    raw = hashlib.sha256((graph["source_hash"] + ":frame:" + identity).encode("utf-8")).digest()[:16]
    return "leaf:frame:" + str(UUID(bytes=raw, version=4))


def assignments(refs):
    expected = {
        P1: {"panel_ref": P1, "string_ref": S1, "seq": 0,
             "inverter_id": INV, "string_input_number": 0},
        P2: {"panel_ref": P2, "string_ref": S1, "seq": 1,
             "inverter_id": INV, "string_input_number": 0},
        P3: {"panel_ref": P3, "string_ref": S2, "seq": 0,
             "inverter_id": INV, "string_input_number": 1},
    }
    return [expected[ref] for ref in refs]


@pytest.fixture
def u(graph, passing, service):
    """U: remove the old frame, set source geometry, then genuinely confirm sizing."""
    def build(*, angles=(0, 0, 0), centres=((1, 0), (2, 0), (3, 0)),
              handles=("A1", "A2", "A3"), mode="global", zones=None):
        value = copy.deepcopy(graph)
        value["frames"] = []
        for panel, angle, centre, handle in zip(value["panels"], angles, centres, handles):
            panel.update(frame_ref=None, matrix_cell=None, angle=angle, centre=list(centre))
            if handle is None:
                panel["provenance"].pop("source_handle")
            else:
                panel["provenance"]["source_handle"] = handle
        if zones in ("split", "partial"):
            value["electrical_zones"][0]["panel_refs"] = [P1, P2]
        if zones == "split":
            other = copy.deepcopy(value["electrical_zones"][0])
            other.update(id=app_id("zone-el", 2), name="Roof 2", panel_refs=[P3])
            value["electrical_zones"].append(other)
        return confirm(value, sizing_params(value, passing, mode))["graph"]
    return build


def job(backend, request, job_id="pg-job-1"):
    with held(backend) as fence:
        return local.run_local_graph_commit(
            backend, TENANT, TOOL, request, drawing_id="solar", source_version=1,
            holder="fixture-owner", fence=fence, job_id=job_id)


def tool_row():
    return next(row for row in json.loads((SERVER / "write_tools.json").read_text())["tools"]
                if row["name"] == TOOL)


def test_declaration_is_local_graph_commit():
    # D: every declaration field is pinned, including fields unchanged by the re-point.
    expected = {
        "schema": "leaf.solar-tool.v1", "name": TOOL,
        "builtin": "builtins/solar_panel_groups.py", "family": "stringing",
        "adapter": "local-graph-commit", "entitlement": "run_write",
        "requires_persisted_graph": True, "seedable": False,
        "invalid_request_code": INVALID, "readiness": {"kind": "w1-chain"},
        "engine": "server-builtin", "interaction": {"mode": "form"},
        "record_store": "write_seed", "record": None, "ledger": ["panel-group-create"],
        "trusted_inputs": [], "maturity": "production", "wave": 1, "order": 30,
        "scenario": "w1-rooftop",
    }
    assert json.loads((SERVER / "solar_tools/solar_panel_groups.json").read_text()) == expected
    assert solar_tools.get(TOOL) == expected
    assert deps.catalog_tool_digest(tool_row()) == (
        "sha256:098e6563d15837eccc55d63f81b45011fa6efc679130b27c8241fef77e47425a")


@pytest.mark.parametrize("name,order,ledger", [
    ("solar-assign-equipment", 70, ["inverter-add"]),
    ("solar-homeruns", 80, ["homeruns"]),
    ("solar-schedule", 90, []),
], ids=["S1-equipment", "S1-homeruns", "S1-schedule"])
def test_sibling_declarations_untouched(name, order, ledger):
    # The sibling's own suite releases this pin when its re-point lands.
    if (SERVER / "tests" / ("test_solar_tool_" + name.replace("-", "_") + ".py")).exists():
        return
    declaration = solar_tools.get(name)
    expected = {"adapter": None, "invalid_request_code": None, "engine": "autocad-lane",
                "readiness": {"kind": "w1-chain"}, "entitlement": "run_write",
                "record_store": "write_seed", "record": None, "order": order, "ledger": ledger}
    assert {key: declaration[key] for key in expected} == expected


@pytest.mark.parametrize("angles,centres,angle_key,row_angle", [
    ((0, 0, 0), ((1, 0), (2, 0), (3, 0)), "0.0", 0.0),
    ((0, 0, 180), ((1, 0), (2, 0), (3, 0)), "0.0", 0.0),
    ((90, 90, 90), ((0, 1), (0, 2), (0, 3)), "90.0", math.pi / 2),
], ids=["C1", "C4", "C5"])
def test_local_run_commits_kernel_matrix(u, angles, centres, angle_key, row_angle):
    source = u(angles=angles, centres=centres)
    before = copy.deepcopy(source)
    request = params()
    result = builtin().run(source, request)
    assert source == before and request == params()
    assert (result["rev"], result["parent_rev"]) == (2, 1)
    cells = [{"code": "panel", "panel_ref": ref, "seq": seq, "inverter_id": INV,
              "string_input_number": input_number, "x": centres[n][0], "y": centres[n][1],
              "angle": angles[n]}
             for n, ref, seq, input_number in [(2, P3, 0, 1), (1, P2, 1, 0), (0, P1, 0, 0)]]
    expected_id = frame_id(source, [P3, P2, P1])
    assert result["frames"] == [{
        "id": expected_id, "kind": "frame", "rev": 2, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "name": "Roof group",
        "panel_refs": [P3, P2, P1], "insertion_point": list(centres[0]),
        "installation_design": "Roof", "module_rows": 1, "module_columns": 3,
        "module_slots": 3, "module_power_watts": 0, "module_width_along_row": 1,
        "module_height_across_row": 2, "electrical_zone_ref": Z1, "matrix": [cells],
        "sequences": [{"string_ref": S1, "ordered_panel_refs": [P1, P2]},
                      {"string_ref": S2, "ordered_panel_refs": [P3]}],
        "panel_assignments": assignments([P3, P2, P1]),
        "provenance": {"created_by": TOOL, "last_writer": TOOL,
                       "created_at": source["panels"][0]["provenance"]["created_at"],
                       "source_rev": 1, "source_hash": source["source_hash"],
                       "kernel": "solar_panel_group_kernel", "angle_key": angle_key,
                       "row_angle": row_angle, "tool_id": TOOL},
    }]
    for panel, col in zip(result["panels"], (2, 1, 0)):
        assert panel["frame_ref"] == expected_id
        assert panel["matrix_cell"] == {"row": 0, "col": col}
        assert panel["rev"] == 2
        assert panel["provenance"]["source_rev"] == 1
        assert panel["provenance"]["last_writer"] == panel["provenance"]["tool_id"] == TOOL
    for key in source.keys() - {"rev", "parent_rev", "frames", "panels"}:
        assert result[key] == source[key]
    require_sizing(result)


def test_matrix_ignores_request_order(u):
    source = u()
    first = builtin().run(source, params())["frames"][0]
    second = builtin().run(source, params([P1, P2, P3]))["frames"][0]
    assert second["matrix"] == first["matrix"]
    assert [cell["panel_ref"] for cell in second["matrix"][0]] == [P3, P2, P1]
    assert second["panel_refs"] == [P1, P2, P3]
    assert second["panel_assignments"] == assignments([P1, P2, P3])
    assert second["id"] == first["id"] == frame_id(source, [P1, P2, P3])


@pytest.mark.parametrize("include_groups", [True, False], ids=["C3", "C9"])
def test_cancel_returns_graph_unchanged(u, include_groups):
    source = u()
    request = params() if include_groups else {"expected_rev": 1}
    request["cancel"] = True
    result = builtin().run(source, request)
    assert result == validate_graph(source)
    assert result["rev"] == 1 and result["frames"] == []


@pytest.mark.parametrize("change,expected_rev,code", [
    pytest.param("stale", 0, "STALE_GRAPH_REVISION", id="stale"),
    pytest.param("units", 1, "UNRESOLVED_UNITS", id="units"),
    pytest.param("malformed", 1, "INVALID_GRAPH_SCHEMA", id="malformed"),
])
def test_cancel_is_checked(u, change, expected_rev, code):
    source = copy.deepcopy(u())
    if change == "units":
        source["project"]["units"]["meters_per_unit"] = 0.3048
    elif change == "malformed":
        del source["panels"]
    before = copy.deepcopy(source)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, {"expected_rev": expected_rev, "cancel": True})
    assert error.value.code == code
    assert source == before


def test_matrix_budget_refuses_before_building_cells(u, monkeypatch):
    module = builtin()
    source = u()
    monkeypatch.setattr(module, "_MAX_MATRIX_CELLS", 2)
    with pytest.raises(GraphValidationError) as error:
        module.run(source, params())
    assert error.value.code == "GRAPH_LIMIT_EXCEEDED"
    monkeypatch.setattr(module, "_MAX_MATRIX_CELLS", 3)
    result = module.run(source, params())
    assert len(result["frames"]) == 1
    assert result["frames"][0]["module_rows"] == 1
    assert result["frames"][0]["module_columns"] == 3


def test_matrix_budget_is_cumulative_across_groups(graph, passing, service, monkeypatch):
    value = copy.deepcopy(graph)
    value["frames"] = []
    for panel, centre, handle in zip(value["panels"], ((1, 0), (2, 0), (3, 0)),
                                     ("A1", "A2", "A3")):
        panel.update(frame_ref=None, matrix_cell=None, angle=0, centre=list(centre))
        panel["provenance"]["source_handle"] = handle
    p4 = app_id("panel", 4)
    panel4 = copy.deepcopy(next(panel for panel in value["panels"] if panel["id"] == P3))
    panel4.update(id=p4, centre=[4, 0])
    panel4["provenance"]["source_handle"] = "A4"
    panel4["assignment"]["seq"] = 1
    value["panels"].append(panel4)
    string2 = next(string for string in value["strings"] if string["id"] == S2)
    string2["ordered_panel_refs"].append(p4)
    string2["module_count"] = len(string2["ordered_panel_refs"])
    for zone in value["electrical_zones"]:
        if P3 in zone["panel_refs"]:
            zone["panel_refs"].append(p4)
    source = confirm(value, sizing_params(value, passing, "global"))["graph"]
    before = copy.deepcopy(source)
    request = {"expected_rev": 1, "groups": [
        {"name": name, "panel_refs": refs, "alignment_tolerance": 0.5,
         "module_width_along_row": 1, "module_height_across_row": 2}
        for name, refs in (("G1", [P1, P2]), ("G2", [P3, p4]))
    ]}
    module = builtin()
    monkeypatch.setattr(module, "_MAX_MATRIX_CELLS", 3)
    with pytest.raises(GraphValidationError) as error:
        module.run(source, request)
    assert error.value.code == "GRAPH_LIMIT_EXCEEDED"
    assert source == before
    monkeypatch.setattr(module, "_MAX_MATRIX_CELLS", 4)
    result = module.run(source, request)
    assert (result["rev"], result["parent_rev"]) == (2, 1)
    assert len(result["frames"]) == 2
    for frame in result["frames"]:
        assert frame["module_rows"] == 1
        assert frame["module_columns"] == 2


def test_matrix_budget_is_derived_from_the_graph_node_limit(u):
    module = builtin()
    assert module._MAX_MATRIX_CELLS == solar_design_graph.MAX_NODES // (1 + 2 * len(module._CELL_KEYS))
    assert solar_design_graph.MAX_NODES == 500000
    assert module._MAX_MATRIX_CELLS == 29411
    frame = module.run(u(), params())["frames"][0]
    assert set(frame["matrix"][0][0]) == set(module._CELL_KEYS)


@pytest.mark.parametrize("change,value,code", [
    pytest.param("params", [], INVALID, id="R1"),
    pytest.param("top", {"extra": 1}, INVALID, id="R2"),
    pytest.param("top", {"cancel": "yes"}, INVALID, id="R3"),
    pytest.param("top", {"groups": []}, INVALID, id="R4"),
    pytest.param("many-groups", None, INVALID, id="R5"),
    pytest.param("missing-numbers", None, INVALID, id="R6"),
    pytest.param("alignment_tolerance", 0, INVALID, id="R7-zero"),
    pytest.param("alignment_tolerance", -1, INVALID, id="R7-negative"),
    pytest.param("alignment_tolerance", True, INVALID, id="R7-bool"),
    pytest.param("alignment_tolerance", "1", INVALID, id="R7-string"),
    pytest.param("alignment_tolerance", 1e7, INVALID, id="R7-large"),
    pytest.param("module_width_along_row", 0, INVALID, id="R8-width"),
    pytest.param("module_height_across_row", 1000001, INVALID, id="R8-height"),
    pytest.param("name", "bad[name", INVALID, id="R9-bracket"),
    pytest.param("name", "  ", INVALID, id="R9-blank"),
    pytest.param("panel_refs", [P1], INVALID, id="R10"),
    pytest.param("top", {"expected_rev": 0}, "STALE_GRAPH_REVISION", id="R11"),
    pytest.param("unsized", None, "SIZING_CONFIRMATION_REQUIRED", id="R12"),
    pytest.param("panel_refs", [P1, P1, P2], "INVALID_GROUP_COVERAGE", id="R13-duplicate"),
    pytest.param("overlap", None, "INVALID_GROUP_COVERAGE", id="R13-overlap"),
    pytest.param("panel_refs", [P1, app_id("panel", 99)], "MISSING_PANEL", id="R14"),
    pytest.param("grouped", "Second", "PANEL_ALREADY_GROUPED", id="R15"),
    pytest.param("grouped", "ROOF GROUP", INVALID, id="R16"),
    pytest.param("handles", ("A1", "a1", "A3"), "AMBIGUOUS_PANEL_HANDLE", id="R17-duplicate"),
    pytest.param("handles", (None, "A2", "A3"), "AMBIGUOUS_PANEL_HANDLE", id="R17-missing"),
    pytest.param("handles", ("A1", "A2", "ZZ"), "INVALID_PANEL_HANDLE", id="R18"),
    pytest.param("angles", (0, 0, 90), "MIXED_GROUP_ANGLE", id="R19"),
    pytest.param("centres", ((1, 0), (1.2, 0), (3, 0)), "GROUP_MATRIX_COLLISION", id="R20"),
    pytest.param("alignment_tolerance", float("nan"), INVALID, id="nonfinite-nan"),
    pytest.param("module_width_along_row", float("inf"), INVALID, id="nonfinite-width"),
    pytest.param("module_height_across_row", False, INVALID, id="bool-height"),
])
def test_refusals(u, change, value, code):
    source = u(**{change: value}) if change in {"handles", "angles", "centres"} else u()
    request = params()
    if change == "params":
        request = value
    elif change == "top":
        request.update(value)
    elif change == "many-groups":
        request["groups"] *= 129
    elif change == "missing-numbers":
        request["groups"] = [{"name": "Roof group", "panel_refs": [P3, P2, P1]}]
    elif change == "unsized":
        source["settings"]["extra"].clear()
    elif change == "overlap":
        request = params([P1, P2])
        request["groups"].append(dict(params([P2, P3])["groups"][0], name="Other"))
    elif change == "grouped":
        source = builtin().run(source, request)
        request = params([P1, P2])
        request["expected_rev"] = 2
        request["groups"][0]["name"] = value
    elif change not in {"handles", "angles", "centres"}:
        request["groups"][0][change] = value
    before = copy.deepcopy(source)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, request)
    assert error.value.code == code
    assert source == before


@pytest.mark.parametrize("request_params", [{}, {"expected_rev": 1}], ids=["R23", "R24"])
def test_request_shape_requires_groups(u, request_params):
    with pytest.raises(GraphValidationError) as error:
        builtin().run(u(), request_params)
    assert error.value.code == INVALID


@pytest.mark.parametrize("mode,zones,refs,expected", [
    ("zones", "split", [P3, P2, P1], "INVALID_ZONE_COVERAGE"),
    ("zones", "split", [P1, P2], Z1),
    ("global", "partial", [P3, P2, P1], None),
], ids=["C6", "C7", "C8"])
def test_zone_reference(u, mode, zones, refs, expected):
    source = u(mode=mode, zones=zones)
    if expected == "INVALID_ZONE_COVERAGE":
        with pytest.raises(GraphValidationError) as error:
            builtin().run(source, params(refs))
        assert error.value.code == expected
    else:
        result = builtin().run(source, params(refs))
        assert result["frames"][0]["electrical_zone_ref"] == expected
        require_sizing(result)


@pytest.mark.parametrize("failure", [
    KeyError, TypeError, IndexError, AttributeError, ZeroDivisionError,
    OverflowError, RecursionError, ValueError,
])
def test_kernel_failure_is_named(u, monkeypatch, failure):
    source = u()
    def fail(*args, **kwargs):
        raise failure("private kernel detail")
    monkeypatch.setattr(kernel, "group_matrix", fail)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, params())
    assert error.value.code == "GROUP_KERNEL_REFUSED"
    assert "private kernel detail" not in str(error.value)


def test_kernel_limit_maps_to_graph_limit(u, monkeypatch):
    def fail(*args, **kwargs):
        raise kernel.PanelGroupMatrixLimitError("x")
    monkeypatch.setattr(kernel, "group_matrix", fail)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(u(), params())
    assert error.value.code == "GRAPH_LIMIT_EXCEEDED"


def test_kernel_graph_validation_error_keeps_its_code(u, monkeypatch):
    source = u()
    def fail(*args, **kwargs):
        raise GraphValidationError("GROUP_MATRIX_COLLISION")
    monkeypatch.setattr(kernel, "group_matrix", fail)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, params())
    assert error.value.code == "GROUP_MATRIX_COLLISION"


@pytest.mark.parametrize("request_params", [
    None, 1, {"expected_rev": 1, "groups": [None]},
    {"expected_rev": 1, "groups": [{"name": 1}]},
], ids=["R22-null", "R22-int", "R22-null-group", "R22-bad-group"])
def test_run_raises_only_graph_validation_errors(u, request_params):
    with pytest.raises(GraphValidationError) as error:
        builtin().run(u(), request_params)
    assert type(error.value) is GraphValidationError
    assert error.value.code == INVALID


def test_run_is_deterministic(u):
    source, request = u(), params()
    first = builtin().run(copy.deepcopy(source), copy.deepcopy(request))
    second = builtin().run(copy.deepcopy(source), copy.deepcopy(request))
    assert first == second
    assert first["frames"][0]["id"] == frame_id(source, [P3, P2, P1])
    assert first["frames"][0]["provenance"]["created_at"] == source["panels"][0]["provenance"]["created_at"]


def test_adapter_commit_and_terminal_proof(u, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, u())
    parent = resolve_graph_context(backend, TENANT, "solar", 1)["graph"]
    request = dict(params(), drawing_id="solar")
    result = job(backend, request)
    assert result["schema_version"] == "leaf.solar-graph-commit.v1"
    assert result["adapter"] == "local-graph-commit"
    assert result["tool"] == TOOL
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert (result["before_rev"], result["after_rev"]) == (1, 2)
    assert result["replayed"] is False
    stored = resolve_graph_context(backend, TENANT, "solar", 2)["graph"]
    assert stored["frames"] == builtin().run(parent, params())["frames"]
    assert stored["frames"][0]["id"] == frame_id(parent, [P3, P2, P1])
    proof = local.graph_commit_provenance(result, request, TENANT, "pg-job-1", TOOL, 1, backend=backend)
    assert proof["source_version"] == 1 and proof["new_version"] == 2
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        local.graph_commit_provenance(result, request, TENANT, "pg-job-1", "solar-settings", 1,
                                      backend=backend)


def test_adapter_request_shape_is_named():
    with pytest.raises(GraphValidationError) as error:
        local.run_local_graph_commit(None, TENANT, TOOL, [], drawing_id="solar", source_version=1,
                                     holder="fixture-owner", fence=1, job_id="pg-bad-shape")
    assert error.value.code == INVALID


def test_adapter_same_job_replays(u, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, u())
    request = dict(params(), drawing_id="solar")
    first = job(backend, request)
    second = job(backend, copy.deepcopy(request))
    assert first["replayed"] is False and second["replayed"] is True
    assert first["new_version"] == second["new_version"] == {
        "drawing_id": "solar", "version": 2, "parent": 1}
    assert latest(backend) == 2
    proof = local.graph_commit_provenance(second, request, TENANT, "pg-job-1", TOOL, 1, backend=backend)
    assert proof["new_version"] == 2


def test_adapter_bundle_refuses_before_builtin(drawing, graph, monkeypatch):
    backend, fence = drawing
    commit(drawing, request_for(backend, graph))
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        pytest.fail("a DWG bundle must refuse before the builtin")
    monkeypatch.setattr(local._load_builtin(TOOL), "run", fail)
    with pytest.raises(GraphValidationError) as error:
        local.run_local_graph_commit(
            backend, BUNDLE_TENANT, TOOL, dict(params(), expected_rev=0), drawing_id=DRAWING,
            source_version=2, holder="writer", fence=fence, job_id="pg-bundle")
    assert error.value.code == "LICENSED_GRAPH_COMMIT_REQUIRED"
    assert calls == []
    assert store.load_manifest(backend, BUNDLE_TENANT, DRAWING)["latest"] == 2


def test_adapter_cancel_publishes_nothing(u, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, u())
    with pytest.raises(GraphValidationError) as error:
        job(backend, dict(params(), drawing_id="solar", cancel=True))
    assert error.value.code == "GRAPH_COMMIT_CANCELLED"
    assert latest(backend) == 1


@pytest.mark.parametrize("kind,expected", [
    ("unsized", {"input_ready": False, "input_reason": "sizing_confirmation_required"}),
    ("sized", {"input_ready": True, "input_reason": None}),
    ("bundle", {"input_ready": False, "input_reason": "licensed_graph_commit_required"}),
], ids=["V1", "V2", "V3"])
def test_readiness_and_availability(u, graph, drawing, tmp_path, monkeypatch, kind, expected):
    tenant, drawing_id = TENANT, "solar"
    if kind == "bundle":
        backend, _ = drawing
        commit(drawing, request_for(backend, graph))
        tenant, drawing_id = BUNDLE_TENANT, DRAWING
    else:
        backend, _ = seed(tmp_path, monkeypatch, u() if kind == "sized" else graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    assert availability.w1_input_readiness(tenant, drawing_id)[TOOL] == expected
    assert availability.capability_adapter(TOOL) == "local-graph-commit"
    state = availability.w1_availability(TOOL, entitled=True, inputs=expected)
    assert state["engine_ready"] is True
    assert state["runnable"] is expected["input_ready"]
    assert "broker_adapter_unavailable" not in state["refusal_reasons"]


@pytest.mark.parametrize("tampered,code", [
    (False, "SIZING_CONFIRMATION_REQUIRED"), (True, "local_graph_commit_invalid"),
], ids=["B1", "B2"])
def test_broker_routes_to_local_rail(rails, tampered, code):
    row = tool_row()
    if tampered:
        row["description"] += " changed"
    _, env = call(rails, tool=row, params=dict(params(), expected_rev=0))
    assert env["ok"] is False
    assert env["error"]["reason_code"] == code
    assert latest(rails[1]) == 1
    # rails patches _get_da to fail, so either response proves no licensed lane ran.


@pytest.fixture
def sized_api(isolated_jobs, no_network, u, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, u())
    yield from _api(backend, tmp_path, monkeypatch)


def test_api_run_commits_panel_groups(sized_api):
    response = sized_api[0].post("/api/run?wait=1", json=body(sized_api, TOOL, params()))
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True
    assert jobs.get_job(envelope["result"]["job_id"])["status"] == "complete"
    assert store.load_manifest(sized_api[1], TENANT, "solar")["head"] == 2
    stored = resolve_graph_context(sized_api[1], TENANT, "solar", 2)["graph"]
    assert [cell["panel_ref"] for cell in stored["frames"][0]["matrix"][0]] == [P3, P2, P1]


def test_parity_receipt_replays_through_studio_path(passing, service, tmp_path, monkeypatch):
    # P1: load the producer only here; its sibling loader must not run at collection.
    spec = importlib.util.spec_from_file_location("_panel_groups_replay_producer",
                                                ROOT / "scripts/solar_w1_studio_groups.py")
    prod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prod)
    receipt = json.loads((ROOT / "docs/parity/receipts/panel-group-create/rooftop-demo.json").read_text())
    fixture_sha = hashlib.sha256((ROOT / "data/rooftop_demo.dwg").read_bytes()).hexdigest()
    intake_bytes = (ROOT / "data/rooftop_demo.v2.intake.json").read_bytes()
    intake = json.loads(intake_bytes)
    assert fixture_sha == receipt["fixture"]["sha256"]
    assert intake["source"]["dwg_sha256"] == fixture_sha
    recorded = receipt["comparison"]["plugin"]["parameters"]
    panels = kernel.panels_from_intake(
        intake, layer_contains=recorded["layer_filter"].strip("*"),
        installation_design=recorded["installation_design"])
    kernel_groups = kernel.group_panels(
        panels, branch_max_offset=recorded["branch_max_offset"],
        alignment_tolerance=recorded["alignment_tolerance"],
        installation_design=recorded["installation_design"])
    created_at = "2026-09-17T00:00:00Z"
    source = new_empty_graph(
        tenant_id=TENANT, drawing_id="solar", source_hash=hashlib.sha256(intake_bytes).hexdigest(),
        units={"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    by_handle, dimensions = prod.import_panels(source, intake, panels, created_at)
    source = validate_graph(source)
    source = confirm(source, sizing_params(source, passing))["graph"]
    assert source["rev"] == 1
    require_sizing(source)
    requests = []
    expected_matrices = {}
    for group in kernel_groups:
        handles = [handle.upper() for handle in group["members"]]
        anchor = min(handles, key=lambda handle: int(handle, 16))
        width, height = dimensions[by_handle[anchor]["id"]]
        requests.append({"name": "group-" + anchor,
                         "panel_refs": [by_handle[handle]["id"] for handle in handles],
                         "alignment_tolerance": recorded["alignment_tolerance"],
                         "module_width_along_row": width, "module_height_across_row": height})
        expected_matrices["group:" + anchor] = [
            [handle.upper() if handle is not None else None for handle in row] for row in group["matrix"]]
    backend, _ = seed(tmp_path, monkeypatch, source)
    request = {"expected_rev": 1, "groups": requests, "drawing_id": "solar"}
    result = job(backend, request, "pg-rooftop-replay")
    stored = resolve_graph_context(backend, TENANT, "solar", 2)["graph"]
    assert len(stored["frames"]) == len(kernel_groups) == 11
    assert len(stored["panels"]) == 2345
    assert all(panel["frame_ref"] is not None for panel in stored["panels"])
    persisted_panels = {panel["id"]: panel for panel in stored["panels"]}
    source_panels = {panel["id"]: panel for panel in source["panels"]}
    handle_by_id = {ref: panel["provenance"]["source_handle"].upper()
                    for ref, panel in persisted_panels.items()}
    actual = {}
    for frame in stored["frames"]:
        handles = [handle_by_id[ref] for ref in frame["panel_refs"]]
        neutral = "group:" + min(handles, key=lambda handle: int(handle, 16))
        actual[neutral] = sorted(handles)
        assert [[handle_by_id[cell["panel_ref"]] if cell["panel_ref"] else None for cell in row]
                for row in frame["matrix"]] == expected_matrices[neutral]
        for row in frame["matrix"]:
            for cell in row:
                if cell["panel_ref"] is not None:
                    panel = source_panels[cell["panel_ref"]]
                    assert (cell["x"], cell["y"], cell["angle"]) == (
                        panel["centre"][0], panel["centre"][1], panel["angle"])
                else:
                    assert cell == {"code": "empty", "panel_ref": None, "seq": None,
                                    "inverter_id": None, "string_input_number": None,
                                    "x": 0.0, "y": 0.0, "angle": 0.0}
    expected = {group["name"]: sorted(member["entity_id"].upper() for member in group["membership"])
                for group in receipt["comparison"]["plugin"]["after"]["groups"]}
    assert actual == expected
    proof = local.graph_commit_provenance(
        result, request, TENANT, "pg-rooftop-replay", TOOL, 1, backend=backend)
    assert proof["source_version"] == 1 and proof["new_version"] == 2
    require_sizing(stored)
