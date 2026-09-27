"""Caller equipment configuration through the intake graph and Studio job rail."""
import copy
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_graph as local
import solar_tools
import store
import write_loop
from solar_design_graph import GraphValidationError
from solar_equipment import equipment_ready
from solar_graph_context import resolve_graph_context
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_equipment import case, licensed, FIELDS  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT, DRAWING  # noqa: F401
from test_w1_local_graph_adapter import seed, held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, body

TOOL = "solar-assign-equipment"
INTAKE_TENANT = "fixture-tenant"
REPLAYED = {"rooftop-inverters-i1.json"}
EXCLUDED = {"rooftop-inverters-i18.json": "combiner_not_graph_equipment"}


@pytest.fixture
def equipment_api(isolated_jobs, no_network, case, tmp_path, monkeypatch):
    graph, _, _ = case
    backend, _ = seed(tmp_path, monkeypatch, graph)
    yield from _api(backend, tmp_path, monkeypatch)


def _run(graph, params):
    before = copy.deepcopy(graph)
    try:
        return local._load_builtin(TOOL).run(graph, params)
    finally:
        assert graph == before


def _existing(case, *, placed):
    graph, params, _ = case
    first = _run(graph, params)
    if placed:
        first["inverters"][0]["provenance"]["source_handle"] = "C1"
    request = copy.deepcopy(params)
    request["expected_rev"] = 1
    request["assignments"].reverse()
    return first, request


def _dispatch(backend, fence, params):
    return local.run_local_graph_commit(
        backend, INTAKE_TENANT, TOOL, params, drawing_id="solar", source_version=1,
        holder="fixture-owner", fence=fence, job_id="equipment-job")


def test_declaration_values():
    expected = {
        "schema": "leaf.solar-tool.v1",
        "name": TOOL,
        "builtin": "builtins/solar_assign_equipment.py",
        "family": "stringing",
        "adapter": "local-graph-commit",
        "entitlement": "run_write",
        "requires_persisted_graph": True,
        "seedable": False,
        "invalid_request_code": "INVALID_EQUIPMENT_REQUEST",
        "readiness": {"kind": "w1-chain"},
        "engine": "server-builtin",
        "interaction": {"mode": "form"},
        "record_store": "write_seed",
        "record": None,
        "ledger": ["inverter-add"],
        "trusted_inputs": [],
        "maturity": "production",
        "wave": 1,
        "order": 70,
        "scenario": "w1-rooftop",
    }
    assert solar_tools.get(TOOL) == expected
    assert (SERVER / "solar_tools/solar_assign_equipment.json").read_bytes() == (
        json.dumps(expected, indent=2) + "\n").encode("utf-8")


def test_routing_derives_from_the_declaration():
    assert availability.capability_adapter(TOOL) == "local-graph-commit"
    assert TOOL in local.LOCAL_GRAPH_TOOLS
    assert availability.is_local_graph_commit({"name": TOOL}) is True
    assert availability.is_cloud_proposal({"name": TOOL}) is False
    assert entitlements.tool_required_capability({"name": TOOL}) == "run_write"
    assert Path(local._load_builtin(TOOL).__file__).resolve().parent == (SERVER / "builtins").resolve()


def test_catalog_row_and_digest_are_unchanged():
    rows = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    row = next(row for row in rows if row["name"] == TOOL)
    assert solar_tools.trusted_record(TOOL) == row
    assert deps.catalog_tool_digest(row) == (
        "sha256:f6f53fc166c6247e0676f929fc7bfef6f0d46f198e8c1c8152ccb8fab26a8892")
    assert solar_tools.catalog_view(row) == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "stringing",
        "wave": 1, "order": 70, "maturity": "production", "engine": "server-builtin",
        "adapter": "local-graph-commit", "entitlement": "run_write",
        "interaction": {"mode": "form"}, "ledger": ["inverter-add"],
    }


def test_run_commits_caller_configuration(case):
    graph, params, _ = case
    result = _run(graph, params)
    assert (result["rev"], result["parent_rev"]) == (1, 0)
    assert len(result["inverters"]) == 1
    inverter = result["inverters"][0]
    assert {key: inverter[key] for key in FIELDS} == params["equipment"][0]
    provenance = inverter["provenance"]
    assert "source_handle" not in provenance
    assert provenance["created_by"] == provenance["last_writer"] == provenance["tool_id"] == TOOL
    assert provenance["source_rev"] == 0
    assert result["extra"]["equipment"]["assignment_requests"] == params["assignments"]
    assert equipment_ready(result) is True
    assert all(item["validity"] == {"state": "stale", "reasons": ["equipment_changed"]}
               for item in result["routes"] + result["schedules"])


def test_run_cancel_returns_the_checked_graph(case):
    graph, params, _ = case
    result = _run(graph, {**params, "cancel": True})
    assert result == graph
    assert result["rev"] == 0


