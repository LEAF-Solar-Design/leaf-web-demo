"""Authenticated Solar output HTTP contracts and neighboring response preservation."""
from copy import deepcopy
from hashlib import sha256
from itertools import combinations
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import deps
import entitlements
import envelopes
import solar_artifacts as standalone
import solar_project_context as project
import solar_project_read as read
import solar_project_artifacts as artifacts
import solar_project_solaredge as solaredge
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, validate_graph
from routers import project_drawings as routes, drawings, jobs, capabilities
from route_flatten import iter_leaf_routes
from solar_drawing_roles import discover_roles
import test_sip_r6_read as reads
import test_sip_r6_artifacts as files
import test_sip_r5_routes as admission_cases
import test_project_scoped_read_tool as generic_cases
import test_s02_partial_tracking as partial_tracking
from test_s02_partial_tracking import mixed as partial_tracking_lane
from test_project_solaredge import accept, lane as tracking_lane, report, samples
from test_sip_r3b_tools import INTAKE, embedded
from test_sip_r6_read import lane as engineering_lane
from test_solar_ground_graph_codec import b18, ground_graph
from test_solar_ground_topology import topology_of
from test_w1_design_graph import graph

_FIND_TOOL = deps.find_tool
_ENGINE_TOOLS = deps.load_engine_registry_tools
_ACCESS = project._access
_DYNAMIC_READ = generic_cases.tool_loader.run_tool_dynamic


