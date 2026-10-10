"""Canonical Solar tools: deterministic preparation, publication, replay and proof."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest

import deps
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
import solar_sizing_client as sizing
import solar_tools
import test_sip_r3a_graph as r3a
from test_sip_r3a_graph import Memory, INTAKE, UNITS, publish, proof, refused
from test_w1_design_graph import graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401

TOOLS = {"solar-settings", "solar-panels-from-drawing", "solar-size-strings",
         "solar-combiners", "solar-feeders", "solar-homeruns", "solar-schedule",
         "solar-string-add", "solar-assign-equipment"}
HASHES = (
    "c9b8f4d3b108024f0ca3d889282fc23df485aca605c4b675a2ca2ffc9e826004",
    "76637c9607c2e780a91070d2e928c234092a0ef09b185aeaffe2381f1d5ae489",
    "53ab57c100a67be2f30688763a6dda13af3a08d6ebbb1c48e33a16028df2d853",
    "34b08719dbbe25e80459a6d705945d8ae4220c35772c78c8feeecafc248dcab1")
COMBINERS_SHA = "2bf76c5170333bfae0c78f48c958947b67c14223662b0db51806795257abaa3f"
FEEDERS_SHA = "3b65cdf69075dc2eb1beda469c785e930165ae7e8162511fcfff079be49aaa7b"


def panel_intake():
    source = deepcopy(INTAKE)
    source["polylines"] = [
        {"layer": "Panels", "closed": True, "handle": format(26 + i, "X"),
         "pts": [[100*i, 0], [100*i+77, 0], [100*i+77, 38.5], [100*i, 38.5]]}
        for i in range(3)]
    return source


def manifest(tool):
    return deps.catalog_tool_digest(solar_tools.trusted_record(tool))


@pytest.fixture
def memory(monkeypatch):
    ids = iter(UUID(int=i) for i in range(1, 10000))
    monkeypatch.setattr(r3a, "uuid4", lambda: next(ids))
    s = Memory()
    monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant", lambda tenant: s)
    monkeypatch.setattr(project.graph_store(), "resolve_version_binding", s.resolve)
    monkeypatch.setattr(project.graph_store(), "publish_version", s.publish)
    monkeypatch.setattr(project, "_access", lambda tenant, proj, write: (s.org, s.actor))
    return s


def prepare(s, tool, params, context=None, *, seed=False, **changes):
    args = dict(checkout=s.lease, job_id=s.job, attempt=1,
                tool_manifest_sha256=manifest(tool))
    args.update(changes)
    ctx = context if context is not None else s.context()
    if seed:
        return adapter.prepare_project_graph_seed(ctx, params, **args)
    return adapter.prepare_project_graph_commit(ctx, tool, params, **args)


def output(s, result):
    return adapter.resolve_project_graph_context(s.context(UUID(result["output_version_id"])))


def commit(s, tool, params, context=None, *, seed=False):
    p = prepare(s, tool, params, context, seed=seed)
    r = publish(s, p)
    return p, r, output(s, r)


def chain(s, steps=4):
    s.set_parent(panel_intake())
    ctx = adapter.resolve_project_graph_context(s.context())
    rows = []
    stages = [
        ("solar-settings", {"expected_rev": 0, "changes": {"panels_in_sequence": 3},
         "initialize": {"schema_version": 1, "source_intake_sha256": ctx.intake_sha256,
                        "units": deepcopy(UNITS)}}),
        ("solar-settings", {"expected_rev": 1,
         "changes": {"num_mppt": 2, "panel_layer_contains": "Panel"}}),
        ("solar-panels-from-drawing", {"expected_rev": 2}),
        ("solar-size-strings", {"expected_rev": 3, "mode": "manual-global", "confirm": True})]
    for i, (tool, params) in enumerate(stages[:steps]):
        row = commit(s, tool, params, ctx, seed=i == 0)
        rows.append(row)
        ctx = row[2]
    return rows


def embedded(graph, **source):
    return {**deepcopy(INTAKE), **deepcopy(source), "solar_design_graph": deepcopy(graph),
            "solar_design_graph_sha256": local.digest(graph)}


def combiner_parent(s):
    import test_solar_tool_combiners as tc
    g, intake, groups = tc.i4()
    s.set_parent(embedded(g, combiner_intake=intake, panel_groups=groups))
    return adapter.resolve_project_graph_context(s.context()), tc.request(0)


def assigned_parent(s, case):
    g, params, intake = case
    assigned = equipment.assign_equipment(g, params, drawing_intake=intake,
                                          licensed_equipment=licensed)["graph"]
    s.set_parent(embedded(assigned, **intake))
    return adapter.resolve_project_graph_context(s.context())


def direct(ctx, tool, params):
    trusted = {}
    if "source_intake" in solar_tools.get(tool)["trusted_inputs"]:
        trusted["source_intake"] = {k: v for k, v in ctx.intake.items()
            if k not in ("solar_design_graph", "solar_design_graph_sha256")}
    return local._load_builtin(tool).run(deepcopy(ctx.graph), deepcopy(params), **trusted)


def forbidden(*args, **kwargs):
    pytest.fail("unexpected content IO, builtin or outbound execution")


def test_sip_r3b_membership(memory):
    assert adapter.SUPPORTED_TOOLS == TOOLS
    for tool in ("solar-commit-solve", "solar-trackers-to-panel-groups", "solar-solaredge-accept",
                 "solar-pvcase-convert", "solar-pvcase-solve", "solar-pvcase-export"):
        refused("SIP_R3_TOOL_UNSUPPORTED", lambda: prepare(memory, tool, {}))


def test_sip_r3b_manifest(memory):
    for tool in TOOLS - {"solar-settings"}:
        refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: prepare(
            memory, tool, {}, tool_manifest_sha256="sha256:" + "0"*64))


def test_sip_r3b_seed_settings(memory):
    assert (memory.org, memory.drawing, memory.job) == (UUID(int=1), UUID(int=3), UUID(int=5))
    assert local.digest(panel_intake()) == "a64a44cee31ab4be712a12a39815e20126f197b9655b9c12c60ffe491e44e91e"
    for i, (_, result, ctx) in enumerate(chain(memory, 2)):
        assert ctx.graph["rev"] == result["after_rev"] == i + 1
        assert ctx.graph_sha256 == HASHES[i]


def test_sip_r3b_panels(memory):
    _, result, ctx = chain(memory, 3)[-1]
    assert ctx.graph["rev"] == result["after_rev"] == 3
    assert ctx.graph_sha256 == HASHES[2]
    assert len(ctx.graph["panels"]) == 3
    assert {k: v for k, v in ctx.intake.items()
            if k not in ("solar_design_graph", "solar_design_graph_sha256")} == panel_intake()


def test_sip_r3b_source_digest(memory, monkeypatch):
    ctx = chain(memory, 2)[-1][2]
    monkeypatch.setattr(local, "_load_builtin", forbidden)
    for code, half in (("GRAPH_DIGEST_MISMATCH", False), ("INVALID_SEED_PARENT", True)):
        intake = ctx.intake
        if half:
            del intake["solar_design_graph_sha256"]
        else:
            intake["solar_design_graph_sha256"] = "0"*64
        memory.set_parent(intake)
        refused(code, lambda: prepare(memory, "solar-panels-from-drawing", {"expected_rev": 2}))


def test_sip_r3b_source_isolation(memory, monkeypatch):
    ctx = chain(memory, 2)[-1][2]
    builtin = local._load_builtin("solar-panels-from-drawing")
    run, seen = builtin.run, []
    def spy(g, params, **trusted):
        seen.append(deepcopy(trusted))
        return run(g, params, **trusted)
    monkeypatch.setattr(builtin, "run", spy)
    monkeypatch.setattr(memory, "context", forbidden)
    monkeypatch.setattr(memory, "get", forbidden)
    for module, names in ((local, ("_source_intake", "_resolve_trusted")),
                          (local.store, ("resolve_version_entry", "load_manifest")),
                          (local.write_loop, ("read_intake", "intake_view"))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    prepare(memory, "solar-panels-from-drawing", {"expected_rev": 2}, ctx)
    assert seen == [{"source_intake": panel_intake()}]


def test_sip_r3b_source_override(memory):
    ctx = chain(memory, 2)[-1][2]
    refused("INVALID_PANEL_IMPORT_REQUEST", lambda: prepare(memory, "solar-panels-from-drawing",
        {"expected_rev": 2, "source_intake": panel_intake()}, ctx))


def test_sip_r3b_manual_sizing(memory):
    p, result, ctx = chain(memory)[-1]
    assert ctx.graph["rev"] == result["after_rev"] == 4
    assert ctx.graph_sha256 == HASHES[3]
    assert len(ctx.graph["panels"]) == 3 and ctx.graph["strings"] == []
    assert ctx.graph["settings"]["voc_cold"] == sizing.manual_voltage()
    assert ctx.graph["settings"]["extra"]["string_sizing"]["records"] == {}
    assert proof(memory, result, p)["graph_sha256"] == HASHES[3]


def test_sip_r3b_sizing_refusals(memory):
    ctx = chain(memory, 3)[-1][2]
    for mode in ("global", "zones", "other", None):
        params = {"expected_rev": 3, "confirm": True}
        if mode is not None:
            params["mode"] = mode
        refused("SIP_R3_SERVICE_EVIDENCE_REQUIRED", lambda: prepare(
            memory, "solar-size-strings", params, ctx))
    for code, params in (
        ("INVALID_SIZING_REQUEST", {"expected_rev": 3, "mode": "manual-global"}),
        ("GRAPH_COMMIT_CANCELLED", {"expected_rev": 3, "mode": "manual-global", "cancel": True}),
        ("STALE_GRAPH_REVISION", {"expected_rev": 2, "mode": "manual-global", "confirm": True})):
        refused(code, lambda: prepare(memory, "solar-size-strings", params, ctx))


def test_sip_r3b_no_outbound(memory, monkeypatch):
    ctx = chain(memory, 3)[-1][2]
    params = {"expected_rev": 3, "mode": "manual-global", "confirm": True}
    for name in ("size", "post_string_length", "resolve_grant"):
        monkeypatch.setattr(sizing, name, forbidden)
    monkeypatch.setattr(sizing.requests.sessions.Session, "request", forbidden)
    p = prepare(memory, "solar-size-strings", params, ctx)
    assert prepare(memory, "solar-size-strings", params, ctx) == p
    result = publish(memory, p)
    assert proof(memory, result, p)["graph_sha256"] == HASHES[3]
    with monkeypatch.context() as patch:
        patch.setattr(local, "_load_builtin", forbidden)
        refused("SIP_R3_SERVICE_EVIDENCE_REQUIRED", lambda: prepare(
            memory, "solar-size-strings", {**params, "mode": "global"}, ctx))


def test_sip_r3b_sizing_identity(memory, monkeypatch):
    ctx = chain(memory, 3)[-1][2]
    builtin = local._load_builtin("solar-size-strings")
    run_bound, seen = builtin.run_bound, []
    def spy(g, params, **identity):
        seen.append(dict(identity))
        return run_bound(g, params, **identity)
    monkeypatch.setattr(builtin, "run_bound", spy)
    monkeypatch.setattr(builtin, "run", forbidden)
    prepare(memory, "solar-size-strings",
            {"expected_rev": 3, "mode": "manual-global", "confirm": True}, ctx)
    assert seen == [{"tenant_id": str(memory.org), "job_id": str(memory.job)}]
    assert all(type(v) is str for identity in seen for v in identity.values())


def test_sip_r3b_combiners(memory):
    import test_solar_tool_combiners as tc
    ctx, params = combiner_parent(memory)
    expected = direct(ctx, "solar-combiners", params)
    _, r, out = commit(memory, "solar-combiners", params, ctx)
    assert out.graph == expected
    assert r["after_rev"] == 1 and out.graph_sha256 == COMBINERS_SHA == tc.C5_TOOL_SHA
    assert len(out.graph["inverters"]) == 22 and len(out.graph["routes"]) == 360


def test_sip_r3b_feeders(memory):
    ctx, params = combiner_parent(memory)
    ctx = commit(memory, "solar-combiners", params, ctx)[2]
    params = {"expected_rev": 1}
    expected = direct(ctx, "solar-feeders", params)
    _, r, out = commit(memory, "solar-feeders", params, ctx)
    assert out.graph == expected and out.graph_sha256 == FEEDERS_SHA
    assert r["after_rev"] == 2


def test_sip_r3b_homeruns(memory, case):
    ctx = assigned_parent(memory, case)
    params = {"expected_rev": ctx.graph["rev"]}
    expected = direct(ctx, "solar-homeruns", params)
    p = prepare(memory, "solar-homeruns", params, ctx)
    assert prepare(memory, "solar-homeruns", params, ctx) == p
    r = publish(memory, p)
    assert output(memory, r).graph == expected
    assert r["after_rev"] == ctx.graph["rev"] + 1


def test_sip_r3b_schedule(memory, case):
    ctx = assigned_parent(memory, case)
    missing = ctx.intake
    missing["solar_design_graph"]["routes"] = []
    missing["solar_design_graph_sha256"] = local.digest(missing["solar_design_graph"])
    memory.set_parent(missing)
    refused("COMPLETE_ROUTING_REQUIRED", lambda: prepare(memory, "solar-schedule",
        {"expected_rev": ctx.graph["rev"], "insertion_point": [0, 0, 0]}))
    memory.set_parent(ctx.intake)
    ctx = commit(memory, "solar-homeruns", {"expected_rev": ctx.graph["rev"]})[2]
    params = {"expected_rev": ctx.graph["rev"], "insertion_point": [0, 0, 0]}
    expected = direct(ctx, "solar-schedule", params)
    _, r, out = commit(memory, "solar-schedule", params, ctx)
    assert out.graph == expected and r["after_rev"] == ctx.graph["rev"] + 1


def test_sip_r3b_replay(memory, monkeypatch):
    rows = chain(memory)[2:]
    ctx, params = combiner_parent(memory)
    row = commit(memory, "solar-combiners", params, ctx)
    rows.append(row)
    rows.append(commit(memory, "solar-feeders", {"expected_rev": 1}, row[2]))
    before = len(memory.versions), len(memory.reads), len(memory.writes)
    monkeypatch.setattr(memory, "get", forbidden)
    monkeypatch.setattr(memory, "put_if_absent_or_verify", forbidden)
    monkeypatch.setattr(local, "_load_builtin", forbidden)
    for p, r, _ in rows:
        assert publish(memory, p) == r
    assert (len(memory.versions), len(memory.reads), len(memory.writes)) == before


def test_sip_r3b_request_binding(memory):
    ctx = chain(memory, 2)[-1][2]
    params = {"expected_rev": 2, "drawing_id": str(memory.drawing)}
    p = prepare(memory, "solar-panels-from-drawing", params, ctx)
    assert p.request["tool"] == "solar-panels-from-drawing"
    assert p.request["parameters"] == {"expected_rev": 2}
    assert p.request["job_id"] == str(memory.job)
    assert p.request["attempt"] == 1
    assert p.request["checkout_fence"] == str(memory.lease.fence)
    assert p.request["parent_version_id"] == str(ctx.parent_version_id)
    assert prepare(memory, "solar-panels-from-drawing",
        {**params, "cancel": False}, ctx).request_sha256 != p.request_sha256
    assert prepare(memory, "solar-panels-from-drawing", params, ctx,
        job_id=UUID(int=999)).request_sha256 != p.request_sha256
    assert prepare(memory, "solar-panels-from-drawing", params, ctx,
        attempt=2).request_sha256 != p.request_sha256
    assert prepare(memory, "solar-panels-from-drawing", params, ctx,
        checkout=replace(memory.lease, fence=8)).request_sha256 != p.request_sha256
    assert prepare(memory, "solar-panels-from-drawing", params,
        replace(ctx, parent_version_id=UUID(int=998))).request_sha256 != p.request_sha256
    changes = {"tool": "solar-feeders", "parameters": {"expected_rev": 1},
               "job_id": str(UUID(int=999)), "attempt": 2, "checkout_fence": "8",
               "parent_version_id": str(UUID(int=998))}
    for field, value in changes.items():
        request = p.request
        request[field] = value
        assert sha256(local.canonical_bytes(request)).hexdigest() != p.request_sha256


def test_sip_r3b_proof_tampering(memory):
    rows = [chain(memory)[-1]]
    ctx, params = combiner_parent(memory)
    rows.append(commit(memory, "solar-combiners", params, ctx))
    for p, r, _ in rows:
        assert proof(memory, r, p)["request_id"] == r["request_id"]
        for field, value in (("after_rev", r["after_rev"] + 1),
                             ("job_id", str(UUID(int=999))), ("graph_sha256", "0"*64)):
            changed = {**r, field: value}
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, changed, p))
        version = memory.versions[UUID(r["output_version_id"])]
        original = deepcopy(version.__dict__)
        original_raw = memory.blobs[version.intake_ref]
        for graph_change in (False, True):
            intake = json.loads(original_raw)
            if graph_change:
                intake["solar_design_graph"]["settings"]["num_mppt"] = 9
                intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
            else:
                intake["custom"]["keep"].append(99)
            raw = local.canonical_bytes(intake)
            digest = sha256(raw).hexdigest()
            version.intake_ref = project.graph_store().publication_intake_key(
                memory.org, memory.project, memory.drawing, digest)
            version.provenance["intake"] = {"ref": version.intake_ref, "sha256": digest}
            memory.blobs[version.intake_ref] = raw
            memory.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, r, p))
            forged = {**r, "graph_sha256": intake["solar_design_graph_sha256"],
                      "output_intake_sha256": digest}
            refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, forged, p))
            version.__dict__.update(deepcopy(original))
        memory.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)


def test_sip_r3b_no_activation():
    assert sha256(Path(local.__file__).read_bytes()).hexdigest() == (
        "cfc20482a1bf59b54aa8f9ccb7cafeac14ce038e2bcf48f02d5bdf22586e1190")
    root = Path(local.__file__).parent
    for path in root.glob("*.py"):
        if any(part in path.stem for part in ("router", "routes", "job", "worker")):
            if path.name in {"canonical_worker.py", "solar_project_jobs.py"}:
                continue
            assert "solar_project_graph" not in path.read_text(encoding="utf-8")
    for tool in TOOLS:
        assert "canonical_only" not in solar_tools.trusted_record(tool)