def test_run_cancel_validates_before_returning(case):
    graph, params, _ = case
    with pytest.raises(GraphValidationError) as exc:
        _run(graph, {**params, "cancel": True, "expected_rev": 8})
    assert exc.value.code == "STALE_GRAPH_REVISION"


@pytest.mark.parametrize("defect,code", [
    ("preview", "EQUIPMENT_PREVIEW_UNSUPPORTED"),
    ("preview_type", "INVALID_EQUIPMENT_REQUEST"),
    ("grant_ref", "INVALID_EQUIPMENT_REQUEST"),
    ("stale", "STALE_GRAPH_REVISION"),
    ("source_handle", "INVALID_EQUIPMENT_CONFIGURATION"),
    ("empty", "INVALID_EQUIPMENT_REQUEST"),
    ("nan", "NONFINITE_NUMBER"),
    ("unsized", "SIZING_CONFIRMATION_REQUIRED"),
    ("units", "UNRESOLVED_UNITS"),
])
def test_run_refusals(case, defect, code):
    graph, original, _ = case
    params = copy.deepcopy(original)
    if defect == "preview":
        params["preview"] = True
    elif defect == "preview_type":
        params["preview"] = 1
    elif defect == "grant_ref":
        params["grant_ref"] = "x"
    elif defect == "stale":
        params["expected_rev"] = 8
    elif defect == "source_handle":
        params["equipment"][0]["source_handle"] = "C1"
    elif defect == "empty":
        params["equipment"] = []
    elif defect == "nan":
        params["equipment"][0]["position"][0] = float("nan")
    elif defect == "unsized":
        graph["settings"]["extra"].clear()
    else:
        graph["project"]["units"]["meters_per_unit"] = 1
    with pytest.raises(GraphValidationError) as exc:
        _run(graph, params)
    assert exc.value.code == code


@pytest.mark.parametrize("field,value", [
    ("block_name", "OtherInverter"), ("layer", "E-INV"),
    ("position", [6, 0, 0]), ("rotation", 90), ("scale", [2, 2, 2]),
])
def test_dwg_placed_inverter_transform_is_frozen(case, field, value):
    first, params = _existing(case, placed=True)
    params["equipment"][0][field] = value
    with pytest.raises(GraphValidationError) as exc:
        _run(first, params)
    assert exc.value.code == "EQUIPMENT_TRANSFORM_EDIT_UNSUPPORTED"


def test_dwg_placed_inverter_takes_assignment_changes(case):
    first, params = _existing(case, placed=True)
    result = _run(first, params)
    assert result["rev"] == 2
    inverter = result["inverters"][0]
    assert inverter["provenance"]["source_handle"] == "C1"
    assert {key: inverter[key] for key in FIELDS} == params["equipment"][0]
    assert [a["string_ref"] for a in inverter["input_assignments"]] == [
        a["string_ref"] for a in reversed(case[1]["assignments"])]
    assert equipment_ready(result) is True


def test_graph_placed_inverter_may_move(case):
    first, params = _existing(case, placed=False)
    params["equipment"][0]["rotation"] = 90
    result = _run(first, params)
    assert result["rev"] == 2
    assert result["inverters"][0]["rotation"] == 90
    assert "source_handle" not in result["inverters"][0]["provenance"]


def test_removal_is_refused(case):
    first, params = _existing(case, placed=False)
    params["equipment"][0]["id"] = app_id("inverter", 999)
    with pytest.raises(GraphValidationError) as exc:
        _run(first, params)
    assert exc.value.code == "EQUIPMENT_REMOVAL_UNSUPPORTED"


def test_local_graph_commit_publishes_and_proves(case, tmp_path, monkeypatch):
    graph, params, _ = case
    before = copy.deepcopy(graph)
    backend, _ = seed(tmp_path, monkeypatch, graph)
    request = {**params, "drawing_id": "solar"}
    with held(backend) as fence:
        result = _dispatch(backend, fence, request)
    assert graph == before
    assert result["schema_version"] == "leaf.solar-graph-commit.v1"
    assert result["adapter"] == "local-graph-commit"
    assert result["tool"] == TOOL
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert (result["before_rev"], result["after_rev"]) == (0, 1)
    assert result["drawing_changed"] is True
    assert result["replayed"] is False
    assert result["request_sha256"] == local.request_digest(TOOL, "solar", 1, params)
    assert result["project_id"] == "leaf:project:00000000-0000-4000-8000-000000000001"
    assert local.graph_commit_provenance(
        result, request, INTAKE_TENANT, "equipment-job", TOOL, 1, backend=backend) == {
            "execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
            "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
            "intake_sha256": result["intake_sha256"], "source_version": 1, "new_version": 2,
        }
    changed = copy.deepcopy(request)
    changed["assignments"].reverse()
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        local.graph_commit_provenance(
            result, changed, INTAKE_TENANT, "equipment-job", TOOL, 1, backend=backend)
    assert store.load_manifest(backend, INTAKE_TENANT, "solar")["head"] == 2
    stored = resolve_graph_context(backend, INTAKE_TENANT, "solar")["graph"]
    assert len(stored["inverters"]) == 1
    inverter = stored["inverters"][0]
    assert {key: inverter[key] for key in FIELDS} == params["equipment"][0]
    assert "source_handle" not in inverter["provenance"]


