"""Circuit schedules committed through the intake graph and Studio job rail."""
import copy
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import deps
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_solve_results
import solar_tools
import store
import write_loop
from product_capability_availability import is_cloud_proposal, is_local_graph_commit
from solar_design_graph import GraphValidationError, validate_graph
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from solar_wiring_client import local_routes
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed as licensed_equipment  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT, DRAWING  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_routes_schedule import licensed
from test_w1_solve_commit import seed

TOOL = "solar-schedule"
INTAKE_TENANT = "fixture-tenant"
ROWS = [
    ["S1", 2, "10 AWG", 13.123359580052492, 9.84251968503937, 22.965879265091864],
    ["S2", 1, "10 AWG", 6.561679790026246, 6.561679790026246, 13.123359580052492],
]


@pytest.fixture
def routed(graph):
    value = copy.deepcopy(graph)
    value["routes"] = local_routes(value)
    return value


def params():
    return {"expected_rev": 0, "insertion_point": [10, 20]}


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def dispatch(backend, fence, request):
    return solar_local_graph.run_local_graph_commit(
        backend, INTAKE_TENANT, TOOL, request, drawing_id="solar", source_version=1,
        holder="fixture-owner", fence=fence, job_id="schedule-job")


def test_declaration_is_local_graph_commit():
    declaration = solar_tools.get(TOOL)
    assert declaration["adapter"] == "local-graph-commit"
    assert declaration["invalid_request_code"] == "INVALID_SCHEDULE_REQUEST"
    assert declaration["engine"] == "server-builtin"
    assert declaration["readiness"] == {"kind": "w1-chain"}
    assert "solar-schedule" in solar_local_graph.LOCAL_GRAPH_TOOLS
    assert availability.capability_adapter("solar-schedule") == "local-graph-commit"
    assert is_local_graph_commit({"name": "solar-schedule"})
    assert not is_cloud_proposal({"name": "solar-schedule"})
    tools = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    row = next(row for row in tools if row["name"] == TOOL)
    assert deps.catalog_tool_digest(row) == (
        "sha256:df46c4d27c872c9f00025999ab984ca48c4bf4e0dae830ab7a5b01de8461fea9")


def test_other_three_declarations_untouched_while_unwired():
    hashes = {
        "solar-panel-groups": "279224e2d82806173b7dffa2bc634a75540a62d3c0970eb63efd2344f4a7e130",
        "solar-assign-equipment": "9f95b00f74a9c4e34d9c5bf3dd25796ed30ecb32ec655e43b3eedb693bec2a67",
        "solar-homeruns": "94764746ee2839be5f2b4df7a388940766aca23b6284301e44b46297304a581a",
    }
    for name, expected in hashes.items():
        if solar_tools.get(name)["adapter"] is None:
            data = (SERVER / "solar_tools" / (name.replace("-", "_") + ".json")).read_bytes()
            assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == expected


def test_no_parity_receipt_is_claimed():
    assert solar_tools.get("solar-schedule")["ledger"] == []
    assert not (SERVER.parent / "docs/parity/receipts/solar-schedule").exists()


