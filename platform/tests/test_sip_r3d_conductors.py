"""Real PostgreSQL conductor custody, exact HTTP replay and stored-content proof."""
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
    "_sip_r3d_pg_multi_helpers", ROOT / "platform/tests/test_sip_r3c_multi_add.py")
multi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(multi)
http_cases = multi.http_cases
pg = multi.pg
world, http = multi.world, multi.http
adapter, local, project = pg.adapter, pg.local, pg.project
db, store, graph_store = pg.db, pg.store, pg.store_graph

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

TOOL = "solar-string-conductors"
UNKNOWN = ["EQUIPMENT_POWER_UNKNOWN", "EQUIPMENT_VOLTAGE_UNKNOWN"]


def manual(s):
    sized = multi.manual(s)
    _, _, ctx = pg._chain.commit(s, multi.TOOL, multi.parameters(sized), sized)
    s.parent = ctx.parent_version_id
    return ctx


def parameters(ctx, gauge="8 AWG"):
    return {"operation": "set-conductors", "expected_rev": ctx.graph["rev"],
        "assignments": [{"string_ref": string["id"], "wire_gauge": gauge}
                        for string in ctx.graph["strings"]]}


def identities(g):
    return [(entity["id"], entity["provenance"]["created_at"]) for entity in
            [g["project"], g["settings"], *[item for name in (
                "electrical_zones", "frames", "panels", "strings", "inverters", "routes", "schedules")
                for item in g[name]]]]


def bind(http, s):
    # One fresh subject per scope; the exact same principal mints and submits.
    if not hasattr(s, "principal"):
        s.principal = pg.deps.TenantContext(
            str(s.org), org_id=str(s.org), tier="hosted_pro", subject=str(uuid4()))
    http.principal["tenant"] = s.principal


def headers(s, key):
    return {"X-Org-Id": str(s.org), "X-Project-Id": str(s.project), "Idempotency-Key": key,
        "X-Checkout-Capability": pg.checkout_capability.mint(s.principal,
            pg.project.checkout_scope(s.project, s.drawing), s.lease.fence)}


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


def run(http, s, params, *, key="conductor-key"):
    return http.client.post("/api/run?wait=0", json=body(s, params),
                            headers=headers(s, key))


def preview(http, s, params, *, key="conductor-key"):
    request = body(s, params)
    return http.client.get("/api/capabilities", params={
        "project_id": str(s.project), "drawing_id": str(s.drawing),
        "input_version_id": str(s.parent), "project_runs": json.dumps({TOOL: {
            "params": request["params"], "catalog_digest": request["catalog_digest"],
            "idempotency_key": key}})}, headers=headers(s, key))


def state(response):
    assert response.status_code == 200, response.text
    return next(row["availability"] for family in response.json()["families"]
                for row in family["capabilities"] if row["name"] == TOOL)


def test_sip_r3d_pg_preparation_publication(world):
    s = world()
    ctx = manual(s)
    params, original = parameters(ctx), ctx.intake_bytes
    p = prepare(s, ctx, params)
    assert prepare(s, ctx, params) == p and ctx.intake_bytes == original
    assert params == parameters(ctx)
    g = graph_bytes(p)
    assert identities(g) == identities(ctx.graph)
    assert [string["wire_gauge"] for string in g["strings"]] == ["8 AWG", "8 AWG"]
    assert g["rev"] == ctx.graph["rev"] + 1 and g["parent_rev"] == ctx.graph["rev"]
    for name in ("panels", "strings", "inverters", "routes", "schedules"):
        assert len(g[name]) == len(ctx.graph[name])
    before = pg.census(s)
    r = pg.publish(s, p)
    assert pg.delta(pg.census(s), before)[0] == 1
    assert r["before_rev"] == ctx.graph["rev"] and r["after_rev"] == ctx.graph["rev"] + 1
    after = pg._chain.output(s, r)
    assert after.graph == g and prove(s, r, p)["output_version_id"] == r["output_version_id"]
    assert store.get_drawing_version(s.org, s.project, UUID(r["output_version_id"])).oss_object == (
        store.get_drawing_version(s.org, s.project, ctx.parent_version_id).oss_object)
    ep_params = multi.equipment(after)
    ep_params["expected_rev"] = after.graph["rev"]
    _, _, equipped = pg._chain.commit(s, "solar-assign-equipment", ep_params, after)
    conductor, receipt, final = pg._chain.commit(s, TOOL, parameters(equipped, "6 AWG"), equipped)
    assert final.graph["rev"] == equipped.graph["rev"] + 1
    assert identities(final.graph) == identities(equipped.graph)
    for entity in [*final.graph["strings"], *final.graph["inverters"]]:
        assert entity["validity"] == {"state": "invalid", "reasons": UNKNOWN}
    assert prove(s, receipt, conductor)["output_version_id"] == receipt["output_version_id"]
    before = snapshot(s)
    for tool, request, code in (
            ("solar-homeruns", {"expected_rev": final.graph["rev"]}, "EQUIPMENT_ASSIGNMENT_REQUIRED"),
            ("solar-schedule", {"expected_rev": final.graph["rev"], "insertion_point": [0, 0, 0]},
             "ROUTING_TOPOLOGY_REQUIRED")):
        with pytest.raises(pg.service.GraphValidationError) as exc:
            pg._chain.prepare(s, tool, request, final)
        assert exc.value.code == code
    assert snapshot(s) == before


