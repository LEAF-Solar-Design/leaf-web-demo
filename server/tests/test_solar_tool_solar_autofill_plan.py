"""Auto-fill preview produces a proven request, applied and reverted unchanged."""
import copy
import importlib.util
import json
import math
import types
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_local_read
import solar_tools
import store
from solar_autofill import AutofillError
from solar_design_graph import GraphValidationError, validate_graph
from solar_local_graph import stable_numbers
from solar_sizing_client import digest
from test_solar_autofill import (
    STRING_LENGTH, DONOR, RECEIVER, MOVED_PANEL, REMOVED_28, captured, captured_graph,
    captured_groups, committed, group, membership, as_request)
from test_solar_tool_solar_autofill import (
    BEFORE_COUNTS, AFTER_COUNTS, BEFORE_MEMBERS, AFTER_MEMBERS, graph, plan_request,
    autofill_api, sha, assert_members, assert_commit, dimensions, post)
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_solar_w2_registration import head_graph

REPO = Path(__file__).resolve().parents[2]
TOOL = "solar-autofill-plan"
TENANT = "fixture-tenant"
P = {"panels_per_string": 14, "alignment_tolerance": 12.0}
NONE = {"status": "none", "reason_code": None, "request": None, "revert_request": None}
plan_builtin = solar_local_read._load_builtin(TOOL)
commit = solar_local_graph._load_builtin("solar-autofill")
DECLARATION = json.loads('''{
  "schema": "leaf.solar-tool.v1",
  "name": "solar-autofill-plan",
  "builtin": "builtins/solar_autofill_plan.py",
  "family": "placement",
  "adapter": "local-graph-read",
  "entitlement": "run_read",
  "requires_persisted_graph": true,
  "seedable": false,
  "invalid_request_code": "INVALID_AUTOFILL_PLAN_REQUEST",
  "readiness": {"kind": "facets", "facets": ["frames"]},
  "engine": "server-builtin",
  "interaction": {"mode": "form"},
  "record_store": "registry",
  "record": {
    "name": "solar-autofill-plan",
    "version": "1.0.0",
    "description": "Preview the auto-fill correction plan for the current panel groups, ready to apply with solar-autofill.",
    "kind": "script",
    "family_id": "placement",
    "engine_op": "solar_autofill_plan",
    "entry": "builtins/solar_autofill_plan.py",
    "params": {
      "type": "object",
      "properties": {
        "drawing_id": {"type": "string", "maxLength": 128},
        "panels_per_string": {"type": "integer", "minimum": 1, "maximum": 4096},
        "alignment_tolerance": {"type": "number", "minimum": 0.000001, "maximum": 1000000}
      },
      "required": [],
      "additionalProperties": false
    },
    "returns": {"type": "object"},
    "capabilities": ["drawing.read"],
    "allow_local_fallback": false
  },
  "ledger": ["auto-fill"],
  "trusted_inputs": [],
  "maturity": "preview",
  "wave": 2,
  "order": 58,
  "scenario": "w2-rooftop"
}''')


@pytest.fixture
def plan_api(autofill_api, monkeypatch):
    autofill_api[2][TOOL] = solar_tools.trusted_record(TOOL)
    # The rail wires the write lane; drawing.read runs on the fast lane.
    monkeypatch.setitem(jobs._executors, jobs.LANE_FAST, jobs._executors[jobs.LANE_SLOW])
    return autofill_api


def post_plan(client, params):
    return client[0].post("/api/run?wait=1", json=body(client, TOOL, params))


def records():
    return [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]


def port_counts(counts, frame_ids):
    names = {value: key for key, value in frame_ids.items()}
    return {names[ref]: count for ref, count in counts.items()}


def port_order(out, frame_ids):
    names = {value: key for key, value in frame_ids.items()}
    return [names[ref] for ref in out["scan_order"]]


def port_corrections(out, panel_ids, frame_ids):
    panels = {value: key for key, value in panel_ids.items()}
    frames = {value: key for key, value in frame_ids.items()}
    return [{"from": frames[row["from_ref"]], "to": frames[row["to_ref"]],
             "panels": [panels[ref] for ref in row["panel_refs"]], "chain": row["chain"]}
            for row in out["corrections"]]