def test_run_builds_the_typed_schedule(routed):
    source = copy.deepcopy(routed)
    request = params()
    result = builtin().run(routed, request)
    assert routed == source
    assert request == params()
    assert (result["rev"], result["parent_rev"]) == (1, 0)
    assert len(result["schedules"]) == 2
    assert result["schedules"][0] == source["schedules"][0]
    table = result["schedules"][-1]
    assert set(table) == {
        "column_units", "extra", "headers", "id", "insertion_point", "kind", "layer",
        "provenance", "rev", "rows", "source_refs", "source_rev", "validity",
    }
    assert table["kind"] == "schedule"
    assert table["rev"] == 1 and table["source_rev"] == 0
    assert table["layer"] == "LEAF-SCHEDULES"
    assert table["insertion_point"] == [10.0, 20.0, 0.0]
    assert table["headers"] == [
        "Circuit", "Modules", "Conductor", "Start homerun", "End homerun", "Total wire"]
    assert table["rows"] == ROWS
    assert all(type(row[1]) is int for row in table["rows"])
    assert table["column_units"] == [None, "count", None, "ft", "ft", "ft"]
    assert table["extra"] == {
        "column_types": ["string", "integer", "string", "number", "number", "number"],
        "row_source_refs": [app_id("string", 1), app_id("string", 2)],
    }
    routes = routed["routes"]
    assert table["source_refs"] == [
        app_id("string", 1), app_id("inverter", 1), app_id("panel", 1), app_id("panel", 2),
        routes[0]["id"], routes[1]["id"], app_id("string", 2), app_id("panel", 3),
        routes[2]["id"], routes[3]["id"],
    ]
    assert table["validity"] == {"state": "valid", "reasons": []}
    assert table["provenance"] == {
        "created_by": TOOL, "created_at": routes[0]["provenance"]["created_at"],
        "last_writer": TOOL, "source_rev": 0, "source_hash": "a" * 64, "tool_id": TOOL,
    }
    identity = digest({"tool": TOOL, "source_graph_sha256": digest(source),
                       "insertion_point": [10.0, 20.0, 0.0]})
    assert table["id"] == "leaf:schedule:" + str(
        uuid.UUID(bytes=bytes.fromhex(identity)[:16], version=4))
    assert re.fullmatch(
        r"leaf:schedule:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        table["id"])


def test_run_with_a_3d_insertion_point(routed):
    flat = builtin().run(copy.deepcopy(routed), params())
    result = builtin().run(copy.deepcopy(routed), {
        "expected_rev": 0, "insertion_point": [10, 20, 5]})
    assert result["schedules"][-1]["insertion_point"] == [10.0, 20.0, 5.0]
    assert result["schedules"][-1]["id"] != flat["schedules"][-1]["id"]


def test_run_is_deterministic(routed):
    first = builtin().run(copy.deepcopy(routed), params())
    second = builtin().run(copy.deepcopy(routed), params())
    assert first == second
    assert digest(first) == digest(second)


def _w15s_stale_schedule(case):
    graph, equipment_params, drawing_intake = case
    source = equipment.assign_equipment(
        graph, equipment_params, drawing_intake=drawing_intake,
        licensed_equipment=licensed_equipment)["graph"]
    source["routes"] = local_routes(source)
    source["schedules"] = []
    scheduled = builtin().run(source, dict(params(), expected_rev=source["rev"]))
    return solar_local_graph._load_builtin("solar-homeruns").run(
        scheduled, {"expected_rev": scheduled["rev"]})


def test_w15s_rerun_replaces_a_stale_schedule(case):
    """The real homerun writer marks the first schedule stale before replacement."""
    source = _w15s_stale_schedule(case)
    stale = source["schedules"][0]
    assert stale["validity"] == {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    result = builtin().run(source, dict(params(), expected_rev=source["rev"]))
    assert len(result["schedules"]) == 1
    assert result["schedules"][0]["validity"]["state"] == "valid"
    assert result["schedules"][0]["id"] != stale["id"]
    solar_solve_results.require_current_export(result)


@pytest.mark.parametrize("state", ["invalid", "unknown"])
def test_w15s_every_non_valid_state_is_replaced(case, state):
    """Set each other schema-admitted state on the existing scheduled graph."""
    source = _w15s_stale_schedule(case)
    previous = source["schedules"][0]
    previous["validity"] = {"state": state, "reasons": []}
    assert validate_graph(source) == source
    result = builtin().run(source, dict(params(), expected_rev=source["rev"]))
    assert len(result["schedules"]) == 1
    assert result["schedules"][0]["validity"]["state"] == "valid"
    assert result["schedules"][0]["id"] != previous["id"]
    solar_solve_results.require_current_export(result)


def test_w15s_valid_schedules_are_kept(routed):
    source = copy.deepcopy(routed)
    source["schedules"] = []
    first = builtin().run(source, params())
    result = builtin().run(first, {
        "expected_rev": first["rev"], "insertion_point": [30, 40]})
    assert len(result["schedules"]) == 2
    assert result["schedules"][0] == first["schedules"][0]
    assert result["schedules"][1]["insertion_point"] == [30.0, 40.0, 0.0]
    assert result["schedules"][1]["id"] != result["schedules"][0]["id"]
    assert all(s["validity"]["state"] == "valid" for s in result["schedules"])


def test_w15s_only_stale_ones_are_removed(routed, case):
    """Mix a valid fixture schedule with one made stale by the real homerun writer."""
    source = _w15s_stale_schedule(case)
    stale_id = source["schedules"][0]["id"]
    valid = copy.deepcopy(routed["schedules"][0])
    source["schedules"].insert(0, valid)
    result = builtin().run(source, dict(params(), expected_rev=source["rev"]))
    assert len(result["schedules"]) == 2
    assert result["schedules"][0] == valid
    assert stale_id not in [s["id"] for s in result["schedules"]]
    assert result["schedules"][-1]["id"] != valid["id"]
    assert result["schedules"][-1]["validity"]["state"] == "valid"


def test_w15s_cancel_removes_nothing(case):
    """Cancellation preserves a schedule made stale by the real homerun writer."""
    source = _w15s_stale_schedule(case)
    before = copy.deepcopy(source)
    result = builtin().run(source, {"expected_rev": source["rev"], "cancel": True})
    assert result == source == before
    assert result["rev"] == before["rev"]
    assert result["schedules"][0]["validity"]["state"] == "stale"


def test_w15s_refusal_removes_nothing(case):
    """A routing refusal preserves the real homerun writer's stale schedule."""
    source = _w15s_stale_schedule(case)
    source["routes"].pop()
    schedules = source["schedules"]
    before = copy.deepcopy(schedules)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, dict(params(), expected_rev=source["rev"]))
    assert error.value.code == "COMPLETE_ROUTING_REQUIRED"
    assert source["schedules"] is schedules
    assert source["schedules"] == before
    assert [s["id"] for s in source["schedules"]] == [s["id"] for s in before]


def test_w15s_id_is_computed_over_the_source_graph(case):
    """Identity includes the schedule made stale by the real homerun writer."""
    source = _w15s_stale_schedule(case)
    before = copy.deepcopy(source)
    identity = digest({"tool": TOOL, "source_graph_sha256": digest(source),
                       "insertion_point": [10.0, 20.0, 0.0]})
    expected_id = "leaf:schedule:" + str(
        uuid.UUID(bytes=bytes.fromhex(identity)[:16], version=4))
    result = builtin().run(source, dict(params(), expected_rev=source["rev"]))
    assert [s["id"] for s in result["schedules"]] == [expected_id]
    assert source == before


def test_w15s_licensed_path_is_unchanged(case):
    """Licensed preview keeps the real homerun writer's stale schedule."""
    source = _w15s_stale_schedule(case)
    before = copy.deepcopy(source)
    result = builtin().create_schedule(
        source, dict(params(), expected_rev=source["rev"]), licensed_write=licensed)["graph"]
    assert len(result["schedules"]) == 2
    assert result["schedules"][0] == before["schedules"][0]
    assert result["schedules"][0]["validity"]["state"] == "stale"
    assert result["schedules"][-1]["validity"]["state"] == "valid"
    assert source == before


def test_licensed_and_local_rows_agree(routed):
    before = copy.deepcopy(routed)
    local = builtin().run(routed, params())
    licensed_graph = builtin().create_schedule(
        routed, params(), licensed_write=licensed)["graph"]
    assert routed == before
    assert {key: value for key, value in local.items() if key != "schedules"} == {
        key: value for key, value in licensed_graph.items() if key != "schedules"}
    assert local["schedules"][:-1] == licensed_graph["schedules"][:-1]
    local_table = copy.deepcopy(local["schedules"][-1])
    licensed_table = copy.deepcopy(licensed_graph["schedules"][-1])
    for table in (local_table, licensed_table):
        del table["id"]
        del table["provenance"]["created_at"]
    assert local_table == licensed_table


@pytest.mark.parametrize("defect,code", [
    ("not_object", "INVALID_SCHEDULE_REQUEST"),
    ("rows", "INVALID_SCHEDULE_REQUEST"),
    ("id", "INVALID_SCHEDULE_REQUEST"),
    ("licensed_write", "INVALID_SCHEDULE_REQUEST"),
    ("cancel_type", "INVALID_SCHEDULE_REQUEST"),
    ("stale_rev", "STALE_GRAPH_REVISION"),
    ("missing_rev", "STALE_GRAPH_REVISION"),
    ("missing_point", "INVALID_ROUTE_POINT"),
    ("short_point", "INVALID_ROUTE_POINT"),
    ("large_point", "INVALID_ROUTE_POINT"),
    ("incomplete", "COMPLETE_ROUTING_REQUIRED"),
    ("wrong_length", "COMPLETE_ROUTING_REQUIRED"),
    ("missing_gauge", "ROUTING_TOPOLOGY_REQUIRED"),
    ("units", "UNRESOLVED_UNITS"),
])
def test_run_refusals(routed, graph, defect, code):
    request = params()
    source = copy.deepcopy(routed)
    if defect == "not_object":
        request = []
    elif defect in ("rows", "id", "licensed_write"):
        request[defect] = {"rows": [], "id": "leaf:schedule:x", "licensed_write": None}[defect]
    elif defect == "cancel_type":
        request = {"expected_rev": 0, "cancel": "yes"}
    elif defect == "stale_rev":
        request["expected_rev"] = 99
    elif defect == "missing_rev":
        del request["expected_rev"]
    elif defect == "missing_point":
        del request["insertion_point"]
    elif defect == "short_point":
        request["insertion_point"] = [1]
    elif defect == "large_point":
        request["insertion_point"] = [1e13, 0]
    elif defect == "incomplete":
        source = copy.deepcopy(graph)
    elif defect == "wrong_length":
        source["routes"][0]["length_ft"] += 1
    elif defect == "missing_gauge":
        source["strings"][0]["wire_gauge"] = ""
    elif defect == "units":
        source["project"]["units"]["meters_per_unit"] = 1
    before = copy.deepcopy(source)
    original_request = copy.deepcopy(request)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, request)
    assert error.value.code == code
    assert source == before
    assert request == original_request