@pytest.mark.parametrize("defect,code", [
    ("list", "INVALID_EQUIPMENT_REQUEST"),
    ("cancel", "GRAPH_COMMIT_CANCELLED"),
    ("negative_zero", "INVALID_NUMERIC_PARAM"),
])
def test_local_graph_commit_refusals(case, tmp_path, monkeypatch, defect, code):
    graph, params, _ = case
    before = copy.deepcopy(graph)
    backend, _ = seed(tmp_path, monkeypatch, graph)
    request = {**copy.deepcopy(params), "drawing_id": "solar"}
    if defect == "list":
        request = []
    elif defect == "cancel":
        request["cancel"] = True
    else:
        request["equipment"][0]["position"][2] = -0.0
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as exc:
            _dispatch(backend, fence, request)
    assert exc.value.code == code
    assert graph == before
    assert store.load_manifest(backend, INTAKE_TENANT, "solar")["latest"] == 1


def test_dwg_bundle_refuses_local_commit(drawing, case, monkeypatch):
    graph, params, _ = case
    before = copy.deepcopy(graph)
    backend, fence = drawing
    commit(drawing, request_for(backend, graph))
    head = store.load_manifest(backend, TENANT, DRAWING)["head"]
    with pytest.raises(GraphValidationError) as exc:
        local.run_local_graph_commit(
            backend, TENANT, TOOL, {**params, "drawing_id": DRAWING},
            drawing_id=DRAWING, source_version=head, holder="writer", fence=fence,
            job_id="equipment-bundle-job")
    assert exc.value.code == "LICENSED_GRAPH_COMMIT_REQUIRED"
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    assert availability.w1_input_readiness(
        TENANT, DRAWING, project_id=graph["project"]["id"])[TOOL] == {
            "input_ready": False, "input_reason": "licensed_graph_commit_required"}
    assert store.load_manifest(backend, TENANT, DRAWING)["head"] == head
    assert graph == before


def test_equipment_error_does_not_block_its_own_correction(case, tmp_path, monkeypatch):
    graph, params, _ = case
    backend, _ = seed(tmp_path, monkeypatch, graph)
    request = {**copy.deepcopy(params), "drawing_id": "solar"}
    request["equipment"][0]["max_dc_power_kw"] = 1
    with held(backend) as fence:
        _dispatch(backend, fence, request)
    stored = resolve_graph_context(backend, INTAKE_TENANT, "solar")["graph"]
    assert all(item["validity"]["state"] == "invalid" and
               "EQUIPMENT_POWER_EXCEEDED" in item["validity"]["reasons"]
               for item in stored["strings"])
    readiness = availability.w1_graph_readiness(stored)
    assert readiness[TOOL] == {"input_ready": True, "input_reason": None}
    assert readiness["solar-homeruns"] == {
        "input_ready": False, "input_reason": "equipment_assignment_required"}
    corrected = _run(stored, {**params, "expected_rev": stored["rev"]})
    assert all(item["validity"]["state"] == "valid" for item in corrected["strings"])
    assert availability.w1_graph_readiness(corrected)[TOOL] == {
        "input_ready": True, "input_reason": None}
    mixed = copy.deepcopy(stored)
    mixed["strings"][0]["validity"]["reasons"].append("MODULE_COUNT_MISMATCH")
    assert availability.w1_graph_readiness(mixed)[TOOL] == {
        "input_ready": False, "input_reason": "valid_strings_required"}


def test_equipment_error_is_corrected_through_the_run_route(equipment_api, case):
    _, params, _ = case
    backend = equipment_api[1]
    request = copy.deepcopy(params)
    request["expected_rev"] = 0
    request["equipment"][0]["max_dc_power_kw"] = 1
    response = equipment_api[0].post("/api/run?wait=1", json=body(equipment_api, TOOL, request))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert store.load_manifest(backend, INTAKE_TENANT, "solar")["head"] == 2
    stored = resolve_graph_context(backend, INTAKE_TENANT, "solar")["graph"]
    assert all(item["validity"]["state"] == "invalid" and
               "EQUIPMENT_POWER_EXCEEDED" in item["validity"]["reasons"]
               for item in stored["strings"])
    request = {**copy.deepcopy(params), "expected_rev": stored["rev"]}
    response = equipment_api[0].post("/api/run?wait=1", json=body(equipment_api, TOOL, request))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert store.load_manifest(backend, INTAKE_TENANT, "solar")["head"] == 3
    stored = resolve_graph_context(backend, INTAKE_TENANT, "solar")["graph"]
    assert all(item["validity"]["state"] == "valid" for item in stored["strings"])
    assert jobs.get_job(env["result"]["job_id"])["status"] == "complete"


