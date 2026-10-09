"""Canonical Solar string and equipment creation: deterministic, replayable and provable."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
from uuid import UUID

import solar_equipment
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
from solar_equipment import FIELDS
from test_sip_r3a_graph import proof, publish, refused
from test_sip_r3b_tools import (HASHES, assigned_parent, chain, commit, forbidden, manifest,  # noqa: F401
                                memory, prepare)
from test_w1_design_graph import graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401
from test_w2_string_add import free_panels, solved  # noqa: F401

STRING = "solar-string-add"
EQUIPMENT = "solar-assign-equipment"
NINE = {"solar-settings", "solar-panels-from-drawing", "solar-size-strings",
        "solar-combiners", "solar-feeders", "solar-homeruns", "solar-schedule",
        "solar-string-add", "solar-assign-equipment"}
# Measured on the R3b-1 chain: the rev-4 parent and its ordered panels.
PARENT_VERSION = UUID("00000000-0000-0000-0000-00000000000c")
PARENT_INTAKE = "aaffa0f67f4a30d0d1fb8bb6b07980d10e2cc5aceb5922ff227e9d598ad2c73b"
PARENT_TIME = "2026-01-01T00:00:00Z"
PANELS = ["leaf:panel:8ef419c1-2f02-4af1-970e-b61cd90a4e6e",
          "leaf:panel:10edff78-525b-46e0-bc0f-52b58d311504",
          "leaf:panel:3e1c2937-ec4b-42e7-8fed-694dbdd15deb"]
S = {"expected_rev": 4, "operation": "add-string", "ordered_panel_refs": PANELS}
# The frozen derivation, calculated independently of this implementation.
ORACLE = "leaf:string:17a79df6-acd1-4c68-b03e-1404aaebe916"
# The single empty-ID inverter requested on the rev-5 parent, frozen the same way.
ALONE = "leaf:inverter:092bb814-c63f-4d50-b913-0063f5088ec9"
CONFIG = {
    "id": "leaf:inverter:00000000-0000-4000-8000-000000000001",
    "number": 1, "type_key": "INV", "model": "fixture",
    "position": [5, 0, 0], "rotation": 0, "scale": [1, 1, 1],
    "block_name": "FixtureInverter", "layer": "0",
    "mppt_count": 1, "total_dc_inputs": 1, "mppt_inputs": {"A": 1},
    "max_dc_voltage": 1500, "max_ac_power_kw": 100,
    "max_dc_power_kw": 100, "is_solaredge": False,
}
ASSIGNMENT = {"string_ref": ORACLE, "inverter_ref": CONFIG["id"], "mppt_letter": "A",
              "input_number": 0}
UNKNOWN = ["EQUIPMENT_POWER_UNKNOWN", "EQUIPMENT_VOLTAGE_UNKNOWN"]
INJECTED = ({"creation": {"created_at": PARENT_TIME}}, {"_creation": {"created_at": PARENT_TIME}},
            {"created_at": PARENT_TIME}, {"string_id": ORACLE}, {"inverter_ids": [CONFIG["id"]]})


def E(rev=5, equipment=None, assignments=None):
    return {"expected_rev": rev,
            "equipment": deepcopy([CONFIG] if equipment is None else equipment),
            "assignments": deepcopy([ASSIGNMENT] if assignments is None else assignments)}


def empty(number):
    return {**deepcopy(CONFIG), "id": "", "number": number}


def created(prepared):
    return json.loads(prepared.output_intake_bytes)["solar_design_graph"]


def rev4(s):
    ctx = chain(s)[-1][2]
    assert ctx.parent_version_id == PARENT_VERSION and ctx.intake_sha256 == PARENT_INTAKE
    assert ctx.created_at == PARENT_TIME and ctx.graph_sha256 == HASHES[3]
    assert {panel["id"] for panel in ctx.graph["panels"]} == set(PANELS)
    return ctx


def through_string(s):
    p, r, ctx = commit(s, STRING, S, rev4(s))
    return p, r, ctx


def through_equipment(s):
    sp, sr, ctx5 = through_string(s)
    ep, er, ctx6 = commit(s, EQUIPMENT, E(), ctx5)
    return (sp, sr, ctx5), (ep, er, ctx6)


def spy_clock(monkeypatch, module):
    """Record the module's own UUID and clock reads while keeping their behavior."""
    calls, real_new_id = [], module.new_id

    def new_id(kind):
        calls.append(("id", kind))
        return real_new_id(kind)

    class Clock:
        @staticmethod
        def now(tz=None):
            calls.append(("now", tz))
            return datetime.now(tz)

    monkeypatch.setattr(module, "new_id", new_id)
    monkeypatch.setattr(module, "datetime", Clock)
    return calls


