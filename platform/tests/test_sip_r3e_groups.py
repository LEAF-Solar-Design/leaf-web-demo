"""R3e-2 real PostgreSQL grouping settlement, replay, refusal and rollback proofs.

Only authentication, immutable blob storage and synthetic sizing response bytes
are fixture boundaries. Admission, preparation, exchange custody and proof run
the product implementations. Every durable scope and external subject is fresh.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from leaf_platform import db, store, project_graph_store as graph_store
from leaf_platform import canonical_jobs as jobs, project_sizing_store as exchanges
import test_sip_r3a_graph as base
import test_sip_r4_jobs as pg
import solar_project_context as project
import solar_project_graph as adapter
import solar_project_jobs as service
import solar_project_sizing as sizing
import solar_sizing_client as cloud
import solar_local_graph as local
from solar_design_graph import GraphValidationError
from leaf_cloud_grants import CloudGrant
from test_sip_r14_evidence import SMALL

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")
TOOL = "solar-panel-groups"


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected grant, network or replay IO")


def source():
    value = deepcopy(base.INTAKE)
    value["polylines"] = [
        {"layer": "Panels", "closed": True, "handle": format(26 + i, "X"),
         "pts": [[77*i, 0], [77*i+77, 0], [77*i+77, 38.5], [77*i, 38.5]]}
        for i in range(10)]
    return value


@pytest.fixture
def world(make_org, monkeypatch):
    scopes = {}
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", uuid4().hex + uuid4().hex)
    # Existing private test seams label these bytes honestly as synthetic.
    monkeypatch.setattr(cloud, "SIZING_CLASSIFICATION", "synthetic")
    monkeypatch.setattr(adapter, "SYNTHETIC_SIZING_PROOF_ACCEPTED", True)
    monkeypatch.setattr(cloud, "resolve_grant", lambda ref, tenant: CloudGrant(tenant, "fixture"))
    monkeypatch.setattr(cloud.requests, "post", forbidden)
    monkeypatch.setattr(cloud, "post_string_length", forbidden)

    def fresh():
        org = make_org("R3e " + uuid4().hex, tier="hosted_pro")
        proj = store.create_project(org.org_id, "Solar " + uuid4().hex,
                                    authority_mode="postgres_canonical")
        actors = [store.create_identity_binding(org.org_id, "auth0", "r3e-" + uuid4().hex,
                                               role="editor") for _ in range(2)]
        drawing = store.create_drawing_artifact(org.org_id, proj.project_id, "Roof " + uuid4().hex)
        key = f"tenants/{org.org_id}/drawings/{uuid4()}/v/00000001.intake.json"
        parent = store.create_drawing_version(org.org_id, proj.project_id,
            drawing_id=drawing.drawing_id, oss_object=key[:-12] + ".dwg", intake_ref=key)
        raw = local.canonical_bytes(source())
        blobs = base.Blobs()
        blobs.data[key] = raw
        provenance = {"schema": "leaf.drawing-import.v1", "source": {
            "kind": "account_upload", "tenant_id": str(org.org_id),
            "drawing_id": key.split("/")[3], "version": 1,
            "stored_object": {"ref": parent.oss_object},
            "intake": {"ref": key, "sha256": sha256(raw).hexdigest()}}}
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                        (Jsonb(provenance), org.org_id, parent.version_id))
            for actor in actors:
                cur.execute("INSERT INTO project_member_bindings "
                    "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                    "VALUES (%s, %s, %s, %s, 'editor', %s)",
                    (uuid4(), org.org_id, proj.project_id, actor.binding_id, actor.binding_id))
        lease = db.run_transaction(lambda conn: graph_store.acquire_checkout(
            org.org_id, proj.project_id, drawing.drawing_id, actor_binding_id=actors[0].binding_id,
            holder="R3e editor", ttl_s=3600, expected_fence=None, conn=conn))
        s = SimpleNamespace(org=org.org_id, project=proj.project_id, drawing=drawing.drawing_id,
            parent=parent.version_id, actor=actors[0].binding_id, other=actors[1].binding_id,
            lease=lease, blobs=blobs, proofs=[], exchanges=[])
        scopes[str(s.org)] = s
        return s

    monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant",
                        lambda tenant: scopes[str(tenant)].blobs)
    monkeypatch.setattr(project.platform_link, "resolve_caller_binding", lambda tenant:
        SimpleNamespace(platform_tenant_id=UUID(str(tenant)), binding_id=scopes[str(tenant)].actor))
    yield fresh
    for s in scopes.values():
        with db.cursor() as cur:
            cur.execute("UPDATE jobs SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL "
                        "WHERE org_id=%s AND status IN ('queued', 'running')", (s.org,))


def context(s, version=None):
    return adapter.resolve_project_graph_context(base.context(s, version))


def census(s):
    with db.cursor() as cur:
        counts = []
        for table in ("drawing_versions", "history_operations", "outbox_entries", "project_sizing_exchanges", "jobs"):
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE org_id=%s AND project_id=%s",
                        (s.org, s.project))
            counts.append(cur.fetchone()["n"])
        return tuple(counts)


def submit(s, ctx, tool, params, *, key=None, **changes):
    return pg.submit(s, tool=tool, params=params, parent=ctx.parent_version_id,
                     key=key or str(uuid4()), **changes)


def pending(s, ctx, tool, params, *, key=None):
    job = submit(s, ctx, tool, params, key=key)
    row = pg.claim(s, job, owner="r3e-" + uuid4().hex, seconds=3600)
    if adapter._global_sizing(tool, params):
        # The R3b canonical_custody sequence, with actual admission and synthetic classification.
        plan = sizing.plan_sizing_request(row["params"], ctx.graph, ctx.intake_sha256, pg.manifest(tool))
        scope, attempt = adapter._exchange_authority(row)
        exchanges.prepare_exchange(scope, attempt, request_bytes=plan["wire_bytes"],
            request_sha256=plan["wire_sha256"], grant_ref=row["params"]["grant_ref"], classification="synthetic")
        assert exchanges.begin_send(scope, attempt)
        raw = local.canonical_bytes(SMALL)
        assert exchanges.capture_response(scope, attempt, response_bytes=raw,
                                          response_sha256=sha256(raw).hexdigest()) == "stored"
        expected = adapter._sizing_expected(row, ctx, exchanges.get_exchange(scope, attempt))
        evidence = sizing.build_sizing_evidence(row["params"], ctx.graph, ctx.intake_sha256,
            pg.manifest(tool), raw, identity=expected["identity"],
            parent_version_id=str(ctx.parent_version_id), producer=expected["producer"],
            classification="synthetic")
        raw = local.canonical_bytes(evidence)
        assert exchanges.attach_evidence(scope, attempt, evidence_bytes=raw,
                                          evidence_sha256=sha256(raw).hexdigest()) == "stored"
        s.exchanges.append((scope, attempt))
    return row, pg.prepared(row)


def settled(s, row, p):
    before = census(s)
    outcome, result = pg.complete(row, p)
    assert outcome == "applied"
    assert tuple(a-b for a, b in zip(census(s), before)) == (1, 1, 1, 0, 0)
    r = result["graph_commit"]
    assert store.verify_history_operation(s.org, UUID(result["history_operation_id"]))
    with db.cursor() as cur:
        cur.execute("SELECT * FROM outbox_entries WHERE aggregate_id=%s",
                    (UUID(result["history_operation_id"]),))
        outbox = cur.fetchone()
        assert outbox["event_type"] == "history.operation.appended"
        assert outbox["payload"] == {"operationId": result["history_operation_id"], "jobId": row["job_id"]}
    assert db.run_transaction(lambda conn: adapter.project_graph_job_provenance(
        UUID(row["job_id"]), r, expected=p, conn=conn))["output_version_id"] == r["output_version_id"]
    s.proofs.append((row, p, result))
    return context(s, UUID(r["output_version_id"]))


def commit(s, ctx, tool, params):
    return settled(s, *pending(s, ctx, tool, params))


def sized(s, *, manual=False):
    ctx = context(s)
    ctx = commit(s, ctx, "solar-settings", {"expected_rev": 0,
        "changes": {"panels_in_sequence": 10, "num_mppt": 1, "panel_layer_contains": "Panel"},
        "project_changes": {"name": "Canonical Roof", "zip_code": "44224"},
        "initialize": {"schema_version": 1, "source_intake_sha256": ctx.intake_sha256,
                       "units": deepcopy(base.UNITS)}})
    ctx = commit(s, ctx, "solar-panels-from-drawing", {"expected_rev": ctx.graph["rev"]})
    params = {"expected_rev": ctx.graph["rev"], "mode": "manual-global" if manual else "global", "confirm": True}
    if not manual:
        params.update(grant_ref="g_" + uuid4().hex, requests={ctx.graph["settings"]["id"]: {
            "module_name": "M", "full_inverter_name": "I", "bifacial": False,
            "bifacial_coefficient": "0", "racking_params": {"racking_type": "fixed_tilt",
                "surface_tilt": "20", "surface_azimuth": "180", "albedo": "0.2"},
            "max_voltage": "600", "thermal_model_type": "sapm", "open_circuit_rise": False,
            "zip_code": "44224"}})
    ctx = commit(s, ctx, "solar-size-strings", params)
    assert ctx.graph["frames"] == [] and len(ctx.graph["panels"]) == 10
    return ctx


def groups(ctx):
    return {"expected_rev": ctx.graph["rev"], "groups": [{
        "name": "Roof", "panel_refs": [p["id"] for p in ctx.graph["panels"]],
        "alignment_tolerance": .5, "module_width_along_row": 77, "module_height_across_row": 38.5}]}


def strings(ctx):
    return {"expected_rev": ctx.graph["rev"], "operation": "add-string",
            "ordered_panel_refs": [p["id"] for p in ctx.graph["panels"]]}


def refused(code, operation):
    with pytest.raises((project.ProjectContextError, GraphValidationError)) as caught:
        operation()
    assert getattr(caught.value, "reason_code", getattr(caught.value, "code", None)) == code


def test_sip_r3e_pg_publication_chain(world, monkeypatch):
    """R3e-2-13 Nine real completions prove one frame and every durable settlement."""
    s = world()
    ctx = sized(s)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    params = groups(ctx)
    # Independently derive the grouping frame from the exact normalized request.
    raw = json.dumps({"schema": "leaf.solar-project-creation.v1", "organization_id": str(s.org),
        "project_id": str(s.project), "drawing_id": str(s.drawing),
        "parent_version_id": str(ctx.parent_version_id), "parent_intake_sha256": ctx.intake_sha256,
        "created_at": ctx.created_at, "tool": TOOL, "parameters": params, "kind": "frame", "index": 0},
        sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    frame_id = "leaf:frame:" + str(UUID(bytes=sha256(raw).digest()[:16], version=4))
    ctx = commit(s, ctx, TOOL, params)
    ctx = commit(s, ctx, "solar-string-add", strings(ctx))
    config = {"id": "leaf:inverter:" + str(uuid4()), "number": 1, "type_key": "INV",
        "model": "fixture", "position": [5, 0, 0], "rotation": 0, "scale": [1, 1, 1],
        "block_name": "FixtureInverter", "layer": "0", "mppt_count": 1,
        "total_dc_inputs": 1, "mppt_inputs": {"A": 1}, "max_dc_voltage": 1500,
        "max_ac_power_kw": 100, "max_dc_power_kw": 100, "is_solaredge": False}
    ctx = commit(s, ctx, "solar-assign-equipment", {"expected_rev": ctx.graph["rev"],
        "equipment": [config], "assignments": [{"string_ref": ctx.graph["strings"][0]["id"],
            "inverter_ref": config["id"], "mppt_letter": "A", "input_number": 0}]})
    ctx = commit(s, ctx, "solar-string-conductors", {"expected_rev": ctx.graph["rev"],
        "operation": "set-conductors", "assignments": [
            {"string_ref": ctx.graph["strings"][0]["id"], "wire_gauge": "10 AWG"}]})
    ctx = commit(s, ctx, "solar-homeruns", {"expected_rev": ctx.graph["rev"]})
    ctx = commit(s, ctx, "solar-schedule", {"expected_rev": ctx.graph["rev"], "insertion_point": [0, 0, 0]})
    g = ctx.graph
    assert tuple(len(g[k]) for k in ("frames", "panels", "strings", "inverters", "routes", "schedules")) == (1, 10, 1, 1, 2, 1)
    assert g["frames"][0]["id"] == frame_id
    assert {p["frame_ref"] for p in g["panels"]} == {frame_id}
    exchange = exchanges.get_exchange(*s.exchanges[0])
    assert exchange["state"] == "published"
    assert g["frames"][0]["module_power_watts"] == json.loads(exchange["response_bytes"])["pmp"] == 400.0
    assert g["inverters"][0]["validity"]["state"] == "valid"
    assert len(s.proofs) == 9 and census(s) == (10, 9, 9, 1, 9)


def test_sip_r3e_pg_historical_replay(world, monkeypatch):
    """R3e-2-14 Exact historical submission/completion replay bypasses fresh authority."""
    s = world()
    ctx = sized(s)
    params, key = groups(ctx), str(uuid4())
    row, p = pending(s, ctx, TOOL, params, key=key)
    grouped = settled(s, row, p)
    result = s.proofs[-1][2]
    commit(s, grouped, "solar-string-add", strings(grouped))
    monkeypatch.setattr(graph_store, "_clock", lambda cur: s.lease.expires_at)
    before = census(s), len(s.blobs.reads), len(s.blobs.writes)
    with monkeypatch.context() as patch:
        for obj, attr in ((project, "verify_at_admission"), (adapter, "publish_project_graph_commit"),
                (adapter, "_pending_project_graph_job_provenance"), (local, "_load_builtin"),
                (s.blobs, "get"), (s.blobs, "put_if_absent_or_verify"), (cloud, "resolve_grant")):
            patch.setattr(obj, attr, forbidden)
        assert submit(s, ctx, TOOL, params, key=key)["job_id"] == row["job_id"]
        assert pg.complete(row, p) == ("duplicate", result)
        changed = deepcopy(params)
        changed["groups"][0]["name"] = "Changed intent"
        refused("SIP_R4_IDEMPOTENCY_CONFLICT", lambda: submit(s, ctx, TOOL, changed, key=key))
    assert (census(s), len(s.blobs.reads), len(s.blobs.writes)) == before


def test_sip_r3e_pg_stale_and_fenced(world):
    """R3e-2-15 Stale revision/head, replaced checkout and lost ownership never settle."""
    for cause in ("head", "fence", "owner"):
        s = world()
        ctx = sized(s)
        before = census(s)
        refused("STALE_GRAPH_REVISION", lambda: submit(s, ctx, TOOL, {**groups(ctx), "expected_rev": 999}))
        assert census(s) == before
        row, p = pending(s, ctx, TOOL, groups(ctx))
        if cause == "head":
            commit(s, ctx, "solar-settings", {"expected_rev": ctx.graph["rev"], "changes": {"num_mppt": 2}})
        elif cause == "fence":
            db.run_transaction(lambda conn: graph_store.acquire_checkout(s.org, s.project, s.drawing,
                actor_binding_id=s.actor, holder="Replacement", ttl_s=3600,
                expected_fence=s.lease.fence, conn=conn))
        else:
            pg.expire_worker(row["job_id"])
        before = census(s)
        if cause == "owner":
            assert pg.complete(row, p) == ("not_owner", None)
        else:
            refused("SIP_R1_STALE_VERSION" if cause == "head" else "SIP_R1_CHECKOUT_STALE",
                    lambda: pg.complete(row, p))
        assert census(s) == before


def test_sip_r3e_pg_refusal_atomicity(world, monkeypatch):
    """R3e-2-16 Manual power and malformed/ungroupable requests have no durable effects."""
    manual = world()
    manual_ctx = sized(manual, manual=True)
    before = census(manual), len(manual.blobs.writes)
    refused("MODULE_POWER_REQUIRED", lambda: submit(manual, manual_ctx, TOOL, groups(manual_ctx)))
    assert (census(manual), len(manual.blobs.writes)) == before
    s = world()
    ctx = sized(s)
    params = groups(ctx)
    duplicate = deepcopy(params)
    duplicate["groups"][0]["panel_refs"][1] = duplicate["groups"][0]["panel_refs"][0]
    geometry = deepcopy(params)
    geometry["groups"][0]["alignment_tolerance"] = 1000000
    malformed = deepcopy(params)
    malformed["groups"][0]["surprise"] = True
    before = census(s), len(s.blobs.writes)
    for value, code in ((duplicate, "INVALID_GROUP_COVERAGE"), (geometry, "GROUP_MATRIX_COLLISION"),
            (malformed, "INVALID_GROUP_REQUEST"), ({**params, "_creation": {}}, "INVALID_SETTINGS_REQUEST")):
        refused(code, lambda: submit(s, ctx, TOOL, value))
        assert (census(s), len(s.blobs.writes)) == before


def test_sip_r3e_pg_custody_and_tamper(world):
    """R3e-2-17 Foreign/missing/substituted custody and consistently rehashed output fail."""
    s, foreign = world(), world()
    ctx, foreign_ctx = sized(s), sized(foreign)
    # Context byte substitutions retain valid graph digests and exercise the real database custody.
    value = ctx.intake
    original_ref = deepcopy(value["solar_design_graph"]["settings"]["extra"]["string_sizing"]["project_exchange"])
    foreign_ref = foreign_ctx.graph["settings"]["extra"]["string_sizing"]["project_exchange"]
    for replacement, code in ((None, "SIP_R3_SERVICE_EVIDENCE_REQUIRED"),
            (foreign_ref, "SIP_R3_PROOF_REJECTED"),
            ({**original_ref, "evidence_sha256": "0" * 64}, "SIP_R3_PROOF_REJECTED")):
        changed = deepcopy(value)
        evidence = changed["solar_design_graph"]["settings"]["extra"]["string_sizing"]
        if replacement is None:
            del evidence["project_exchange"]
        else:
            evidence["project_exchange"] = replacement
        changed["solar_design_graph_sha256"] = local.digest(changed["solar_design_graph"])
        raw = local.canonical_bytes(changed)
        altered = replace(ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
        before = census(s)
        refused(code, lambda: db.run_transaction(lambda conn: adapter.prepare_project_graph_commit(
            altered, TOOL, groups(ctx), checkout=s.lease, job_id=uuid4(), attempt=1,
            tool_manifest_sha256=pg.manifest(TOOL), conn=conn)))
        assert census(s) == before
    assert (s.org, s.project, s.drawing) != (foreign.org, foreign.project, foreign.drawing)
    grouped = commit(s, ctx, TOOL, groups(ctx))
    row, p, result = s.proofs[-1]
    r = result["graph_commit"]
    version = store.get_drawing_version(s.org, s.project, grouped.parent_version_id)
    for field in ("power", "identity", "unrelated"):
        changed = json.loads(s.blobs.data[version.intake_ref])
        if field == "power":
            changed["solar_design_graph"]["frames"][0]["module_power_watts"] += 1
        elif field == "identity":
            ref = changed["solar_design_graph"]["frames"][0]["id"]
            changed = json.loads(json.dumps(changed).replace(ref, "leaf:frame:" + str(uuid4())))
        else:
            changed["custom"]["keep"].append(99)
        changed["solar_design_graph_sha256"] = local.digest(changed["solar_design_graph"])
        raw = local.canonical_bytes(changed)
        digest = sha256(raw).hexdigest()
        key = graph_store.publication_intake_key(s.org, s.project, s.drawing, digest)
        s.blobs.data[key] = raw
        provenance = deepcopy(version.provenance)
        provenance["intake"] = {"ref": key, "sha256": digest}
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                "WHERE org_id=%s AND version_id=%s", (key, Jsonb(provenance),
                graph_store._publication_fingerprint(provenance), s.org, version.version_id))
        forged = {**r, "graph_sha256": changed["solar_design_graph_sha256"], "output_intake_sha256": digest}
        for receipt in (r, forged):
            refused("SIP_R3_PROOF_REJECTED", lambda: db.run_transaction(lambda conn:
                adapter.project_graph_job_provenance(UUID(row["job_id"]), receipt, expected=p, conn=conn)))
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                "WHERE org_id=%s AND version_id=%s", (version.intake_ref, Jsonb(version.provenance),
                graph_store._publication_fingerprint(version.provenance), s.org, version.version_id))


def test_sip_r3e_pg_rollback_reclaim(world, monkeypatch):
    """R3e-2-18 Roll back after outbox; immutable orphan bytes survive, reclaim settles once."""
    s = world()
    ctx = sized(s)
    row, p = pending(s, ctx, TOOL, groups(ctx))
    before = census(s), deepcopy(pg.load(row["job_id"]))
    byte_count = sum(map(len, s.blobs.data.values()))
    original = jobs._insert_outbox
    observed = []
    def abort(*args, **kwargs):
        original(*args, **kwargs)
        observed.append(True)
        raise RuntimeError("injected after publication, proof, history and outbox")
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "_insert_outbox", abort)
        with pytest.raises(RuntimeError, match="injected after"):
            pg.complete(row, p)
    assert observed == [True]
    assert (census(s), pg.load(row["job_id"])) == before
    surviving = sum(map(len, s.blobs.data.values())) - byte_count
    assert surviving == len(p.output_intake_bytes) > 0
    pg.expire_worker(row["job_id"])
    reclaimed = pg.claim(s, row, owner="r3e-" + uuid4().hex, seconds=3600)
    assert reclaimed["attempt"] == row["attempt"] + 1
    assert pg.complete(row, p) == ("not_owner", None)
    retry = pg.prepared(reclaimed)
    assert retry.output_intake_bytes == p.output_intake_bytes
    settled(s, reclaimed, retry)
    result = s.proofs[-1][2]
    counts = census(s)
    assert counts == (5, 4, 4, 1, 4)
    assert pg.complete(reclaimed, retry) == ("duplicate", result)
    assert pg.complete(row, p) == ("conflict", None)
    assert census(s) == counts
    assert sum(map(len, s.blobs.data.values())) == byte_count + surviving