def test_intake_readiness_on_the_equipment_case(case, tmp_path, monkeypatch):
    graph, _, _ = case
    before = copy.deepcopy(graph)
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    assert availability.w1_input_readiness(INTAKE_TENANT, "solar")[TOOL] == {
        "input_ready": True, "input_reason": None}
    assert graph == before


def test_intake_readiness_on_the_base_graph(graph, tmp_path, monkeypatch):
    before = copy.deepcopy(graph)
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    assert availability.w1_input_readiness(INTAKE_TENANT, "solar")[TOOL] == {
        "input_ready": False, "input_reason": "valid_strings_required"}
    assert graph == before


def test_parity_receipts_are_classified_and_i1_replays_through_the_studio_rail(equipment_api, case):
    receipts = SERVER.parent / "docs/parity/receipts/inverter-add"
    assert {path.name for path in receipts.iterdir() if path.is_file()} == REPLAYED | set(EXCLUDED)
    assert REPLAYED.isdisjoint(EXCLUDED)
    excluded = json.loads((receipts / "rooftop-inverters-i18.json").read_text(encoding="utf-8"))
    assert EXCLUDED["rooftop-inverters-i18.json"] == "combiner_not_graph_equipment"
    assert len(excluded["comparison"]["plugin"]["after"]["rows"]) == 1
    assert excluded["comparison"]["plugin"]["after"]["rows"][0]["role"] == "combiner"
    receipt = json.loads((receipts / "rooftop-inverters-i1.json").read_text(encoding="utf-8"))
    assert receipt["comparator"]["verdict"] == "pass"
    assert receipt["comparison"]["plugin"]["units"] == "in"
    assert receipt["comparison"]["studio"]["provenance"]["operation"] == "inverter-add-all"
    rows = receipt["comparison"]["plugin"]["after"]["rows"]
    assert len(rows) == 8
    assert all(row["role"] == "inverter" for row in rows)
    assert all(row["rotation"] == {"kind": "angle", "unit": "deg", "value": 0.0} for row in rows)
    assert all(row["scale"] == 8.673644733572 for row in rows)
    assert [row["position"]["value"] for row in rows] == [
        [16758.3563267214, 1576.097866801099],
        [14298.59861051802, 1644.100118557831],
        [20260.85195774109, 2025.737421601047],
        [18680.17286698163, 2030.831390476388],
        [18436.98933559365, 3800.225486334549],
        [14522.5827700701, 3954.99625300923],
        [20260.85195774109, 3954.99625300923],
        [16613.12670764324, 4121.285469013931],
    ]
    graph, params, _ = case
    before = copy.deepcopy(graph)
    assert graph["project"]["units"]["drawing_units"] == "in"
    configs = []
    for k, row in enumerate(rows, 1):
        assert row["position"]["unit"] == "in"
        configs.append({
            **copy.deepcopy(params["equipment"][0]), "id": app_id("inverter", 100 + k),
            "number": k, "position": [*row["position"]["value"], 0.0],
            "rotation": row["rotation"]["value"], "scale": [row["scale"]] * 3,
        })
    assignments = [{**a, "inverter_ref": app_id("inverter", 101)} for a in params["assignments"]]
    request = {"expected_rev": 0, "equipment": configs, "assignments": assignments}
    response = equipment_api[0].post("/api/run?wait=1", json=body(equipment_api, TOOL, request))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    assert result["tool"] == TOOL
    assert result["adapter"] == "local-graph-commit"
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    job = jobs.get_job(result["job_id"])
    assert job["status"] == "complete"
    assert job["provenance"]["execution_mode"] == "local_graph_commit"
    backend = equipment_api[1]
    assert store.load_manifest(backend, INTAKE_TENANT, "solar")["head"] == 2
    stored = resolve_graph_context(backend, INTAKE_TENANT, "solar")["graph"]
    assert len(stored["inverters"]) == 8
    assert [{key: inverter[key] for key in FIELDS} for inverter in stored["inverters"]] == configs
    assert len(stored["inverters"][0]["input_assignments"]) == 2
    assert all(inverter["input_assignments"] == [] for inverter in stored["inverters"][1:])
    assert all("source_handle" not in inverter["provenance"] for inverter in stored["inverters"])
    assert equipment_ready(stored) is True
    assert graph == before
