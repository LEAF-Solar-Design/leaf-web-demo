"""Hermetic HTTP-to-PostgreSQL Solar sizing, including durable recovery and proof."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_r14e_pg_admission", ROOT / "platform/tests/test_sip_r5_admission.py")
http_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(http_helpers)
pg = http_helpers.pg
graph, service, db, local = pg.adapter, pg.service, pg.db, pg.local
from leaf_platform import project_sizing_store as exchanges
from leaf_cloud_grants import CloudGrant
import solar_sizing_client as cloud
from solar_sizing_power import frame_module_power
from solar_design_graph import GraphValidationError

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

SMALL = {"cells": 60, "voc": 50, "isc": 10, "pmp": 400, "vmp": 40, "imp": 10,
    "bpmp": 0, "bvoc": -0.2, "alpha_sc": 0, "min_temp": 0,
    "simulation_results": {"standard": {"Conditions": "fixture", "max_module_voltage": 55,
        "string_design_voltage": 600, "string_length": 10}}}


def forbidden(*args, **kwargs):
    raise AssertionError("unregistered outbound HTTP or forbidden grant resolution")


class Response:
    def __init__(self, raw, status):
        self.raw, self.status_code = raw, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.raw


@pytest.fixture
def world(make_org, monkeypatch):
    scopes, sends = {}, []
    old_label, old_accept = cloud.SIZING_CLASSIFICATION, graph.SYNTHETIC_SIZING_PROOF_ACCEPTED
    cloud.SIZING_CLASSIFICATION = "synthetic"
    graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = True
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", uuid4().hex + uuid4().hex)
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(cloud.requests, "post", forbidden)
    monkeypatch.setattr(pg.project.write_loop, "upload_backend_for_tenant",
                        lambda tenant: scopes[str(tenant)].blobs)
    monkeypatch.setattr(pg.project.platform_link, "resolve_caller_binding", lambda tenant:
        SimpleNamespace(platform_tenant_id=UUID(str(tenant)), binding_id=scopes[str(tenant)].actor))

    def grant(ref, tenant):
        s = scopes[str(tenant)]
        assert ref == s.grant_ref
        return CloudGrant(str(s.org), s.token)
    monkeypatch.setattr(cloud, "resolve_grant", grant)

    def fresh():
        org = make_org("R14-E " + uuid4().hex, tier="hosted_pro")
        project = pg.store.create_project(org.org_id, "Solar", authority_mode="postgres_canonical")
        subject = str(uuid4())
        actor = pg.store.create_identity_binding(org.org_id, "auth0", subject, role="editor")
        artifact = pg.store.create_drawing_artifact(org.org_id, project.project_id)
        source = str(uuid4())
        key = f"tenants/{org.org_id}/drawings/{source}/v/00000001.intake.json"
        parent = pg.store.create_drawing_version(org.org_id, project.project_id,
            drawing_id=artifact.drawing_id, oss_object=key[:-12] + ".dwg", intake_ref=key)
        intake = {"dwg": {}, "layers": [], "inserts": [], "faces3d": [],
            "blockdefs": [], "geodata": None, "polylines": [
                {"layer": "Panels", "closed": True, "handle": uuid4().hex,
                 "pts": [[8*i, 0], [8*i+6, 0], [8*i+6, 3], [8*i, 3]]} for i in range(10)]}
        raw = local.canonical_bytes(intake)
        blobs = pg.base_scope.__wrapped__.__globals__["Blobs"]()
        blobs.data[key] = raw
        provenance = {"schema": "leaf.drawing-import.v1", "source": {
            "kind": "account_upload", "tenant_id": str(org.org_id), "drawing_id": source,
            "version": 1, "stored_object": {"ref": parent.oss_object},
            "intake": {"ref": key, "sha256": sha256(raw).hexdigest()}}}
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                        (Jsonb(provenance), org.org_id, parent.version_id))
            cur.execute("INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s,%s,%s,%s,'editor',%s)",
                (uuid4(), org.org_id, project.project_id, actor.binding_id, actor.binding_id))
        lease = db.run_transaction(lambda conn: pg.store_graph.acquire_checkout(
            org.org_id, project.project_id, artifact.drawing_id, actor_binding_id=actor.binding_id,
            holder=str(uuid4()), ttl_s=3600, expected_fence=None, conn=conn))
        s = SimpleNamespace(org=org.org_id, project=project.project_id, drawing=artifact.drawing_id,
            parent=parent.version_id, actor=actor.binding_id, subject=subject, lease=lease,
            blobs=blobs, grant_ref=uuid4().hex, token=uuid4().hex, sends=sends, jobs=[])
        scopes[str(s.org)] = s
        return s

    try:
        yield fresh
    finally:
        cloud.SIZING_CLASSIFICATION = old_label
        graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = old_accept
        for s in scopes.values():
            # Include unexpectedly admitted jobs when a refusal assertion fails.
            with db.cursor() as cur:
                cur.execute("UPDATE jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL "
                            "WHERE org_id=%s AND project_id=%s "
                            "AND execution_context->>'drawing_id'=%s AND status IN ('queued','running')",
                            (s.org, s.project, str(s.drawing)))


@pytest.fixture
def http(monkeypatch):
    yield from http_helpers.http.__wrapped__(monkeypatch)


def principal(s):
    return pg.deps.TenantContext(str(s.org), org_id=str(s.org), tier="hosted_pro", subject=s.subject)


def headers(s, key):
    return {"X-Org-Id": str(s.org), "X-Project-Id": str(s.project), "Idempotency-Key": key,
        "X-Checkout-Capability": pg.checkout_capability.mint(principal(s),
            pg.project.checkout_scope(s.project, s.drawing), s.lease.fence)}


def request(http, s, tool, params, *, key=None, parent=None):
    http.principal["tenant"] = principal(s)
    key = key or str(uuid4())
    body = {"tool": tool, "params": {**deepcopy(params), "drawing_id": str(s.drawing)},
            "dwg": str(parent or s.parent), "catalog_digest": pg.manifest(tool)}
    return http.client.post("/api/run?wait=0", json=body, headers=headers(s, key))


def submit(http, s, tool, params):
    http.principal["tenant"] = principal(s)
    key, before = str(uuid4()), census(s)
    sent = len(s.sends)
    query = {"project_id": str(s.project), "drawing_id": str(s.drawing),
        "input_version_id": str(s.parent), "project_runs": json.dumps({tool: {
            "params": {**params, "drawing_id": str(s.drawing)},
            "catalog_digest": pg.manifest(tool), "idempotency_key": key}})}
    preview = http.client.get("/api/capabilities", params=query, headers=headers(s, key))
    assert preview.status_code == 200, preview.text
    state = next(r["availability"] for f in preview.json()["families"]
                 for r in f["capabilities"] if r["name"] == tool)
    assert state["runnable"] and state["admission_checked"], json.dumps(state)
    result = request(http, s, tool, params, key=key)
    assert result.status_code == 202, result.text
    assert len(s.sends) == sent and census(s) == before
    job = SimpleNamespace(id=result.json()["job_id"], tool=tool, params=deepcopy(params),
                          key=key, parent=s.parent, claims=[], prepared=None)
    s.jobs.append(job)
    return job


def census(s):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM drawing_versions WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        versions = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM history_operations WHERE org_id=%s AND project_id=%s "
                    "AND operation_type='solar.graph.completed'", (s.org, s.project))
        history = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM outbox_entries o JOIN history_operations h "
                    "ON o.aggregate_id=h.operation_id WHERE h.org_id=%s AND h.project_id=%s "
                    "AND h.operation_type='solar.graph.completed'", (s.org, s.project))
        outbox = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM project_sizing_exchanges WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        return versions, history, outbox, cur.fetchone()["n"]


def context(s):
    return graph.resolve_project_graph_context(pg.context(s))


def run(http, s, job, monkeypatch, *, success=True):
    claim = pg.jobs.claim_project_graph_job
    prepare = service.prepare_project_graph_job
    def observe(*args, **kwargs):
        row = claim(*args, **kwargs)
        assert row is not None and row["job_id"] == job.id
        job.claims.append(deepcopy(row))
        return row
    def prepared(*args, **kwargs):
        result = prepare(*args, **kwargs)
        # Downstream publication also reconstructs the historical sizing job.
        if result.request["job_id"] == job.id:
            job.prepared = result
        return result
    before = census(s)
    with monkeypatch.context() as patch:
        patch.setattr(pg.jobs, "claim_project_graph_job", observe)
        patch.setattr(service, "prepare_project_graph_job", prepared)
        assert service.run_once(str(uuid4()), tool_name=job.tool, lease_seconds=120)
    row = pg.load(job.id)
    if success:
        assert row["status"] == "succeeded", row.get("error")
        assert census(s)[:3] == tuple(x + 1 for x in before[:3])
        s.parent = UUID(row["result"]["output_version_id"])
        http.principal["tenant"] = principal(s)
        terminal = http.client.get(f"/api/jobs/{job.id}")
        assert terminal.status_code == 200 and terminal.json()["status"] == "complete"
        assert terminal.json()["result"] == row["result"]
        assert pg.store.verify_history_operation(s.org, UUID(row["result"]["history_operation_id"]))
        receipt = row["result"]["graph_commit"]
        assert {k: v for k, v in receipt.items() if k != "output_version_id"} == json.loads(job.prepared.receipt_bytes)
        assert receipt["organization_id"] == str(s.org) and receipt["project_id"] == str(s.project)
        assert receipt["drawing_id"] == str(s.drawing) and receipt["job_id"] == job.id
        assert receipt["checkout_fence"] == str(s.lease.fence)
        assert receipt["tool_manifest_sha256"] == pg.manifest(job.tool)
        assert receipt["request_sha256"] == sha256(job.prepared.request_bytes).hexdigest()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM history_operations WHERE operation_id=%s AND org_id=%s",
                        (row["result"]["history_operation_id"], s.org))
            history = cur.fetchone()
            assert history["operation_type"] == "solar.graph.completed"
            assert history["payload"] == {"jobId": job.id, "inputVersionId": str(job.parent),
                "outputVersionId": str(s.parent), "requestHash": receipt["request_sha256"],
                "graphHash": receipt["graph_sha256"]}
            cur.execute("SELECT * FROM outbox_entries WHERE aggregate_id=%s AND org_id=%s",
                        (history["operation_id"], s.org))
            events = cur.fetchall()
            assert len(events) == 1 and events[0]["event_type"] == "history.operation.appended"
    return row


def step(http, s, monkeypatch, tool, params):
    job = submit(http, s, tool, params)
    run(http, s, job, monkeypatch)
    return job


def panels(http, s, monkeypatch):
    ctx = context(s)
    assert ctx.graph is None
    step(http, s, monkeypatch, "solar-settings", {"expected_rev": 0,
        "changes": {"panels_in_sequence": 10, "panel_layer_contains": "Panel"},
        "project_changes": {"name": "R14-E", "zip_code": "44224"},
        "initialize": {"schema_version": 1, "source_intake_sha256": ctx.intake_sha256,
                       "units": deepcopy(pg.UNITS)}})
    step(http, s, monkeypatch, "solar-panels-from-drawing", {"expected_rev": context(s).graph["rev"]})
    assert len(context(s).graph["panels"]) == 10


def sizing_params(s):
    g = context(s).graph
    return {"expected_rev": g["rev"], "mode": "global", "confirm": True,
        "grant_ref": s.grant_ref, "requests": {g["settings"]["id"]: {
            "module_name": "M-" + uuid4().hex, "full_inverter_name": "I", "bifacial": False,
            "bifacial_coefficient": "0", "racking_params": {"racking_type": "fixed_tilt",
                "surface_tilt": "20", "surface_azimuth": "180", "albedo": "0.2"},
            "max_voltage": "600", "thermal_model_type": "sapm", "open_circuit_rise": False,
            "zip_code": "44224"}}}


def replay(s, job, monkeypatch, *, raw=None, status=200, failure=None):
    wire = cloud.wire_bytes(cloud.SizingRequest.model_validate(next(iter(job.params["requests"].values()))))
    raw = local.canonical_bytes(SMALL) if raw is None else raw
    def post(url, **kwargs):
        s.sends.append(job.id)
        assert url == cloud.SIZING_URL
        assert kwargs == {"data": wire, "headers": {"Authorization": "Bearer " + s.token,
            "Content-Type": "application/json"}, "timeout": (5, 45), "allow_redirects": False, "stream": True}
        # This independent connection cannot see the worker's uncommitted writes.
        with db.cursor() as cur:
            cur.execute("SELECT * FROM project_sizing_exchanges WHERE job_id=%s AND org_id=%s",
                        (job.id, s.org))
            row = cur.fetchone()
        assert row["state"] == "send_started" and bytes(row["request_bytes"]) == wire
        assert row["sender_worker"] == job.claims[-1]["lease_owner"]
        assert row["sender_attempt"] == job.claims[-1]["attempt"]
        assert row["classification"] == "synthetic"
        if failure:
            raise failure
        return Response(raw, status)
    monkeypatch.setattr(cloud.requests, "post", post)


def sized(http, s, monkeypatch, *, raw=None):
    panels(http, s, monkeypatch)
    job = submit(http, s, "solar-size-strings", sizing_params(s))
    replay(s, job, monkeypatch, raw=raw)
    run(http, s, job, monkeypatch)
    monkeypatch.setattr(cloud.requests, "post", forbidden)
    return job


def groups(s):
    g = context(s).graph
    return {"expected_rev": g["rev"], "groups": [{
        "name": "Roof", "panel_refs": [p["id"] for p in g["panels"]],
        "alignment_tolerance": .5, "module_width_along_row": 77, "module_height_across_row": 38.5}]}


def downstream(http, s, monkeypatch, *, finish=True):
    if finish:
        step(http, s, monkeypatch, "solar-panel-groups", groups(s))
    g = context(s).graph
    step(http, s, monkeypatch, "solar-string-add", {"expected_rev": g["rev"],
        "operation": "add-string", "ordered_panel_refs": [p["id"] for p in g["panels"]]})
    g = context(s).graph
    equipment = {"id": "leaf:inverter:" + str(uuid4()), "number": 1, "type_key": "INV",
        "model": "fixture", "position": [5, 0, 0], "rotation": 0, "scale": [1, 1, 1],
        "block_name": "FixtureInverter", "layer": "0", "mppt_count": 1, "total_dc_inputs": 1,
        "mppt_inputs": {"A": 1}, "max_dc_voltage": 1500, "max_ac_power_kw": 100,
        "max_dc_power_kw": 100, "is_solaredge": False}
    step(http, s, monkeypatch, "solar-assign-equipment", {"expected_rev": g["rev"],
        "equipment": [equipment], "assignments": [{"string_ref": g["strings"][0]["id"],
            "inverter_ref": equipment["id"], "mppt_letter": "A", "input_number": 0}]})
    g = context(s).graph
    step(http, s, monkeypatch, "solar-string-conductors", {"expected_rev": g["rev"],
        "operation": "set-conductors", "assignments": [
            {"string_ref": g["strings"][0]["id"], "wire_gauge": "8 AWG"}]})
    if finish:
        step(http, s, monkeypatch, "solar-homeruns", {"expected_rev": context(s).graph["rev"]})
        step(http, s, monkeypatch, "solar-schedule", {
            "expected_rev": context(s).graph["rev"], "insertion_point": [0, 0, 0]})


def exchange(job):
    row = pg.load(job.id)
    scope, lease = graph._exchange_authority(row)
    return exchanges.get_exchange(scope, lease)


def proofs(s, job, *, rejected=False):
    receipt = pg.load(job.id)["result"]["graph_commit"]
    calls = [lambda conn: graph.project_graph_commit_provenance(str(s.org), receipt,
                expected=job.prepared, conn=conn),
             lambda conn: graph.project_graph_job_provenance(UUID(job.id), receipt,
                expected=job.prepared, conn=conn)]
    for call in calls:
        if rejected:
            with pytest.raises(pg.project.ProjectContextError) as caught:
                db.run_transaction(call)
            assert caught.value.reason_code == "SIP_R3_PROOF_REJECTED"
        else:
            assert db.run_transaction(call)["output_version_id"] == receipt["output_version_id"]


def refuse_downstream(http, s):
    before = census(s)
    g = context(s).graph
    response = request(http, s, "solar-string-add", {
        "expected_rev": g["rev"], "operation": "add-string",
        "ordered_panel_refs": [p["id"] for p in g["panels"]]})
    assert response.status_code == 500, response.text
    assert response.json()["error"]["reason_code"] == "SIP_R3_PROOF_REJECTED"
    assert census(s) == before


def test_success_chain(world, http, monkeypatch):
    s = world()
    job = sized(http, s, monkeypatch)
    downstream(http, s, monkeypatch)
    g, e = context(s).graph, exchange(job)
    assert census(s) == (10, 9, 9, 1) and s.sends == [job.id]
    assert g["settings"]["panels_in_sequence"] == 10
    assert g["settings"]["voc_cold"] == {"passes": True, "override_accepted": False,
        "suggested_string_length": 0, "per_module": 52.5, "string_voltage": 525.0, "max_dc_voltage": 600.0}
    assert len(s.jobs) == 9
    assert len(g["frames"]) == 1 and len(g["panels"]) == 10
    assert {p["frame_ref"] for p in g["panels"]} == {g["frames"][0]["id"]}
    assert g["frames"][0]["module_power_watts"] == json.loads(e["response_bytes"])["pmp"] == SMALL["pmp"] == 400.0
    assert len(g["strings"]) == len(g["inverters"]) == len(g["schedules"]) == 1
    assert len(g["routes"]) == 2 and g["inverters"][0]["validity"]["state"] == "valid"
    assert frame_module_power(g, g["strings"][0]["ordered_panel_refs"], None) == 400.0
    string = g["strings"][0]
    assert g["schedules"][0]["rows"] == [[string["circuit_tag"], 10, "8 AWG",
        g["routes"][0]["length_ft"], g["routes"][1]["length_ft"],
        sum(r["length_ft"] for r in g["routes"])]]
    evidence = json.loads(e["evidence_bytes"])
    assert e["classification"] == evidence["classification"] == "synthetic"
    assert e["state"] == "published"
    for key in ("request", "response", "evidence"):
        assert sha256(e[key + "_bytes"]).hexdigest() == e[key + "_sha256"]
    assert e["org_id"] == s.org and e["project_id"] == s.project and e["drawing_id"] == s.drawing
    assert e["actor_binding_id"] == s.actor
    assert e["sender_worker"] == e["published_worker"] == job.claims[0]["lease_owner"]
    assert e["sender_attempt"] == e["published_attempt"] == 1
    assert evidence["identity"] == {"organization_id": str(s.org), "project_id": str(s.project),
        "drawing_id": str(s.drawing), "job_id": job.id, "actor_id": str(s.actor),
        "target_settings_id": e["target_settings_id"]}
    assert evidence["input"]["parent_version_id"] == str(job.parent)
    assert evidence["input"]["tool_manifest_sha256"] == pg.manifest(job.tool)
    assert evidence["input"]["parent_intake_sha256"] == pg.load(job.id)["execution_context"]["parent_intake_sha256"]
    assert evidence["grant"]["reference"] == s.grant_ref
    assert evidence["request"]["wire_sha256"] == e["request_sha256"]
    assert evidence["response"]["wire_sha256"] == e["response_sha256"]
    assert evidence["producer"] == {"adapter_version": cloud.ADAPTER_VERSION,
        "service_identity": cloud.SIZING_URL, "http_status": 200,
        "worker_id": e["sender_worker"], "attempt": 1}
    assert job.prepared.request["tool_manifest_sha256"] == pg.manifest(job.tool)
    proofs(s, job)
    before = census(s)
    # Only the exact value True accepts synthetic custody; None and 0 are not "unset means on".
    refused_values = (False, None, 0, 1, "True", [True], "true", "TRUE", "1", "yes", "on", b"true")
    assert all((type(value), value) != (type(other), other)
               for i, value in enumerate(refused_values) for other in refused_values[i + 1:])
    for value in refused_values:
        graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = value
        proofs(s, job, rejected=True)
        refuse_downstream(http, s)
    graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = True
    proofs(s, job)
    assert census(s) == before


def test_frame_power_is_the_response_power(world, http, monkeypatch):
    # Every other fixture answers 400 W, so a product that returned a constant 400 would pass them.
    s = world()
    job = sized(http, s, monkeypatch, raw=local.canonical_bytes({**deepcopy(SMALL), "pmp": 450}))
    step(http, s, monkeypatch, "solar-panel-groups", groups(s))
    g, e = context(s).graph, exchange(job)
    assert json.loads(e["response_bytes"])["pmp"] == 450 != SMALL["pmp"]
    assert len(g["frames"]) == 1 and g["frames"][0]["module_power_watts"] == 450.0
    assert frame_module_power(g, [p["id"] for p in g["panels"]], None) == 450.0
    assert census(s) == (5, 4, 4, 1) and s.sends == [job.id]
    proofs(s, job)
    # A later response for the same settings must replace the earlier power.
    second = submit(http, s, "solar-size-strings", sizing_params(s))
    replay(s, second, monkeypatch, raw=local.canonical_bytes({**deepcopy(SMALL), "pmp": 400}))
    run(http, s, second, monkeypatch)
    monkeypatch.setattr(cloud.requests, "post", forbidden)
    g, later = context(s).graph, exchange(second)
    assert later["target_settings_id"] == e["target_settings_id"] == g["settings"]["id"]
    assert json.loads(later["response_bytes"])["pmp"] == 400 != json.loads(e["response_bytes"])["pmp"]
    assert len(g["frames"]) == 1 and g["frames"][0]["module_power_watts"] == 400.0
    assert frame_module_power(g, [p["id"] for p in g["panels"]], None) == 400.0 != 450.0
    assert census(s) == (6, 5, 5, 2) and s.sends == [job.id, second.id]
    proofs(s, second)


def test_synthetic_switch_off_before_the_first_sizing_run(world, http, monkeypatch):
    # The switch is off before the preview, the admission and the worker. Admission never reads it;
    # the worker stores the one response and then refuses the synthetic evidence instead of publishing.
    for value in (False, None):
        s = world()
        panels(http, s, monkeypatch)
        graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = value
        job = submit(http, s, "solar-size-strings", sizing_params(s))
        before, parent = census(s), s.parent
        replay(s, job, monkeypatch)
        row = run(http, s, job, monkeypatch, success=False)
        monkeypatch.setattr(cloud.requests, "post", forbidden)
        assert row["status"] == "failed" and row["result"] is None
        assert row["error"]["reason_code"] == "SIP_R14_SYNTHETIC_EVIDENCE_REFUSED"
        assert row["error"]["retryable"] is False
        e = exchange(job)
        assert e["state"] == "response_stored" and e["response_bytes"] == local.canonical_bytes(SMALL)
        assert e["published_worker"] is None and e["published_attempt"] is None
        assert census(s) == (*before[:3], 1) and pg.context(s).binding.head_version_id == parent
        assert s.sends.count(job.id) == 1 and len(job.claims) == 1
        # Turning the switch back on revives nothing: the job is terminal and nothing is claimable.
        graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = True
        assert not service.run_once(str(uuid4()), tool_name=job.tool, lease_seconds=120)
        assert pg.load(job.id) == row and exchange(job) == e
        assert census(s) == (*before[:3], 1) and s.sends.count(job.id) == 1


def test_same_job_replay(world, http, monkeypatch):
    s = world()
    job = sized(http, s, monkeypatch)
    downstream(http, s, monkeypatch)
    before = census(s), exchange(job), deepcopy(pg.load(job.id))
    monkeypatch.setattr(pg.store_graph, "_clock", lambda cur: s.lease.expires_at)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    response = request(http, s, job.tool, job.params, key=job.key, parent=job.parent)
    assert response.status_code == 202 and response.json()["job_id"] == job.id
    row = job.claims[0]
    outcome, result = service.complete_project_graph_job(UUID(job.id), row["lease_owner"], 1, job.prepared)
    assert outcome == "duplicate" and result == pg.load(job.id)["result"]
    proofs(s, job)
    assert (census(s), exchange(job), pg.load(job.id)) == before and s.sends == [job.id]


def test_response_tamper(world, http, monkeypatch):
    s = world()
    job = sized(http, s, monkeypatch)
    original = exchange(job)
    altered = json.loads(original["response_bytes"])
    altered["pmp"] += 1
    raw = local.canonical_bytes(altered)
    def update(conn):
        conn.execute("UPDATE project_sizing_exchanges SET response_bytes=%s,response_sha256=%s "
                     "WHERE org_id=%s AND project_id=%s AND drawing_id=%s AND job_id=%s",
                     (raw, sha256(raw).hexdigest(), s.org, s.project, s.drawing, job.id))
        raise AssertionError("immutable response update was accepted")
    with pytest.raises(psycopg.Error):
        db.run_transaction(update)
    assert exchange(job) == original
    get = exchanges.get_exchange
    def corrupt(*args, **kwargs):
        result = deepcopy(get(*args, **kwargs))
        if result and str(result["job_id"]) == job.id:
            result.update(response_bytes=raw, response_sha256=sha256(raw).hexdigest())
        return result
    with monkeypatch.context() as patch:
        patch.setattr(exchanges, "get_exchange", corrupt)
        proofs(s, job, rejected=True)
    proofs(s, job)
    assert exchange(job) == original


def test_other_request_substitution(world, http, monkeypatch):
    a, b = world(), world()
    ja, jb = sized(http, a, monkeypatch), sized(http, b, monkeypatch)
    proofs(a, ja)
    proofs(b, jb)
    ea, eb = exchange(ja), exchange(jb)
    assert ea["request_bytes"] != eb["request_bytes"]
    get = exchanges.get_exchange
    def substitute(*args, **kwargs):
        result = deepcopy(get(*args, **kwargs))
        if result and str(result["job_id"]) == ja.id:
            for name in ("request", "response", "evidence"):
                for suffix in ("_bytes", "_sha256"):
                    result[name + suffix] = eb[name + suffix]
        return result
    before = census(a), census(b)
    with monkeypatch.context() as patch:
        patch.setattr(exchanges, "get_exchange", substitute)
        proofs(a, ja, rejected=True)
        refuse_downstream(http, a)
    scope, lease = graph._exchange_authority(pg.load(ja.id))
    for changes in ({"org_id": b.org, "admitted_tenant": str(b.org)},
                    {"project_id": b.project}, {"drawing_id": b.drawing}):
        assert get(replace(scope, **changes), lease) is None
    assert (census(a), census(b)) == before
    proofs(a, ja)


def test_service_failure(world, http, monkeypatch):
    voltage, power = deepcopy(SMALL), deepcopy(SMALL)
    voltage["simulation_results"]["standard"]["string_design_voltage"] = 500
    power["pmp"] = 0
    for status, raw, reason in ((503, b"unavailable", "CLOUD_UPSTREAM_FAILURE"),
            (200, b"", "CLOUD_RESPONSE_INVALID"), (200, b"{bad", "CLOUD_RESPONSE_INVALID"),
            (200, local.canonical_bytes(voltage), "CLOUD_RESPONSE_INVALID"),
            (200, local.canonical_bytes(power), "CLOUD_RESPONSE_INVALID")):
        s = world()
        panels(http, s, monkeypatch)
        job = submit(http, s, "solar-size-strings", sizing_params(s))
        before, parent = census(s), s.parent
        replay(s, job, monkeypatch, raw=raw, status=status)
        row = run(http, s, job, monkeypatch, success=False)
        assert row["status"] == "failed" and row["error"]["reason_code"] == reason
        assert row["error"]["retryable"] is False
        e = exchange(job)
        assert e["state"] == "refused"
        assert e["response_bytes"] == (raw if status == 200 and raw else None)
        assert census(s) == (*before[:3], 1) and s.parent == parent
        monkeypatch.setattr(cloud.requests, "post", forbidden)
        response = request(http, s, job.tool, job.params, key=job.key, parent=job.parent)
        assert response.status_code == 202 and response.json()["job_id"] == job.id
        assert pg.load(job.id) == row and exchange(job) == e
        changed = request(http, s, job.tool, {"expected_rev": job.params["expected_rev"],
            "mode": "manual-global", "confirm": True}, key=job.key, parent=job.parent)
        assert changed.status_code == 409
        assert changed.json()["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"
        # Neither request revived the failed job: the stored row is unchanged and nothing is claimable.
        assert pg.load(job.id) == row and exchange(job) == e
        assert not service.run_once(str(uuid4()), tool_name=job.tool, lease_seconds=120)
        assert pg.load(job.id) == row
        assert s.sends.count(job.id) == 1 and census(s) == (*before[:3], 1)


def test_lost_response(world, http, monkeypatch):
    for crash in (False, True):
        s = world()
        panels(http, s, monkeypatch)
        job = submit(http, s, "solar-size-strings", sizing_params(s))
        before = census(s)
        replay(s, job, monkeypatch, failure=(RuntimeError("crash after dispatch") if crash
                                            else cloud.requests.Timeout("lost response")))
        row = run(http, s, job, monkeypatch, success=False)
        monkeypatch.setattr(cloud.requests, "post", forbidden)
        if crash:
            e = exchange(job)
            assert row["status"] == "running" and e["state"] == "send_started"
            assert e["response_bytes"] is None
            pg.expire_worker(job.id)
            row = run(http, s, job, monkeypatch, success=False)
            assert job.claims[1]["attempt"] == 2
        assert row["status"] == "failed" and row["error"]["retryable"] is False
        assert row["error"]["reason_code"] == "SIZING_OUTCOME_UNKNOWN"
        e = exchange(job)
        assert e["state"] == "outcome_unknown"
        response = request(http, s, job.tool, job.params, key=job.key, parent=job.parent)
        assert response.status_code == 202 and response.json()["job_id"] == job.id
        # The replay revived nothing: the stored row is unchanged and nothing is claimable.
        assert pg.load(job.id) == row and exchange(job) == e
        assert not service.run_once(str(uuid4()), tool_name=job.tool, lease_seconds=120)
        assert pg.load(job.id) == row
        assert census(s) == (*before[:3], 1) and s.sends.count(job.id) == 1


def test_response_saved_before_publication_failure(world, http, monkeypatch):
    s = world()
    panels(http, s, monkeypatch)
    job = submit(http, s, "solar-size-strings", sizing_params(s))
    replay(s, job, monkeypatch)
    complete, attempted = exchanges.complete_exchange, []
    def abort(*args, **kwargs):
        result = complete(*args, **kwargs)
        assert result[0] == "applied"
        attempted.append(result)
        raise RuntimeError("rollback after real settlement")
    before = census(s)
    with monkeypatch.context() as patch:
        patch.setattr(exchanges, "complete_exchange", abort)
        row = run(http, s, job, monkeypatch, success=False)
    assert len(attempted) == 1 and row["status"] == "running"
    assert row["result"] is None and row["output_version_id"] is None
    old_prepared = job.prepared
    saved = exchange(job)
    assert saved["state"] == "response_stored" and saved["evidence_bytes"]
    assert saved["published_worker"] is None and saved["output_version_id"] is None
    assert census(s) == (*before[:3], 1)
    # Before publication only the response guard stands between a stored response and a rewrite.
    altered = json.loads(saved["response_bytes"])
    altered["pmp"] += 1
    raw = local.canonical_bytes(altered)
    def update(conn):
        conn.execute("UPDATE project_sizing_exchanges SET response_bytes=%s,response_sha256=%s "
                     "WHERE org_id=%s AND project_id=%s AND drawing_id=%s AND job_id=%s",
                     (raw, sha256(raw).hexdigest(), s.org, s.project, s.drawing, job.id))
        raise AssertionError("stored response update was accepted before publication")
    with pytest.raises(psycopg.Error, match="sizing response is immutable"):
        db.run_transaction(update)
    assert exchange(job) == saved
    monkeypatch.setattr(cloud.requests, "post", forbidden)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    pg.expire_worker(job.id)
    run(http, s, job, monkeypatch)
    final = exchange(job)
    for key in ("response_bytes", "response_sha256", "evidence_bytes", "evidence_sha256",
                "sender_worker", "sender_attempt"):
        assert final[key] == saved[key]
    assert final["published_worker"] == job.claims[1]["lease_owner"] != final["sender_worker"]
    assert final["published_attempt"] == 2 and final["sender_attempt"] == 1
    after = census(s), deepcopy(pg.load(job.id)), final
    with pytest.raises(exchanges.SizingExchangeError):
        service.complete_project_graph_job(UUID(job.id), job.claims[0]["lease_owner"], 1, old_prepared)
    assert (census(s), pg.load(job.id), exchange(job)) == after
    assert census(s) == (before[0]+1, before[1]+1, before[2]+1, 1) and s.sends == [job.id]
    proofs(s, job)


def test_manual_boundary_preserved(world, http, monkeypatch):
    s = world()
    panels(http, s, monkeypatch)
    step(http, s, monkeypatch, "solar-size-strings", {
        "expected_rev": context(s).graph["rev"], "mode": "manual-global", "confirm": True})
    # Manual confirmation has no service power authority, so grouping also refuses.
    before = census(s)
    refused = request(http, s, "solar-panel-groups", groups(s))
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["reason_code"] == "MODULE_POWER_REQUIRED"
    assert census(s) == before
    downstream(http, s, monkeypatch, finish=False)
    g = context(s).graph
    assert g["settings"]["extra"]["string_sizing"]["records"] == {}
    assert g["settings"]["voc_cold"] == cloud.manual_voltage()
    assert g["strings"][0]["wire_gauge"] == "8 AWG"
    with pytest.raises(GraphValidationError, match="MODULE_POWER_REQUIRED"):
        frame_module_power(g, g["strings"][0]["ordered_panel_refs"], None)
    before = census(s)
    for tool, params, reason in (("solar-homeruns", {}, "EQUIPMENT_ASSIGNMENT_REQUIRED"),
            ("solar-schedule", {"insertion_point": [0, 0, 0]}, "ROUTING_TOPOLOGY_REQUIRED")):
        result = request(http, s, tool, {"expected_rev": g["rev"], **params})
        assert result.status_code == 409, result.text
        assert result.json()["error"]["reason_code"] == reason
    assert census(s) == before and before[3] == 0 and not s.sends
