"""R3e-2 canonical grouping: preparation, custody, reconstruction and full chain.

The SQL/blob boundary below is an in-memory double. PostgreSQL settlement is
proved separately in platform/tests/test_sip_r3e_groups.py.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import deps
import entitlements
import platform_link
import product_capability_availability as availability
import solar_design_graph as design
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
import solar_project_jobs as service
import solar_project_sizing as sizing
import solar_sizing_client as cloud
import solar_tools
from leaf_platform import canonical_jobs as jobs
import test_sip_r3a_graph as a3
import test_sip_r3b_tools as b3
import test_sip_r3b_chain as chain
import test_sip_r3c_multi_add as multi
import test_sip_r4_jobs as r4
import test_sip_r5_policy as policy
from test_sip_r14_worker import World
import test_sip_r14_admission as admission
import test_w1_design_graph as w1
from test_sip_r14_evidence import SMALL

TOOL = "solar-panel-groups"
TWELVE = frozenset((
    "solar-settings", "solar-panels-from-drawing", "solar-size-strings",
    "solar-combiners", "solar-feeders", "solar-homeruns", "solar-schedule",
    "solar-string-add", "solar-assign-equipment", "solar-string-multi-add",
    "solar-string-conductors", "solar-panel-groups", "solar-correct-string", "solar-solve-proposal",
))
CREATING = {"solar-string-add", "solar-assign-equipment",
            "solar-string-multi-add", "solar-panel-groups"}


def source():
    value = deepcopy(a3.INTAKE)
    value["polylines"] = [
        {"layer": "Panels", "closed": True, "handle": format(26 + i, "X"),
         "pts": [[77*i, 0], [77*i+77, 0], [77*i+77, 38.5], [77*i, 38.5]]}
        for i in range(10)]
    return value


def seed_params(ctx):
    return {"expected_rev": 0,
        "changes": {"panels_in_sequence": 10, "num_mppt": 1, "panel_layer_contains": "Panel"},
        "project_changes": {"name": "Canonical Roof", "zip_code": "44224"},
        "initialize": {"schema_version": 1, "source_intake_sha256": ctx.intake_sha256,
                       "units": deepcopy(a3.UNITS)}}


def groups(ctx, split=False):
    refs = [p["id"] for p in ctx.graph["panels"]]
    return {"expected_rev": ctx.graph["rev"], "groups": [
        {"name": "Roof " + str(i), "panel_refs": part, "alignment_tolerance": .5,
         "module_width_along_row": 77, "module_height_across_row": 38.5}
        for i, part in enumerate((refs[:5], refs[5:]) if split else (refs,))]}


def derived(ctx, params, index=0, *, tool=TOOL, kind="frame"):
    # Independent frozen serialization: never call the adapter's derivation helper.
    raw = json.dumps({
        "schema": "leaf.solar-project-creation.v1",
        "organization_id": str(ctx.organization_id), "project_id": str(ctx.project_id),
        "drawing_id": str(ctx.drawing_id), "parent_version_id": str(ctx.parent_version_id),
        "parent_intake_sha256": ctx.intake_sha256, "created_at": ctx.created_at,
        "tool": tool, "parameters": params, "kind": kind, "index": index,
    }, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    return "leaf:" + kind + ":" + str(UUID(bytes=sha256(raw).digest()[:16], version=4))


def prepare(w, ctx, params=None, **changes):
    return b3.prepare(w.s, TOOL, groups(ctx) if params is None else params, ctx,
                      conn=w, **changes)


def commit(w, ctx, tool, params, *, seed=False):
    p = b3.prepare(w.s, tool, params, ctx, seed=seed, conn=w, job_id=uuid4())
    r = adapter.publish_project_graph_commit(p, actor_binding_id=w.s.actor, conn=w)
    assert adapter.project_graph_commit_provenance(str(w.s.org), r, expected=p, conn=w)[
        "output_version_id"] == r["output_version_id"]
    w.proofs.append((p, r))
    return b3.output(w.s, r)


def world(monkeypatch, *, manual=False):
    # Synthetic response bytes and explicit test-only synthetic proof acceptance.
    monkeypatch.setattr(cloud, "SIZING_CLASSIFICATION", "synthetic")
    monkeypatch.setattr(adapter, "SYNTHETIC_SIZING_PROOF_ACCEPTED", True)
    w = World(monkeypatch)
    w.proofs = []
    w.s.set_parent(source())
    ctx = adapter.resolve_project_graph_context(w.s.context())
    ctx = commit(w, ctx, "solar-settings", seed_params(ctx), seed=True)
    ctx = commit(w, ctx, "solar-panels-from-drawing", {"expected_rev": ctx.graph["rev"]})
    if manual:
        ctx = commit(w, ctx, adapter.SIZING_TOOL,
            {"expected_rev": ctx.graph["rev"], "mode": "manual-global", "confirm": True})
    else:
        w.context = ctx
        w.params = sizing.plan_sizing_request(admission.parameters(ctx.graph), ctx.graph,
            ctx.intake_sha256, b3.manifest(adapter.SIZING_TOOL))["normalized_params"]
        w.row.update(input_version_id=str(ctx.parent_version_id), params=w.params)
        w.row["execution_context"]["parent_intake_sha256"] = ctx.intake_sha256
        w.run()
        result = w.completed()
        p = service.prepare_project_graph_job(w.row, conn=w)
        assert adapter.project_graph_job_provenance(w.job_id, result, expected=p, conn=w)
        w.proofs.append((p, result))
        ctx = b3.output(w.s, result)
    assert len(ctx.graph["panels"]) == 10 and ctx.graph["frames"] == []
    # All downstream grouping work must be local, including proof reconstruction.
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    monkeypatch.setattr(cloud.requests, "post", admission.forbidden)
    monkeypatch.setattr(cloud, "post_string_length", admission.forbidden)
    return w, ctx


def altered(ctx, value):
    raw = local.canonical_bytes(value)
    return replace(ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())


def equipment_params(ctx):
    config = deepcopy(chain.CONFIG)
    return {"expected_rev": ctx.graph["rev"], "equipment": [config], "assignments": [
        {"string_ref": ctx.graph["strings"][0]["id"], "inverter_ref": config["id"],
         "mppt_letter": "A", "input_number": 0}]}


def string_params(ctx):
    return {"expected_rev": ctx.graph["rev"], "operation": "add-string",
            "ordered_panel_refs": [p["id"] for p in ctx.graph["panels"]]}


def policy_setup(w, ctx, monkeypatch):
    # Keep real preparation and the custody SQL boundary; only admission IO/insert is isolated.
    a = r4.admission.__wrapped__(w.s, monkeypatch)
    monkeypatch.setattr(platform_link.platform_db(), "run_transaction", w.transaction)
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *args:
        {"org_id": w.s.org, "project_id": w.s.project, "authority_mode": "postgres_canonical"})
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "hosted_pro")
    monkeypatch.setattr(entitlements, "resolve_roles", lambda tenant: ((), False))
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *args: {"run_write": True})
    r4.at(a, monkeypatch, ctx, TOOL)
    return a


def request(a, ctx, params, **changes):
    return policy.request(a, params, tool_name=TOOL,
                          input_version_id=ctx.parent_version_id, **changes)


def test_sip_r3e_membership(monkeypatch):
    """R3e-2-01 Exact membership, manifest identity and unsupported boundary."""
    assert adapter.SUPPORTED_TOOLS == TWELVE
    assert adapter.CREATION_TOOLS == CREATING
    assert adapter._tool(TOOL, b3.manifest(TOOL)) == solar_tools.get(TOOL)
    changed = deepcopy(solar_tools.trusted_record(TOOL))
    changed["description"] += " changed"
    a3.refused("SIP_R3_TOOL_MANIFEST_MISMATCH",
               lambda: adapter._tool(TOOL, deps.catalog_tool_digest(changed)))
    for tool in multi.UNSUPPORTED:
        a3.refused("SIP_R3_TOOL_UNSUPPORTED", lambda: adapter._tool(tool, b3.manifest(tool)))


def test_sip_r3e_preparation(monkeypatch):
    """R3e-2-02 Equal bytes, independent ordered IDs, parent time and normalization."""
    w, ctx = world(monkeypatch)
    params = groups(ctx, split=True)
    before = deepcopy((params, ctx.intake, w.s.versions, w.s.blobs))
    p = prepare(w, ctx, params)
    assert prepare(w, ctx, params) == p
    assert prepare(w, ctx, {**params, "drawing_id": str(w.s.drawing)}) == p
    assert p.request["parameters"] == params and "drawing_id" not in p.request["parameters"]
    frames = chain.created(p)["frames"]
    assert [f["id"] for f in frames] == [derived(ctx, params, i) for i in range(2)]
    assert [f["provenance"]["created_at"] for f in frames] == [ctx.created_at] * 2
    assert (params, ctx.intake, w.s.versions, w.s.blobs) == before


def test_sip_r3e_identity_inputs(monkeypatch):
    """R3e-2-03 Every creation input participates; retry authority changes only binding."""
    w, ctx = world(monkeypatch)
    params = groups(ctx, split=True)
    base = prepare(w, ctx, params)
    expected = derived(ctx, params)
    for field, value in (("organization_id", uuid4()), ("project_id", uuid4()),
            ("drawing_id", uuid4()), ("parent_version_id", uuid4()),
            ("intake_sha256", "a" * 64), ("created_at", "2026-02-03T04:05:06Z")):
        other = replace(ctx, **{field: value})
        assert adapter._creation(other, TOOL, params)["new_id"]("frame", 0, set()) == derived(other, params)
        assert derived(other, params) != expected
    intake = ctx.intake
    intake["custom"]["keep"].append(99)
    other = altered(ctx, intake)
    assert adapter._creation(other, TOOL, params)["new_id"]("frame", 0, set()) == derived(other, params) != expected
    variations = []
    for field, value in (("name", "Different"), ("alignment_tolerance", .7),
                         ("module_width_along_row", 78), ("module_height_across_row", 39)):
        changed = deepcopy(params)
        changed["groups"][0][field] = value
        variations.append(changed)
    changed = deepcopy(params)
    changed["groups"][0]["panel_refs"].reverse()
    variations += [changed, {**params, "groups": list(reversed(params["groups"]))}]
    for changed in variations:
        candidate = prepare(w, ctx, changed)
        assert chain.created(candidate)["frames"][0]["id"] == derived(ctx, changed) != expected
    for tool, kind, slot in (("solar-string-add", "frame", 0), (TOOL, "string", 0), (TOOL, "frame", 1)):
        assert adapter._creation(ctx, tool, params)["new_id"](kind, slot, set()) == derived(
            ctx, params, slot, tool=tool, kind=kind) != expected
    for changes in ({"job_id": uuid4()}, {"attempt": 2},
            {"checkout": replace(w.s.lease, holder_binding_id=uuid4())},
            {"checkout": replace(w.s.lease, fence=w.s.lease.fence + 1)}):
        p = prepare(w, ctx, params, **changes)
        assert p.request_bytes != base.request_bytes and p.output_intake_bytes == base.output_intake_bytes


def test_sip_r3e_manual_boundary(monkeypatch):
    """R3e-2-04 Actual manual sizing cannot invent grouping or equipment power."""
    w, ctx = world(monkeypatch, manual=True)
    before = deepcopy((w.s.versions, w.s.blobs, w.s.writes))
    a3.refused("MODULE_POWER_REQUIRED", lambda: prepare(w, ctx))
    assert (w.s.versions, w.s.blobs, w.s.writes) == before
    ctx = commit(w, ctx, "solar-string-add", string_params(ctx))
    ctx = commit(w, ctx, "solar-assign-equipment", equipment_params(ctx))
    assert ctx.graph["frames"] == []
    assert ctx.graph["inverters"][0]["validity"]["reasons"] == chain.UNKNOWN
    a3.refused("EQUIPMENT_ASSIGNMENT_REQUIRED", lambda: b3.prepare(w.s, "solar-homeruns",
        {"expected_rev": ctx.graph["rev"]}, ctx, conn=w))


def test_sip_r3e_custody(monkeypatch):
    """R3e-2-05 Embedded sizing alone, changed scope/evidence or unproved publication refuses."""
    w, ctx = world(monkeypatch)
    assert prepare(w, ctx)
    before = deepcopy((w.s.versions, w.s.blobs))
    value = ctx.intake
    del value["solar_design_graph"]["settings"]["extra"]["string_sizing"]["project_exchange"]
    value["solar_design_graph_sha256"] = local.digest(value["solar_design_graph"])
    a3.refused("SIP_R3_SERVICE_EVIDENCE_REQUIRED", lambda: prepare(w, altered(ctx, value)))
    for target, key, bad in ((w.row, "project_id", str(uuid4())),
            (w.exchange, "evidence_sha256", "0" * 64), (w.row, "status", "running")):
        old = target[key]
        target[key] = bad
        try:
            a3.refused("SIP_R3_PROOF_REJECTED", lambda: prepare(w, ctx))
        finally:
            target[key] = old
    assert (w.s.versions, w.s.blobs) == before


def test_sip_r3e_builtin_refusals(monkeypatch):
    """R3e-2-06 Builtin refusal codes and both allocation collisions remain atomic."""
    w, ctx = world(monkeypatch)
    params = groups(ctx)
    cases = [("STALE_GRAPH_REVISION", {**params, "expected_rev": 999}),
        ("GRAPH_COMMIT_CANCELLED", {**params, "cancel": True}),
        ("INVALID_GROUP_REQUEST", {**params, "_creation": {}})]
    duplicate = deepcopy(params)
    duplicate["groups"][0]["panel_refs"][1] = duplicate["groups"][0]["panel_refs"][0]
    missing = deepcopy(params)
    missing["groups"][0]["panel_refs"][1] = "leaf:panel:" + str(uuid4())
    malformed = deepcopy(params)
    malformed["groups"][0]["surprise"] = True
    collision = deepcopy(params)
    collision["groups"][0]["alignment_tolerance"] = 1000000
    cases += [("INVALID_GROUP_COVERAGE", duplicate), ("MISSING_PANEL", missing),
              ("INVALID_GROUP_REQUEST", malformed), ("GROUP_MATRIX_COLLISION", collision)]
    before = deepcopy((ctx.intake, w.s.versions, w.s.blobs, w.s.writes))
    for code, value in cases:
        a3.refused(code, lambda: prepare(w, ctx, value))
    for ref, slots in ((ctx.graph["panels"][0]["id"], [0]),
                       ("leaf:frame:" + str(uuid4()), [0, 1])):
        seen = []
        def collide(*args):
            seen.append(args[-1])
            return ref
        with monkeypatch.context() as patch:
            patch.setattr(adapter, "_creation_id", collide)
            a3.refused("DUPLICATE_APPLICATION_ID", lambda: prepare(w, ctx, groups(ctx, split=True)))
        assert seen == slots
    assert (ctx.intake, w.s.versions, w.s.blobs, w.s.writes) == before


def test_sip_r3e_admission(monkeypatch):
    """R3e-2-07 Preview and submission run real preparation before entitlement and insert."""
    w, ctx = world(monkeypatch)
    a = policy_setup(w, ctx, monkeypatch)
    params = {**groups(ctx), "drawing_id": str(w.s.drawing)}
    before = deepcopy((w.s.versions, w.s.blobs, w.s.writes))
    assert request(a, ctx, params)["status_code"] == 200
    assert not a.accepted
    assert request(a, ctx, params, preview=False)["status_code"] == 202
    assert len(a.accepted) == 1 and a.accepted[0][4] == groups(ctx)
    assert prepare(w, ctx, params, job_id=UUID(int=0)).output_intake_bytes == prepare(w, ctx, params).output_intake_bytes
    assert (w.s.versions, w.s.blobs, w.s.writes) == before
    bad = deepcopy(params)
    bad["groups"][0]["surprise"] = True
    counts = len(a.accepted), a.events.count("entitlement"), a.events.count("insert")
    policy.assert_refusal(request(a, ctx, bad, preview=False), "INVALID_GROUP_REQUEST")
    assert (len(a.accepted), a.events.count("entitlement"), a.events.count("insert")) == counts


def test_sip_r3e_historical_replay(monkeypatch):
    """R3e-2-08 Historical publication replay performs no content IO or fresh preparation."""
    w, ctx = world(monkeypatch)
    p = prepare(w, ctx)
    r = adapter.publish_project_graph_commit(p, actor_binding_id=w.s.actor, conn=w)
    later = b3.output(w.s, r)
    commit(w, later, "solar-string-add", string_params(later))
    w.s.lease = replace(w.s.lease, expires_at=a3.NOW)
    before = len(w.s.versions), list(w.s.reads), list(w.s.writes)
    with monkeypatch.context() as patch:
        for obj, attr in ((w.s, "get"), (w.s, "put_if_absent_or_verify"),
                          (local, "_load_builtin"), (adapter, "_reprepare"),
                          (project, "verify_at_admission")):
            patch.setattr(obj, attr, admission.forbidden)
        assert adapter.publish_project_graph_commit(p, actor_binding_id=w.s.actor, conn=w) == r
    assert (len(w.s.versions), w.s.reads, w.s.writes) == before


def test_sip_r3e_stored_proof(monkeypatch):
    """R3e-2-09 Fully rehashed stored mutations fail with original and forged receipts."""
    w, ctx = world(monkeypatch)
    p = prepare(w, ctx)
    r = adapter.publish_project_graph_commit(p, actor_binding_id=w.s.actor, conn=w)
    assert adapter.project_graph_commit_provenance(str(w.s.org), r, expected=p, conn=w)
    def change(value, field):
        g = value["solar_design_graph"]
        f = g["frames"][0]
        if field == "id":
            old, f["id"] = f["id"], "leaf:frame:" + str(uuid4())
            for panel in g["panels"]:
                if panel["frame_ref"] == old:
                    panel["frame_ref"] = f["id"]
        elif field == "time":
            f["provenance"]["created_at"] = "2026-02-03T04:05:06Z"
        elif field == "power":
            f["module_power_watts"] += 1
        elif field == "matrix":
            f["matrix"][0][0]["x"] += 1
        elif field == "membership":
            f["panel_refs"].reverse()
        else:
            value["custom"]["keep"].append(99)
        return value
    for field in ("id", "time", "power", "matrix", "membership", "unrelated"):
        forged, restore = chain.forge(w.s, r, lambda value: change(value, field))
        try:
            for receipt in (r, forged):
                a3.refused("SIP_R3_PROOF_REJECTED", lambda: adapter.project_graph_commit_provenance(
                    str(w.s.org), receipt, expected=p, conn=w))
        finally:
            restore()


def test_sip_r3e_job_binding(monkeypatch):
    """R3e-2-10 Worker preparation and terminal proof bind every stored job identity."""
    w, ctx = world(monkeypatch)
    p = prepare(w, ctx)
    row = r4.record(w.s, p)
    w.rows[p.request["job_id"]] = row
    assert service.prepare_project_graph_job(row, conn=w) == p
    r = adapter.publish_project_graph_commit(p, actor_binding_id=w.s.actor, conn=w)
    assert adapter.project_graph_job_provenance(w.s.job, r, expected=p, conn=w)
    changes = [("params", {**row["params"], "expected_rev": 99}),
        ("input_version_id", str(uuid4())), ("job_id", str(uuid4())), ("attempt", 2)]
    for field, value in (("tool_manifest_sha256", "0" * 64), ("actor_binding_id", str(uuid4())),
                         ("checkout_fence", "99"), ("parent_intake_sha256", "0" * 64)):
        changes.append(("execution_context", {**row["execution_context"], field: value}))
    for field, value in changes:
        bad = deepcopy(row)
        bad[field] = value
        # _validate_graph_request is the shared stored-request binding contract.
        a3.refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: jobs._validate_graph_request(bad, p.request))
        w.rows[p.request["job_id"]] = bad
        a3.refused("SIP_R4_JOB_BINDING_MISMATCH", lambda:
            adapter.project_graph_job_provenance(w.s.job, r, expected=p, conn=w))
    w.rows[p.request["job_id"]] = row


def test_sip_r3e_readiness(monkeypatch):
    """R3e-2-11 Project admission enforces custody; standalone readiness pins stay intact."""
    w, ctx = world(monkeypatch)
    a = policy_setup(w, ctx, monkeypatch)
    assert request(a, ctx, groups(ctx))["status_code"] == 200
    assert solar_tools.get(TOOL)["readiness"] == {"kind": "w1-chain"}
    assert availability.w1_graph_readiness(ctx.graph)[TOOL]["input_ready"] is True
    old = w.exchange["evidence_sha256"]
    w.exchange["evidence_sha256"] = "0" * 64
    policy.assert_refusal(request(a, ctx, groups(ctx)), "SIP_R3_PROOF_REJECTED",
                          status=500, message="Project Solar admission unavailable.")
    w.exchange["evidence_sha256"] = old
    with monkeypatch.context() as patch:
        manual, parent = world(patch, manual=True)
        manual_a = policy_setup(manual, parent, patch)
        policy.assert_refusal(request(manual_a, parent, groups(parent)), "MODULE_POWER_REQUIRED")
    baseline = w1.graph.__wrapped__()
    reasons = {name: row["input_reason"] for name, row in availability.w1_graph_readiness(baseline).items()}
    assert local.digest(reasons) == "b823899eccecf01a72b2c335dce5c64dc2714bd2943b6cd2aa9c503ba6045b8c"
    ground = deepcopy(ctx.graph)
    ground["project"]["installation_design"] = "Ground"
    assert availability.w1_graph_readiness(ground)[TOOL]["input_ready"] is False


def test_sip_r3e_downstream_chain(monkeypatch):
    """R3e-2-12 Nine proved publications close the frame and downstream output gap."""
    w, ctx = world(monkeypatch)
    params = groups(ctx)
    frame_id = derived(ctx, params)
    ctx = commit(w, ctx, TOOL, params)
    ctx = commit(w, ctx, "solar-string-add", string_params(ctx))
    ctx = commit(w, ctx, "solar-assign-equipment", equipment_params(ctx))
    ctx = commit(w, ctx, "solar-string-conductors", {"expected_rev": ctx.graph["rev"],
        "operation": "set-conductors", "assignments": [
            {"string_ref": ctx.graph["strings"][0]["id"], "wire_gauge": "10 AWG"}]})
    ctx = commit(w, ctx, "solar-homeruns", {"expected_rev": ctx.graph["rev"]})
    ctx = commit(w, ctx, "solar-schedule", {"expected_rev": ctx.graph["rev"], "insertion_point": [0, 0, 0]})
    g = ctx.graph
    assert tuple(len(g[k]) for k in ("frames", "panels", "strings", "inverters", "routes", "schedules")) == (1, 10, 1, 1, 2, 1)
    assert g["inverters"][0]["validity"]["state"] == "valid"
    assert g["frames"][0]["id"] == frame_id
    assert {p["frame_ref"] for p in g["panels"]} == {frame_id}
    assert g["frames"][0]["module_power_watts"] == json.loads(w.exchange["response_bytes"])["pmp"] == 400.0
    assert g["strings"][0]["wire_gauge"] == "10 AWG"
    assert (len(w.s.versions), len(w.proofs), len(w.exchanges)) == (10, 9, 1)
