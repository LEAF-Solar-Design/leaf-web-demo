"""Auto-fill plans commit and revert through the registry and drawing rail."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import checkout_capability
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_solar_autofill import (
    STRING_LENGTH, DONOR, RECEIVER, MOVED_PANEL, captured_groups, captured,
    captured_graph, membership, as_request,
)
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest

TOOL = "solar-autofill"
TENANT = "fixture-tenant"
BEFORE_COUNTS = "7083c6f6954a9c8997ea30472fad6286c6bf2208844a4fae27e1a3aa5a7b23c8"
AFTER_COUNTS = "22193924e396757e833699eab1f63a2dceccc4fe12e74272f1036f6552ee1c60"
BEFORE_MEMBERS = "d23ab4dbadfef46edac182470450bdb2960494d05c994a88558923102df99789"
AFTER_MEMBERS = "50a82656160bc19eb87dcf017b3fc9bf770eecb774c008b6835cda2549bab774"


@pytest.fixture
def graph(captured_graph):
    return copy.deepcopy(captured_graph[0])


@pytest.fixture
def plan_request(graph, captured_graph):
    _, plan, panel_ids, frame_ids = captured_graph
    return dict(as_request(graph, plan["corrections"], panel_ids, frame_ids),
                operation="apply-corrections")


@pytest.fixture
def autofill_api(request):
    client = request.getfixturevalue("api")
    client[2][TOOL] = solar_tools.trusted_record(TOOL)
    return client


def sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def assert_members(value, captured_graph, *, after):
    members = membership(value, captured_graph[2], captured_graph[3])
    assert sha({key: len(refs) for key, refs in members.items()}) == (
        AFTER_COUNTS if after else BEFORE_COUNTS)
    assert sha({key: sorted(refs) for key, refs in members.items()}) == (
        AFTER_MEMBERS if after else BEFORE_MEMBERS)
    assert MOVED_PANEL in members[RECEIVER if after else DONOR]


def frame(value, captured_graph, name):
    return next(row for row in value["frames"] if row["id"] == captured_graph[3][name])


def dimensions(value, captured_graph, name):
    row = frame(value, captured_graph, name)
    return row["module_rows"], row["module_columns"]


def placements(value):
    return {p["id"]: (p["frame_ref"], p["matrix_cell"]) for p in value["panels"]}


def run(value, params):
    return solar_local_graph._load_builtin(TOOL).run(value, params)


def post(client, params):
    return client[0].post("/api/run?wait=1", json=body(client, TOOL, params))


def assert_commit(response, client, version):
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {
        "drawing_id": "solar", "version": version, "parent": version - 1}
    assert store.load_manifest(client[1], TENANT, "solar")["head"] == version


def test_autofill_tool_declaration():
    expected = {
        "schema": "leaf.solar-tool.v1",
        "name": "solar-autofill",
        "builtin": "builtins/solar_autofill.py",
        "family": "placement",
        "adapter": "local-graph-commit",
        "entitlement": "run_write",
        "requires_persisted_graph": True,
        "seedable": False,
        "invalid_request_code": "INVALID_AUTOFILL_REQUEST",
        "readiness": {"kind": "facets", "facets": ["frames"]},
        "engine": "server-builtin",
        "interaction": {"mode": "form"},
        "record_store": "registry",
        "record": {
            "name": "solar-autofill",
            "version": "1.0.0",
            "description": "Apply an auto-fill correction plan that rebalances panel group membership, or revert exactly that plan.",
            "kind": "script",
            "family_id": "placement",
            "engine_op": "solar_autofill",
            "entry": "builtins/solar_autofill.py",
            "params": {
                "type": "object",
                "properties": {
                    "drawing_id": {"type": "string", "maxLength": 128},
                    "operation": {"type": "string", "enum": ["apply-corrections", "revert-corrections"]},
                    "expected_rev": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                    "corrections": {
                        "type": "array", "minItems": 1, "maxItems": 4096,
                        "items": {
                            "type": "object",
                            "properties": {
                                "from_ref": {"type": "string", "minLength": 1, "maxLength": 128},
                                "to_ref": {"type": "string", "minLength": 1, "maxLength": 128},
                                "panel_refs": {
                                    "type": "array", "minItems": 1, "maxItems": 4096,
                                    "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                                },
                            },
                            "required": ["from_ref", "to_ref", "panel_refs"],
                            "additionalProperties": False,
                        },
                    },
                    "alignment_tolerance": {"type": "number", "minimum": 0.000001, "maximum": 1000000},
                },
                "required": ["operation", "expected_rev", "corrections"],
                "additionalProperties": False,
            },
            "returns": {"type": "object"},
            "capabilities": ["drawing.write"],
            "allow_local_fallback": False,
        },
        "ledger": ["auto-fill", "auto-fill-revert"],
        "trusted_inputs": [],
        "maturity": "preview",
        "wave": 2,
        "order": 60,
        "scenario": "w2-rooftop",
    }
    assert solar_tools.get(TOOL) == expected
    properties = expected["record"]["params"]["properties"]
    assert set(properties["operation"]["enum"]) == set(
        solar_local_graph._load_builtin(TOOL).OPERATIONS)
    assert "default" not in properties["operation"]
    assert "default" not in properties["expected_rev"]


def test_autofill_tool_registry_and_catalog(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    tools = solar_tools.local_graph_tools()
    assert tools.index("solar-panel-group-delete") < tools.index(TOOL) < tools.index("solar-string-add")
    assert TOOL not in availability.W1_CAPABILITIES
    assert len(availability.W1_CAPABILITIES) == 9
    record = solar_tools.trusted_record(TOOL)
    assert entitlements.tool_required_capability(record) == "run_write"
    found = [(family, row) for family in catalog.build_catalog(deps.all_tools(TENANT))
             for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "placement"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "placement",
        "wave": 2, "order": 60, "maturity": "preview", "engine": "server-builtin",
        "adapter": "local-graph-commit", "entitlement": "run_write",
        "interaction": {"mode": "form"}, "ledger": ["auto-fill", "auto-fill-revert"]}
    assert row["params_schema"] == record["params"]
    assert deps.find_tool(TOOL, TENANT) == record


@pytest.mark.parametrize("path,value,valid", [
    (None, None, True),
    (("alignment_tolerance",), 12.0, True),
    (("alignment_tolerance",), 12, True),
    (("alignment_tolerance",), 1000000, True),
    (("operation",), "revert-corrections", True),
    (("drawing_id",), "solar", True),
    (("corrections", 0, "from_ref"), "a" * 128, True),
    (("corrections", 0, "to_ref"), "b" * 128, True),
    (("corrections", 0, "to_ref"), "b" * 129, False),
    (("corrections", 0, "panel_refs"), [f"p-{i}" for i in range(4096)], True),
    (("corrections", 0, "panel_refs"), [f"p-{i}" for i in range(4097)], False),
    (("corrections",), [{"from_ref": "a", "to_ref": "b", "panel_refs": ["p"]}] * 4096, True),
    (("corrections",), [{"from_ref": "a", "to_ref": "b", "panel_refs": ["p"]}] * 4097, False),
    (("alignment_tolerance",), 1e-6, True),
    (("alignment_tolerance",), 1e-7, False),
    (("operation",), "swap", False),
    (("operation",), None, False),
    (("expected_rev",), None, False),
    (("expected_rev",), -1, False),
    (("expected_rev",), 2147483648, False),
    (("corrections",), [], False),
    (("corrections",), None, False),
    (("extra",), True, False),
    (("corrections", 0, "panel_refs"), None, False),
    (("corrections", 0, "extra"), True, False),
    (("corrections", 0, "panel_refs"), ["p", "p"], False),
    (("corrections", 0, "panel_refs"), [], False),
    (("corrections", 0, "from_ref"), "", False),
    (("corrections", 0, "from_ref"), "a" * 129, False),
    (("drawing_id",), "a" * 129, False),
    (("alignment_tolerance",), 0, False),
    (("alignment_tolerance",), -1.0, False),
    (("alignment_tolerance",), 1000000.5, False),
    (("alignment_tolerance",), "12", False),
    (("alignment_tolerance",), True, False),
])
def test_autofill_tool_schema(path, value, valid):
    params = {"operation": "apply-corrections", "expected_rev": 0,
              "corrections": [{"from_ref": "frame-a", "to_ref": "frame-b",
                               "panel_refs": ["panel-1"]}]}
    if path:
        target = params
        for key in path[:-1]:
            target = target[key]
        if value is None:
            del target[path[-1]]
        else:
            target[path[-1]] = value
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(params) is valid


def test_autofill_tool_kernel_c1(captured):
    _, plan = captured
    assert STRING_LENGTH == 14
    assert plan["feasible"] is True and plan["valid"] is True
    assert plan["total_moved"] == 1
    canonical = [{key: value for key, value in row.items() if key != "distance"}
                 for row in plan["corrections"]]
    assert canonical == [{"chain": "direct", "from": DONOR, "panels": [MOVED_PANEL], "to": RECEIVER}]
    assert sha(canonical) == "87285589060e862c3904d80bb9fce72737b841fc19205f81d14e3fb247089d52"
    assert abs(plan["corrections"][0]["distance"] - 1026.7857530610381) < 1e-9
    assert sha(plan["counts"]) == AFTER_COUNTS


@pytest.mark.parametrize("case,reason", [
    ("captured", None), ("empty", "frames_required"), ("units", "unresolved_units"),
])
def test_autofill_tool_readiness(graph, case, reason):
    if case == "empty":
        graph["frames"] = []
        for panel in graph["panels"]:
            panel["frame_ref"] = panel["matrix_cell"] = None
    elif case == "units":
        graph["project"]["units"]["meters_per_unit"] *= 2
    assert availability.w1_local_commit_inputs(graph)[TOOL] == {
        "input_ready": reason is None, "input_reason": reason}


def test_autofill_tool_route_apply_revert(autofill_api, graph, plan_request, captured_graph):
    assert_members(graph, captured_graph, after=False)
    response = post(autofill_api, dict(plan_request, alignment_tolerance=12.0))
    assert_commit(response, autofill_api, 2)
    filled = head_graph(autofill_api[1])
    assert filled["rev"] == 1
    assert_members(filled, captured_graph, after=True)
    panel = next(p for p in filled["panels"] if p["id"] == captured_graph[2][MOVED_PANEL])
    assert panel["frame_ref"] == captured_graph[3][RECEIVER]
    assert panel["matrix_cell"] == {"row": 0, "col": 15}
    assert panel["provenance"]["last_writer"] == TOOL
    assert dimensions(filled, captured_graph, RECEIVER) == (11, 16)
    assert dimensions(filled, captured_graph, DONOR) == (10, 10)
    response = post(autofill_api, dict(plan_request, operation="revert-corrections", expected_rev=1))
    assert_commit(response, autofill_api, 3)
    reverted = head_graph(autofill_api[1])
    assert reverted["rev"] == 2
    assert_members(reverted, captured_graph, after=False)
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 2
    assert all(row["status"] == "complete" for row in records)


@pytest.mark.parametrize("tolerance", [None, 12.0])
def test_autofill_tool_revert_layout_contract(graph, plan_request, captured_graph, tolerance):
    request = dict(plan_request)
    if tolerance is not None:
        request["alignment_tolerance"] = tolerance
    filled = run(graph, request)
    assert_members(filled, captured_graph, after=True)
    if tolerance is None:
        assert dimensions(filled, captured_graph, RECEIVER) == (2, 137)
        assert dimensions(filled, captured_graph, DONOR) == (1, 71)
    inverse = dict(plan_request, operation="revert-corrections", expected_rev=1)
    reverted = run(filled, inverse)
    assert_members(reverted, captured_graph, after=False)
    assert (placements(reverted) == placements(graph)) is (tolerance is None)
    assert digest(run(filled, dict(inverse, alignment_tolerance=12.0))) == digest(reverted)


def test_autofill_tool_integer_tolerance_matches_float(graph, plan_request):
    assert digest(run(graph, dict(plan_request, alignment_tolerance=12))) == digest(
        run(graph, dict(plan_request, alignment_tolerance=12.0)))


def test_autofill_tool_integer_tolerance_converted(graph, plan_request, monkeypatch):
    builtin = solar_local_graph._load_builtin(TOOL)
    original = builtin._regrid
    seen = []

    def regrid(frame, panels, tolerance):
        seen.append(tolerance)
        assert type(tolerance) is float
        return original(frame, panels, tolerance)

    monkeypatch.setattr(builtin, "_regrid", regrid)
    builtin.run(graph, dict(plan_request, alignment_tolerance=12))
    assert seen == [12.0, 12.0]


def test_autofill_tool_minimum_tolerance_commits(autofill_api, plan_request, captured_graph):
    assert_commit(post(autofill_api, dict(plan_request, alignment_tolerance=1e-6)), autofill_api, 2)
    assert_members(head_graph(autofill_api[1]), captured_graph, after=True)


@pytest.mark.parametrize("exception", [KeyError, IndexError, ZeroDivisionError])
def test_autofill_tool_unstable_regrid_contained(graph, plan_request, monkeypatch, exception):
    builtin = solar_local_graph._load_builtin(TOOL)
    before = copy.deepcopy(graph)

    def unstable(frame, panels, tolerance):
        raise exception(0)

    monkeypatch.setattr(builtin, "_regrid", unstable)
    with pytest.raises(GraphValidationError) as error:
        builtin.run(graph, dict(plan_request, alignment_tolerance=12))
    assert error.value.code == "REGRID_TOLERANCE_UNSTABLE"
    assert graph == before


def test_autofill_tool_integer_tolerance_route(autofill_api, plan_request, captured_graph):
    assert_commit(post(autofill_api, dict(plan_request, alignment_tolerance=12)), autofill_api, 2)
    filled = head_graph(autofill_api[1])
    assert_members(filled, captured_graph, after=True)
    assert dimensions(filled, captured_graph, RECEIVER) == (11, 16)


@pytest.mark.parametrize("tolerance,code", [
    (True, "INVALID_AUTOFILL_REQUEST"), (0, "INVALID_AUTOFILL_REQUEST"),
    (-1.0, "INVALID_AUTOFILL_REQUEST"), (1000000.5, "INVALID_AUTOFILL_REQUEST"),
    (1000001, "INVALID_AUTOFILL_REQUEST"), ("12", "INVALID_AUTOFILL_REQUEST"),
    (1000000, "REGRID_CELL_COLLISION"),
    (1e-7, "INVALID_AUTOFILL_REQUEST"),
])
def test_autofill_tool_tolerance_refusals(graph, plan_request, tolerance, code):
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as error:
        run(graph, dict(plan_request, alignment_tolerance=tolerance))
    assert error.value.code == code
    assert graph == before


@pytest.mark.parametrize("patch", [
    {"operation": "swap"}, {"alignment_tolerance": 0}, {"corrections": []},
    {"extra": True}, {"corrections": None},
])
def test_autofill_tool_route_schema_refusals(autofill_api, plan_request, patch):
    params = dict(plan_request, **patch)
    if params["corrections"] is None:
        del params["corrections"]
    response = post(autofill_api, params)
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(row["status"] != "complete" for row in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(row.get("error") or {}).get("reason_code") for row in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(autofill_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_autofill_tool_route_builtin_refusal(autofill_api, plan_request):
    params = copy.deepcopy(plan_request)
    params["corrections"][0]["to_ref"] = params["corrections"][0]["from_ref"]
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(params)
    response = post(autofill_api, params)
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "INVALID_AUTOFILL_REQUEST"
    assert store.load_manifest(autofill_api[1], TENANT, "solar")["head"] == 1
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "failed"


def test_autofill_tool_revision_and_idempotence(graph, plan_request, captured_graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        applied = dispatch(backend, fence, TOOL, plan_request)
        assert applied["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (applied["before_rev"], applied["after_rev"]) == (0, 1)

        def commit(params, version, job):
            return solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
                source_version=version, holder="fixture-owner", fence=fence, job_id=job)

        inverse = dict(plan_request, operation="revert-corrections", expected_rev=1)
        reverted = commit(inverse, 2, "autofill-revert")
        assert reverted["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
        assert (reverted["before_rev"], reverted["after_rev"]) == (1, 2)
        assert_members(head_graph(backend), captured_graph, after=False)
        with pytest.raises(GraphValidationError, match="PANEL_NOT_IN_GROUP"):
            commit(dict(inverse, expected_rev=2), 3, "autofill-repeat-revert")
        assert latest(backend) == 3
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            commit(plan_request, 3, "autofill-stale-apply")
        assert latest(backend) == 3


def test_autofill_tool_drawing_undo_redo(autofill_api, plan_request, captured_graph, monkeypatch):
    from routers import drawings

    assert_commit(post(autofill_api, dict(plan_request, alignment_tolerance=12.0)), autofill_api, 2)
    client, backend, _, route, _, tenant = autofill_api
    client.app.include_router(drawings.router)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "autofill-test-checkout-secret")
    _, fence = route._checkout_identity()
    headers = {checkout_capability.CAPABILITY_HEADER: checkout_capability.mint(tenant, "solar", fence)}
    original = resolve_graph_context(backend, TENANT, "solar", 1)["graph"]
    committed = head_graph(backend)
    undone = client.post("/api/drawings/solar/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert head_graph(backend) == original
    assert len(membership(head_graph(backend), captured_graph[2], captured_graph[3])[DONOR]) == 71
    redone = client.post("/api/drawings/solar/redo", headers=headers)
    assert redone.status_code == 200, redone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 2
    assert head_graph(backend) == committed