def test_sip_r3d_pg_scope_isolation(world):
    s = world()
    first = manual(s)
    source = str(uuid4())
    key = f"tenants/{s.org}/drawings/{source}/v/00000001.intake.json"
    artifact = store.create_drawing_artifact(s.org, s.project, "Second conductor drawing")
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
    assert candidates[0].output_intake_bytes == candidates[1].output_intake_bytes
    assert candidates[0].request_bytes != candidates[1].request_bytes
    for scope, ctx, p, foreign in ((s, first, candidates[0], candidates[1]),
                                  (second_scope, second, candidates[1], candidates[0])):
        assert prepare(scope, ctx, params) == p
        assert identities(graph_bytes(p)) == identities(ctx.graph)
        foreign_lease = second_scope.lease if scope is s else s.lease
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: prepare(scope, ctx, params, checkout=foreign_lease))
        r = pg.publish(scope, p)
        assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, r, foreign))
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: db.run_transaction(
            lambda conn: adapter.project_graph_commit_provenance(
                str(uuid4()), r, expected=p, conn=conn)))


def test_sip_r3d_pg_historical_replay(world, monkeypatch):
    s = world()
    ctx = manual(s)
    job = pg.submit(s, tool=TOOL, params=parameters(ctx), parent=ctx.parent_version_id)
    row = pg.claim(s, job)
    p = pg.prepared(row)
    outcome, result = pg.complete(row, p)
    assert outcome == "applied"
    r = result["graph_commit"]
    ctx5 = pg._chain.output(s, r)
    pg._chain.commit(s, "solar-settings", {"expected_rev": ctx5.graph["rev"], "changes": {"num_mppt": 3}}, ctx5)
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
    assert db.run_transaction(lambda conn: adapter.project_graph_job_provenance(
        UUID(row["job_id"]), r, expected=p, conn=conn))["output_version_id"] == r["output_version_id"]
    before = snapshot(s)
    pg.refused("SIP_R1_STALE_VERSION", lambda: pg.submit(
        s, tool=TOOL, params=parameters(ctx), parent=ctx.parent_version_id, key="new-write"))
    assert snapshot(s) == before


def test_sip_r3d_pg_stored_content_tampering(world):
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
    for change in ("gauge", "timestamp", "identity", "unrelated"):
        assert prove(s, r, p)["output_version_id"] == terminal(r)["output_version_id"]
        intake = ctx5.intake
        string = intake["solar_design_graph"]["strings"][1]
        if change == "gauge":
            string["wire_gauge"] = "6 AWG"
        elif change == "timestamp":
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

    for key, value in (("graph_sha256", "0" * 64), ("actor_binding_id", str(uuid4()))):
        receipt = {**r, key: value}
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: prove(s, receipt, p))
        pg.refused("SIP_R3_PROOF_REJECTED", lambda: terminal(receipt))
    altered = {**deepcopy(version.provenance), "actor_binding_id": str(uuid4())}
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s, import_fingerprint=%s "
                    "WHERE org_id=%s AND version_id=%s",
                    (Jsonb(altered), graph_store._publication_fingerprint(altered), s.org, version_id))
    pg.refused("SIP_R3_PROOF_REJECTED", lambda: prove(s, r, p))
    pg.refused("SIP_R3_PROOF_REJECTED", lambda: terminal(r))
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s, import_fingerprint=%s "
                    "WHERE org_id=%s AND version_id=%s", (Jsonb(version.provenance),
                    graph_store._publication_fingerprint(version.provenance), s.org, version_id))
        cur.execute("UPDATE jobs SET params=%s WHERE org_id=%s AND job_id=%s",
                    (Jsonb(parameters(ctx, "6 AWG")), s.org, UUID(row["job_id"])))
    assert prove(s, r, p)["output_version_id"] == r["output_version_id"]
    pg.refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: terminal(r))
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET params=%s WHERE org_id=%s AND job_id=%s",
                    (Jsonb(row["params"]), s.org, UUID(row["job_id"])))
    assert terminal(r)["output_version_id"] == r["output_version_id"]