def test_cancel_returns_the_source_graph(routed):
    before = copy.deepcopy(routed)
    result = builtin().run(routed, {"expected_rev": 0, "cancel": True})
    assert result == routed == before
    assert result["rev"] == 0 and len(result["schedules"]) == 1


def test_dispatch_publishes_proves_and_replays(routed, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, routed)
    request = dict(params(), drawing_id="solar")
    with held(backend) as fence:
        receipt = dispatch(backend, fence, request)
        assert receipt["schema_version"] == "leaf.solar-graph-commit.v1"
        assert receipt["adapter"] == "local-graph-commit"
        assert receipt["tool"] == TOOL
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert receipt["before_rev"] == 0 and receipt["after_rev"] == 1
        assert receipt["drawing_changed"] is True
        assert receipt["replayed"] is False
        assert receipt["request_sha256"] == solar_local_graph.request_digest(
            TOOL, "solar", 1, params())
        assert receipt["before_graph_sha256"] == digest(routed)
        assert store.load_manifest(backend, INTAKE_TENANT, "solar")["latest"] == 2
        stored = resolve_graph_context(backend, INTAKE_TENANT, "solar", "head")["graph"]
        assert stored["schedules"][-1]["rows"] == ROWS
        proof = solar_local_graph.graph_commit_provenance(
            receipt, request, INTAKE_TENANT, "schedule-job", TOOL, 1, backend=backend)
        assert proof == {
            "execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
            "request_sha256": receipt["request_sha256"],
            "graph_sha256": receipt["graph_sha256"],
            "intake_sha256": receipt["intake_sha256"], "source_version": 1, "new_version": 2,
        }
        replay = dispatch(backend, fence, request)
        assert replay["replayed"] is True
        assert replay["new_version"] == receipt["new_version"]
        assert replay["graph_sha256"] == receipt["graph_sha256"]
        assert replay["intake_sha256"] == receipt["intake_sha256"]
        assert store.load_manifest(backend, INTAKE_TENANT, "solar")["latest"] == 2