def private_keyword(function):
    parameter = inspect.signature(function).parameters["_creation"]
    return parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is None


def forge(s, result, change):
    """Rewrite one stored output consistently, as R3b-1 does: companion digest, provenance, fingerprint."""
    version = s.versions[UUID(result["output_version_id"])]
    original = deepcopy(version.__dict__)
    intake = change(json.loads(s.blobs[version.intake_ref]))
    intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
    raw = local.canonical_bytes(intake)
    digest = sha256(raw).hexdigest()
    version.intake_ref = project.graph_store().publication_intake_key(s.org, s.project, s.drawing, digest)
    version.provenance["intake"] = {"ref": version.intake_ref, "sha256": digest}
    s.blobs[version.intake_ref] = raw
    s.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)
    forged = {**result, "graph_sha256": intake["solar_design_graph_sha256"],
              "output_intake_sha256": digest}

    def restore():
        version.__dict__.update(deepcopy(original))
        s.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)

    return forged, restore


def derived(ctx, tool, params, kind, index):
    """One creation ID rebuilt here from the frozen contract, never through the adapter."""
    raw = json.dumps({
        "schema": "leaf.solar-project-creation.v1",
        "organization_id": str(ctx.organization_id), "project_id": str(ctx.project_id),
        "drawing_id": str(ctx.drawing_id), "parent_version_id": str(ctx.parent_version_id),
        "parent_intake_sha256": ctx.intake_sha256, "created_at": ctx.created_at,
        "tool": tool, "parameters": params, "kind": kind, "index": index,
    }, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    return "leaf:" + kind + ":" + str(UUID(bytes=sha256(raw).digest()[:16], version=4))


def test_sip_r3b_chain_membership(memory):
    assert adapter.SUPPORTED_TOOLS == NINE
    for tool in ("solar-string-multi-add", "solar-assign-strings", "solar-commit-solve",
                 "solar-pvcase-convert", "solar-solaredge-accept", "solar-trackers-to-panel-groups",
                 "solar-string-delete", "solar-correct-string"):
        refused("SIP_R3_TOOL_UNSUPPORTED", lambda: prepare(memory, tool, {}))
    for tool in (STRING, EQUIPMENT):
        refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: prepare(
            memory, tool, {}, tool_manifest_sha256="sha256:" + "0" * 64))


def test_sip_r3b_chain_string_preparation(memory):
    ctx = rev4(memory)
    params, graph_before = deepcopy(S), ctx.graph
    one = prepare(memory, STRING, params, ctx)
    assert prepare(memory, STRING, params, ctx) == one
    assert params == S and ctx.graph == graph_before
    after = created(one)
    receipt = json.loads(one.receipt_bytes)
    assert after["rev"] == receipt["after_rev"] == 5 and receipt["before_rev"] == 4
    assert len(after["strings"]) == 1
    string = after["strings"][0]
    assert string["id"] == ORACLE == adapter._creation_id(ctx, STRING, S, "string", 0)
    assert derived(ctx, STRING, S, "string", 0) == ORACLE
    assert string["provenance"]["created_at"] == ctx.created_at == PARENT_TIME
    assert string["ordered_panel_refs"] == PANELS
    assert one.request["parameters"] == S
    bound = prepare(memory, STRING, {**S, "drawing_id": str(memory.drawing)}, ctx)
    assert bound == one