def assert_schema_refusal(response, client):
    env = response.json()
    assert env.get("ok") is not True, response.text
    rows = records()
    assert all(row["status"] != "complete" for row in rows)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(row.get("error") or {}).get("reason_code") for row in rows]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(client[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_autofill_plan_declaration():
    assert solar_tools.get(TOOL) == DECLARATION
    assert solar_tools.load().get(TOOL) == DECLARATION
    record = solar_tools.trusted_record(TOOL)
    assert record == DECLARATION["record"]
    Draft7Validator.check_schema(record["params"])
    assert all("default" not in prop for prop in record["params"]["properties"].values())


def test_autofill_plan_registry_and_catalog(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    assert TOOL in solar_tools.local_graph_read_tools()
    assert TOOL in solar_local_read.local_graph_read_tools()
    assert TOOL not in solar_tools.local_graph_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-read"
    assert TOOL not in availability.W1_CAPABILITIES
    assert len(availability.W1_CAPABILITIES) == 9
    record = solar_tools.trusted_record(TOOL)
    assert entitlements.tool_required_capability(record) == "run_read"
    found = [(family, row) for family in catalog.build_catalog(deps.all_tools(TENANT))
             for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "placement"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "placement",
        "wave": 2, "order": 58, "maturity": "preview", "engine": "server-builtin",
        "adapter": "local-graph-read", "entitlement": "run_read",
        "interaction": {"mode": "form"}, "ledger": ["auto-fill"]}
    assert row["params_schema"] == record["params"]
    assert deps.find_tool(TOOL, TENANT) == record
    assert TOOL not in (REPO / "server" / "write_tools.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("instance,valid", [
    ({}, True), ({"panels_per_string": 14}, True), ({"panels_per_string": 14.0}, True),
    ({"panels_per_string": 1}, True), ({"panels_per_string": 4096}, True),
    ({"alignment_tolerance": 12.0}, True), ({"alignment_tolerance": 12}, True),
    ({"alignment_tolerance": 1e-6}, True), ({"alignment_tolerance": 1000000}, True),
    ({"drawing_id": "solar"}, True), ({"drawing_id": "a" * 128}, True),
    ({"panels_per_string": 14.5}, False), ({"panels_per_string": 0}, False),
    ({"panels_per_string": 4097}, False), ({"panels_per_string": "14"}, False),
    ({"panels_per_string": True}, False), ({"panels_per_string": None}, False),
    ({"alignment_tolerance": 1e-7}, False), ({"alignment_tolerance": 1000000.5}, False),
    ({"alignment_tolerance": 0}, False), ({"alignment_tolerance": True}, False),
    ({"alignment_tolerance": "12"}, False), ({"drawing_id": "a" * 129}, False),
    ({"extra": 1}, False), ({"expected_rev": 0}, False),
    ({"operation": "apply-corrections"}, False),
])
def test_autofill_plan_schema(instance, valid):
    assert Draft7Validator(DECLARATION["record"]["params"]).is_valid(instance) is valid


def test_autofill_plan_c1(graph, captured_graph):
    out = plan_builtin.run(graph, P)
    assert set(out) == {
        "schema", "graph_rev", "panels_per_string", "panels_per_string_source", "scan_order",
        "feasible", "valid", "violations", "disruption", "total_moved", "counts_before",
        "counts_after", "corrections", "apply"}
    assert out["schema"] == "leaf.solar-autofill-plan.v1"
    assert out["graph_rev"] == 0
    assert out["panels_per_string"] == STRING_LENGTH == 14
    assert type(out["panels_per_string"]) is int
    assert out["panels_per_string_source"] == "request"
    assert out["feasible"] is True and out["valid"] is True
    assert out["violations"] == [
        "Zone solved as 4 spatial cluster(s); trades confined within each cluster."]
    assert out["disruption"] == 2 and out["total_moved"] == 1
    assert port_order(out, captured_graph[3]) == [
        "group:8228", "group:8386", "group:84EC", "group:8567", "group:85CF",
        "group:87F1", "group:81E1"]
    assert sha(port_counts(out["counts_before"], captured_graph[3])) == BEFORE_COUNTS
    assert sha(port_counts(out["counts_after"], captured_graph[3])) == AFTER_COUNTS
    projected = port_corrections(out, captured_graph[2], captured_graph[3])
    assert projected == [{"chain": "direct", "from": DONOR, "panels": [MOVED_PANEL], "to": RECEIVER}]
    assert sha(projected) == "87285589060e862c3904d80bb9fce72737b841fc19205f81d14e3fb247089d52"
    assert abs(out["corrections"][0]["distance"] - 1026.7857530610381) < 1e-9
    expected = dict(as_request(graph, captured_graph[1]["corrections"],
                               captured_graph[2], captured_graph[3]),
                    operation="apply-corrections", alignment_tolerance=12.0)
    assert out["apply"] == {
        "status": "ready", "reason_code": None, "request": expected,
        "revert_request": {"operation": "revert-corrections", "expected_rev": 1,
                           "corrections": expected["corrections"]}}


def test_autofill_plan_purity(graph):
    before = copy.deepcopy(graph)
    params = copy.deepcopy(P)
    out = plan_builtin.run(graph, params)
    assert out == plan_builtin.run(copy.deepcopy(graph), copy.deepcopy(params))
    assert graph == before and params == P
    assert stable_numbers(out) is True
    assert all(set(row) == {"from_ref", "to_ref", "panel_refs", "distance", "chain"}
               for row in out["corrections"])
    assert set(out["apply"]) == {"status", "reason_code", "request", "revert_request"}


@pytest.mark.parametrize("tolerance", [None, 12, 12.0])
def test_autofill_plan_tolerance_echo(graph, tolerance):
    params = {"panels_per_string": 14}
    if tolerance is not None:
        params["alignment_tolerance"] = tolerance
    out = plan_builtin.run(graph, params)
    request = out["apply"]["request"]
    assert out["corrections"] == plan_builtin.run(graph, P)["corrections"]
    keys = {"operation", "expected_rev", "corrections"}
    if tolerance is not None:
        keys.add("alignment_tolerance")
        assert request["alignment_tolerance"] == 12.0
        assert type(request["alignment_tolerance"]) is float
    assert set(request) == keys
    assert "alignment_tolerance" not in out["apply"]["revert_request"]


@pytest.mark.parametrize("setting,params,source", [
    (14, {}, "settings"), (13, {"panels_per_string": 14}, "request"), (0, {}, None),
])
def test_autofill_plan_length_source(graph, setting, params, source):
    expected = plan_builtin.run(graph, P)["corrections"]
    value = copy.deepcopy(graph)
    value["settings"]["panels_in_sequence"] = setting
    if source is None:
        with pytest.raises(GraphValidationError) as error:
            plan_builtin.run(value, params)
        assert error.value.code == "STRING_LENGTH_NOT_SIZED"
    else:
        out = plan_builtin.run(value, params)
        assert out["panels_per_string_source"] == source
        assert out["corrections"] == expected


def test_autofill_plan_integral_float_length(graph):
    out = plan_builtin.run(graph, dict(P, panels_per_string=14.0))
    assert out == plan_builtin.run(graph, P)
    assert type(out["panels_per_string"]) is int


def test_autofill_plan_edited_group_scans_last():
    value, panel_ids, frame_ids = committed([
        group("c", 72, (0.0, 1200.0)), group("a", 137, (0.0, 0.0)),
        group("b", 134, (0.0, 600.0))])
    remover = solar_local_graph._load_builtin("solar-panel-remove")
    value = remover.remove_panels(value, {"expected_rev": 0, "frame_ref": frame_ids["c"],
                                         "panel_refs": [panel_ids["c-71"]]})["graph"]
    out = plan_builtin.run(value, {"panels_per_string": 14})
    assert out["graph_rev"] == 1
    assert port_order(out, frame_ids) == ["a", "b", "c"]
    assert port_corrections(out, panel_ids, frame_ids) == [
        {"from": "c", "to": "a", "panels": ["c-0"], "chain": "direct"}]
    assert abs(out["corrections"][0]["distance"] - 1062.875267235485) < 1e-9
    assert port_counts(out["counts_after"], frame_ids) == {"a": 138, "b": 134, "c": 70}
    assert out["feasible"] is True and out["valid"] is True
    assert out["violations"] == [] and out["disruption"] == 2
    assert out["apply"]["status"] == "ready"
    filled = commit.run(value, out["apply"]["request"])
    assert filled["rev"] == 2
    assert {name: len(refs) for name, refs in membership(filled, panel_ids, frame_ids).items()} == {
        "c": 70, "a": 138, "b": 134}


@pytest.fixture(scope="module")
def product_graph():
    spec = importlib.util.spec_from_file_location(
        "solar_w1_studio_autofill_for_plan", REPO / "scripts" / "solar_w1_studio_autofill.py")
    af = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(af)
    raw = (REPO / "data" / "rooftop_unsplit.intake.json").read_bytes()
    intake = json.loads(raw)
    args = types.SimpleNamespace(layer_contains="Panels", branch_max_offset=120.0,
                                 alignment_tolerance=12.0)
    grouped, geometry = af.commit_groups(
        intake, af.sha256(raw), af.sha256((REPO / "data" / "rooftop_unsplit.dwg").read_bytes()),
        args, "Roof", "2026-09-29T00:00:00+00:00")
    per_frame, _ = af.remove.removal_plan(grouped, REMOVED_28)
    value, rebuilt = grouped, []
    remover = solar_local_graph._load_builtin("solar-panel-remove")
    for frame_id, refs in per_frame.items():
        value = remover.remove_panels(value, {"expected_rev": value["rev"],
                                             "frame_ref": frame_id, "panel_refs": refs})["graph"]
        rebuilt.append(frame_id)
    return af, value, geometry, rebuilt


def test_autofill_plan_product_parity(product_graph):
    af, value, geometry, rebuilt = product_graph
    assert value["rev"] == 2 and len(value["panels"]) == 882
    out = plan_builtin.run(value, {"panels_per_string": 14})
    reference = af.autofill(af.solver_groups(value, geometry, rebuilt), 14)
    assert [(r["from_ref"], r["to_ref"], r["panel_refs"]) for r in out["corrections"]] == [
        (r["from"], r["to"], r["panels"]) for r in reference["corrections"]]
    assert out["counts_after"] == reference["counts"]
    handles = {p["id"]: af.normalized_handle(p["provenance"]["source_handle"])
               for p in value["panels"]}
    names = {f["id"]: "group:" + min((handles[r] for r in f["panel_refs"]), key=lambda h: int(h, 16))
             for f in value["frames"]}
    panel_ids = {handle: ref for ref, handle in handles.items()}
    frame_ids = {name: ref for ref, name in names.items()}
    assert port_corrections(out, panel_ids, frame_ids) == [
        {"from": DONOR, "to": RECEIVER, "panels": [MOVED_PANEL], "chain": "direct"}]
    distance = out["corrections"][0]["distance"]
    assert distance == reference["corrections"][0]["distance"]
    assert abs(distance - 1026.7857530610381) < 1e-9
    assert port_order(out, frame_ids)[-1] == DONOR
    assert sha(port_counts(out["counts_after"], frame_ids)) == AFTER_COUNTS
    assert out["apply"]["status"] == "ready"


@pytest.mark.parametrize("tolerance", [None, 12.0])
def test_autofill_plan_multi_correction(graph, captured_graph, tolerance):
    params = {"panels_per_string": 28}
    if tolerance is not None:
        params["alignment_tolerance"] = tolerance
    out = plan_builtin.run(graph, params)
    assert out["feasible"] is True and out["valid"] is True
    assert out["total_moved"] == 14 and out["disruption"] == 28
    assert out["violations"] == [
        "Zone solved as 3 spatial cluster(s); trades confined within each cluster."]
    projected = port_corrections(out, captured_graph[2], captured_graph[3])
    assert [(r["from"], r["to"], r["panels"]) for r in projected] == [
        ("group:87F1", "group:84EC", ["8891", "8884", "8883", "8882", "8881", "8880", "887F"]),
        ("group:87F1", "group:81E1", ["8877", "8876", "8875", "8874", "8873", "8872", "8871"])]
    for row, distance in zip(out["corrections"], [2402.3453454966875, 2665.9671565072563]):
        assert abs(row["distance"] - distance) < 1e-9
    assert sha(port_counts(out["counts_after"], captured_graph[3])) == (
        "cdd10f83726c225a44cddbe343538fe833aeafc59b6f726cff8914e0f5fe6fde")
    assert out["apply"]["status"] == "ready"


def test_autofill_plan_infeasible(graph, captured_graph):
    out = plan_builtin.run(graph, {"panels_per_string": 4096})
    assert out["feasible"] is False and out["valid"] is False
    assert out["total_moved"] == 0 and out["corrections"] == []
    assert out["apply"] == NONE
    assert sha(port_counts(out["counts_after"], captured_graph[3])) == BEFORE_COUNTS
    assert len(out["violations"]) == 8
    assert out["violations"][0] == "Zone total 854 has no feasible partition across 7 groups for n=4096."
    names = {value: key for key, value in captured_graph[3].items()}
    actual = out["violations"][1:]
    for ref, name in names.items():
        actual = [text.replace(ref, name) for text in actual]
    assert set(actual) == {
        f"Group {name} count {count} infeasible after solver."
        for name, count in port_counts(out["counts_after"], captured_graph[3]).items()}


def test_autofill_plan_nothing_left(graph, captured_graph):
    first = plan_builtin.run(graph, P)
    out = plan_builtin.run(commit.run(graph, first["apply"]["request"]), P)
    assert out["graph_rev"] == 1
    assert out["feasible"] is True and out["valid"] is True
    assert out["total_moved"] == out["disruption"] == 0
    assert out["corrections"] == [] and out["apply"] == NONE
    assert port_order(out, captured_graph[3]) == [
        "group:8386", "group:84EC", "group:8567", "group:85CF", "group:87F1",
        "group:8228", "group:81E1"]


@pytest.mark.parametrize("a,c", [(12, 2), (13, 1), (26, 2)])
def test_autofill_plan_commit_refusal(a, c):
    value, _, frame_ids = committed([group("a", a, (0.0, 0.0)), group("c", c, (0.0, 200.0))])
    out = plan_builtin.run(value, {"panels_per_string": 14})
    assert port_counts(out["counts_after"], frame_ids) == {"a": a + c, "c": 0}
    assert out["feasible"] is True and out["valid"] is True
    assert out["apply"] == dict(NONE, status="refused", reason_code="GROUP_WOULD_BE_EMPTY")


def test_autofill_plan_regrid_refusal(graph):
    out = plan_builtin.run(graph, dict(P, alignment_tolerance=1000000))
    assert out["apply"] == dict(NONE, status="refused", reason_code="REGRID_CELL_COLLISION")
    assert out["corrections"] == plan_builtin.run(graph, P)["corrections"]


def test_autofill_plan_no_groups(graph):
    graph["frames"] = []
    for panel in graph["panels"]:
        panel["frame_ref"] = panel["matrix_cell"] = None
    validate_graph(graph)
    out = plan_builtin.run(graph, {"panels_per_string": 14})
    assert out["scan_order"] == out["corrections"] == []
    assert out["counts_before"] == out["counts_after"] == {}
    assert out["feasible"] is True and out["valid"] is True
    assert out["apply"] == NONE


def test_autofill_plan_unresolved_units(graph):
    graph["project"]["units"]["meters_per_unit"] *= 2
    with pytest.raises(GraphValidationError) as error:
        plan_builtin.run(graph, P)
    assert error.value.code == "UNRESOLVED_UNITS"


@pytest.mark.parametrize("params,code", [
    ([], "INVALID_AUTOFILL_PLAN_REQUEST"), (None, "INVALID_AUTOFILL_PLAN_REQUEST"),
    ("x", "INVALID_AUTOFILL_PLAN_REQUEST"),
    *[({"panels_per_string": value}, "INVALID_AUTOFILL_PLAN_REQUEST")
      for value in (0, -1, 4097, 14.5, True, "14", None)],
    *[({"panels_per_string": 14, "alignment_tolerance": value}, "INVALID_AUTOFILL_PLAN_REQUEST")
      for value in (0, 1e-7, 1000000.5, True, "12")],
    ({"panels_per_string": 14, "extra": 1}, "INVALID_AUTOFILL_PLAN_REQUEST"),
    ({"panels_per_string": 14, "drawing_id": "solar"}, "INVALID_AUTOFILL_PLAN_REQUEST"),
    ({"panels_per_string": 14, "expected_rev": 0}, "INVALID_AUTOFILL_PLAN_REQUEST"),
    ({"panels_per_string": 14, "alignment_tolerance": float("inf")}, "NONFINITE_NUMBER"),
])
def test_autofill_plan_request_refusals(graph, params, code):
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as error:
        plan_builtin.run(graph, params)
    assert error.value.code == code
    assert graph == before


def test_autofill_plan_kernel_refusal(graph, monkeypatch):
    def refuse(*args):
        raise AutofillError("x")
    monkeypatch.setattr(plan_builtin, "autofill", refuse)
    with pytest.raises(GraphValidationError) as error:
        plan_builtin.run(graph, P)
    assert error.value.code == "AUTOFILL_PLAN_REFUSED"


def test_autofill_plan_zone_key(graph):
    graph["frames"][0]["electrical_zone_ref"] = "leaf:zone:test"
    groups = plan_builtin.solver_groups(graph)
    frames = {f["id"]: f for f in graph["frames"]}
    for entry in groups:
        expected = "leaf:zone:test|" if entry["id"] == graph["frames"][0]["id"] else "|"
        assert entry["zone"] == expected
        assert [p["id"] for p in entry["panels"]] == frames[entry["id"]]["panel_refs"]
        assert all(set(p) == {"id", "x", "y", "angle"} for p in entry["panels"])


def test_autofill_plan_route_preview(plan_api, graph):
    before = copy.deepcopy(store.load_manifest(plan_api[1], TENANT, "solar"))
    response = post_plan(plan_api, P)
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    assert result["output"] == plan_builtin.run(graph, P)
    assert result["output_sha256"] == digest(result["output"])
    assert result["drawing_changed"] is False
    manifest = store.load_manifest(plan_api[1], TENANT, "solar")
    assert manifest["head"] == 1
    assert manifest["latest"] == before["latest"] == 1
    assert manifest["versions"] == before["versions"]
    rows = records()
    assert len(rows) == 1 and rows[0]["status"] == "complete"


def test_autofill_plan_route_apply_revert(plan_api, captured_graph):
    response = post_plan(plan_api, P)
    assert response.status_code == 200, response.text
    preview = response.json()["result"]["output"]
    assert_commit(post(plan_api, preview["apply"]["request"]), plan_api, 2)
    filled = head_graph(plan_api[1])
    assert filled["rev"] == 1
    assert_members(filled, captured_graph, after=True)
    panel = next(p for p in filled["panels"] if p["id"] == captured_graph[2][MOVED_PANEL])
    assert panel["frame_ref"] == captured_graph[3][RECEIVER]
    assert panel["matrix_cell"] == {"row": 0, "col": 15}
    assert panel["provenance"]["last_writer"] == "solar-autofill"
    assert dimensions(filled, captured_graph, RECEIVER) == (11, 16)
    assert dimensions(filled, captured_graph, DONOR) == (10, 10)
    second = post_plan(plan_api, P)
    assert second.status_code == 200, second.text
    out = second.json()["result"]["output"]
    assert out["graph_rev"] == 1 and out["apply"]["status"] == "none"
    assert out["total_moved"] == 0
    assert_commit(post(plan_api, preview["apply"]["revert_request"]), plan_api, 3)
    reverted = head_graph(plan_api[1])
    assert reverted["rev"] == 2
    assert_members(reverted, captured_graph, after=False)
    rows = records()
    assert len(rows) == 4 and all(row["status"] == "complete" for row in rows)


@pytest.mark.parametrize("params", [
    {"panels_per_string": 0}, {"panels_per_string": 4097},
    {"panels_per_string": 14, "extra": 1}, {"panels_per_string": 14, "alignment_tolerance": 0},
])
def test_autofill_plan_route_schema_refusals(plan_api, params):
    assert_schema_refusal(post_plan(plan_api, params), plan_api)


def test_autofill_plan_route_builtin_refusal(plan_api):
    response = post_plan(plan_api, {})
    assert response.json().get("ok") is not True
    assert "STRING_LENGTH_NOT_SIZED" in response.text
    assert all(row["status"] != "complete" for row in records())
    assert store.load_manifest(plan_api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("value,code", [
    (0.0, None), (-0.0, None), (1.0, "STALE_GRAPH_REVISION"),
    (2147483647.0, "STALE_GRAPH_REVISION"), (1.5, "INVALID_AUTOFILL_REQUEST"),
    (2147483648.0, "INVALID_AUTOFILL_REQUEST"), (True, "INVALID_AUTOFILL_REQUEST"),
    ("0", "INVALID_AUTOFILL_REQUEST"), (None, "INVALID_AUTOFILL_REQUEST"),
])
def test_autofill_plan_float_revision(graph, plan_request, value, code):
    before = copy.deepcopy(graph)
    params = dict(plan_request, expected_rev=value)
    if code is None:
        assert digest(commit.run(graph, params)) == digest(
            commit.run(copy.deepcopy(graph), dict(plan_request, expected_rev=0)))
    else:
        with pytest.raises(GraphValidationError) as error:
            commit.run(graph, params)
        assert error.value.code == code
    assert graph == before


def test_autofill_plan_float_revert(graph, plan_request, captured_graph):
    filled = commit.run(graph, plan_request)
    reverted = commit.run(filled, dict(plan_request, operation="revert-corrections", expected_rev=1.0))
    assert reverted["rev"] == 2
    assert_members(reverted, captured_graph, after=False)


def test_autofill_plan_route_float_revision(plan_api, plan_request, captured_graph):
    assert_commit(post(plan_api, dict(plan_request, expected_rev=0.0)), plan_api, 2)
    assert_members(head_graph(plan_api[1]), captured_graph, after=True)


def test_autofill_plan_route_nonintegral_revision(plan_api, plan_request):
    assert_schema_refusal(post(plan_api, dict(plan_request, expected_rev=1.5)), plan_api)


@pytest.mark.parametrize("angle,accepted", [
    (10**19, False), (720, True), (-720, True), (720.5, False), (-720.5, False),
])
def test_autofill_plan_panel_angle_bound(graph, monkeypatch, angle, accepted):
    original = plan_builtin.kernel.angle_rationalise

    def bounded(value):
        assert abs(value) <= 4 * math.pi, "unbounded angle reached the kernel"
        return original(value)

    monkeypatch.setattr(plan_builtin.kernel, "angle_rationalise", bounded)
    panel = graph["panels"][0]
    panel["angle"] = angle
    for frame in graph["frames"]:
        for row in frame["matrix"]:
            for cell in row:
                if cell["panel_ref"] == panel["id"]:
                    cell["angle"] = angle
    if accepted:
        assert plan_builtin.run(graph, {"panels_per_string": 14})["schema"] == plan_builtin.PLAN_SCHEMA
    else:
        with pytest.raises(GraphValidationError) as error:
            plan_builtin.run(graph, {"panels_per_string": 14})
        assert error.value.code == "AUTOFILL_PANEL_ANGLE_UNSUPPORTED"


@pytest.mark.parametrize("rev", [999999, 999998])
def test_autofill_plan_revert_revision_available(monkeypatch, rev):
    value, _, _ = committed([
        group("a", 137, (0.0, 0.0)), group("b", 134, (0.0, 600.0)),
        group("c", 71, (0.0, 1200.0))])
    value["rev"], value["parent_rev"] = rev, rev - 1
    if rev == 999999:
        def unexpected(*args):
            pytest.fail("unavailable revert must be refused before the dry run")
        monkeypatch.setattr(plan_builtin._APPLIER, "run", unexpected)
    out = plan_builtin.run(value, {"panels_per_string": 14})
    if rev == 999999:
        assert out["apply"] == dict(NONE, status="refused", reason_code="AUTOFILL_REVERT_UNAVAILABLE")
    else:
        assert out["apply"]["status"] == "ready"
        assert out["apply"]["revert_request"]["expected_rev"] == 999999


def test_autofill_plan_graph_revision_schema_bound():
    schema = json.loads((REPO / "contract" / "solar-design-graph.v1.schema.json").read_text())
    assert plan_builtin.MAX_GRAPH_REV == schema["properties"]["rev"]["maximum"]


@pytest.mark.parametrize("row_angle", ["oops", None, True, 1e19])
def test_autofill_plan_regrid_metadata_invalid(graph, monkeypatch, row_angle):
    for frame in graph["frames"]:
        frame["provenance"]["row_angle"] = row_angle

    def unexpected(*args):
        pytest.fail("invalid regrid metadata must be refused before the dry run")

    monkeypatch.setattr(plan_builtin._APPLIER, "run", unexpected)
    out = plan_builtin.run(graph, P)
    assert out["apply"] == dict(NONE, status="refused", reason_code="AUTOFILL_REGRID_METADATA_INVALID")


def test_autofill_plan_without_tolerance_ignores_row_angle(graph):
    params = {"panels_per_string": 14}
    expected = plan_builtin.run(graph, params)
    for frame in graph["frames"]:
        frame["provenance"]["row_angle"] = "oops"
    assert plan_builtin.run(graph, params) == expected


@pytest.mark.parametrize("refusal", [None, "DRY_RUN_REFUSED"])
def test_autofill_plan_dry_run_executed(graph, monkeypatch, refusal):
    original = plan_builtin._APPLIER.run
    calls = []
    results = []

    def tracked(value, request):
        calls.append(copy.deepcopy(request))
        expected = graph if len(calls) == 1 else results[0]
        assert value == expected and value is not expected
        if refusal is not None:
            raise GraphValidationError(refusal)
        result = original(value, request)
        results.append(result)
        request["corrections"].append({"mutated": True})
        return result

    monkeypatch.setattr(plan_builtin._APPLIER, "run", tracked)
    out = plan_builtin.run(graph, P)
    assert len(calls) == (2 if refusal is None else 1)
    if refusal is None:
        assert out["apply"]["status"] == "ready"
        assert calls[0] == out["apply"]["request"]
        assert calls[1] == out["apply"]["revert_request"]
    else:
        assert out["apply"] == dict(NONE, status="refused", reason_code=refusal)


@pytest.mark.parametrize("counts,length,ready", [
    ((1, 3, 4), 4, False), ((137, 134, 71), 14, True),
])
def test_autofill_plan_revert_proven(counts, length, ready):
    value, panel_ids, frame_ids = committed([
        group(name, count, (0.0, float(index * 200)))
        for index, (name, count) in enumerate(zip(("a", "b", "c"), counts))])
    out = plan_builtin.run(value, {"panels_per_string": length})
    if ready:
        assert out["apply"]["status"] == "ready"
        filled = commit.run(value, out["apply"]["request"])
        reverted = commit.run(filled, out["apply"]["revert_request"])
        assert membership(reverted, panel_ids, frame_ids) == membership(value, panel_ids, frame_ids)
    else:
        assert port_counts(out["counts_after"], frame_ids) == {"a": 2, "b": 3, "c": 3}
        assert out["apply"] == dict(NONE, status="refused", reason_code="AUTOFILL_REVERT_UNAVAILABLE")


def test_autofill_plan_geometry_overflow():
    value, panel_ids, frame_ids = committed([
        group("a", 137, (0.0, 0.0)), group("b", 134, (0.0, 600.0)),
        group("c", 71, (0.0, 1200.0))])
    coordinates = {panel_ids["c-0"]: 1e308, panel_ids["c-1"]: -1e308}
    for panel in value["panels"]:
        if panel["id"] in coordinates:
            panel["centre"][0] = coordinates[panel["id"]]
    frame = next(frame for frame in value["frames"] if frame["id"] == frame_ids["c"])
    for row in frame["matrix"]:
        for cell in row:
            if cell["panel_ref"] in coordinates:
                cell["x"] = coordinates[cell["panel_ref"]]
    validate_graph(value)
    with pytest.raises(GraphValidationError) as error:
        plan_builtin.run(value, {"panels_per_string": 14})
    assert error.value.code == "AUTOFILL_GEOMETRY_UNSUPPORTED"


@pytest.mark.parametrize("invalid_frame", ["b", "a"])
def test_autofill_plan_regrid_metadata_touched_only(invalid_frame):
    value, _, frame_ids = committed([
        group("a", 137, (0.0, 0.0)), group("b", 134, (0.0, 600.0)),
        group("c", 71, (0.0, 1200.0))])
    frame = next(frame for frame in value["frames"] if frame["id"] == frame_ids[invalid_frame])
    frame["provenance"]["row_angle"] = "oops"
    out = plan_builtin.run(value, P)
    if invalid_frame == "b":
        assert out["apply"]["status"] == "ready"
        commit.run(value, out["apply"]["request"])
    else:
        assert out["apply"] == dict(NONE, status="refused", reason_code="AUTOFILL_REGRID_METADATA_INVALID")


@pytest.mark.parametrize("operation", ["apply-corrections", "revert-corrections"])
def test_autofill_plan_dry_run_arithmetic_refusal(graph, monkeypatch, operation):
    original = plan_builtin._APPLIER.run

    def overflow(value, request):
        if request["operation"] == operation:
            raise OverflowError("geometry")
        return original(value, request)

    monkeypatch.setattr(plan_builtin._APPLIER, "run", overflow)
    out = plan_builtin.run(graph, P)
    assert out["apply"] == dict(NONE, status="refused", reason_code="AUTOFILL_GEOMETRY_UNSUPPORTED")