def test_terminal_proof_rejects_a_tampered_receipt(routed, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, routed)
    request = dict(params(), drawing_id="solar")
    with held(backend) as fence:
        receipt = dispatch(backend, fence, request)
    for altered, submitted in (
        (dict(receipt, graph_sha256="0" * 64), request),
        (dict(receipt, after_rev=5), request),
        (receipt, dict(request, insertion_point=[11, 20])),
    ):
        with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
            solar_local_graph.graph_commit_provenance(
                altered, submitted, INTAKE_TENANT, "schedule-job", TOOL, 1, backend=backend)


@pytest.mark.parametrize("submitted_params,code", [
    ([], "INVALID_SCHEDULE_REQUEST"),
    ({"expected_rev": 0, "insertion_point": [-0.0, 20]}, "INVALID_NUMERIC_PARAM"),
    ({"expected_rev": 0, "cancel": True}, "GRAPH_COMMIT_CANCELLED"),
])
def test_dispatch_refusals(routed, tmp_path, monkeypatch, submitted_params, code):
    backend, _ = seed(tmp_path, monkeypatch, routed)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, submitted_params)
    assert error.value.code == code
    assert store.load_manifest(backend, INTAKE_TENANT, "solar")["latest"] == 1


def test_bundle_drawing_never_reaches_the_builtin(drawing, graph, monkeypatch):
    backend, fence = drawing
    commit(drawing, request_for(backend, graph))

    def fail(*args, **kwargs):
        pytest.fail("schedule builtin reached for a DWG bundle")

    monkeypatch.setattr(solar_local_graph._load_builtin(TOOL), "run", fail)
    with pytest.raises(GraphValidationError) as error:
        solar_local_graph.run_local_graph_commit(
            backend, TENANT, TOOL, params(), drawing_id=DRAWING, source_version=2,
            holder="writer", fence=fence, job_id="dwg-schedule-job")
    assert error.value.code == "LICENSED_GRAPH_COMMIT_REQUIRED"
    assert store.load_manifest(backend, TENANT, DRAWING)["latest"] == 2