def test_sip_r3b_chain_equipment_preparation(memory):
    _, _, ctx5 = through_string(memory)
    params = E()
    one = prepare(memory, EQUIPMENT, params, ctx5)
    assert prepare(memory, EQUIPMENT, params, ctx5) == one
    assert params == E()
    after = created(one)
    assert after["rev"] == json.loads(one.receipt_bytes)["after_rev"] == 6
    inverter, string = after["inverters"][0], after["strings"][0]
    assert [item["id"] for item in after["inverters"]] == [CONFIG["id"]]
    assert inverter["provenance"]["created_at"] == ctx5.created_at
    assert inverter["input_assignments"] == [
        {"string_ref": ORACLE, "mppt_letter": "A", "input_number": 0}]
    assert string["id"] == ORACLE and string["inverter_ref"] == string["to_ref"] == CONFIG["id"]
    assert after["extra"]["equipment"]["assignment_requests"] == [ASSIGNMENT]
    # The creation time is the exact parent's stored time, not a constant.
    later = replace(ctx5, created_at="2026-02-03T04:05:06Z")
    moved = created(prepare(memory, EQUIPMENT, params, later))
    assert moved["inverters"][0]["provenance"]["created_at"] == "2026-02-03T04:05:06Z"
    # An empty-ID configuration takes the ID derived for its index; two take two IDs.
    alone = E(equipment=[empty(1)], assignments=[])
    p = prepare(memory, EQUIPMENT, alone, ctx5)
    assert prepare(memory, EQUIPMENT, alone, ctx5) == p
    assert [item["id"] for item in created(p)["inverters"]] == [ALONE] == [
        derived(ctx5, EQUIPMENT, alone, "inverter", 0)]
    mixed = E(equipment=[CONFIG, empty(2), empty(3)])
    p = prepare(memory, EQUIPMENT, mixed, ctx5)
    assert prepare(memory, EQUIPMENT, mixed, ctx5) == p
    ids = [item["id"] for item in created(p)["inverters"]]
    assert ids == [CONFIG["id"]] + [derived(ctx5, EQUIPMENT, mixed, "inverter", i) for i in (1, 2)]
    assert len(set(ids)) == 3 and all(ref.startswith("leaf:inverter:") for ref in ids)
    # An existing inverter keeps its ID and creation time; only the new one is derived.
    _, _, ctx6 = commit(memory, EQUIPMENT, params, ctx5)
    kept = ctx6.graph["inverters"][0]["provenance"]["created_at"]
    assert kept == PARENT_TIME
    retained = E(rev=6, equipment=[CONFIG, empty(2)])
    later6 = replace(ctx6, created_at="2027-01-01T00:00:00Z")
    inverters = created(prepare(memory, EQUIPMENT, retained, later6))["inverters"]
    assert inverters[0]["id"] == CONFIG["id"] and inverters[0]["provenance"]["created_at"] == kept
    assert inverters[1]["id"] == derived(later6, EQUIPMENT, retained, "inverter", 1)
    assert inverters[1]["provenance"]["created_at"] == "2027-01-01T00:00:00Z"


def test_sip_r3b_chain_id_collision(memory, monkeypatch):
    ctx4 = rev4(memory)
    _, _, ctx5 = commit(memory, STRING, S, ctx4)
    _, _, ctx6 = commit(memory, EQUIPMENT, E(), ctx5)
    fresh = "leaf:inverter:00000000-0000-4000-8000-0000000000ff"
    cases = (
        (PANELS[0], STRING, S, ctx4),
        (CONFIG["id"], EQUIPMENT, E(rev=6, equipment=[CONFIG, empty(2)]), ctx6),
        (fresh, EQUIPMENT, E(equipment=[empty(1), empty(2)], assignments=[]), ctx5),
        # Nothing is allocated before the derived ID here, so only the graph's own entities can refuse it.
        (CONFIG["id"], EQUIPMENT, E(rev=6, equipment=[empty(1)], assignments=[]), ctx6))
    for derived, tool, params, ctx in cases:
        before = deepcopy(params), ctx.intake_bytes, len(memory.versions), memory.head
        with monkeypatch.context() as patch:
            patch.setattr(adapter, "_creation_id", lambda *args, ref=derived: ref)
            refused("DUPLICATE_APPLICATION_ID", lambda: prepare(memory, tool, params, ctx))
        assert (params, ctx.intake_bytes, len(memory.versions), memory.head) == before
    # Without the forced seam the same requests prepare normally.
    assert created(prepare(memory, STRING, S, ctx4))["strings"][0]["id"] == ORACLE
    # The empty-only request derives a fresh ID then, and is refused only for leaving the placed inverter out.
    refused("EQUIPMENT_REMOVAL_UNSUPPORTED", lambda: prepare(
        memory, EQUIPMENT, E(rev=6, equipment=[empty(1)], assignments=[]), ctx6))


