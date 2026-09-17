"""Offline equipment candidates and licensed boundary checks using synthetic data."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

from solar_design_graph import GraphValidationError, deserialize_graph, serialize_graph
from solar_equipment import FIELDS, equipment_candidate, equipment_ready
from solar_sizing_client import SIZING_URL, digest, sizing_basis
from mutation_plan import plan_sha256
from test_w1_design_graph import graph  # noqa: F401
from test_w1_graph_versions import drawing  # noqa: F401

spec = importlib.util.spec_from_file_location("solar_assign_equipment", SERVER / "builtins/solar_assign_equipment.py")
equipment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(equipment)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        pytest.fail("equipment tests must not use the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


@pytest.fixture
def case(graph):
    request = {"schema_version": "leaf.string-length.v1", "module": {
        "model": "fixture-module", "voc": 50, "temp_coeff_pct_per_c": 0},
        "inverter": {"model": "fixture-inverter", "max_dc_voltage": 600},
        "design_min_temp_c": 0, "panels_in_sequence": 2, "units": "SI"}
    response = {"panels_in_sequence": 2, "voc_cold": copy.deepcopy(graph["settings"]["voc_cold"])}
    graph["settings"]["global_string_sizing_confirmed"] = True
    graph["settings"]["extra"]["string_sizing"] = {
        "mode": "global", "basis_sha256": sizing_basis(graph), "records": {
            graph["settings"]["id"]: {"endpoint": SIZING_URL, "adapter_version": "1.0.0",
                "request": request, "response": response,
                "request_sha256": digest(request), "response_sha256": digest(response)}}}
    inverter = graph["inverters"][0]
    inverter["provenance"]["source_handle"] = "B1"
    config = {key: copy.deepcopy(inverter[key]) for key in FIELDS if key in inverter}
    config.update(position=[5, 0, 0], rotation=30, scale=[1, 1, 1], block_name="FixtureInverter",
                  layer="0", mppt_inputs={"A": 2}, max_dc_power_kw=1.2)
    params = {"expected_rev": 0, "equipment": [config], "assignments": [
        {**assignment, "inverter_ref": inverter["id"]} for assignment in inverter["input_assignments"]]}
    graph["inverters"] = []
    for string in graph["strings"]:
        string["inverter_ref"] = None
    for frame in graph["frames"]:
        for record in frame["panel_assignments"] + [cell for row in frame["matrix"] for cell in row]:
            record["inverter_id"], record["string_input_number"] = None, None
    intake = {"inserts": [],
              "blocks": {"FixtureInverter": {"complete": True, "children": [], "count": 0}}}
    return graph, params, intake


def licensed(**kwargs):
    assert kwargs["timeout"] == 50
    return {"plan_sha256": plan_sha256(kwargs["plan"]), "placements": [
        {"id": item["id"], "handle": f"C{n}", "configuration": {key: item[key] for key in FIELDS}}
        for n, item in enumerate(kwargs["graph"]["inverters"], 1)]}


def test_assignment_placement_and_reopen(case):
    graph, params, intake = case
    before = copy.deepcopy(graph)
    output = equipment.assign_equipment(graph, params, drawing_intake=intake, licensed_equipment=licensed)
    reopened = deserialize_graph(serialize_graph(output["graph"]))
    assert output["ready"] and equipment_ready(reopened)
    assert reopened["rev"] == 1 and reopened["parent_rev"] == 0
    assert graph == before
    assert {key: reopened["inverters"][0][key] for key in FIELDS} == params["equipment"][0]
    assert reopened["inverters"][0]["provenance"]["source_handle"] == "C1"
    assert reopened["extra"]["equipment"]["assignment_requests"] == params["assignments"]
    assert reopened["frames"][0]["matrix"][0][2]["string_input_number"] == 1
    assert reopened["opaque_stores"] == before["opaque_stores"]
    assert all(item["validity"]["state"] == "stale" for item in reopened["routes"] + reopened["schedules"])


@pytest.mark.parametrize("defect,reason", [
    ("unassigned", "EQUIPMENT_UNASSIGNED"), ("slot", "EQUIPMENT_INPUT_CAPACITY_EXCEEDED"),
    ("mppt", "EQUIPMENT_INPUT_CAPACITY_EXCEEDED"), ("voltage", "EQUIPMENT_VOLTAGE_EXCEEDED"),
    ("power", "EQUIPMENT_POWER_EXCEEDED"), ("duplicate_slot", "EQUIPMENT_INPUT_CAPACITY_EXCEEDED")])
def test_overload_and_unassigned_remain_visible(case, defect, reason):
    graph, params, _ = case
    if defect == "unassigned":
        params["assignments"].pop(0)
    elif defect == "slot":
        params["assignments"][0]["input_number"] = 9
    elif defect == "mppt":
        params["assignments"][0]["mppt_letter"] = "Z"
    elif defect == "duplicate_slot":
        params["assignments"][1]["input_number"] = 0
    elif defect == "voltage":
        params["equipment"][0]["max_dc_voltage"] = 75
    else:
        params["equipment"][0]["max_dc_power_kw"] = 1
    result = deserialize_graph(serialize_graph(equipment_candidate(graph, params)))
    assert not equipment_ready(result)
    assert len(result["strings"]) == 2
    assert any(reason in s["validity"]["reasons"] for s in result["strings"])
    assert result["extra"]["equipment"]["assignment_requests"] == params["assignments"]


def test_repair_clears_only_equipment_errors(case):
    graph, params, _ = case
    broken = copy.deepcopy(params)
    broken["assignments"] = []
    first = equipment_candidate(graph, broken)
    params["expected_rev"] = 1
    fixed = equipment_candidate(first, params)
    assert equipment_ready(fixed)
    first["strings"][0]["validity"]["reasons"].append("upstream_failure")
    assert not equipment_ready(equipment_candidate(first, params))


@pytest.mark.parametrize("defect", ["stale", "nan", "boolean", "capacity", "duplicate", "secret", "unsized"])
def test_invalid_input_is_atomic(case, defect):
    graph, params, intake = case
    if defect == "stale":
        params["expected_rev"] = 8
    elif defect == "nan":
        params["equipment"][0]["position"][0] = float("nan")
    elif defect == "boolean":
        params["equipment"][0]["mppt_count"] = True
    elif defect == "capacity":
        params["equipment"][0]["mppt_inputs"]["A"] = 1
    elif defect == "duplicate":
        params["assignments"].append(copy.deepcopy(params["assignments"][0]))
    elif defect == "secret":
        params["grant_ref"] = "not-accepted"
    else:
        graph["settings"]["extra"].clear()
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError):
        equipment.assign_equipment(graph, params, drawing_intake=intake,
                                   licensed_equipment=lambda **kw: pytest.fail("preflight must refuse"))
    assert graph == before


@pytest.mark.parametrize("defect", ["hash", "transform", "handle", "missing"])
def test_licensed_reply_cannot_change_candidate(case, defect):
    graph, params, intake = case
    before = copy.deepcopy(graph)

    def bad(**kwargs):
        reply = licensed(**kwargs)
        if defect == "hash":
            reply["plan_sha256"] = "0" * 64
        elif defect == "transform":
            reply["placements"][0]["configuration"]["rotation"] = 99
        elif defect == "handle":
            reply["placements"][0]["handle"] = "not-a-handle"
        else:
            reply["placements"] = []
        return reply

    with pytest.raises(GraphValidationError, match="INVALID_LICENSED_EQUIPMENT"):
        equipment.assign_equipment(graph, params, drawing_intake=intake, licensed_equipment=bad)
    assert graph == before


def test_preview_cancel_and_missing_license(case):
    graph, params, intake = case
    assert equipment.assign_equipment(graph, {"expected_rev": 0, "cancel": True},
                                      drawing_intake={})["graph"] == graph
    preview = equipment.assign_equipment(graph, {**params, "preview": True}, drawing_intake=intake)
    assert preview["preview"] and preview["plan"]
    with pytest.raises(GraphValidationError, match="LICENSED_EQUIPMENT_REQUIRED"):
        equipment.assign_equipment(graph, params, drawing_intake=intake)


def test_reopen_rechecks_limits_instead_of_trusting_validity(case):
    graph, params, _ = case
    result = equipment_candidate(graph, params)
    result["inverters"][0]["max_dc_power_kw"] = .5
    assert not equipment_ready(result)


def test_mppt_inputs_are_scoped_and_mirrored(case):
    graph, params, _ = case
    params["equipment"][0].update(mppt_count=2, mppt_inputs={"A": 1, "B": 1})
    params["assignments"][1].update(mppt_letter="B", input_number=0)
    result = equipment_candidate(graph, params)
    assert equipment_ready(result)
    assert [a["mppt_letter"] for a in result["inverters"][0]["input_assignments"]] == ["A", "B"]
    assert all(cell["string_input_number"] == 0 for cell in result["frames"][0]["matrix"][0])


def test_existing_placement_supports_assignment_changes(case):
    graph, params, intake = case
    first = equipment.assign_equipment(graph, params, drawing_intake=intake, licensed_equipment=licensed)["graph"]
    intake["inserts"] = [{"handle": "C1", "kind": "INSERT", "name": "FixtureInverter",
                          "layer": "0", "pt": [5, 0, 0], "rot": 30, "scale": [1, 1, 1]}]
    params["expected_rev"] = 1
    params["assignments"].reverse()
    output = equipment.assign_equipment(first, params, drawing_intake=intake, licensed_equipment=licensed)
    assert output["ready"] and output["mutations"]["added"] == []
    params["equipment"][0]["rotation"] = 90
    with pytest.raises(GraphValidationError, match="EQUIPMENT_TRANSFORM_EDIT_UNSUPPORTED"):
        equipment.assign_equipment(first, params, drawing_intake=intake, licensed_equipment=licensed)


def test_existing_version_transaction_persists_equipment_once(case, drawing):
    import store
    from test_w1_graph_versions import TENANT, DRAWING, commit, request_for

    graph, params, intake = case
    backend, _ = drawing
    commit(drawing, request_for(backend, graph, apply_id="equipment-base"))
    output = equipment.assign_equipment(graph, params, drawing_intake=intake, licensed_equipment=licensed)
    request = request_for(backend, output["graph"], apply_id="equipment-assigned", dwg=b"synthetic equipment DWG")
    request["mapping"]["application_to_handle"].update({output["graph"]["inverters"][0]["id"]: "C1"})
    assert commit(drawing, request, dwg=b"synthetic equipment DWG") == 3
    assert commit(drawing, request, dwg=b"synthetic equipment DWG") == 3
    reopened = store.read_graph_bundle(store.FilesystemBackend(backend.root), TENANT, DRAWING)
    assert reopened["graph"] == request["graph"]
    assert reopened["mapping"] == request["mapping"]
    assert equipment_ready(reopened["graph"])
    assert len(store.load_manifest(backend, TENANT, DRAWING)["versions"]) == 3