def test_studio_run_refuses_incomplete_routing(api):
    response = api[0].post("/api/run?wait=1", json=body(api, TOOL, params()))
    assert response.status_code == 409, response.text
    env = response.json()
    assert env["reason_code"] == "complete_routing_required"
    assert env["availability"]["engine_ready"] is True
    assert env["availability"]["input_ready"] is False
    assert env["availability"]["refusal_reasons"] == ["complete_routing_required"]
    assert not jobs._query("SELECT job_id FROM jobs")
    assert store.load_manifest(api[1], INTAKE_TENANT, "solar")["head"] == 1


def test_studio_run_commits_the_schedule(api, case):
    graph, equipment_params, drawing_intake = case
    assigned = equipment.assign_equipment(
        graph, equipment_params, drawing_intake=drawing_intake,
        licensed_equipment=licensed_equipment)["graph"]
    assigned["routes"] = local_routes(assigned)
    assert assigned["rev"] == 1
    intake = {"layers": [], "polylines": [], "solar_design_graph": assigned,
              "solar_design_graph_sha256": digest(assigned)}
    holder, fence = api[3]._checkout_identity()
    assert write_loop._put_bytes_version(
        api[1], INTAKE_TENANT, "solar", json.dumps(intake).encode(),
        1, {}, holder=holder, fence=fence, require_parent_is_head=True) == 2
    assert availability.w1_input_readiness(api[5], "solar", version=2)[TOOL] == {
        "input_ready": True, "input_reason": None}
    request = body(api, TOOL, {"expected_rev": 1, "insertion_point": [10, 20]})
    request["dwg_version"] = 2
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    receipt = env["result"]
    assert receipt["tool"] == TOOL
    assert receipt["adapter"] == "local-graph-commit"
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
    assert receipt["before_rev"] == 1 and receipt["after_rev"] == 2
    assert receipt["replayed"] is False
    job = jobs.get_job(receipt["job_id"])
    assert job["status"] == "complete"
    assert job["provenance"]["execution_mode"] == "local_graph_commit"
    assert job["provenance"]["source_version"] == 2
    assert job["provenance"]["new_version"] == 3
    assert job["provenance"] == env["execution_provenance"]
    assert store.load_manifest(api[1], INTAKE_TENANT, "solar")["head"] == 3
    stored = resolve_graph_context(api[1], INTAKE_TENANT, "solar", "head")["graph"]
    assert stored["schedules"][-1]["rows"] == ROWS
    entry = json.loads(api[4].LEDGER_PATH.read_text().splitlines()[-1])
    assert entry["tool"] == TOOL
    assert entry["aps_live"] is False