def test_sip_r3b_chain_concurrent_isolation(memory):
    ctx = rev4(memory)
    drawing = UUID(int=77)
    contexts = {
        "drawing-a": (ctx, memory.lease),
        "drawing-b": (replace(ctx, drawing_id=drawing), replace(memory.lease, drawing_id=drawing)),
        "parent-b": (replace(ctx, parent_version_id=UUID(int=998)), memory.lease)}
    digest = manifest(STRING)

    def run(name):
        context, lease = contexts[name]
        return name, prepare(memory, STRING, deepcopy(S), context, checkout=lease,
                             tool_manifest_sha256=digest)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, [name for _ in range(4) for name in contexts]))
    prepared = {}
    for name, p in results:
        prepared.setdefault(name, []).append(p)
    assert {name: len(rows) for name, rows in prepared.items()} == dict.fromkeys(contexts, 4)
    for rows in prepared.values():
        assert all(p == rows[0] for p in rows)
    ids = {name: created(rows[0])["strings"][0]["id"] for name, rows in prepared.items()}
    assert ids["drawing-a"] == ORACLE and len(set(ids.values())) == 3
    for name, (context, _) in contexts.items():
        assert ids[name] == adapter._creation_id(context, STRING, S, "string", 0)
    # Different full parent content, same embedded graph: a different ID.
    intake = ctx.intake
    intake["custom"]["keep"].append(99)
    raw = local.canonical_bytes(intake)
    changed = replace(ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    assert created(prepare(memory, STRING, S, changed))["strings"][0]["id"] not in ids.values()
    # Job, attempt and fence stay request-bound and never move a creation ID.
    base = prepared["drawing-a"][0]
    for changes in ({"job_id": UUID(int=999)}, {"attempt": 2},
                    {"checkout": replace(memory.lease, fence=8)}):
        other = prepare(memory, STRING, S, ctx, **changes)
        assert other.request_sha256 != base.request_sha256
        assert other.output_intake_bytes == base.output_intake_bytes


def test_sip_r3b_chain_equipment_isolation(memory):
    _, _, ctx5 = through_string(memory)
    params = E(equipment=[empty(1), empty(2)], assignments=[])
    drawing = UUID(int=77)
    contexts = {
        "drawing-a": (ctx5, memory.lease),
        "drawing-b": (replace(ctx5, drawing_id=drawing), replace(memory.lease, drawing_id=drawing)),
        "parent-b": (replace(ctx5, parent_version_id=UUID(int=998)), memory.lease),
        "time-b": (replace(ctx5, created_at="2026-02-03T04:05:06Z"), memory.lease)}
    digest = manifest(EQUIPMENT)
    ids = {}
    for name, (context, lease) in contexts.items():
        p = prepare(memory, EQUIPMENT, deepcopy(params), context, checkout=lease,
                    tool_manifest_sha256=digest)
        assert prepare(memory, EQUIPMENT, deepcopy(params), context, checkout=lease,
                       tool_manifest_sha256=digest) == p
        ids[name] = [item["id"] for item in created(p)["inverters"]]
        # Each slot's ID is the frozen derivation over that scope, parent, time, request and index.
        assert ids[name] == [derived(context, EQUIPMENT, params, "inverter", i) for i in (0, 1)]
    seen = {ref for refs in ids.values() for ref in refs}
    assert len(seen) == 8
    # Different full parent content, same embedded graph: different IDs.
    intake = ctx5.intake
    intake["custom"]["keep"].append(99)
    raw = local.canonical_bytes(intake)
    changed = replace(ctx5, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    other = [item["id"] for item in created(prepare(memory, EQUIPMENT, params, changed))["inverters"]]
    assert other == [derived(changed, EQUIPMENT, params, "inverter", i) for i in (0, 1)]
    assert not seen & set(other)
    # A different request moves every derived ID, the slot whose configuration did not change included.
    renumbered = E(equipment=[empty(1), empty(3)], assignments=[])
    moved = [item["id"] for item in created(prepare(memory, EQUIPMENT, renumbered, ctx5))["inverters"]]
    assert moved == [derived(ctx5, EQUIPMENT, renumbered, "inverter", i) for i in (0, 1)]
    assert not (seen | set(other)) & set(moved)
    # Job, attempt and fence stay request-bound and never move a creation ID.
    base = prepare(memory, EQUIPMENT, params, ctx5)
    for changes in ({"job_id": UUID(int=999)}, {"attempt": 2},
                    {"checkout": replace(memory.lease, fence=8)}):
        again = prepare(memory, EQUIPMENT, params, ctx5, **changes)
        assert again.request_sha256 != base.request_sha256
        assert again.output_intake_bytes == base.output_intake_bytes


def test_sip_r3b_chain_legacy_string_defaults(solved, monkeypatch):
    module = local._load_builtin(STRING)
    for function in (module._new_string, module.add_string, module.run):
        assert private_keyword(function)
    calls = spy_clock(monkeypatch, module)
    original = deepcopy(solved)
    free = [panel["id"] for panel in free_panels(solved)]
    g = deepcopy(solved)
    string = module._new_string(g, free[:2], module.slot_tables(g))
    assert calls == [("id", "string"), ("now", timezone.utc)]
    assert string["id"].startswith("leaf:string:") and string["inverter_ref"] is None
    assert datetime.fromisoformat(string["provenance"]["created_at"]).utcoffset() is not None
    assert set(string) == {"id", "kind", "rev", "validity", "provenance", "extra", "circuit_tag",
                           "circuit_kind", "ordered_panel_refs", "module_count", "from_ref", "to_ref",
                           "tag_text_ref", "wire_gauge", "length_ft", "route", "inverter_ref"}
    # Producer shape one: a single add over the freed panels.
    calls.clear()
    result = module.add_string(solved, {"expected_rev": solved["rev"], "ordered_panel_refs": free[:2]})
    assert set(result) == {"graph", "string_ref", "circuit_tag", "ordered_panel_refs", "total"}
    assert calls == [("id", "string"), ("now", timezone.utc)] and solved == original
    assert result["string_ref"] == result["graph"]["strings"][-1]["id"]
    # Producer shape two: adds chained in cut order, each on the previous result.
    calls.clear()
    graph_now, committed = solved, []
    for members in (free[:2], free[2:4]):
        step = module.add_string(graph_now, {"expected_rev": graph_now["rev"],
                                             "ordered_panel_refs": members})
        graph_now = step["graph"]
        committed.append(step["string_ref"])
    assert calls == [("id", "string"), ("now", timezone.utc)] * 2 and len(set(committed)) == 2
    calls.clear()
    after = module.run(solved, {"operation": "add-string", "expected_rev": solved["rev"],
                                "ordered_panel_refs": free[:2]})
    assert calls == [("id", "string"), ("now", timezone.utc)]
    assert len(after["strings"]) == len(solved["strings"]) + 1 and solved == original


def test_sip_r3b_chain_legacy_equipment_defaults(case, monkeypatch):
    g, params, intake = case
    builtin = local._load_builtin(EQUIPMENT)
    for function in (solar_equipment.equipment_candidate, builtin.run):
        assert private_keyword(function)
    calls = spy_clock(monkeypatch, solar_equipment)
    supplied = params["equipment"][0]["id"]
    assert supplied
    first = solar_equipment.equipment_candidate(deepcopy(g), deepcopy(params))
    assert [item["id"] for item in first["inverters"]] == [supplied]
    assert calls == [("now", timezone.utc)]
    stamp = first["inverters"][0]["provenance"]["created_at"]
    assert datetime.fromisoformat(stamp).utcoffset() is not None
    # An existing inverter keeps its creation provenance and reads no clock.
    calls.clear()
    again = solar_equipment.equipment_candidate(first, {**deepcopy(params), "expected_rev": first["rev"]})
    assert again["inverters"][0]["provenance"]["created_at"] == stamp and calls == []
    # An empty ID still takes a random inverter ID.
    calls.clear()
    blank = deepcopy(params)
    blank["equipment"][0]["id"], blank["assignments"] = "", []
    made = solar_equipment.equipment_candidate(deepcopy(g), blank)
    assert calls == [("id", "inverter"), ("now", timezone.utc)]
    assert made["inverters"][0]["id"].startswith("leaf:inverter:")
    assert made["inverters"][0]["id"] != supplied
    calls.clear()
    out = builtin.run(deepcopy(g), deepcopy(params))
    assert [item["id"] for item in out["inverters"]] == [supplied] and calls == [("now", timezone.utc)]
    # The licensed adapter and its revalidation keep their behavior.
    calls.clear()
    placed = equipment.assign_equipment(deepcopy(g), deepcopy(params), drawing_intake=deepcopy(intake),
                                        licensed_equipment=licensed)
    assert placed["ready"] is True and calls == [("now", timezone.utc)]
    inverter = placed["graph"]["inverters"][0]
    assert inverter["id"] == supplied and inverter["provenance"]["source_handle"] == "C1"
    calls.clear()
    assert solar_equipment.equipment_ready(placed["graph"]) is True and calls == []


def test_sip_r3b_chain_exact_replay(memory, monkeypatch):
    (sp, sr, _), (ep, er, _) = through_equipment(memory)
    before = len(memory.versions), len(memory.reads), len(memory.writes)
    with monkeypatch.context() as patch:
        patch.setattr(memory, "get", forbidden)
        patch.setattr(memory, "put_if_absent_or_verify", forbidden)
        patch.setattr(local, "_load_builtin", forbidden)
        assert publish(memory, sp) == sr
        assert publish(memory, ep) == er
    assert (len(memory.versions), len(memory.reads), len(memory.writes)) == before
    for p, r in ((sp, sr), (ep, er)):
        assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]
        assert proof(memory, r, p.request)["request_sha256"] == p.request_sha256


def test_sip_r3b_chain_stored_content_tampering(memory):
    (sp, sr, ctx5), (ep, er, ctx6) = through_equipment(memory)
    for p, r, collection, ref in ((sp, sr, "strings", ORACLE),
                                  (ep, er, "inverters", CONFIG["id"])):
        assert proof(memory, r, p)["graph_sha256"] == r["graph_sha256"]
        replacement = ref.rsplit(":", 1)[0] + ":00000000-0000-4000-8000-0000000000aa"

        def timestamp(intake, collection=collection):
            intake["solar_design_graph"][collection][0]["provenance"]["created_at"] = "2026-01-02T00:00:00Z"
            return intake

        def identity(intake, ref=ref, replacement=replacement):
            text = json.dumps(intake)
            assert ref in text
            return json.loads(text.replace(ref, replacement))

        def unrelated(intake):
            intake["custom"]["keep"].append(99)
            return intake

        for change in (timestamp, identity, unrelated):
            forged, restore = forge(memory, r, change)
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, r, p))
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, forged, p))
            restore()
        assert proof(memory, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3b_chain_manual_power_boundary(memory):
    _, (_, er, ctx6) = through_equipment(memory)
    g = ctx6.graph
    assert er["after_rev"] == g["rev"] == 6
    assert tuple(len(g[name]) for name in ("panels", "strings", "inverters", "routes", "schedules")) == (
        3, 1, 1, 0, 0)
    for entity in (g["inverters"][0], g["strings"][0]):
        assert entity["validity"]["state"] == "invalid" and entity["validity"]["reasons"] == UNKNOWN
    assert g["settings"]["extra"]["string_sizing"]["records"] == {}
    before = len(memory.versions), memory.head
    refused("EQUIPMENT_ASSIGNMENT_REQUIRED", lambda: prepare(
        memory, "solar-homeruns", {"expected_rev": 6}, ctx6))
    refused("ROUTING_TOPOLOGY_REQUIRED", lambda: prepare(
        memory, "solar-schedule", {"expected_rev": 6, "insertion_point": [0, 0, 0]}, ctx6))
    assert (len(memory.versions), memory.head) == before


def test_sip_r3b_chain_refusals(memory, case):
    ctx4 = rev4(memory)
    for operation in ("add", "multi-add", None, 5, ["add-string"]):
        params = {key: value for key, value in S.items() if key != "operation"}
        if operation is not None:
            params["operation"] = operation
        refused("INVALID_STRING_ADD_REQUEST", lambda: prepare(memory, STRING, params, ctx4))
    for extra in INJECTED:
        refused("INVALID_STRING_ADD_REQUEST", lambda: prepare(memory, STRING, {**S, **extra}, ctx4))
    longest = local._load_builtin(STRING).max_string_length(ctx4.graph)
    too_long = PANELS + ["leaf:panel:00000000-0000-4000-8000-%012d" % i for i in range(longest)]
    for code, refs, rev in (
            ("DUPLICATE_PANEL_MEMBERSHIP", [PANELS[0], PANELS[0]], 4),
            ("MISSING_PANEL", [PANELS[0], "leaf:panel:00000000-0000-4000-8000-000000000000"], 4),
            ("STRING_TOO_LONG", too_long[:longest + 1], 4),
            ("STALE_GRAPH_REVISION", PANELS, 3)):
        refused(code, lambda: prepare(memory, STRING,
                                      {**S, "expected_rev": rev, "ordered_panel_refs": refs}, ctx4))
    _, _, ctx5 = commit(memory, STRING, S, ctx4)
    published = len(memory.versions), memory.head
    refused("PANEL_ALREADY_ASSIGNED", lambda: prepare(
        memory, STRING, {**S, "expected_rev": 5, "ordered_panel_refs": PANELS[:1]}, ctx5))
    malformed = (
        ("INVALID_EQUIPMENT_CONFIGURATION", E(equipment=[{**CONFIG, "number": 0}])),
        ("INVALID_EQUIPMENT_CONFIGURATION", E(equipment=[{**CONFIG, "extra": 1}])),
        ("INVALID_EQUIPMENT_ASSIGNMENT", E(assignments=[
            {key: value for key, value in ASSIGNMENT.items() if key != "input_number"}])),
        ("INVALID_EQUIPMENT_ASSIGNMENT", E(assignments=[
            {**ASSIGNMENT, "inverter_ref": "leaf:inverter:00000000-0000-4000-8000-0000000000bb"}])),
        ("DUPLICATE_EQUIPMENT", E(equipment=[CONFIG, {**CONFIG, "number": 2}])),
        ("DUPLICATE_EQUIPMENT", E(equipment=[CONFIG, empty(1)])),
        ("EQUIPMENT_PREVIEW_UNSUPPORTED", {**E(), "preview": True}),
        ("GRAPH_COMMIT_CANCELLED", {**E(), "cancel": True}))
    for code, params in malformed:
        refused(code, lambda: prepare(memory, EQUIPMENT, params, ctx5))
    for extra in INJECTED:
        refused("INVALID_EQUIPMENT_REQUEST", lambda: prepare(memory, EQUIPMENT, {**E(), **extra}, ctx5))
    assert (len(memory.versions), memory.head) == published
    # A placed inverter's transform stays immutable on the canonical path.
    placed = assigned_parent(memory, case)
    configs = [{key: item[key] for key in FIELDS} for item in placed.graph["inverters"]]
    configs[0]["position"] = [9, 9, 9]
    refused("EQUIPMENT_TRANSFORM_EDIT_UNSUPPORTED", lambda: prepare(memory, EQUIPMENT, {
        "expected_rev": placed.graph["rev"], "equipment": configs,
        "assignments": placed.graph["extra"]["equipment"]["assignment_requests"]}, placed))
