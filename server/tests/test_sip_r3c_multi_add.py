"""Canonical multi-add: independent creation oracles, replay and both proof authorities."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timezone
from hashlib import sha256
import inspect
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
from test_sip_r3b_tools import (chain, commit, embedded, forbidden, manifest, memory,  # noqa: F401
                                output, prepare)
from test_sip_r3b_chain import (ELEVEN, PANELS, PARENT_INTAKE, PARENT_VERSION, PARENT_TIME,
                                CONFIG, UNKNOWN, S, ORACLE, created, forge, rev4)
import test_sip_r4_jobs as r4
import test_sip_r5_policy as policy_cases
import test_solar_tool_solar_string_multi_add as legacy
from test_w1_design_graph import graph  # noqa: F401
from test_solar_ground_graph_codec import b18, ground_graph, slot_ids  # noqa: F401

TOOL = "solar-string-multi-add"
M = {"expected_rev": 4, "operation": "add-strings", "string_length": 2,
     "ordered_panel_refs": PANELS}
IDS = ["leaf:string:bb85f3ba-958a-4740-8720-0a175f1ee27d",
       "leaf:string:100fd959-8d6f-4df6-9b70-bbd019918bcd"]
GRAPH_SHA = "7bdf60c961ea2ad58e45c73a50198c9ce51b5162301ca2a4c46fdfe4f53a44cf"
INTAKE_SHA = "278b4800db7e5e49d7c3e3c36fcc2f6f40a8bb5c67d81ab488ef287e1e7be63e"
UNSUPPORTED = ("solar-assign-strings", "solar-pvcase-convert",
               "solar-solaredge-accept", "solar-trackers-to-panel-groups",
               "solar-string-delete")


def derived(ctx, params, index, tool=TOOL):
    # Independently serialize the entire frozen object, without adapter creation helpers.
    raw = json.dumps({
        "schema": "leaf.solar-project-creation.v1",
        "organization_id": str(ctx.organization_id), "project_id": str(ctx.project_id),
        "drawing_id": str(ctx.drawing_id), "parent_version_id": str(ctx.parent_version_id),
        "parent_intake_sha256": ctx.intake_sha256, "created_at": ctx.created_at,
        "tool": tool, "parameters": params, "kind": "string", "index": index,
    }, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    return "leaf:string:" + str(UUID(bytes=sha256(raw).digest()[:16], version=4))


def identities(prepared):
    return [string["id"] for string in created(prepared)["strings"]]


def altered(ctx, graph):
    raw = local.canonical_bytes(embedded(graph))
    return replace(ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())


def equipment_params():
    config = {**deepcopy(CONFIG), "total_dc_inputs": 2, "mppt_inputs": {"A": 2}}
    return {"expected_rev": 5, "equipment": [config], "assignments": [
        {"string_ref": ref, "inverter_ref": config["id"], "mppt_letter": "A",
         "input_number": index} for index, ref in enumerate(IDS)]}


def job_proof(s, result, prepared, monkeypatch):
    monkeypatch.setattr(jobs, "_graph_scope", lambda *args: None)
    conn = r4.Connection(s, r4.record(s, prepared))
    return adapter.project_graph_job_provenance(s.job, result, expected=prepared, conn=conn)


# W1C-3 adds commit; fifteen supported tools, four creation tools.
def test_sip_r3c_multi_add_membership(memory):
    assert adapter.SUPPORTED_TOOLS == ELEVEN and len(ELEVEN) == 15
    assert adapter.CREATION_TOOLS == {"solar-string-add", "solar-assign-equipment", TOOL, "solar-panel-groups"}
    assert adapter._tool(TOOL, manifest(TOOL)) == solar_tools.get(TOOL)
    refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: adapter._tool(TOOL, "x"))
    for tool in UNSUPPORTED:
        refused("SIP_R3_TOOL_UNSUPPORTED", lambda: prepare(memory, tool, {}))


def test_sip_r3c_multi_add_preparation(memory, monkeypatch):
    ctx = rev4(memory)
    assert ctx.parent_version_id == PARENT_VERSION and ctx.intake_sha256 == PARENT_INTAKE
    params, original = deepcopy(M), deepcopy((ctx.intake, memory.versions, memory.blobs))
    module = local._load_builtin(TOOL)
    with monkeypatch.context() as patch:
        patch.setattr(module, "new_id", forbidden)
        patch.setattr(module, "datetime", type("NoClock", (), {"now": staticmethod(forbidden)}))
        p = prepare(memory, TOOL, params, ctx)
        assert prepare(memory, TOOL, params, ctx) == p
    assert params == M and (ctx.intake, memory.versions, memory.blobs) == original
    assert identities(p) == IDS == [derived(ctx, M, index) for index in (0, 1)]
    assert not set(IDS) & {ORACLE}
    assert identities(prepare(memory, "solar-string-add", S, ctx)) == [ORACLE]
    assert derived(ctx, S, 0, "solar-string-add") == ORACLE
    g, receipt = created(p), json.loads(p.receipt_bytes)
    assert g["rev"] == receipt["after_rev"] == 5 and receipt["before_rev"] == 4
    assert local.digest(g) == receipt["graph_sha256"] == GRAPH_SHA
    assert p.output_intake_sha256 == INTAKE_SHA
    assert [item["provenance"]["created_at"] for item in g["strings"]] == [PARENT_TIME] * 2
    assert g["project"]["provenance"]["created_at"] == ctx.graph["project"]["provenance"]["created_at"]
    r = publish(memory, p)
    assert r["output_version_id"] == str(UUID(int=13)) and output(memory, r).graph == g


def test_sip_r3c_multi_add_order_and_normalization(memory):
    ctx = rev4(memory)
    p = prepare(memory, TOOL, M, ctx)
    assert prepare(memory, TOOL, {**M, "drawing_id": str(memory.drawing)}, ctx) == p
    assert p.request["parameters"] == M and "drawing_id" not in p.request["parameters"]
    refused("DRAWING_ID_CONFLICT", lambda: prepare(
        memory, TOOL, {**M, "drawing_id": str(UUID(int=77))}, ctx))
    for refs in (PANELS, list(reversed(PANELS))):
        params = {**M, "ordered_panel_refs": refs}
        candidate = prepare(memory, TOOL, params, ctx)
        g = created(candidate)
        assert g["rev"] == 5 and g["parent_rev"] == 4
        assert [s["ordered_panel_refs"] for s in g["strings"]] == [refs[:2], refs[2:]]
        assert [s["circuit_tag"] for s in g["strings"]] == ["S1", "S2"]
        assert identities(candidate) == [derived(ctx, params, i) for i in (0, 1)]
        assignments = {panel["id"]: panel["assignment"] for panel in g["panels"]}
        for string in g["strings"]:
            for seq, ref in enumerate(string["ordered_panel_refs"]):
                assert assignments[ref] == {"string_ref": string["id"], "seq": seq}
        if refs != PANELS:
            assert not set(identities(candidate)) & set(IDS)
    module = local._load_builtin(TOOL)
    receipt = module.add_strings(ctx.graph, {k: v for k, v in M.items() if k != "operation"},
                                _creation=adapter._creation(ctx, TOOL, M))
    assert receipt["sequences"] == [2, 1, 1, 1]
    assert receipt["string_refs"] == IDS and receipt["ordered_panel_refs"] == [PANELS[:2], PANELS[2:]]


def test_sip_r3c_multi_add_id_collision(memory, monkeypatch, graph, b18):
    ctx = rev4(memory)
    cases = [(ctx, M, entity["id"]) for entity in design.entities(ctx.graph)]
    _, _, single = commit(memory, "solar-string-add", {**S, "ordered_panel_refs": PANELS[:1]}, ctx)
    cases.append((single, {**M, "expected_rev": 5, "ordered_panel_refs": PANELS[1:]},
                  single.graph["strings"][0]["id"]))
    compact = ground_graph(graph, b18, 1)
    compact_ctx = altered(ctx, compact)
    compact_refs = slot_ids(compact, 0, 294)
    cases.append((compact_ctx, {**M, "expected_rev": compact["rev"],
                               "ordered_panel_refs": compact_refs[:3]}, compact_refs[-1]))
    # Two otherwise unused allocations: only allocation zero can occupy this ID.
    cases.append((ctx, M, "leaf:string:00000000-0000-4000-8000-0000000000ff"))
    for context, params, ref in cases:
        before = deepcopy((params, context.intake, memory.versions, memory.blobs, memory.head))
        seen = []
        original = adapter._creation_id
        def collision(*args):
            seen.append(args[-1])
            return ref
        with monkeypatch.context() as patch:
            patch.setattr(adapter, "_creation_id", collision)
            refused("DUPLICATE_APPLICATION_ID", lambda: prepare(memory, TOOL, params, context))
        assert adapter._creation_id is original
        assert seen == ([0, 1] if ref.endswith("0000000000ff") else [0])
        assert (params, context.intake, memory.versions, memory.blobs, memory.head) == before
    assert identities(prepare(memory, TOOL, M, ctx)) == IDS


def test_sip_r3c_multi_add_creation_isolation(memory):
    ctx = rev4(memory)
    base = prepare(memory, TOOL, M, ctx)
    assert identities(base) == IDS == [derived(ctx, M, i) for i in (0, 1)]
    # Vary the complete normalized request independently of the parent: even the revision
    # contributes to identity before the builtin enforces that revision against its graph.
    for params in ({**M, "expected_rev": 5}, {**M, "string_length": 3},
                   {**M, "ordered_panel_refs": list(reversed(PANELS))}):
        channel = adapter._creation(ctx, TOOL, params)
        occupied, refs = set(), []
        for index in (0, 1):
            ref = channel["new_id"]("string", index, occupied)
            refs.append(ref)
            occupied.add(ref)
        assert refs == [derived(ctx, params, i) for i in (0, 1)]
        assert len(set(refs)) == 2 and not set(refs) & set(IDS)
    for field, value in (("organization_id", UUID(int=71)), ("project_id", UUID(int=72)),
            ("drawing_id", UUID(int=73)), ("parent_version_id", UUID(int=74)),
            ("created_at", "2026-02-03T04:05:06Z"), ("intake_sha256", "a" * 64)):
        changed = replace(ctx, **{field: value})
        lease = replace(memory.lease, organization_id=changed.organization_id,
                        project_id=changed.project_id, drawing_id=changed.drawing_id)
        p = prepare(memory, TOOL, M, changed, checkout=lease)
        assert prepare(memory, TOOL, M, changed, checkout=lease) == p
        assert identities(p) == [derived(changed, M, i) for i in (0, 1)]
        assert len(set(identities(p))) == 2 and not set(identities(p)) & set(IDS)
    intake = ctx.intake
    intake["custom"]["keep"].append(99)
    raw = local.canonical_bytes(intake)
    other = replace(ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    assert identities(prepare(memory, TOOL, M, other)) == [derived(other, M, i) for i in (0, 1)]
    for params in ({**M, "string_length": 3}, {**M, "ordered_panel_refs": list(reversed(PANELS))}):
        p = prepare(memory, TOOL, params, ctx)
        assert identities(p) == [derived(ctx, params, i) for i in range(len(identities(p)))]
        assert not set(identities(p)) & set(IDS)
    graph = ctx.graph
    graph["rev"] = 5
    graph["parent_rev"] = 4
    moved = altered(ctx, graph)
    params = {**M, "expected_rev": 5}
    assert identities(prepare(memory, TOOL, params, moved)) == [derived(moved, params, i) for i in (0, 1)]
    for changes in ({"job_id": UUID(int=99)}, {"attempt": 2},
            {"checkout": replace(memory.lease, fence=8)},
            {"checkout": replace(memory.lease, holder_binding_id=UUID(int=98))}):
        p = prepare(memory, TOOL, M, ctx, **changes)
        assert p.request_bytes != base.request_bytes and p.output_intake_bytes == base.output_intake_bytes


def test_sip_r3c_multi_add_concurrent_isolation(memory):
    ctx = rev4(memory)
    contexts = {"a": (ctx, memory.lease),
        "drawing": (replace(ctx, drawing_id=UUID(int=77)),
                    replace(memory.lease, drawing_id=UUID(int=77))),
        "parent": (replace(ctx, parent_version_id=UUID(int=998)), memory.lease)}
    def run(name):
        context, lease = contexts[name]
        return name, prepare(memory, TOOL, deepcopy(M), context, checkout=lease)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(run, [name for _ in range(4) for name in contexts]))
    saved = {}
    for name, prepared in rows:
        assert prepared == saved.setdefault(name, prepared)
        assert identities(prepared) == [derived(contexts[name][0], M, i) for i in (0, 1)]
    assert identities(saved["a"]) == IDS
    assert len({ref for p in saved.values() for ref in identities(p)}) == 6


def test_sip_r3c_multi_add_legacy_defaults(graph, monkeypatch):
    module = local._load_builtin(TOOL)
    for function in (module._new_strings, module.add_strings, module.run):
        parameter = inspect.signature(function).parameters["_creation"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is None
    g = legacy.multi_graph(graph)
    params = {"expected_rev": 0, "string_length": 14, "ordered_panel_refs": [
        legacy.BY_HANDLE[handle] for row in legacy.RECEIPT_ROWS for handle in row]}
    original = deepcopy((g, params))
    legacy.pin(module, monkeypatch)
    calls, clock, new_id = [], module.datetime, module.new_id
    class Clock:
        @staticmethod
        def now(tz=None):
            calls.append(("now", tz))
            return clock.now(tz)
    def allocate(kind):
        calls.append(("id", kind))
        return new_id(kind)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "new_id", allocate)
    receipt = module.add_strings(g, params)
    assert calls == [("now", timezone.utc), ("id", "string"), ("id", "string")]
    assert sha256(local.canonical_bytes(receipt)).hexdigest() == (
        "b8031ddb2f002e8322c2ebb17cc5df46484501b27c9ab45fd4e11372c5fbf1e2")
    assert local.digest(receipt["graph"]) == "0edc711c1fcd57830c64443056e9e2c694aa5c9158e9bfa2fd5467474fcf2af4"
    assert (g, params) == original
    # Old test doubles accept no private keyword at either forwarding boundary.
    with monkeypatch.context() as patch:
        patch.setitem(module.OPERATIONS, "add-strings", lambda intake, request: {"graph": intake})
        assert module.run(g, {**params, "operation": "add-strings"}) is g
    original_builder = module._new_strings
    seen = []
    def old_builder(graph, chunks, tables):
        seen.append(chunks)
        return original_builder(graph, chunks, tables)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_new_strings", old_builder)
        assert module.add_strings(g, params)["ordered_panel_refs"] == seen[0]


def test_sip_r3c_multi_add_exact_replay(memory, monkeypatch):
    p, r, ctx = commit(memory, TOOL, M, rev4(memory))
    commit(memory, "solar-settings", {"expected_rev": 5, "changes": {"num_mppt": 3}}, ctx)
    assert memory.head != UUID(r["output_version_id"])
    before = len(memory.versions), len(memory.reads), len(memory.writes)
    with monkeypatch.context() as patch:
        patch.setattr(memory, "get", forbidden)
        patch.setattr(memory, "put_if_absent_or_verify", forbidden)
        patch.setattr(local, "_load_builtin", forbidden)
        assert publish(memory, p) == r
    assert (len(memory.versions), len(memory.reads), len(memory.writes)) == before
    assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
    assert job_proof(memory, r, p, monkeypatch)["output_version_id"] == r["output_version_id"]


def test_sip_r3c_multi_add_stored_content_tampering(memory, monkeypatch):
    p, r, ctx = commit(memory, TOOL, M, rev4(memory))
    def timestamp(intake):
        intake["solar_design_graph"]["strings"][1]["provenance"]["created_at"] = "2026-01-02T00:00:00Z"
        return intake
    def identity(intake):
        return json.loads(json.dumps(intake).replace(IDS[0],
            "leaf:string:00000000-0000-4000-8000-0000000000aa"))
    def unrelated(intake):
        intake["custom"]["keep"].append(99)
        return intake
    for change in (timestamp, identity, unrelated):
        assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
        assert job_proof(memory, r, p, monkeypatch)["output_version_id"] == r["output_version_id"]
        forged, restore = forge(memory, r, change)
        for result in (r, forged):
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, result, p))
            refused("SIP_R3_PROOF_REJECTED", lambda: job_proof(memory, result, p, monkeypatch))
        restore()
    assert output(memory, r).intake_bytes == ctx.intake_bytes


def test_sip_r3c_multi_add_refusals(memory, graph, monkeypatch):
    ctx = rev4(memory)
    before = deepcopy((ctx.intake, memory.versions, memory.blobs, memory.head))
    for changes, code in (({"expected_rev": 3}, "STALE_GRAPH_REVISION"),
            ({"string_length": 0}, "INVALID_STRING_MULTI_ADD_REQUEST"),
            ({"string_length": 901}, "INVALID_STRING_MULTI_ADD_REQUEST"),
            ({"string_length": 4}, "STRING_TOO_LONG"),
            ({"ordered_panel_refs": ["missing"]}, "MISSING_PANEL"),
            ({"ordered_panel_refs": [PANELS[0]] * 2}, "DUPLICATE_PANEL_MEMBERSHIP")):
        refused(code, lambda: prepare(memory, TOOL, {**M, **changes}, ctx))
    for extra in ({"_creation": {}}, {"creation": {}}, {"created_at": PARENT_TIME},
            {"string_id": IDS[0]}, {"string_ids": IDS}, {"operation": "add-string"},
            {"operation": None}, {"operation": ["add-strings"]}, {"string_length": True},
            {"ordered_panel_refs": []}, {"ordered_panel_refs": PANELS * 301},
            {"ordered_panel_refs": ["x" * 129]}, {"ordered_panel_refs": [1]},
            {"ordered_panel_refs": "panels"}, {"expected_rev": True}):
        refused("INVALID_STRING_MULTI_ADD_REQUEST", lambda: prepare(memory, TOOL, {**M, **extra}, ctx))
    refused("INVALID_STRING_MULTI_ADD_REQUEST", lambda: prepare(
        memory, TOOL, {k: v for k, v in M.items() if k != "operation"}, ctx))
    refused("INVALID_JSON_VALUE", lambda: prepare(
        memory, TOOL, {**M, "ordered_panel_refs": tuple(PANELS)}, ctx))
    for params, code in (({**M, 1: "bad"}, "INVALID_JSON_OBJECT"),
            ({**M, "ordered_panel_refs": [chr(0xD800)]}, "INVALID_JSON_STRING"),
            ({**M, "string_length": float("nan")}, "NONFINITE_NUMBER"),
            ({**M, "string_length": 1 << 65}, "NUMBER_LIMIT_EXCEEDED"),
            ([], "INVALID_SETTINGS_REQUEST")):
        refused(code, lambda: prepare(memory, TOOL, params, ctx))
    with monkeypatch.context() as patch:
        patch.setattr(design, "MAX_NODES", 1)
        refused("GRAPH_LIMIT_EXCEEDED", lambda: prepare(memory, TOOL, M, ctx))
    for changes, code in (({"graph_schema_version": 2}, "UNSUPPORTED_SCHEMA_VERSION"),
            ({"parent_rev": 2}, "INVALID_REVISION_CHAIN"), ({"rev": "four"}, "INVALID_GRAPH_SCHEMA")):
        malformed = ctx.graph
        malformed.update(changes)
        refused(code, lambda: prepare(memory, TOOL, M, altered(ctx, malformed)))
    assert (ctx.intake, memory.versions, memory.blobs, memory.head) == before
    # A manual sizing record that no longer matches the graph it labels is refused as
    # rejected custody before the tool loads (R14-C); an honestly unsized graph has no record.
    inconsistent = ctx.graph
    inconsistent["settings"]["panels_in_sequence"] = 0
    refused("SIP_R3_PROOF_REJECTED", lambda: prepare(memory, TOOL, M, altered(ctx, inconsistent)))
    unsized = ctx.graph
    unsized["settings"]["panels_in_sequence"] = 0
    unsized["settings"]["global_string_sizing_confirmed"] = False
    del unsized["settings"]["extra"]["string_sizing"]
    refused("STRING_LENGTH_NOT_SIZED", lambda: prepare(memory, TOOL, M, altered(ctx, unsized)))
    exhausted = ctx.graph
    exhausted["settings"]["string_number"] = 1_000_000
    refused("STRING_NUMBER_EXHAUSTED", lambda: prepare(memory, TOOL, M, altered(ctx, exhausted)))
    module = local._load_builtin(TOOL)
    infeasible = legacy.multi_graph(graph)
    refs = [legacy.BY_HANDLE[h] for h in legacy.HANDLES[:11]]
    refused("MULTI_ADD_INFEASIBLE_COUNT", lambda: module.run(
        infeasible, {"expected_rev": 0, "operation": "add-strings", "string_length": 14,
                     "ordered_panel_refs": refs}))
    unresolved = legacy.variant(graph, "UNITS")
    refused("UNRESOLVED_UNITS", lambda: module.run(unresolved, legacy.request()))
    _, _, strung = commit(memory, TOOL, M, ctx)
    refused("PANEL_ALREADY_ASSIGNED", lambda: prepare(memory, TOOL, {**M, "expected_rev": 5}, strung))


def test_sip_r3c_multi_add_admission_and_terminal(monkeypatch):
    a = policy_cases.setup(monkeypatch)
    s, ctx = a.s, rev4(a.s)
    r4.at(a, monkeypatch, ctx, TOOL)
    params = {**deepcopy(M), "drawing_id": str(s.drawing)}
    before = deepcopy((s.versions, s.blobs, s.writes, a.accepted))
    preview = policy_cases.request(a, params, tool_name=TOOL, input_version_id=ctx.parent_version_id)
    assert preview["status_code"] == 200
    assert (s.versions, s.blobs, s.writes, a.accepted) == before
    submitted = r4.submit(a, params=params, tool_name=TOOL, input_version_id=ctx.parent_version_id,
                          tool_manifest_sha256=manifest(TOOL), checkout_capability="cap")
    assert submitted["status"] == "queued" and a.accepted[-1][4] == M
    assert a.events.count("insert") == 1
    probe = prepare(s, TOOL, M, ctx, job_id=UUID(int=0))
    real = prepare(s, TOOL, M, ctx)
    assert probe.output_intake_bytes == real.output_intake_bytes
    row = r4.record(s, real)
    monkeypatch.setattr(jobs, "_graph_scope", lambda *args: None)
    assert service.prepare_project_graph_job(row, conn=s) == real
    for key in ("drawing_id", "actor_binding_id", "checkout_fence", "parent_intake_sha256"):
        request = real.request
        request[key] = "altered"
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: jobs._validate_graph_request(row, request))
    receipt = publish(s, real)
    assert job_proof(s, receipt, real, monkeypatch)["output_version_id"] == receipt["output_version_id"]
    assert proof(s, receipt, real)["graph_sha256"] == GRAPH_SHA


def test_sip_r3c_multi_add_manual_power_boundary(memory):
    _, _, ctx5 = commit(memory, TOOL, M, rev4(memory))
    p, r, ctx6 = commit(memory, "solar-assign-equipment", equipment_params(), ctx5)
    g = ctx6.graph
    assert r["after_rev"] == g["rev"] == 6
    assert tuple(len(g[name]) for name in ("panels", "strings", "inverters", "routes", "schedules")) == (
        3, 2, 1, 0, 0)
    for entity in [*g["inverters"], *g["strings"]]:
        assert entity["validity"] == {"state": "invalid", "reasons": UNKNOWN}
    assert g["settings"]["extra"]["string_sizing"]["records"] == {}
    assert [s["inverter_ref"] for s in g["strings"]] == [CONFIG["id"]] * 2
    before = len(memory.versions), memory.head, list(memory.writes)
    refused("EQUIPMENT_ASSIGNMENT_REQUIRED", lambda: prepare(
        memory, "solar-homeruns", {"expected_rev": 6}, ctx6))
    refused("ROUTING_TOPOLOGY_REQUIRED", lambda: prepare(
        memory, "solar-schedule", {"expected_rev": 6, "insertion_point": [0, 0, 0]}, ctx6))
    assert (len(memory.versions), memory.head, memory.writes) == before
    assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