@pytest.fixture
def http(monkeypatch):
    s = reads.setup(monkeypatch)
    files.immutable(s, monkeypatch)
    principal = {"tenant": s.tenant, "member": True, "calls": 0}
    def access(tenant, proj, *, write):
        principal["calls"] += 1
        if str(tenant) != str(s.org) or proj != s.project:
            project.refuse("CONTEXT_NOT_FOUND")
        if not principal["member"]:
            project.refuse("PROJECT_FORBIDDEN")
        assert write is False
        return s.org, s.actor
    monkeypatch.setattr(project, "_access", access)
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.middleware("http")(routes.no_store_responses)
    app.include_router(routes.router)
    app.include_router(drawings.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: principal["tenant"]
    app.dependency_overrides[deps.require_tenant] = lambda: principal["tenant"]
    with TestClient(app) as client:
        yield SimpleNamespace(s=s, client=client, app=app, principal=principal)


def read_path(http, version=None):
    s = http.s
    return f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{version or s.head}/solar-reads"


def file_path(http, artifact_id):
    s = http.s
    return f"/api/projects/{s.project}/drawings/{s.drawing}/solar-artifacts/{artifact_id}"


def body(tool=reads.CALC, params=None, **changes):
    result = {"tool": tool, "params": deepcopy(reads.NEC[reads.CALC] if params is None else params),
              "catalog_digest": reads.tools_cases.manifest(tool)}
    result.update(changes)
    return result


def post(http, **changes):
    return http.client.post(read_path(http), json=body(**changes))


def refusal(response, reason, status, sentence=None):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["reason_code"] == reason
    assert error["error_code"] == ("FORBIDDEN" if status == 403 else "INTERNAL" if status >= 500 else "BAD_PARAMS")
    assert response.headers["cache-control"] == "no-store"
    if sentence is not None:
        assert error["message"] == sentence
    return error


def specimen(response):
    stable = ("content-type", "cache-control", "etag", "x-leaf-artifact-id",
              "x-content-type-options", "content-disposition", "x-leaf-source-version")
    return response.status_code, response.content, {key: response.headers[key] for key in stable if key in response.headers}


def forged(s, prepared, **changes):
    meta = {**prepared.meta, **changes}
    binding = {key: meta[key] for key in artifacts._BINDING_KEYS}
    binding["schema"] = artifacts.BINDING_SCHEMA
    meta["artifact_id"] = sha256(canonical_bytes(binding)).hexdigest()
    s.blobs[artifacts._key(meta, meta["artifact_id"])] = canonical_bytes(meta)
    return meta["artifact_id"]


def test_sip_r6_routes_mounted_and_flagged(http, monkeypatch):
    expected = {
        ("POST", "/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-reads"),
        ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-schedule-comparison"),
        ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-engineering-comparison"),
        ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/solar-artifacts/{artifact_id}")}
    counts = dict.fromkeys(expected, 0)
    for path, route in iter_leaf_routes(http.app.routes):
        for method in getattr(route, "methods", ()) or ():
            if (method, path) in expected:
                counts[method, path] += 1
                assert deps.require_active_tenant in [dep.call for dep in route.dependant.dependencies]
    assert set(counts.values()) == {1}
    for value in (None, "0", "true", "1 ", " 1", "01", "", "1"):
        if value is None:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        else:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", value)
        if value == "1":
            assert post(http).status_code == 200
        else:
            before = list(http.s.reads), http.principal["calls"]
            empty = {"artifact_id": "0" * 64, "source_version_id": str(http.s.head)}
            for response in (post(http), http.client.get(file_path(http, "0" * 64)),
                             compare_schedules(http, empty, empty)):
                refusal(response, "project_execution_disabled", 409,
                        "Project Solar outputs are not enabled on this server.")
            assert (http.s.reads, http.principal["calls"]) == before


def schedule_artifact(http, version, *, tool="solar-electrical-schedules"):
    response = http.client.post(read_path(http, version), json=body(tool=tool, params={}))
    assert response.status_code == 200, response.text
    return response.json()["result"]["output"]["artifact"]


def save_schedule_graph(http, graph):
    s = http.s
    s.parent = type(s.parent)(uuid4(), s.drawing, s.project, s.org, len(s.versions) + 1,
                              created_at=s.parent.created_at)
    s.set_parent(reads.tools_cases.embedded(graph))
    s.head = s.parent.version_id
    return s.head


def compare_schedules(http, before, after, **params):
    s = http.s
    path = f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{before['source_version_id']}"
    return http.client.get(f"{path}/solar-schedule-comparison", params={
        "compare_version_id": after["source_version_id"], "before_artifact_id": before["artifact_id"],
        "after_artifact_id": after["artifact_id"], **params,
    })


def compare_saved_cables(http, before, after, **params):
    s = http.s
    path = f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{before}"
    return http.client.get(f"{path}/solar-schedule-comparison", params={
        "compare_version_id": after, "cable_offset": 0, **params,
    })


def test_sip_r6_compares_saved_cables_without_artifacts(http):
    graph = files.schedules_cases.solaredge_layout(files.design_cases.graph.__wrapped__(), "direct")
    before = save_schedule_graph(http, graph)
    revised = deepcopy(graph)
    route = revised["routes"][0]
    route["points"].insert(1, [0, 12])
    after = save_schedule_graph(http, revised)
    s = http.s
    custody = dict(s.blobs), s.head, list(s.writes)
    lengths = (16.40, 82.02)  # 5 m and 25 m, rounded to workbook precision.

    for first, second, expected in ((before, after, lengths), (after, before, lengths[::-1]),
                                    (before, before, ())):
        response = compare_saved_cables(http, first, second)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        result = response.json()["result"]
        assert result["schema"] == "leaf.solar-project-saved-cable-length-comparison.v1"
        assert (result["organization_id"], result["project_id"], result["drawing_id"]) == (
            str(s.org), str(s.project), str(s.drawing))
        assert result["before"]["source_version_id"] == str(first)
        assert result["after"]["source_version_id"] == str(second)
        assert result["offset"] == 0 and result["next_offset"] is None
        if expected:
            change, = result["changes"]
            assert result["total_changes"] == 1 and change["change"] == "changed"
            assert change["route_id"] == route["id"]
            assert tuple(change[side]["length_ft"] for side in ("before", "after")) == expected
            label = next(row["circuit_tag"] for row in graph["strings"] if row["id"] == route["from_ref"])
            assert all(change[side]["schedule_label"] == label for side in ("before", "after"))
        else:
            assert result["total_changes"] == 0 and result["changes"] == []

    assert compare_saved_cables(http, before, uuid4()).status_code == 404
    assert compare_saved_cables(http, before, after, before_artifact_id="a" * 64).status_code == 400
    assert (s.blobs, s.head, s.writes) == custody


@pytest.mark.parametrize(
    ("before_detour", "after_detour", "before_z", "after_z", "expected"),
    [
        (None, None, None, 0, ()),
        (None, None, 0, 8, ()),
        (12, -12, 0, 0, ()),
        (None, 12, 0, 0, (16.40, 82.02)),
    ],
    ids=["xy-to-xyz", "elevation-only", "mirrored-detour", "longer-detour"],
)
def test_saved_cable_lengths_use_plan_coordinates(
    http, before_detour, after_detour, before_z, after_z, expected,
):
    graph = files.schedules_cases.solaredge_layout(files.design_cases.graph.__wrapped__(), "direct")
    before, after = deepcopy(graph), deepcopy(graph)
    for revision, detour, elevation in ((before, before_detour, before_z), (after, after_detour, after_z)):
        points = revision["routes"][0]["points"]
        if detour is not None:
            points.insert(1, [0, detour])
        if elevation is not None:
            for point in points:
                point.append(elevation)

    before_id = save_schedule_graph(http, before)
    after_id = save_schedule_graph(http, after)
    state = http.s
    custody = dict(state.blobs), state.head, list(state.writes)
    response = compare_saved_cables(http, before_id, after_id)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert result["total_changes"] == (1 if expected else 0)
    assert result["before"]["source_version_id"] == str(before_id)
    assert result["after"]["source_version_id"] == str(after_id)
    assert (state.blobs, state.head, state.writes) == custody
    if expected:
        change, = result["changes"]
        assert change["route_id"] == graph["routes"][0]["id"]
        assert change["change"] == "changed"
        assert (change["before"]["length_ft"], change["after"]["length_ft"]) == expected
    else:
        assert result["changes"] == []


@pytest.mark.parametrize("variant", ["relabel", "unassign", "input-swap"])
def test_sip_r6_schedule_comparison_direct(http, variant):
    make_graph = files.design_cases.graph.__wrapped__
    layout = files.schedules_cases.solaredge_layout
    graph = layout(make_graph(), "direct")
    before = schedule_artifact(http, save_schedule_graph(http, graph))
    target = deepcopy(graph)
    first, second = (item["id"] for item in graph["strings"])
    if variant == "relabel":
        target["strings"][0]["circuit_tag"] = "Revised label"
        expected = {first: ["circuit_label"]}
    elif variant == "unassign":
        target = layout(make_graph(), "partial")
        expected = {second: ["collector_id", "mppt_letter", "input_number"]}
    else:
        inputs = target["inverters"][0]["input_assignments"]
        inputs[0]["input_number"], inputs[1]["input_number"] = 1, 0
        for frame in target["frames"]:
            for panel in frame["panel_assignments"] + [cell for row in frame["matrix"] for cell in row]:
                panel["string_input_number"] = 1 - panel["string_input_number"]
        expected = {first: ["input_number"], second: ["input_number"]}
    after = schedule_artifact(http, save_schedule_graph(http, target))
    blobs, head, writes = dict(http.s.blobs), http.s.head, list(http.s.writes)

    response = compare_schedules(http, before, after)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert result["schema"] == "leaf.solar-project-schedule-comparison.v1"
    for side, ref in (("before", before), ("after", after)):
        assert result[side] == {field: ref[field] for field in result[side]}
        assert set(result[side]) == {"artifact_id", "source_version_id", "source_intake_sha256",
                                     "graph_sha256", "content_sha256"}
    assert [row["string_id"] for row in result["changes"]] == sorted(expected)
    assert {row["string_id"]: row["changed_fields"] for row in result["changes"]} == expected
    assert {row["change"] for row in result["changes"]} == {"changed"}
    if variant == "unassign":
        assert result["changes"][0]["after"]["input_number"] is None
    if variant == "input-swap":
        assert {row["after"]["input_number"] for row in result["changes"]} == {0, 1}
    reversed_result = compare_schedules(http, after, before).json()["result"]
    assert reversed_result["before"] == result["after"] and reversed_result["after"] == result["before"]
    assert [(row["after"], row["before"]) for row in reversed_result["changes"]] == [
        (row["before"], row["after"]) for row in result["changes"]]
    assert compare_schedules(http, before, before).json()["result"]["changes"] == []
    assert (http.s.blobs, http.s.head, http.s.writes) == (blobs, head, writes)


def test_sip_r6_schedule_comparison_reports_replaced_string(http):
    graph = files.schedules_cases.solaredge_layout(files.design_cases.graph.__wrapped__(), "direct")
    before = schedule_artifact(http, save_schedule_graph(http, graph))
    target = deepcopy(graph)
    string = target["strings"][0]
    removed_id = string["id"]
    added_id = files.design_cases.app_id("string", 3)
    string["id"] = added_id
    for panel in target["panels"]:
        if panel["id"] in string["ordered_panel_refs"]:
            panel["assignment"]["string_ref"] = added_id
    frame = target["frames"][0]
    for ref in frame["sequences"] + frame["panel_assignments"] + target["inverters"][0]["input_assignments"]:
        if ref["string_ref"] == removed_id:
            ref["string_ref"] = added_id
    for route in target["routes"]:
        if route["from_ref"] == removed_id:
            route["from_ref"] = added_id
    after = schedule_artifact(http, save_schedule_graph(http, target))

    row = {"circuit_label": string["circuit_tag"], "module_count": string["module_count"],
           "collector_id": string["inverter_ref"], "mppt_letter": "A", "input_number": 0,
           "upstream_inverter_id": None}
    for left, right, old_id, new_id in ((before, after, removed_id, added_id),
                                        (after, before, added_id, removed_id)):
        response = compare_schedules(http, left, right)
        assert response.status_code == 200, response.text
        changes = response.json()["result"]["changes"]
        assert [change["string_id"] for change in changes] == sorted((old_id, new_id))
        assert {change["string_id"]: change for change in changes} == {
            old_id: {"string_id": old_id, "change": "removed", "changed_fields": [],
                     "before": row, "after": None},
            new_id: {"string_id": new_id, "change": "added", "changed_fields": [],
                     "before": None, "after": row},
        }


def test_sip_r6_schedule_comparison_balanced_inverter_swap(http, monkeypatch):
    s = http.s
    record = files.sizing_cases.passing.__wrapped__()
    with monkeypatch.context() as sizing:
        files.sizing_cases.service.__wrapped__(sizing, record)
        graph = files.schedules_cases.build_i7(files.design_cases.graph.__wrapped__(), record)
    for inverter in graph["inverters"]:
        if inverter["is_l2"]:
            inverter["is_solaredge"] = True

    def publish(value):
        source = reads.tools_cases.canonical_custody(s, monkeypatch, validate_graph(value))
        reads.tools_cases.commit(s, "solar-settings",
            {"expected_rev": source.graph["rev"], "project_changes": {"name": value["project"]["name"]}}, source)
        return schedule_artifact(http, s.head)

    before = publish(graph)
    target = deepcopy(graph)
    strings = {row["id"]: row for row in target["strings"]}
    assignments = [(collector, slot, strings[slot["string_ref"]]) for collector in target["inverters"]
                   for slot in collector["input_assignments"]]
    (left, left_slot, first), (right, right_slot, second) = next(
        (a, b) for a, b in combinations(assignments, 2)
        if a[0]["l2_ref"] != b[0]["l2_ref"] and a[2]["module_count"] == b[2]["module_count"]
        and (a[1]["mppt_letter"], a[1]["input_number"]) == (b[1]["mppt_letter"], b[1]["input_number"]))
    assert first["module_count"] > 0
    left_slot["string_ref"], right_slot["string_ref"] = second["id"], first["id"]
    first["inverter_ref"], second["inverter_ref"] = right["id"], left["id"]
    destinations = {first["id"]: right["id"], second["id"]: left["id"]}
    for route in target["routes"]:
        if route["from_ref"] in destinations:
            route["to_ref"] = destinations[route["from_ref"]]
    after = publish(target)

    response = compare_schedules(http, before, after)
    assert response.status_code == 200, response.text
    changes = response.json()["result"]["changes"]
    assert [row["string_id"] for row in changes] == sorted(destinations)
    for row in changes:
        assert row["changed_fields"] == ["collector_id", "upstream_inverter_id"]
        assert row["after"]["collector_id"] == destinations[row["string_id"]]
        assert row["after"]["module_count"] == row["before"]["module_count"]
    assert {row["before"]["upstream_inverter_id"] for row in changes} == {
        row["after"]["upstream_inverter_id"] for row in changes}


def test_sip_r6_schedule_comparison_refuses_either_corrupt_source_or_wrong_binding(http):
    graph = files.schedules_cases.solaredge_layout(files.design_cases.graph.__wrapped__(), "direct")
    version = save_schedule_graph(http, graph)
    before = schedule_artifact(http, version)
    other = schedule_artifact(http, version, tool="solar-string-data")
    graph["strings"][0]["circuit_tag"] = "Different"
    after = schedule_artifact(http, save_schedule_graph(http, graph))
    refusal(compare_schedules(http, before, after, after_artifact_id=before["artifact_id"]),
            "SCHEDULE_COMPARISON_VERSION_MISMATCH", 409)
    refusal(compare_schedules(http, before, after, before_artifact_id=after["artifact_id"]),
            "SCHEDULE_COMPARISON_VERSION_MISMATCH", 409)
    refusal(compare_schedules(http, other, after), "SCHEDULE_COMPARISON_ARTIFACT_KIND", 409)
    refusal(compare_schedules(http, before, other), "SCHEDULE_COMPARISON_ARTIFACT_KIND", 409)
    refusal(compare_schedules(http, before, after, compare_version_id="invalid"), "SIP_R6_INVALID_REQUEST", 400)
    for reference in (before, after):
        blob_key = artifacts._key(reference, reference["content_sha256"], blob=True)
        intake_key = http.s.versions[UUID(reference["source_version_id"])].intake_ref
        for key, reason in ((blob_key, "ARTIFACT_CORRUPT"), (intake_key, "SIP_R1_INTAKE_DIGEST_MISMATCH")):
            original = http.s.blobs[key]
            http.s.blobs[key] = b"corrupt"
            response = compare_schedules(http, before, after)
            refusal(response, reason, 500)
            assert response.json()["result"] is None
            http.s.blobs[key] = original
    http.principal["member"] = False
    refusal(compare_schedules(http, before, after), "SIP_R1_PROJECT_FORBIDDEN", 403)


def test_sip_r6_request_validation(http):
    sentence = "Project Solar read request is invalid."
    writes = list(http.s.writes)
    for raw in (b"[]", b"null", b'"text"', b"{", b"{}", b'{"tool":1}',
                b'{"tool":"x","tool":"y","catalog_digest":"a"}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":1,"x":2}}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":NaN}}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":Infinity}}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":1e999}}'):
        refusal(http.client.post(read_path(http), content=raw), "SIP_R6_INVALID_REQUEST", 400, sentence)
    for change in ({"params": []}, {"params": None}, {"current": 1}, {"current": "true"},
                   {"tool": ""}, {"catalog_digest": ""}, {"catalog_digest": 1},
                   {"job_id": "supplied"}, {"checkout": "cap"}, {"source_version": 1},
                   {"params": {**reads.NEC[reads.CALC], "drawing_id": str(UUID(int=999))}}):
        # Each case is sent as written: body(**change) would read params=None as the defaults
        # and would hash a manifest for a tool named "" before any request is made.
        refusal(http.client.post(read_path(http), json={**body(), **change}),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
    refusal(http.client.post(read_path(http), content=b" " * 262145),
            "SIP_R6_REQUEST_TOO_LARGE", 413, "Project Solar read request is too large.")
    for identity in (http.s.project, http.s.drawing, http.s.head):
        refusal(http.client.post(read_path(http).replace(str(identity), "invalid"), json=body()),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
    for header in ("X-Org-Id", "X-Project-Id"):
        refusal(http.client.post(read_path(http), json=body(), headers={header: "invalid"}),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
        refusal(http.client.post(read_path(http), json=body(), headers={header: str(UUID(int=999))}),
                "SIP_R1_CONTEXT_NOT_FOUND", 404, "Project drawing operation unavailable.")
    assert http.s.writes == writes


def test_sip_r6_read_response_binding(http):
    response = post(http)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert set(result) == {"schema", "adapter", "organization_id", "project_id", "drawing_id",
        "input_version_id", "input_intake_sha256", "graph_sha256", "graph_project_id",
        "head_version_id", "is_head", "current_required", "tool", "tool_manifest_sha256",
        "request_sha256", "output", "output_sha256", "output_bytes", "drawing_changed"}
    assert result["schema"] == "leaf.solar-project-read.v1"
    assert result["adapter"] == "project-local-graph-read"
    for key, identity in (("organization_id", http.s.org), ("project_id", http.s.project),
                          ("drawing_id", http.s.drawing), ("input_version_id", http.s.head)):
        assert result[key] == str(identity)
    assert result["input_intake_sha256"] == http.s.ctx.intake_sha256
    assert result["graph_sha256"] == http.s.ctx.graph_sha256
    assert result["graph_project_id"] == http.s.ctx.graph_project_id != str(http.s.project)
    request = {key: result[key] for key in ("organization_id", "project_id", "drawing_id",
        "input_version_id", "input_intake_sha256", "graph_sha256", "tool", "tool_manifest_sha256")}
    request.update(schema="leaf.solar-project-read-request.v1", parameters=reads.NEC[reads.CALC])
    assert result["request_sha256"] == sha256(canonical_bytes(request)).hexdigest()
    assert result["output_sha256"] == sha256(canonical_bytes(result["output"])).hexdigest()
    assert result["output_bytes"] == len(canonical_bytes(result["output"]))
    assert result["is_head"] is True and result["current_required"] is False
    assert result["drawing_changed"] is False and "job_id" not in response.text
    for private in ("tenants/", "intake_ref", "checkout_capability", "verified-editor"):
        assert private not in response.text


def test_sip_r6_membership_and_foreign_scope(http, monkeypatch):
    prepared = files.save(http.s)
    for role in ("viewer", "editor"):
        http.principal["tenant"] = deps.TenantContext(str(http.s.org), org_id=str(http.s.org),
            tier="hosted_pro", subject=role)
        assert post(http).status_code == 200
        assert http.client.get(file_path(http, prepared.meta["artifact_id"])).content == prepared.content
    for tenant, member, status, reason in (
        (http.s.tenant, False, 403, "SIP_R1_PROJECT_FORBIDDEN"),
        (deps.TenantContext(str(UUID(int=999)), tier="hosted_pro", subject="foreign"),
         True, 404, "SIP_R1_CONTEXT_NOT_FOUND")):
        http.principal.update(tenant=tenant, member=member)
        before = list(http.s.reads)
        refusal(post(http), reason, status, "Project drawing operation unavailable.")
        refusal(http.client.get(file_path(http, prepared.meta["artifact_id"])), reason, status,
                "Project drawing operation unavailable.")
        assert http.s.reads == before
    http.principal.update(tenant=http.s.tenant, member=True)
    original = project.resolve_context
    calls = []
    def revoked(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            http.principal["member"] = False
        return original(*args, **kwargs)
    value = standalone.ArtifactOutput({}, "application/json", "Deferred.json", b"{}")
    before = list(http.s.writes)
    monkeypatch.setattr(project, "resolve_context", revoked)
    monkeypatch.setattr(reads.local._load_builtin(reads.CALC), "run", lambda *args: value)
    refusal(post(http), "SIP_R1_PROJECT_FORBIDDEN", 403)
    assert http.s.writes == before


def test_sip_r6_download_and_etag(http):
    files.string_source(http.s)
    generated = post(http, tool="solar-string-data", params={})
    assert generated.status_code == 200, generated.text
    ref = generated.json()["result"]["output"]["artifact"]
    path = ref["download"]
    response = http.client.get(path)
    assert response.status_code == 200 and len(response.content) == 973
    assert response.content == reads.local._load_builtin("solar-string-data").run(http.s.ctx.graph, {}).content
    assert response.headers["content-type"] == "application/json"
    assert response.headers["etag"] == '"' + ref["content_sha256"] + '"'
    assert response.headers["x-leaf-artifact-id"] == ref["artifact_id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="StringData.json"'
    assert "x-leaf-source-version" not in response.headers
    conditional = http.client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert conditional.status_code == 304 and conditional.content == b""
    assert conditional.headers["etag"] == response.headers["etag"]
    assert http.client.get(path, headers={"If-None-Match": '"other"'}).status_code == 200
    http.principal["member"] = False
    refusal(http.client.get(path, headers={"If-None-Match": response.headers["etag"]}),
            "SIP_R1_PROJECT_FORBIDDEN", 403)


def test_sip_r6_download_refusals(http, monkeypatch):
    assert routes._OUTPUT_FAILURES["IMPORT_STORE_UNAVAILABLE"] == (503, "Project source storage is unavailable.")
    assert routes._OUTPUT_FAILURES["ARTIFACT_STORE_UNAVAILABLE"] == (503, "Project artifact storage is unavailable.")
    assert routes._OUTPUT_FAILURES["IMPORT_WRITES_DRAINED"] == (503, "Project source creation is temporarily unavailable.")
    assert routes._OUTPUT_FAILURES["REPORT_WRITES_DRAINED"] == (503, "Project report creation is temporarily unavailable.")
    assert routes._OUTPUT_FAILURES["ARTIFACT_WRITES_DRAINED"] == (503, "Project artifact creation is temporarily unavailable.")
    prepared = files.save(http.s)
    path = file_path(http, prepared.meta["artifact_id"])
    for reason, (status, sentence) in routes._OUTPUT_FAILURES.items():
        with monkeypatch.context() as patch:
            def failure(*args, **kwargs):
                raise GraphValidationError(reason)
            patch.setattr(artifacts, "read_project_artifact", failure)
            error = refusal(http.client.get(path), reason, status, sentence)
            assert error["retryable"] is (reason in (
                "ARTIFACT_STORE_UNAVAILABLE", "ARTIFACT_WRITES_DRAINED",
                "IMPORT_STORE_UNAVAILABLE", "IMPORT_WRITES_DRAINED", "REPORT_WRITES_DRAINED",
                "REPORT_BUSY", "REPORT_PARSE_TIMEOUT", "REPORT_PARSE_UNAVAILABLE"))
    for reason, status, retry in (("CONTEXT_NOT_FOUND", 404, False), ("PROJECT_FORBIDDEN", 403, False),
                                ("INTAKE_DIGEST_MISMATCH", 500, False), ("STORE_UNAVAILABLE", 503, True)):
        with monkeypatch.context() as patch:
            patch.setattr(artifacts, "read_project_artifact", lambda *args, **kwargs: project.refuse(reason))
            assert refusal(http.client.get(path), "SIP_R1_" + reason, status,
                "Project drawing operation unavailable.")["retryable"] is retry
    refusal(http.client.get(file_path(http, "invalid")), "ARTIFACT_ID_INVALID", 400)
    refusal(http.client.get(file_path(http, "0" * 64)), "ARTIFACT_NOT_FOUND", 404)
    http.s.blobs[files.blob_key(prepared)] = b"tampered"
    refusal(http.client.get(path, headers={"If-None-Match": '"' + prepared.meta["content_sha256"] + '"'}),
            "ARTIFACT_CORRUPT", 500)
    http.s.blobs[files.blob_key(prepared)] = prepared.content
    fake = forged(http.s, prepared, source_intake_sha256="d" * 64)
    refusal(http.client.get(file_path(http, fake)), "SIP_R6_ARTIFACT_SOURCE_MISMATCH", 500)
    http.s.head = next(v.version_id for v in http.s.versions.values() if v.seq == 2)
    refusal(http.client.get(path + "?current=true"), "ARTIFACT_STALE", 409)
    assert http.client.get(path).status_code == 200
    original = project._access
    accesses = []
    def revoked(tenant, proj, *, write):
        accesses.append(True)
        if len(accesses) == 3:
            http.principal["member"] = False
        return original(tenant, proj, write=write)
    monkeypatch.setattr(project, "_access", revoked)
    refusal(http.client.get(path, headers={"If-None-Match": "*"}), "SIP_R1_PROJECT_FORBIDDEN", 403)


def test_sip_r6_entitlements_and_sanitization(http, monkeypatch):
    before = list(http.s.reads), list(http.s.writes)
    with monkeypatch.context() as patch:
        patch.setattr(entitlements, "entitlements_for", lambda *args: {"run_read": False})
        expected = entitlements.entitlement_denied_response("run_read", "hosted_pro")
        response = post(http)
        assert response.status_code == expected.status_code and response.content == expected.body
        assert (http.s.reads, http.s.writes) == before
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise entitlements.EntitlementsError("private policy detail")
        patch.setattr(entitlements, "entitlements_for", unavailable)
        expected = entitlements.policy_unavailable_response("run_read", "hosted_pro")
        response = post(http)
        assert response.status_code == expected.status_code and response.content == expected.body
        assert (http.s.reads, http.s.writes) == before
    denial = JSONResponse(content={"policy": "stored refusal"}, status_code=403)
    with monkeypatch.context() as patch:
        seen = []
        patch.setattr(read.stored, "stored_job_entitlement_verdict", lambda org, kind:
            (seen.append((org, kind)) or denial, None))
        patch.setattr(reads.local, "_read_output", reads.blocked)
        response = post(http)
        assert response.status_code == 403 and response.content == denial.body
        assert seen == [(http.s.org, "extract")] and http.s.writes == before[1]
    with monkeypatch.context() as patch:
        def unexpected(*args, **kwargs):
            raise RuntimeError("private key SQL token detail")
        patch.setattr(read, "run_project_read", unexpected)
        refusal(post(http), "SIP_R6_INTERNAL", 500, "Project Solar output is unavailable.")
        assert "private" not in post(http).text
    for reason in routes._INVALID_ARTIFACT:
        response = routes._output_failure(reason)
        assert response.status_code == 500
        assert json.loads(response.body)["error"]["message"] == "The Solar read produced an invalid artifact."


def _r1_specimens(http, monkeypatch):
    """R1's five neighbouring responses under this file's fixture, once per flag state."""
    s = http.s
    monkeypatch.setattr(project, "checkout_status", lambda *args: SimpleNamespace(
        organization_id=s.org, checkout=None, observed_at=s.lease.acquired_at))
    monkeypatch.setattr(project, "acquire_project_checkout", lambda *args, **kwargs: (s.lease, "cap"))
    monkeypatch.setattr(project, "release_project_checkout", lambda *args, **kwargs: True)
    monkeypatch.setattr(drawings, "_backend", lambda tenant: s)
    legacy = standalone.ArtifactSink(s, str(s.org), str(s.drawing), {
        "project_id": s.ctx.graph_project_id, "resolved_version": 1,
        "graph_sha256": s.ctx.graph_sha256}, reads.CALC, "c" * 64, False)
    prepared = legacy.prepare(standalone.ArtifactOutput({}, "application/json", "Legacy.json", b"{}"))
    legacy.finish(prepared)
    context = f"/api/projects/{s.project}/drawing-versions/{s.head}/context?drawing_id={s.drawing}"
    checkout = f"/api/projects/{s.project}/drawings/{s.drawing}/checkout"
    standalone_path = f"/api/drawings/{s.drawing}/artifacts/{prepared.meta['artifact_id']}"
    specimens = []
    for enabled in (False, True):
        if enabled:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
        else:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        responses = [http.client.request(method, path) for method, path in (
            ("GET", context), ("GET", checkout), ("POST", checkout), ("DELETE", checkout),
            ("GET", standalone_path))]
        # X07 explicitly extends the context contract. Validate that extension
        # against fixture intake, then keep the frozen digest over every legacy
        # field and header rather than replacing it with a new opaque digest.
        payload = responses[0].json()
        assert payload.pop('role_discovery') == discover_roles(s.ctx.intake)
        responses[0]._content = JSONResponse(payload).body
        responses[0].headers['content-length'] = str(len(responses[0].content))
        specimens.append([specimen(response) for response in responses])
    return specimens


def test_sip_r6_existing_responses_unchanged(http, monkeypatch):
    specimens = _r1_specimens(http, monkeypatch)
    assert specimens[0] == specimens[1]
    assert [row[0] for row in specimens[0]] == [200] * 5
    with monkeypatch.context() as patch:
        patch.setattr(deps, "find_tool", _FIND_TOOL)
        patch.setattr(deps, "load_engine_registry_tools", _ENGINE_TOOLS)
        patch.setattr(project, "_access", _ACCESS)
        def stable_read(*args, **kwargs):
            result = _DYNAMIC_READ(*args, **kwargs)
            result["timing_ms"] = 0
            return result
        patch.setattr(generic_cases.tool_loader, "run_tool_dynamic", stable_read)
        memory = generic_cases.memory.__wrapped__(patch)
        generator = generic_cases.lane.__wrapped__(memory, patch)
        actual = next(generator)
        try:
            for enabled in (False, True):
                if enabled:
                    patch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
                else:
                    patch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
                before = generic_cases.request(actual)
                generic_cases.success(actual, before)
                actual.client.app.include_router(routes.router)
                after = generic_cases.request(actual)
                generic_cases.success(actual, after)
                assert specimen(before) == specimen(after)
        finally:
            with pytest.raises(StopIteration):
                next(generator)
    with monkeypatch.context() as patch:
        generator = admission_cases.lane.__wrapped__(patch)
        actual = next(generator)
        try:
            invalid = {"expected_rev": 0, "surprise": True}
            for enabled in (False, True):
                if enabled:
                    patch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
                else:
                    patch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
                before = [specimen(admission_cases.run(actual, invalid)),
                          specimen(admission_cases.preview(actual, invalid)),
                          specimen(actual.client.get("/api/jobs/unknown"))]
                actual.client.app.include_router(routes.router)
                after = [specimen(admission_cases.run(actual, invalid)),
                         specimen(admission_cases.preview(actual, invalid)),
                         specimen(actual.client.get("/api/jobs/unknown"))]
                assert before == after
        finally:
            with pytest.raises(StopIteration):
                next(generator)


# R1's five neighbouring responses (the project context GET, checkout GET, POST and DELETE, and the
# standalone artifact GET) as measured on main 9b7366cc, the parent of this change, with this file's
# fixture: status, the sha256 of the body and the stable headers. Both flag states must return exactly
# these. The row above compares the two flag states with each other, so a change that alters R1's
# responses in both states at once passes it; this row compares them with main.
_R1_JSON_HEADERS = {"cache-control": "no-store", "content-type": "application/json"}
_R1_ON_MAIN = [
    # project context GET
    (200, "94f85a53f91439091f37640396e96b17be525ce37041590ad4dd2ea01c564fd0",
     _R1_JSON_HEADERS),
    # checkout GET
    (200, "f8b011d6fe81ff3dd7cf998b0a35903a97cd69a16d65857fed7ff73dbbec23d4",
     _R1_JSON_HEADERS),
    # checkout POST
    (200, "f7d0ce9dd4fba070fa6a52bee1b4344d5bd1f0496c92b7da27fa5e5c8992bc13",
     _R1_JSON_HEADERS),
    # checkout DELETE
    (200, "4f134f844e172589b75b6bcf037d76363ac7534f318631ffc8ec9134213c8fc8",
     _R1_JSON_HEADERS),
    # standalone artifact GET
    (200, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
     {
        "cache-control": "private, no-cache",
        "content-disposition": "attachment; filename=\"Legacy.json\"",
        "content-type": "application/json",
        "etag": "\"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a\"",
        "x-content-type-options": "nosniff",
        "x-leaf-artifact-id": "6c2072cec098e1982c7e5b0d5d56e30578a2cb088c5234c35c0b29e4e215b430",
        "x-leaf-source-version": "1"}),
]


def test_sip_r6_existing_responses_match_main(http, monkeypatch):
    for state in _r1_specimens(http, monkeypatch):
        assert [(status, sha256(content).hexdigest(), headers)
                for status, content, headers in state] == _R1_ON_MAIN


_UNSET = object()


def test_sip_r6_parameters_refuse_directly():
    """The service validates its own inputs, whatever the route checked before it."""
    project_id, drawing_id, version_id = UUID(int=1), UUID(int=2), UUID(int=3)

    def call(project_id=project_id, drawing_id=drawing_id, version_id=version_id, tool=reads.CALC,
             params=_UNSET, digest="d" * 64, current=False):
        return read._parameters(project_id, drawing_id, version_id, tool,
                                {"value": 1} if params is _UNSET else params, digest, current)

    assert call() == {"value": 1}
    assert call(params={"value": 1, "drawing_id": str(drawing_id)}) == {"value": 1}
    assert call(current=True) == {"value": 1}
    for case in (dict(tool=""), dict(tool=1), dict(tool=None),
                 dict(params=[]), dict(params=None), dict(params="value"), dict(params=1),
                 dict(digest=""), dict(digest=1), dict(digest=None),
                 dict(current=1), dict(current=0), dict(current="true"), dict(current=None),
                 dict(project_id=str(project_id)), dict(drawing_id=str(drawing_id)),
                 dict(version_id=str(version_id)), dict(version_id=3),
                 dict(params={"value": 1, "drawing_id": str(UUID(int=9))}),
                 dict(params={"value": 1, "drawing_id": 2})):
        with pytest.raises(GraphValidationError) as raised:
            call(**case)
        assert raised.value.code == "SIP_R6_INVALID_REQUEST", case


def test_sip_r6_route_refuses_before_service(http, monkeypatch):
    """A malformed body is refused by the route itself: the service is never called."""
    calls = []
    real = read.run_project_read

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(read, "run_project_read", spy)
    sentence = "Project Solar read request is invalid."
    for change in ({"params": []}, {"params": None}, {"params": "value"}, {"tool": ""}, {"tool": 1},
                   {"catalog_digest": ""}, {"catalog_digest": 1}, {"current": 1},
                   {"current": "true"}):
        refusal(http.client.post(read_path(http), json={**body(), **change}),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
    assert calls == []
    response = post(http)
    assert response.status_code == 200 and len(calls) == 1
    args, kwargs = calls[0]
    assert args[1:] == (UUID(str(http.s.project)), UUID(str(http.s.drawing)), UUID(str(http.s.head)),
                        reads.CALC, reads.NEC[reads.CALC])
    assert kwargs == {"catalog_digest": body()["catalog_digest"], "current": False}


# R6 follow-up rows (#345): the three coverage gaps R6's last read named, plus the full-header
# comparison of R1's neighbouring responses. Each fails under the mutation named in its comment.


def test_sip_r6f_all_headers(http, monkeypatch):
    # Every response header, duplicates included, not only the stable subset `specimen` keeps:
    # a route that adds any header to R1's neighbouring responses fails here.
    monkeypatch.setitem(globals(), "specimen", lambda r:
        (r.status_code, r.content, sorted(r.headers.multi_items())))
    for state in _r1_specimens(http, monkeypatch):
        for (status, content, headers), (want, digest, pinned) in zip(state, _R1_ON_MAIN):
            assert (status, sha256(content).hexdigest(), headers) == (
                want, digest, sorted({**pinned, "content-length": str(len(content))}.items()))


def test_sip_r6f_integer_bound():
    # An integer past 64 bits is refused by the read's own JSON bound, before any graph is read.
    with pytest.raises(GraphValidationError) as exc:
        read._parameters(UUID(int=1), UUID(int=2), UUID(int=3),
                         reads.CALC, {"value": 18446744073709551616}, "d" * 64, False)
    assert exc.value.code == "SIP_R6_INVALID_REQUEST"


def test_sip_r6f_drawing_type(monkeypatch):
    # The drawing id must be an exact str. The JSON bound would refuse a subclass first, so it is
    # switched off here to isolate the type clause.
    class Text(str):
        pass
    monkeypatch.setattr(read, "_bounded_json", lambda value: None)
    with pytest.raises(GraphValidationError) as exc:
        read._parameters(UUID(int=1), UUID(int=2), UUID(int=3),
                         reads.CALC, {"drawing_id": Text(str(UUID(int=2)))}, "d" * 64, False)
    assert exc.value.code == "SIP_R6_INVALID_REQUEST"


def test_sip_r6f_float_before_service(http, monkeypatch):
    # A non-finite float in the request body is refused before the read service is called; an
    # ordinary request through the same spy then reaches it once.
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    calls = []
    real = read.run_project_read
    def spy(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(read, "run_project_read", spy)
    response = http.client.post(read_path(http), content=
        b'{"tool":"solar-nec-ampacity-correction","catalog_digest":"d","params":{"value":1e999}}')
    refusal(response, "SIP_R6_INVALID_REQUEST", 400)
    assert calls == []
    assert post(http).status_code == 200
    assert calls == [True]

@pytest.mark.parametrize("requested,expected", [
    ("bytes=2-8", slice(2, 9)),
    ("bytes=3-", slice(3, None)),
    ("bytes=-5", slice(-5, None)),
])
def test_project_artifact_byte_range(http, requested, expected):
    saved = files.save(http.s)
    path = file_path(http, saved.meta["artifact_id"])
    etag = '"' + saved.meta["content_sha256"] + '"'
    response = http.client.get(path, headers={"Range": requested, "If-None-Match": '"other"'})
    start, stop, _ = expected.indices(len(saved.content))
    assert response.status_code == 206
    assert response.content == saved.content[expected]
    assert response.headers["content-range"] == f"bytes {start}-{stop - 1}/{len(saved.content)}"
    assert response.headers["content-length"] == str(stop - start)
    assert response.headers["etag"] == etag
    assert response.headers["accept-ranges"] == "bytes"
    cached = http.client.get(path, headers={"Range": requested, "If-None-Match": etag})
    assert cached.status_code == 304 and not cached.content
    full = http.client.get(path, headers={"Range": requested, "If-Range": '"old"'})
    assert full.status_code == 200 and full.content == saved.content


def test_project_artifact_range_limits(http):
    saved = files.save(http.s)
    path = file_path(http, saved.meta["artifact_id"])
    size = len(saved.content)
    for requested in (f"bytes={size}-", "bytes=-0"):
        response = http.client.get(path, headers={"Range": requested})
        assert response.status_code == 416 and not response.content
        assert response.headers["content-range"] == f"bytes */{size}"
    for requested in ("bytes=0-999999", "bytes=-999999"):
        response = http.client.get(path, headers={"Range": requested})
        assert response.status_code == 206 and response.content == saved.content
    for requested in ("bytes=7-1", "items=0-1", "bytes=0-1,3-4"):
        response = http.client.get(path, headers={"Range": requested})
        assert response.status_code == 200 and response.content == saved.content
    http.principal["member"] = False
    denied = http.client.get(path, headers={"Range": "bytes=0-1", "If-None-Match": "*"})
    refusal(denied, "SIP_R1_PROJECT_FORBIDDEN", 403)
    assert "content-range" not in denied.headers and "etag" not in denied.headers


def _engineering_save(lane, intake):
    lane.parent = deepcopy(lane.parent)
    lane.parent.version_id = uuid4()
    lane.head = lane.parent.version_id
    lane.set_parent(intake)
    return lane.head


def _engineering_save_graph(lane, graph):
    return _engineering_save(lane, embedded(validate_graph(graph)))


def _engineering_request(lane, before, after, headers=None):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: lane.tenant
    with TestClient(app) as client:
        return client.get(
            f"/api/projects/{lane.project}/drawings/{lane.drawing}/versions/{before}/solar-engineering-comparison",
            params={"compare_version_id": str(after)},
            headers=headers,
        )


def _engineering_compare(lane, before, after):
    writes = list(lane.writes)
    head = lane.head
    response = _engineering_request(lane, before, after)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert lane.writes == writes
    assert lane.head == head
    assert result["before"]["input_version_id"] == str(before)
    assert result["after"]["input_version_id"] == str(after)
    return result, {entry["metric"]: entry for entry in result["metrics"]}


def _engineering_amounts(entry):
    return entry["before"], entry["after"], entry["delta"]


def test_exact_roof_versions_preserve_fractional_power_and_signed_delta(engineering_lane, graph):
    lane = engineering_lane
    graph = topology_of(graph)
    graph["frames"][0]["module_power_watts"] = 400.125
    before = _engineering_save_graph(lane, graph)
    graph["frames"][0]["module_power_watts"] = 500.25
    after = _engineering_save_graph(lane, graph)

    result, totals = _engineering_compare(lane, before, after)
    assert result["before"]["graph_sha256"] != result["after"]["graph_sha256"]
    assert result["before"]["intake_sha256"] != result["after"]["intake_sha256"]
    assert list(totals) == ["panels", "optimizers", "inverters", "strings", "kw_dc"]
    assert _engineering_amounts(totals["kw_dc"]) == ("1.200375", "1.50075", "0.300375")
    assert _engineering_amounts(totals["panels"]) == ("3", "3", "0")
    assert _engineering_amounts(totals["strings"]) == ("2", "2", "0")
    assert _engineering_amounts(totals["inverters"]) == ("1", "1", "0")
    assert _engineering_amounts(totals["optimizers"]) == (None, None, None)
    _, backwards = _engineering_compare(lane, after, before)
    assert backwards["kw_dc"]["delta"] == "-0.300375"
    _, same = _engineering_compare(lane, after, after)
    assert same["kw_dc"]["delta"] == "0"


def test_counts_compact_ground_modules_without_counting_an_absent_device(engineering_lane, graph, b18):
    lane = engineering_lane
    before = _engineering_save_graph(lane, ground_graph(graph, b18, 1))
    after = _engineering_save_graph(lane, ground_graph(graph, b18, 2))
    _, totals = _engineering_compare(lane, before, after)
    assert _engineering_amounts(totals["panels"]) == ("294", "588", "294")
    assert _engineering_amounts(totals["kw_dc"]) == ("117.6", "235.2", "117.6")
    assert _engineering_amounts(totals["inverters"]) == ("0", "0", "0")
    assert _engineering_amounts(totals["strings"]) == ("0", "0", "0")


def test_missing_graph_and_legacy_equipment_keep_each_metric_unavailable(engineering_lane, graph):
    lane = engineering_lane
    before = _engineering_save(lane, deepcopy(INTAKE))
    graph["frames"][0]["module_power_watts"] = 0
    after = _engineering_save_graph(lane, graph)
    result, totals = _engineering_compare(lane, before, after)
    assert result["before"]["graph_sha256"] is None
    assert all(entry["before_reason"] == "saved_graph_unavailable" for entry in totals.values())
    assert _engineering_amounts(totals["panels"]) == (None, "3", None)
    assert totals["inverters"]["after_reason"] == "saved_equipment_type_unavailable"
    assert totals["kw_dc"]["after_reason"] == "complete_module_power_unavailable"


@pytest.mark.parametrize("complete", [True, False])
def test_uses_only_revision_bound_measured_optimizers(tracking_lane, monkeypatch, complete):
    lane = tracking_lane
    matrices = deepcopy(samples.GRIDS)
    marker = 0
    for matrix in matrices:
        matrix["Optimizers"] = []
        for row, record in enumerate(matrix["Rows"]):
            for column, cell in enumerate(record["Panels"]):
                if cell["Code"] == 1:
                    marker += 1
                    matrix["Optimizers"].append({
                        "id": f"pdf-optimizer-{marker}", "vendor_label": str(marker),
                        "cells": [[row, column]],
                    })
    if not complete:
        matrices[-1]["Optimizers"].pop()
    monkeypatch.setattr("solar_project_solaredge.reports.parse_source", lambda data: deepcopy(matrices))
    _, _, accepted = accept(lane, report(lane))
    tracking = solaredge.read_tracking(lane.tenant, accepted, {"section": "frames"})
    assert tracking["accepted"] is True
    assert tracking["optimizer_measurement"]["complete"] is complete

    _, totals = _engineering_compare(lane, lane.ctx.parent_version_id, accepted.parent_version_id)
    metric = totals["optimizers"]
    assert _engineering_amounts(metric) == (None, "6" if complete else None, None)
    assert metric["before_reason"] == "verified_optimizer_measurement_unavailable"
    assert metric["after_reason"] == (None if complete else "verified_optimizer_measurement_unavailable")
    _, reverse_totals = _engineering_compare(lane, accepted.parent_version_id, lane.ctx.parent_version_id)
    assert reverse_totals["optimizers"]["after_reason"] == "verified_optimizer_measurement_unavailable"


def test_matched_only_partial_acceptance_cannot_supply_after_optimizer_total(partial_tracking_lane):
    lane = partial_tracking_lane
    lane.matrices[0]["Optimizers"] = [
        {"id": f"pdf-optimizer-{index + 1}", "vendor_label": str(index + 1), "cells": [[0, index]]}
        for index in range(2)
    ]
    result = partial_tracking.report(lane)
    prepared = partial_tracking.tools.prepare(
        lane, solaredge.ACCEPT_TOOL,
        partial_tracking.params(lane, result, accept_matched_only=True), lane.ctx,
    )
    accepted = partial_tracking.tools.output(lane, partial_tracking.tools.publish(lane, prepared))
    tracking = solaredge.read_tracking(lane.tenant, accepted, {"section": "frames"})
    assert tracking["accepted"] is True
    assert tracking["acceptance"]["status"] == "partial"
    assert tracking["optimizer_measurement"]["complete"] is False

    _, totals = _engineering_compare(lane, lane.ctx.parent_version_id, accepted.parent_version_id)
    assert _engineering_amounts(totals["optimizers"]) == (None, None, None)
    assert totals["optimizers"]["after_reason"] == "verified_optimizer_measurement_unavailable"


@pytest.mark.parametrize("missing_side", ["before", "after"])
def test_missing_version_refuses_whole_comparison(engineering_lane, missing_side):
    lane = engineering_lane
    versions = dict.fromkeys(("before", "after"), lane.head)
    versions[missing_side] = uuid4()
    response = _engineering_request(lane, versions["before"], versions["after"])
    assert response.status_code == 404
    assert response.json()["error"]["reason_code"] == "SIP_R1_CONTEXT_NOT_FOUND"


@pytest.mark.parametrize("corrupt_side", ["before", "after"])
def test_both_versions_require_verified_intake_bytes(engineering_lane, graph, corrupt_side):
    lane = engineering_lane
    versions = {side: _engineering_save_graph(lane, graph) for side in ("before", "after")}
    key = lane.versions[versions[corrupt_side]].intake_ref
    lane.blobs[key] = b"corrupt"
    response = _engineering_request(lane, versions["before"], versions["after"])
    assert response.status_code == 500
    assert response.json()["error"]["reason_code"] == "SIP_R1_INTAKE_DIGEST_MISMATCH"
    assert response.json()["result"] is None


def test_engineering_comparison_refuses_oversized_serialized_result(engineering_lane, monkeypatch):
    lane = engineering_lane
    response = _engineering_request(lane, lane.head, lane.head)
    assert response.status_code == 200

    monkeypatch.setattr(routes.output_reads, "MAX_OUTPUT_BYTES", len(response.content))
    at_limit = _engineering_request(lane, lane.head, lane.head)
    assert at_limit.status_code == 200
    monkeypatch.setattr(routes.output_reads, "MAX_OUTPUT_BYTES", len(response.content) - 1)
    refused = _engineering_request(lane, lane.head, lane.head)
    assert refused.status_code == 500
    assert refused.headers["cache-control"] == "no-store"
    assert refused.json()["error"]["reason_code"] == "READ_OUTPUT_LIMIT_EXCEEDED"
    assert refused.json()["result"] is None


def test_comparison_validates_baseline_scope_and_feature_flag(engineering_lane, monkeypatch):
    lane = engineering_lane
    invalid = _engineering_request(lane, lane.head, "invalid")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["reason_code"] == "SIP_R6_INVALID_REQUEST"
    wrong_org = _engineering_request(lane, lane.head, lane.head, {"X-Org-Id": str(uuid4())})
    assert wrong_org.status_code == 404
    assert wrong_org.json()["error"]["reason_code"] == "SIP_R1_CONTEXT_NOT_FOUND"
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0")
    disabled = _engineering_request(lane, lane.head, lane.head)
    assert disabled.status_code == 409
    assert disabled.json()["error"]["reason_code"] == "project_execution_disabled"
