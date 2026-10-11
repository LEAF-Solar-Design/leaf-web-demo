"""Real PostgreSQL custody for deterministic multi-add, HTTP admission and settlement."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
# Load the PostgreSQL HTTP helpers by origin: their server siblings have the same basename.
_spec = importlib.util.spec_from_file_location(
    "_sip_r3c_pg_http_helpers", ROOT / "platform/tests/test_sip_r5_admission.py")
http_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(http_cases)
pg = http_cases.pg
world, http = http_cases.world, http_cases.http
adapter, local, project = pg.adapter, pg.local, pg.project
db, store, graph_store = pg.db, pg.store, pg.store_graph

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

TOOL = "solar-string-multi-add"
UNKNOWN = ["EQUIPMENT_POWER_UNKNOWN", "EQUIPMENT_VOLTAGE_UNKNOWN"]


def manual(s):
    # Publish every construction step; no graph row is edited into a sized parent.
    source = deepcopy(pg.context(s).intake)
    source["polylines"] = [
        {"layer": "Panels", "closed": True, "handle": format(26 + i, "X"),
         "pts": [[100*i, 0], [100*i+77, 0], [100*i+77, 38.5], [100*i, 38.5]]}
        for i in range(3)]
    pg.set_parent(s, source)
    ctx = adapter.resolve_project_graph_context(pg.context(s))
    p = adapter.prepare_project_graph_seed(ctx, pg.seed_params(s),
        checkout=s.lease, job_id=s.job, attempt=1,
        tool_manifest_sha256=pg.manifest("solar-settings"))
    result = pg.publish(s, p)
    ctx = pg._chain.output(s, result)
    for tool, params in (
            ("solar-settings", {"expected_rev": 1,
                "changes": {"num_mppt": 2, "panel_layer_contains": "Panel"}}),
            ("solar-panels-from-drawing", {"expected_rev": 2}),
            ("solar-size-strings", {"expected_rev": 3, "mode": "manual-global", "confirm": True})):
        _, _, ctx = pg._chain.commit(s, tool, params, ctx)
    assert ctx.graph["rev"] == 4 and len(ctx.graph["panels"]) == 3
    s.parent = ctx.parent_version_id
    return ctx


def parameters(ctx):
    return {"expected_rev": 4, "operation": "add-strings", "string_length": 2,
            "ordered_panel_refs": [panel["id"] for panel in ctx.graph["panels"]]}


def derive(ctx, params, index):
    raw = json.dumps({
        "schema": "leaf.solar-project-creation.v1",
        "organization_id": str(ctx.organization_id), "project_id": str(ctx.project_id),
        "drawing_id": str(ctx.drawing_id), "parent_version_id": str(ctx.parent_version_id),
        "parent_intake_sha256": ctx.intake_sha256, "created_at": ctx.created_at,
        "tool": TOOL, "parameters": params, "kind": "string", "index": index,
    }, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    return "leaf:string:" + str(UUID(bytes=sha256(raw).digest()[:16], version=4))


def prepare(s, ctx, params=None, **changes):
    return pg._chain.prepare(s, TOOL, parameters(ctx) if params is None else params, ctx, **changes)


def graph_bytes(prepared):
    return json.loads(prepared.output_intake_bytes)["solar_design_graph"]


def prove(s, receipt, prepared):
    return pg.db.run_transaction(lambda conn: adapter.project_graph_commit_provenance(
        str(s.org), receipt, expected=prepared, conn=conn))


def snapshot(s):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        job_count = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s AND project_id=%s "
                    "AND output_version_id IS NOT NULL", (s.org, s.project))
        outputs = cur.fetchone()["n"]
    return job_count, outputs, pg.census(s), deepcopy(s.blobs.data), list(s.blobs.writes)


def body(s, params):
    return {"tool": TOOL, "dwg": str(s.parent),
        "params": {**deepcopy(params), "drawing_id": str(s.drawing)},
        "catalog_digest": pg.manifest(TOOL)}


def run(http, s, params, *, key="multi-key"):
    return http.client.post("/api/run?wait=0", json=body(s, params),
                            headers=http_cases._headers(s, key))


def preview(http, s, params, *, key="multi-key"):
    request = body(s, params)
    return http.client.get("/api/capabilities", params={
        "project_id": str(s.project), "drawing_id": str(s.drawing),
        "input_version_id": str(s.parent), "project_runs": json.dumps({TOOL: {
            "params": request["params"], "catalog_digest": request["catalog_digest"],
            "idempotency_key": key}})}, headers=http_cases._headers(s, key))


def state(response):
    assert response.status_code == 200, response.text
    return next(row["availability"] for family in response.json()["families"]
                for row in family["capabilities"] if row["name"] == TOOL)


def equipment(ctx):
    config = {**deepcopy(pg._chain.CONFIG), "total_dc_inputs": 2, "mppt_inputs": {"A": 2}}
    return {"expected_rev": 5, "equipment": [config], "assignments": [
        {"string_ref": string["id"], "inverter_ref": config["id"], "mppt_letter": "A",
         "input_number": index} for index, string in enumerate(ctx.graph["strings"])]}


def test_sip_r3c_multi_add_pg_preparation(world):
    s = world()
    ctx = manual(s)
    params, original = parameters(ctx), ctx.intake_bytes
    p = prepare(s, ctx, params)
    assert prepare(s, ctx, params) == p and ctx.intake_bytes == original
    assert params == parameters(ctx)
    g = graph_bytes(p)
    assert [string["id"] for string in g["strings"]] == [derive(ctx, params, i) for i in (0, 1)]
    assert [string["provenance"]["created_at"] for string in g["strings"]] == [ctx.created_at] * 2
    assert [string["ordered_panel_refs"] for string in g["strings"]] == [
        params["ordered_panel_refs"][:2], params["ordered_panel_refs"][2:]]
    before = pg.census(s)
    r = pg.publish(s, p)
    assert pg.delta(pg.census(s), before)[0] == 1
    assert r["before_rev"] == 4 and r["after_rev"] == 5
    ctx5 = pg._chain.output(s, r)
    assert ctx5.graph == g and prove(s, r, p)["output_version_id"] == r["output_version_id"]
    ep, er, ctx6 = pg._chain.commit(s, "solar-assign-equipment", equipment(ctx5), ctx5)
    assert tuple(len(ctx6.graph[name]) for name in
                 ("panels", "strings", "inverters", "routes", "schedules")) == (3, 2, 1, 0, 0)
    for entity in [*ctx6.graph["strings"], *ctx6.graph["inverters"]]:
        assert entity["validity"] == {"state": "invalid", "reasons": UNKNOWN}
    assert ctx6.graph["settings"]["extra"]["string_sizing"]["records"] == {}
    assert prove(s, er, ep)["output_version_id"] == er["output_version_id"]
    before = snapshot(s)
    for tool, request, code in (
            ("solar-homeruns", {"expected_rev": 6}, "EQUIPMENT_ASSIGNMENT_REQUIRED"),
            ("solar-schedule", {"expected_rev": 6, "insertion_point": [0, 0, 0]},
             "ROUTING_TOPOLOGY_REQUIRED")):
        with pytest.raises(pg.service.GraphValidationError) as exc:
            pg._chain.prepare(s, tool, request, ctx6)
        assert exc.value.code == code
    assert snapshot(s) == before


def test_sip_r3c_multi_add_pg_scope_isolation(world):
    s = world()
    first = manual(s)
    source = str(uuid4())
    key = f"tenants/{s.org}/drawings/{source}/v/00000001.intake.json"
    artifact = store.create_drawing_artifact(s.org, s.project, "Second multi-add drawing")
    parent = store.create_drawing_version(s.org, s.project, drawing_id=artifact.drawing_id,
        oss_object=key[:-12] + ".dwg", intake_ref=key)
    s.blobs.data[key] = first.intake_bytes
    provenance = {"schema": "leaf.drawing-import.v1", "source": {
        "kind": "account_upload", "tenant_id": str(s.org), "drawing_id": source, "version": 1,
        "stored_object": {"ref": parent.oss_object},
        "intake": {"ref": key, "sha256": first.intake_sha256}}}
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                    (Jsonb(provenance), s.org, parent.version_id))
    lease = db.run_transaction(lambda conn: graph_store.acquire_checkout(s.org, s.project,
        parent.drawing_id, actor_binding_id=s.actor, holder="Second editor", ttl_s=60,
        expected_fence=None, conn=conn))
    second_scope = SimpleNamespace(**{**s.__dict__, "drawing": parent.drawing_id,
                                     "parent": parent.version_id, "lease": lease, "job": uuid4()})
    second = adapter.resolve_project_graph_context(pg.context(second_scope))
    assert first.intake_bytes == second.intake_bytes and first.drawing_id != second.drawing_id
    params = parameters(first)
    candidates = [prepare(scope, ctx, params) for scope, ctx in ((s, first), (second_scope, second))]
    ids = [[string["id"] for string in graph_bytes(p)["strings"]] for p in candidates]
    assert len(set(ids[0] + ids[1])) == 4
    for scope, ctx, p, foreign in ((s, first, candidates[0], candidates[1]),
                                  (second_scope, second, candidates[1], candidates[0])):
        assert prepare(scope, ctx, params) == p
        assert [string["id"] for string in graph_bytes(p)["strings"]] == [
            derive(ctx, params, i) for i in (0, 1)]
        foreign_lease = second_scope.lease if scope is s else s.lease
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: prepare(scope, ctx, params, checkout=foreign_lease))
        r = pg.publish(scope, p)
        assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, r, foreign))


def test_sip_r3c_multi_add_pg_historical_replay(world, monkeypatch):
    s = world()
    ctx = manual(s)
    p = prepare(s, ctx)
    r = pg.publish(s, p)
    ctx5 = pg._chain.output(s, r)
    pg._chain.commit(s, "solar-settings", {"expected_rev": 5, "changes": {"num_mppt": 3}}, ctx5)
    assert pg.context(s, UUID(r["output_version_id"])).binding.is_head is False
    monkeypatch.setattr(graph_store, "_clock", lambda cur: s.lease.expires_at)
    before = snapshot(s), len(s.blobs.reads)
    with monkeypatch.context() as patch:
        patch.setattr(s.blobs, "get", pg.blocked)
        patch.setattr(s.blobs, "put_if_absent_or_verify", pg.blocked)
        patch.setattr(local, "_load_builtin", pg.blocked)
        assert pg.publish(s, p) == r
    assert (snapshot(s), len(s.blobs.reads)) == before
    assert prove(s, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3c_multi_add_pg_stored_content_tampering(world):
    s = world()
    ctx = manual(s)
    job = pg.submit(s, tool=TOOL, params=parameters(ctx), parent=ctx.parent_version_id)
    row = pg.claim(s, job)
    p = pg.prepared(row)
    outcome, result = pg.complete(row, p)
    assert outcome == "applied"
    r = result["graph_commit"]
    ctx5 = pg._chain.output(s, r)
    version_id = UUID(r["output_version_id"])
    version = store.get_drawing_version(s.org, s.project, version_id)
    def terminal(receipt):
        return db.run_transaction(lambda conn: adapter.project_graph_job_provenance(
            UUID(row["job_id"]), receipt, expected=p, conn=conn))
    for change in ("timestamp", "identity", "unrelated"):
        assert prove(s, r, p)["output_version_id"] == terminal(r)["output_version_id"]
        intake = ctx5.intake
        string = intake["solar_design_graph"]["strings"][1]
        if change == "timestamp":
            string["provenance"]["created_at"] = "2027-02-03T04:05:06Z"
        elif change == "identity":
            ref = string["id"]
            intake = json.loads(json.dumps(intake).replace(ref,
                "leaf:string:00000000-0000-4000-8000-0000000000aa"))
        else:
            intake["custom"]["keep"].append(99)
        intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
        raw = local.canonical_bytes(intake)
        digest = sha256(raw).hexdigest()
        key = graph_store.publication_intake_key(s.org, s.project, s.drawing, digest)
        s.blobs.data[key] = raw
        provenance = deepcopy(version.provenance)
        provenance["intake"] = {"ref": key, "sha256": digest}
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                        "WHERE org_id=%s AND version_id=%s", (key, Jsonb(provenance),
                        graph_store._publication_fingerprint(provenance), s.org, version_id))
        forged = {**r, "graph_sha256": intake["solar_design_graph_sha256"],
                  "output_intake_sha256": digest}
        for receipt in (r, forged):
            pg.refused("SIP_R3_PROOF_REJECTED", lambda: prove(s, receipt, p))
            pg.refused("SIP_R3_PROOF_REJECTED", lambda: terminal(receipt))
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                        "WHERE org_id=%s AND version_id=%s", (version.intake_ref,
                        Jsonb(version.provenance), graph_store._publication_fingerprint(version.provenance),
                        s.org, version_id))
    assert prove(s, r, p)["output_version_id"] == terminal(r)["output_version_id"]


def test_sip_r3c_multi_add_pg_admission_refusals(world, http, monkeypatch):
    s = world()
    ctx = manual(s)
    http_cases._bind(http, s)
    params = parameters(ctx)
    before = snapshot(s), deepcopy(params)
    for changes, code in (({"expected_rev": 3}, "STALE_GRAPH_REVISION"),
            ({"string_length": 0}, "INVALID_SETTINGS_REQUEST"),
            ({"string_length": 901}, "INVALID_SETTINGS_REQUEST"),
            ({"ordered_panel_refs": ["missing"]}, "MISSING_PANEL"),
            ({"ordered_panel_refs": [params["ordered_panel_refs"][0]] * 2}, "DUPLICATE_PANEL_MEMBERSHIP")):
        request = {**params, **changes}
        invalid = state(preview(http, s, request))
        response = run(http, s, request)
        assert response.status_code == invalid["admission"]["status_code"] == 409
        assert response.json()["error"] == invalid["admission"]["error"]
        assert response.json()["error"]["error_code"] == "BAD_PARAMS"
        assert response.json()["error"]["reason_code"] == code
        assert invalid["engine_ready"] and invalid["admission_checked"] and not invalid["runnable"]
        assert snapshot(s) == before[0]
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "_creation_id", lambda *args: ctx.graph["panels"][0]["id"])
        invalid = state(preview(http, s, params))
        response = run(http, s, params)
        assert response.status_code == 409
        assert response.json()["error"] == invalid["admission"]["error"]
        assert response.json()["error"]["reason_code"] == "DUPLICATE_APPLICATION_ID"
    assert (snapshot(s), params) == before


def test_sip_r3c_multi_add_pg_route_to_terminal(world, http, monkeypatch):
    s = world()
    ctx = manual(s)
    params = parameters(ctx)
    http_cases._bind(http, s)
    before = snapshot(s)
    ready = state(preview(http, s, params))
    assert ready["engine_ready"] and ready["input_ready"] and ready["entitled"] and ready["runnable"]
    assert ready["admission_checked"] and ready["admission"] == {"status_code": 200, "error": None}
    assert ready["refusal_reasons"] == [] and snapshot(s) == before
    submitted = run(http, s, params)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    stored = pg.load(job_id)
    assert stored["params"] == params and "drawing_id" not in stored["params"]
    assert stored["tool_name"] == TOOL and stored["input_version_id"] == ctx.parent_version_id
    assert stored["execution_context"]["schema"] == pg.jobs.PROJECT_GRAPH_JOB_SCHEMA
    probe = prepare(s, ctx, job_id=UUID(int=0))
    row = pg.claim(s, {**stored, "job_id": job_id})
    p = pg.prepared(row)
    assert p.output_intake_bytes == probe.output_intake_bytes
    baseline = pg.census(s)
    outcome, result = pg.complete(row, p)
    assert outcome == "applied" and pg.delta(pg.census(s), baseline) == (1, 1, 1, 0)
    receipt = result["graph_commit"]
    final = pg.load(job_id)
    assert final["status"] == "succeeded" and final["result"] == result
    assert str(final["output_version_id"]) == result["output_version_id"] == receipt["output_version_id"]
    assert store.verify_history_operation(s.org, UUID(result["history_operation_id"]))
    assert prove(s, receipt, p)["output_version_id"] == receipt["output_version_id"]
    assert db.run_transaction(lambda conn: adapter.project_graph_job_provenance(
        UUID(job_id), receipt, expected=p, conn=conn))["output_version_id"] == receipt["output_version_id"]
    observed = http.client.get(f"/api/jobs/{job_id}")
    assert observed.status_code == 200 and observed.json()["status"] == "complete"
    assert observed.json()["result"] == result
    after = snapshot(s), len(s.blobs.reads)
    with monkeypatch.context() as patch:
        patch.setattr(project, "verify_at_admission", pg.blocked)
        patch.setattr(pg.service, "_validate_new_project_graph_params", pg.blocked)
        patch.setattr(pg.entitlements, "stored_job_entitlement_verdict", pg.blocked)
        patch.setattr(adapter, "publish_project_graph_commit", pg.blocked)
        patch.setattr(adapter, "project_graph_job_provenance", pg.blocked)
        patch.setattr(local, "_load_builtin", pg.blocked)
        patch.setattr(s.blobs, "get", pg.blocked)
        patch.setattr(s.blobs, "put_if_absent_or_verify", pg.blocked)
        replay = run(http, s, params)
        assert replay.status_code == 202 and replay.json()["job_id"] == job_id
        assert state(preview(http, s, params))["admission"] == {"status_code": 200, "error": None}
        assert pg.complete(row, p, owner="different") == ("duplicate", result)
    assert (snapshot(s), len(s.blobs.reads)) == after
    conflict = run(http, s, {**params, "string_length": 3})
    assert conflict.status_code == 409 and conflict.json()["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"
    # Fresh scopes keep the two failed controls independent of the successful job.
    for control in ("lost-lease", "stale-parent"):
        other = world()
        parent = manual(other)
        job = pg.submit(other, tool=TOOL, params=parameters(parent), parent=parent.parent_version_id)
        claimed = pg.claim(other, job, seconds=3600)
        candidate = pg.prepared(claimed)
        if control == "lost-lease":
            pg.expire_worker(claimed["job_id"])
            census = pg.census(other)
            assert pg.complete(claimed, candidate) == ("not_owner", None)
        else:
            pg._chain.commit(other, "solar-settings",
                {"expected_rev": 4, "changes": {"num_mppt": 3}}, parent)
            census = pg.census(other)
            pg.refused("SIP_R1_STALE_VERSION", lambda: pg.complete(claimed, candidate))
        assert pg.census(other) == census
        saved = pg.load(claimed["job_id"])
        assert saved["output_version_id"] is None and saved["result"] is None
        # A lost lease leaves its job claimable, and the next control's claim takes the oldest
        # claimable job of the tool across every tenant, so each control settles its own scope
        # with the world fixture's teardown statement before the next one claims.
        with pg.db.cursor() as cur:
            cur.execute("UPDATE jobs SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL "
                        "WHERE org_id=%s AND status IN ('queued', 'running')", (other.org,))