def test_sip_r3d_pg_admission_refusals(world, http, monkeypatch):
    s = world()
    ctx = manual(s)
    bind(http, s)
    params = parameters(ctx)
    before = snapshot(s), deepcopy(params)
    for changes, code in (
            ({"expected_rev": ctx.graph["rev"] - 1}, "STALE_GRAPH_REVISION"),
            ({"assignments": [{"string_ref": "missing", "wire_gauge": "8 AWG"}]}, "MISSING_STRING"),
            ({"assignments": [{"string_ref": "不存在", "wire_gauge": "8 AWG"}]}, "MISSING_STRING"),
            ({"assignments": [params["assignments"][0]] * 2}, "DUPLICATE_STRING_ASSIGNMENT"),
            ({"assignments": [{**params["assignments"][0], "wire_gauge": "9 AWG"}]}, "INVALID_SETTINGS_REQUEST"),
            ({"assignments": []}, "INVALID_SETTINGS_REQUEST"),
            ({"assignments": [{**params["assignments"][0], "extra": True}]}, "INVALID_SETTINGS_REQUEST"),
            ({"operation": "other"}, "INVALID_SETTINGS_REQUEST"),
            ({"expected_rev": True}, "INVALID_SETTINGS_REQUEST"),
            ({"cancel": True}, "INVALID_SETTINGS_REQUEST"),
            ({"initialize": {}}, "INVALID_SEED_REQUEST")):
        request = {**params, **changes}
        invalid = state(preview(http, s, request))
        response = run(http, s, request)
        assert response.status_code == invalid["admission"]["status_code"] == 409
        assert response.json()["error"] == invalid["admission"]["error"]
        assert response.json()["error"]["error_code"] == "BAD_PARAMS"
        assert response.json()["error"]["reason_code"] == code
        assert invalid["engine_ready"] and invalid["admission_checked"] and not invalid["runnable"]
        assert snapshot(s) == before[0]
    # Past the assignment bound the preview cannot carry the request: its parameters travel in
    # the query string, which the HTTP client caps at 65,536 bytes, so the bound is checked
    # through the run route alone.
    oversized = {**params, "assignments": [{"string_ref": "x", "wire_gauge": "8 AWG"}] * 4097}
    response = run(http, s, oversized)
    assert (response.status_code, response.json()["error"]["error_code"],
            response.json()["error"].get("reason_code")) == (409, "BAD_PARAMS", "INVALID_SETTINGS_REQUEST")
    assert snapshot(s) == before[0]
    assert (snapshot(s), params) == before


def test_sip_r3d_pg_route_to_terminal(world, http, monkeypatch):
    s = world()
    ctx = manual(s)
    params = parameters(ctx)
    bind(http, s)
    before = snapshot(s)
    ready = state(preview(http, s, params))
    assert ready["engine_ready"] and ready["input_ready"] and ready["entitled"] and ready["runnable"]
    assert ready["admission_checked"] and ready["admission"] == {"status_code": 200, "error": None}
    assert ready["refusal_reasons"] == [] and snapshot(s) == before
    submitted = run(http, s, params)
    assert submitted.status_code == 202, submitted.text
    assert snapshot(s)[0] == before[0] + 1 and pg.census(s) == before[2]
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
    changed = parameters(ctx, "6 AWG")
    conflict = run(http, s, changed)
    assert conflict.status_code == 409 and conflict.json()["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"
    assert snapshot(s) == after[0]
    # Fresh scopes keep the two failed controls independent of the successful job.
    for control in ("lost-lease", "stale-parent"):
        other = world()
        parent = manual(other)
        job = pg.submit(other, tool=TOOL, params=parameters(parent), parent=parent.parent_version_id)
        claimed = pg.claim(other, job, seconds=3600)
        candidate = pg.prepared(claimed)
        if control == "lost-lease":
            pg.expire_worker(claimed["job_id"])
            census, failure_snapshot = pg.census(other), snapshot(other)
            assert pg.complete(claimed, candidate) == ("not_owner", None)
        else:
            pg._chain.commit(other, "solar-settings",
                {"expected_rev": parent.graph["rev"], "changes": {"num_mppt": 3}}, parent)
            census, failure_snapshot = pg.census(other), snapshot(other)
            pg.refused("SIP_R1_STALE_VERSION", lambda: pg.complete(claimed, candidate))
        after_failure = snapshot(other)
        assert pg.census(other) == census and after_failure[:3] == failure_snapshot[:3]
        # A refused completion writes no row and rewrites no stored blob. Losing the lease writes
        # nothing at all; a stale parent can leave the candidate's content-addressed intake blob
        # (put before the fenced transaction refuses), which nothing references.
        added = set(after_failure[3]) - set(failure_snapshot[3])
        assert all(after_failure[3][k] == v for k, v in failure_snapshot[3].items())
        if control == "lost-lease":
            assert not added
        else:
            assert len(added) <= 1 and all(
                k.endswith("/intakes/" + sha256(after_failure[3][k]).hexdigest() + ".json") for k in added)
        saved = pg.load(claimed["job_id"])
        assert saved["output_version_id"] is None and saved["result"] is None
        # A lost lease leaves its job claimable, and the next control's claim takes the oldest
        # claimable job of the tool across every tenant, so each control settles its own scope
        # with the world fixture's teardown statement before the next one claims.
        with pg.db.cursor() as cur:
            cur.execute("UPDATE jobs SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL "
                        "WHERE org_id=%s AND status IN ('queued', 'running')", (other.org,))
