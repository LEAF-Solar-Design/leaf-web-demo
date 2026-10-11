"""Canonical conductors: deterministic bytes, selective invalidation and stored proof."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from uuid import UUID

import pytest

import solar_design_graph as design
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
import solar_project_jobs as service
import solar_tools
from leaf_platform import canonical_jobs as jobs
from test_sip_r3a_graph import publish, proof, refused
from test_sip_r3b_tools import memory, prepare, commit, output, manifest, forbidden  # noqa: F401
from test_sip_r3b_chain import ELEVEN, created, forge, UNKNOWN
import test_sip_r3c_multi_add as multi
import test_sip_r4_jobs as r4
import test_sip_r5_policy as policy_cases
import test_solar_tool_solar_string_conductors as conductors
from test_w1_design_graph import graph  # noqa: F401

TOOL = "solar-string-conductors"
F_SHA = "08096a8fd7530c6e8c4e1753327c7acf1a15b1a5c6aa2dcaeec93ce22f460e72"
D_SHA = "43dff22f69583bca5b45105af026ec3dfe644d596613ad3b81f5ae1a5ef92735"
AFTER_SHA = "f873287a846fd393752846700077e99c69fa6e24c624ff0934f0bac5b9ee44f0"
INTAKE_SHA = "5ae0e73895b442be1fb7b73026445fd0893d256324fb2da3c427978c2e8cfc29"


def dependencies(g):
    result = deepcopy(g)
    conductors.add_route_dependencies(result)
    return result


def intake(g):
    return {"custom": {"keep": [1, 2]}, "solar_design_graph": deepcopy(g),
            "solar_design_graph_sha256": local.digest(g)}


def context(g):
    raw = local.canonical_bytes(intake(g))
    return adapter.ProjectGraphContext(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4),
        raw, sha256(raw).hexdigest(), "2026-10-01T00:00:00Z")


def parent(s, g):
    s.set_parent(intake(g))
    return adapter.resolve_project_graph_context(s.context())


def parameters(g, gauge="8 AWG", *, all_strings=False):
    return {"operation": "set-conductors", "expected_rev": g["rev"],
        "assignments": [{"string_ref": string["id"], "wire_gauge": gauge}
                        for string in (g["strings"] if all_strings else g["strings"][:1])]}


def candidate(g, params=None):
    return adapter._candidate(context(g), TOOL, parameters(g) if params is None else params,
                              False, UUID(int=9))


def identities(g):
    return [(entity["id"], entity["provenance"]["created_at"]) for entity in design.entities(g)]


def test_sip_r3d_membership(memory):
    assert adapter.SUPPORTED_TOOLS == ELEVEN and len(ELEVEN) == 11
    assert adapter.CREATION_TOOLS == {
        "solar-string-add", "solar-assign-equipment", "solar-string-multi-add"}
    assert TOOL not in adapter.CREATION_TOOLS
    assert adapter._tool(TOOL, manifest(TOOL)) == solar_tools.get(TOOL)
    refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: adapter._tool(TOOL, "x"))
    for tool in multi.UNSUPPORTED:
        refused("SIP_R3_TOOL_UNSUPPORTED", lambda: prepare(memory, tool, {}))


def test_sip_r3d_preparation(graph, memory, monkeypatch):
    before = deepcopy(graph)
    old, after, base, raw = candidate(graph)
    assert graph == before and old == before and base is None
    assert local.digest(after) == F_SHA
    assert (after["rev"], after["parent_rev"]) == (1, 0)
    assert [s["wire_gauge"] for s in after["strings"]] == ["8 AWG", "10 AWG"]
    assert after["strings"][1] == graph["strings"][1]
    assert identities(after) == identities(graph)
    assert json.loads(raw)["custom"] == {"keep": [1, 2]}
    ctx = parent(memory, graph)
    module = local._load_builtin(TOOL)
    original, seen = module.run, []
    def run(g, params, **trusted):
        seen.append(trusted)
        return original(g, params)
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(adapter, "_creation", forbidden)
    p = prepare(memory, TOOL, parameters(graph), ctx)
    assert created(p) == after and seen == [{}]


def test_sip_r3d_selective_invalidation(graph):
    d = dependencies(graph)
    before = deepcopy(d)
    assert local.digest(d) == D_SHA
    _, after, _, raw = candidate(d)
    assert d == before and graph["rev"] == 0
    assert local.digest(after) == AFTER_SHA and sha256(raw).hexdigest() == INTAKE_SHA
    assert [r["validity"]["state"] for r in after["routes"]] == ["stale", "valid", "stale"]
    assert [s["validity"]["state"] for s in after["schedules"]] == [
        "stale", "valid", "stale", "stale", "valid"]
    for collection, changed in (("routes", (0, 2)), ("schedules", (0, 2, 3))):
        for index, (old, new) in enumerate(zip(d[collection], after[collection])):
            if index in changed:
                assert new["validity"] == {"state": "stale", "reasons": ["conductor_changed"]}
                assert new["rev"] == after["rev"]
            else:
                assert new == old
            if collection == "routes":
                assert new["wire_gauge"] == old["wire_gauge"]
                assert new["points"] == old["points"]
    for key in ("settings", "inverters", "panels", "frames", "electrical_zones"):
        assert after[key] == d[key]
    assert after["strings"][1] == d["strings"][1]
    assert after["strings"][0]["provenance"]["last_writer"] == TOOL
    assert identities(after) == identities(d)


def test_sip_r3d_same_gauge(graph):
    for g in (deepcopy(graph), dependencies(graph)):
        before = deepcopy(g)
        _, after, _, _ = candidate(g, parameters(g, "10 AWG", all_strings=True))
        assert g == before and after["rev"] == before["rev"] + 1
        assert after["parent_rev"] == before["rev"]
        for key in ("routes", "schedules", "inverters", "settings", "frames"):
            assert after[key] == before[key]
        for old, new in zip(before["strings"], after["strings"]):
            assert new["wire_gauge"] == old["wire_gauge"]
            assert new["validity"] == old["validity"]
            assert new["rev"] == (after["rev"] if new != old else old["rev"])
        assert identities(after) == identities(before)


def test_sip_r3d_normalization_and_identity(memory, graph):
    ctx = parent(memory, dependencies(graph))
    params = parameters(ctx.graph)
    before = deepcopy((ctx.intake, params, memory.versions, memory.blobs))
    p = prepare(memory, TOOL, params, ctx)
    assert prepare(memory, TOOL, params, ctx) == p
    assert prepare(memory, TOOL, {**params, "drawing_id": str(memory.drawing)}, ctx) == p
    assert p.request["parameters"] == params and "drawing_id" not in p.request["parameters"]
    for drawing in (str(UUID(int=77)), "dessin-é"):
        refused("DRAWING_ID_CONFLICT", lambda: prepare(
            memory, TOOL, {**params, "drawing_id": drawing}, ctx))
    for changes in ({"job_id": UUID(int=99)}, {"attempt": 2},
            {"checkout": replace(memory.lease, fence=8)},
            {"checkout": replace(memory.lease, holder_binding_id=UUID(int=98))}):
        other = prepare(memory, TOOL, params, ctx, **changes)
        assert other.output_intake_bytes == p.output_intake_bytes
        assert other.request_bytes != p.request_bytes
    both = parameters(ctx.graph, all_strings=True)
    reverse = {**both, "assignments": list(reversed(both["assignments"]))}
    ordered, reversed_p = [prepare(memory, TOOL, request, ctx) for request in (both, reverse)]
    assert ordered.output_intake_bytes == reversed_p.output_intake_bytes
    assert ordered.request["parameters"]["assignments"] == both["assignments"]
    assert reversed_p.request["parameters"]["assignments"] == reverse["assignments"]
    assert ordered.request_id != reversed_p.request_id
    assert (ctx.intake, params, memory.versions, memory.blobs) == before


def test_sip_r3d_request_refusals(memory, graph):
    ctx = parent(memory, graph)
    p = parameters(graph)
    first = p["assignments"][0]
    invalid = [
        ({**p, "expected_rev": 1}, "STALE_GRAPH_REVISION"),
        ({**p, "assignments": [first, first]}, "DUPLICATE_STRING_ASSIGNMENT"),
        ({**p, "assignments": [first, {"string_ref": "missing", "wire_gauge": "8 AWG"}]}, "MISSING_STRING"),
        ({**p, "assignments": [{"string_ref": "不存在", "wire_gauge": "8 AWG"}]}, "MISSING_STRING"),
        ({**p, "assignments": [{**first, "wire_gauge": "9 AWG"}]}, "INVALID_WIRE_GAUGE"),
        ({**p, "assignments": []}, "INVALID_CONDUCTOR_REQUEST"),
        ({**p, "assignments": [first] * 4097}, "INVALID_CONDUCTOR_REQUEST"),
        ({**p, "assignments": [{**first, "extra": True}]}, "INVALID_CONDUCTOR_REQUEST"),
        ({**p, "operation": "other"}, "INVALID_CONDUCTOR_REQUEST"),
        ({**p, "expected_rev": True}, "INVALID_CONDUCTOR_REQUEST"),
    ]
    missing = {"string_ref": "missing", "wire_gauge": "10 AWG"}
    for second, code in (({**missing, "extra": True}, "INVALID_CONDUCTOR_REQUEST"),
                         ({**missing, "wire_gauge": "9 AWG"}, "INVALID_WIRE_GAUGE"),
                         (missing, "DUPLICATE_STRING_ASSIGNMENT")):
        invalid.append(({**p, "assignments": [missing, second]}, code))
    before = deepcopy((graph, p, memory.versions, memory.blobs))
    module = local._load_builtin(TOOL)
    for params, code in invalid:
        original = deepcopy(params)
        refused(code, lambda: module.run(graph, params))
        refused(code, lambda: prepare(memory, TOOL, params, ctx))
        assert params == original
    cancel = {**p, "cancel": True}
    assert module.run(graph, cancel) == graph
    refused("GRAPH_COMMIT_CANCELLED", lambda: prepare(memory, TOOL, cancel, ctx))
    assert (graph, p, memory.versions, memory.blobs) == before


def test_sip_r3d_graph_refusals(memory, graph):
    params = parameters(graph)
    for source, code in (({"custom": {}}, "GRAPH_NOT_EMBEDDED"),
            ({"solar_design_graph": graph}, "INVALID_SEED_PARENT"),
            ({**intake(graph), "solar_design_graph_sha256": "0" * 64}, "GRAPH_DIGEST_MISMATCH")):
        memory.set_parent(source)
        refused(code, lambda: prepare(memory, TOOL, params))
    unresolved = deepcopy(graph)
    unresolved["project"]["units"]["meters_per_unit"] = 1
    before = deepcopy(unresolved)
    refused("UNRESOLVED_UNITS", lambda: candidate(unresolved))
    assert unresolved == before
    for state in ("invalid", "stale"):
        g = deepcopy(graph)
        g["strings"][0]["validity"] = {"state": state, "reasons": ["settings_changed"]}
        _, after, _, _ = candidate(g)
        assert after["strings"][0]["validity"] == g["strings"][0]["validity"]


def test_sip_r3d_admission(monkeypatch, graph):
    a = policy_cases.setup(monkeypatch)
    ctx = parent(a.s, graph)
    r4.at(a, monkeypatch, ctx, TOOL)
    params = parameters(graph)
    before = deepcopy((a.s.versions, a.s.blobs, a.s.writes, a.accepted))
    accepted = policy_cases.request(a, {**params, "drawing_id": str(a.s.drawing)}, tool_name=TOOL)
    assert accepted["status_code"] == 200
    module, tool = policy_cases.policy(), solar_tools.trusted_record(TOOL)
    unchecked = module.project_catalog_availability(tool, enabled=True)
    assert unchecked["refusal_reasons"] == ["project_request_required"]
    assert not unchecked["admission_checked"] and unchecked["admission"] is None
    ready = module.project_catalog_availability(tool, enabled=True, admission=accepted)
    assert ready["runnable"] and ready["admission"] == {"status_code": 200, "error": None}
    assert (a.s.versions, a.s.blobs, a.s.writes, a.accepted) == before
    for changes, code in (
            ({"expected_rev": 1}, "STALE_GRAPH_REVISION"),
            ({"assignments": params["assignments"] * 2}, "DUPLICATE_STRING_ASSIGNMENT"),
            ({"assignments": [{"string_ref": "不存在", "wire_gauge": "8 AWG"}]}, "MISSING_STRING"),
            ({"assignments": [{**params["assignments"][0], "wire_gauge": "9 AWG"}]}, "INVALID_SETTINGS_REQUEST"),
            ({"assignments": []}, "INVALID_SETTINGS_REQUEST"),
            ({"assignments": params["assignments"] * 4097}, "INVALID_SETTINGS_REQUEST"),
            ({"assignments": [{**params["assignments"][0], "extra": True}]}, "INVALID_SETTINGS_REQUEST"),
            ({"operation": "other"}, "INVALID_SETTINGS_REQUEST"),
            ({"expected_rev": True}, "INVALID_SETTINGS_REQUEST"),
            ({"cancel": True}, "INVALID_SETTINGS_REQUEST"),
            ({"initialize": {}}, "INVALID_SEED_REQUEST")):
        result = policy_cases.request(a, {**params, **changes}, tool_name=TOOL)
        policy_cases.assert_refusal(result, code)
        refused_state = module.project_catalog_availability(tool, enabled=True, admission=result)
        assert refused_state["admission_checked"] and not refused_state["runnable"]
        assert (a.s.versions, a.s.blobs, a.s.writes, a.accepted) == before
    r4.submit(a, params=params, tool_name=TOOL, tool_manifest_sha256=manifest(TOOL),
              checkout_capability="cap")
    assert len(a.accepted) == a.events.count("insert") == 1
    assert a.events.count("entitlement") == 2
    assert a.accepted[0][4] == params
    assert prepare(a.s, TOOL, params, ctx, job_id=UUID(int=0)).output_intake_bytes == (
        prepare(a.s, TOOL, params, ctx).output_intake_bytes)
    assert not module.project_catalog_availability(tool, enabled=False)["engine_ready"]
    policy_cases.assert_refusal(policy_cases.request(a, {}, tool_name="solar-assign-strings"),
        "SIP_R3_TOOL_UNSUPPORTED", message="Project Solar admission unavailable.")
    refused("SIP_R3_SERVICE_EVIDENCE_REQUIRED", lambda: prepare(
        a.s, "solar-size-strings", {"expected_rev": 0, "mode": "global", "confirm": True}, ctx))
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0")
    policy_cases.assert_refusal(policy_cases.request(a, params, tool_name=TOOL),
        "project_execution_disabled", message="Project Solar admission unavailable.")


def test_sip_r3d_historical_replay(memory, graph, monkeypatch):
    ctx = parent(memory, dependencies(graph))
    p, r, after = commit(memory, TOOL, parameters(ctx.graph), ctx)
    commit(memory, "solar-settings", {"expected_rev": after.graph["rev"],
           "changes": {"num_mppt": 3}}, after)
    assert memory.head != UUID(r["output_version_id"])
    memory.lease = replace(memory.lease, expires_at=memory.parent.created_at)
    before = len(memory.versions), len(memory.reads), len(memory.writes)
    with monkeypatch.context() as patch:
        patch.setattr(memory, "get", forbidden)
        patch.setattr(memory, "put_if_absent_or_verify", forbidden)
        patch.setattr(local, "_load_builtin", forbidden)
        assert publish(memory, p) == r
    assert (len(memory.versions), len(memory.reads), len(memory.writes)) == before
    assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
    assert multi.job_proof(memory, r, p, monkeypatch)["output_version_id"] == r["output_version_id"]
    assert memory.versions[UUID(r["output_version_id"])].oss_object == memory.parent.oss_object


def test_sip_r3d_stored_content_proof(memory, graph, monkeypatch):
    p, r, ctx = commit(memory, TOOL, parameters(graph), parent(memory, dependencies(graph)))
    def gauge(value):
        value["solar_design_graph"]["strings"][0]["wire_gauge"] = "6 AWG"
        return value
    def unrelated(value):
        value["custom"]["keep"].append(99)
        return value
    def identity(value):
        ref = value["solar_design_graph"]["strings"][0]["id"]
        return json.loads(json.dumps(value).replace(ref,
            "leaf:string:00000000-0000-4000-8000-000000000099"))
    def timestamp(value):
        value["solar_design_graph"]["strings"][1]["provenance"]["created_at"] = "2027-01-01T00:00:00Z"
        return value
    for change in (gauge, unrelated, identity, timestamp):
        assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
        assert multi.job_proof(memory, r, p, monkeypatch)["output_version_id"] == r["output_version_id"]
        forged, restore = forge(memory, r, change)
        for receipt in (r, forged):
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, receipt, p))
            refused("SIP_R3_PROOF_REJECTED", lambda: multi.job_proof(memory, receipt, p, monkeypatch))
        restore()
    for key, value in (("graph_sha256", "0" * 64), ("job_id", str(UUID(int=88))),
                       ("after_rev", ctx.graph["rev"] + 1)):
        receipt = {**r, key: value}
        refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, receipt, p))
        refused("SIP_R3_PROOF_REJECTED", lambda: multi.job_proof(memory, receipt, p, monkeypatch))
    version = memory.versions[UUID(r["output_version_id"])]
    original = deepcopy(version.provenance)
    version.provenance["actor_binding_id"] = str(UUID(int=88))
    memory.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)
    refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, r, p))
    refused("SIP_R3_PROOF_REJECTED", lambda: multi.job_proof(memory, r, p, monkeypatch))
    version.provenance = original
    memory.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(original)
    assert output(memory, r).intake_bytes == ctx.intake_bytes


def test_sip_r3d_job_binding(memory, graph, monkeypatch):
    ctx = parent(memory, graph)
    p = prepare(memory, TOOL, parameters(graph), ctx)
    row = r4.record(memory, p)
    monkeypatch.setattr(jobs, "_graph_scope", lambda *args: None)
    assert service.prepare_project_graph_job(row, conn=memory) == p
    r = publish(memory, p)
    assert multi.job_proof(memory, r, p, monkeypatch)["output_version_id"] == r["output_version_id"]
    for key, value in (("tool", "solar-settings"), ("parameters", {}),
            ("parent_version_id", str(UUID(int=81))), ("drawing_id", str(UUID(int=82))),
            ("actor_binding_id", str(UUID(int=83))), ("checkout_fence", "8"),
            ("tool_manifest_sha256", "x"), ("parent_intake_sha256", "0" * 64),
            ("job_id", str(UUID(int=84))), ("attempt", 2)):
        request = {**p.request, key: value}
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: jobs._validate_graph_request(row, request))
        conn = r4.Connection(memory, row)
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: adapter.project_graph_job_provenance(
            memory.job, r, expected=request, conn=conn))

    for key, value in (("tool_name", "solar-settings"), ("params", {}),
            ("input_version_id", str(UUID(int=81))), ("attempt", 2)):
        stored = {**deepcopy(row), key: value}
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: adapter.project_graph_job_provenance(
            memory.job, r, expected=p, conn=r4.Connection(memory, stored)))
    for key, value in (("drawing_id", str(UUID(int=82))), ("actor_binding_id", str(UUID(int=83))),
            ("checkout_fence", "8"), ("tool_manifest_sha256", "x"),
            ("parent_intake_sha256", "0" * 64)):
        stored = deepcopy(row)
        stored["execution_context"][key] = value
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: adapter.project_graph_job_provenance(
            memory.job, r, expected=p, conn=r4.Connection(memory, stored)))


def test_sip_r3d_r3c_chain_boundary(memory):
    _, _, ctx = commit(memory, multi.TOOL, multi.M, multi.rev4(memory))
    for equipped in (False, True):
        if equipped:
            equipment = multi.equipment_params()
            equipment["expected_rev"] = ctx.graph["rev"]
            _, _, ctx = commit(memory, "solar-assign-equipment", equipment, ctx)
        before = ctx.graph
        params = parameters(before, all_strings=True)
        p, r, after = commit(memory, TOOL, params, ctx)
        assert after.graph["rev"] == before["rev"] + 1
        assert after.graph["parent_rev"] == before["rev"]
        assert identities(after.graph) == identities(before)
        assert [s["wire_gauge"] for s in after.graph["strings"]] == ["8 AWG", "8 AWG"]
        assert [s["ordered_panel_refs"] for s in after.graph["strings"]] == [
            s["ordered_panel_refs"] for s in before["strings"]]
        assert all(len(after.graph[name]) == len(before[name]) for name in
                   ("panels", "strings", "inverters", "routes", "schedules"))
        for old, new in zip(before["strings"], after.graph["strings"]):
            assert new["validity"] == old["validity"]
        if before["inverters"]:
            for entity in [*after.graph["strings"], *after.graph["inverters"]]:
                assert entity["validity"] == {"state": "invalid", "reasons": UNKNOWN}
            for tool, request, code in (
                    ("solar-homeruns", {"expected_rev": after.graph["rev"]}, "EQUIPMENT_ASSIGNMENT_REQUIRED"),
                    ("solar-schedule", {"expected_rev": after.graph["rev"], "insertion_point": [0, 0, 0]},
                     "ROUTING_TOPOLOGY_REQUIRED")):
                refused(code, lambda: prepare(memory, tool, request, after))
        assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
        ctx = after
