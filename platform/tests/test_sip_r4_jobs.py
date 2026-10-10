"""Twelve native PostgreSQL proofs of graph custody and atomic settlement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import importlib.util
import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from leaf_platform import canonical_jobs as jobs, db, entitlements, project_graph_store as store_graph, store
from test_sip_r3a_graph import (scope as base_scope, context, prepare as seed, publish,
                                refused, UNITS, adapter, project, local)
from test_sip_r3b_tools import ROOT, panels_parent, set_parent
# test_sip_r3b_tools adds server/tests to sys.path; choose the database helper
# by origin rather than allowing its identically named server sibling to win.
_chain_spec = importlib.util.spec_from_file_location(
    "_sip_r4_pg_chain_helpers", ROOT / "platform/tests/test_sip_r3b_chain.py")
_chain = importlib.util.module_from_spec(_chain_spec)
_chain_spec.loader.exec_module(_chain)
manual, string_params, equipment_params, through_string = (
    _chain.manual, _chain.string_params, _chain.equipment_params, _chain.through_string)
import checkout_capability
import deps
import solar_project_jobs as service
import solar_tools

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")


@pytest.fixture
def world(make_org, monkeypatch):
    """Each subcase owns fresh real scope; failures cannot leave claimable siblings."""
    scopes = {}
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "b35be9230c184da187fcdf805bd863476165abc508e609b7f136bf6e60c246b3d")

    def fresh():
        s = base_scope.__wrapped__(make_org, monkeypatch)
        scopes[str(s.org)] = s
        monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant",
                            lambda tenant: scopes[str(tenant)].blobs)
        monkeypatch.setattr(project.platform_link, "resolve_caller_binding", lambda tenant:
            SimpleNamespace(platform_tenant_id=UUID(str(tenant)),
                            binding_id=scopes[str(tenant)].actor))
        return s

    yield fresh
    for s in scopes.values():
        with db.cursor() as cur:
            cur.execute("UPDATE jobs SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL "
                        "WHERE org_id=%s AND status IN ('queued', 'running')", (s.org,))


def manifest(tool):
    return deps.catalog_tool_digest(solar_tools.trusted_record(tool))


def seed_params(s):
    ctx = context(s)
    return {"expected_rev": 0, "changes": {"panels_in_sequence": 3},
        "initialize": {"schema_version": 1, "source_intake_sha256": ctx.intake_sha256,
                       "units": deepcopy(UNITS)}}


def submit(s, *, tool="solar-settings", params=None, parent=None, key="graph-key", **changes):
    args = {"tenant": str(s.org), "project_id": s.project, "drawing_id": s.drawing,
        "input_version_id": parent or s.parent, "tool_name": tool,
        "params": params if params is not None else seed_params(s),
        "tool_manifest_sha256": manifest(tool), "idempotency_key": key,
        "checkout_capability": checkout_capability.mint(
            str(s.org), project.checkout_scope(s.project, s.drawing), s.lease.fence)}
    args.update(changes)
    return service.submit_project_graph_job(**args)


def load(job_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
        return cur.fetchone()


def claim(s, job, *, owner="owner", seconds=30):
    row = jobs.claim_project_graph_job(owner, tool_name=job["tool_name"], lease_seconds=seconds)
    assert row is not None and row["job_id"] == job["job_id"]
    return row


def prepared(row):
    return db.run_transaction(lambda conn: service.prepare_project_graph_job(row, conn=conn))


def complete(row, p, *, owner=None, attempt=None):
    return service.complete_project_graph_job(UUID(str(row["job_id"])),
        owner or row["lease_owner"], row["attempt"] if attempt is None else attempt, p)


def census(s):
    with db.cursor() as cur:
        counts = []
        for table in ("drawing_versions", "history_operations", "outbox_entries", "solve_records"):
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE org_id=%s AND project_id=%s",
                        (s.org, s.project))
            counts.append(cur.fetchone()["n"])
        return tuple(counts)


def delta(after, before):
    return tuple(a - b for a, b in zip(after, before))


def expire_worker(job_id):
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=clock_timestamp() WHERE job_id=%s", (job_id,))


def completion(p):
    return {"request": p.request, "output_intake_sha256": p.output_intake_sha256,
            "receipt": json.loads(p.receipt_bytes)}


def blocked(*args, **kwargs):
    raise AssertionError("replay repeated publication, proof, content IO or builtin")


def test_sip_r4_pg_idempotency(world, monkeypatch):
    s = world()
    params = seed_params(s)
    start = Barrier(2)
    def enqueue():
        start.wait(timeout=10)
        return submit(s, params=params)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue) for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]
    assert results[0]["job_id"] == results[1]["job_id"]
    job = results[0]
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s", (s.org,))
        assert cur.fetchone()["n"] == 1
    refused("SIP_R4_IDEMPOTENCY_CONFLICT", lambda:
        submit(s, params={**params, "cancel": False}))
    actor = s.actor
    s.actor = s.other
    refused("SIP_R4_IDEMPOTENCY_CONFLICT", lambda: submit(s, params=params))
    s.actor = actor
    row = claim(s, job)
    assert complete(row, prepared(row))[0] == "applied"
    clock = store_graph._clock
    with monkeypatch.context() as patch:
        patch.setattr(store_graph, "_clock", lambda cur: clock(cur) - timedelta(minutes=2))
        expired = db.run_transaction(lambda conn: store_graph.acquire_checkout(
            s.org, s.project, s.drawing, actor_binding_id=s.actor, holder="Expired replay",
            ttl_s=60, expected_fence=s.lease.fence, conn=conn))
    with db.cursor() as cur:
        assert expired.expires_at < clock(cur)
    with db.cursor() as cur:
        cur.execute("UPDATE orgs SET tier='restricted' WHERE org_id=%s", (s.org,))
    before = census(s), len(s.blobs.reads), len(s.blobs.writes)
    with monkeypatch.context() as patch:
        patch.setattr(project, "verify_at_admission", blocked)
        patch.setattr(adapter, "_tool", blocked)
        patch.setattr(entitlements, "stored_job_entitlement_verdict", blocked)
        patch.setattr(s.blobs, "get", blocked)
        patch.setattr(local, "_load_builtin", blocked)
        assert submit(s, params=params, tool_manifest_sha256=manifest("solar-settings"))["job_id"] == job["job_id"]
    assert (census(s), len(s.blobs.reads), len(s.blobs.writes)) == before
    occupied = store.create_job(s.org, s.project, "run", tool_name="other", params={})
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET idempotency_key='occupied' WHERE job_id=%s", (occupied.job_id,))
    refused("SIP_R4_IDEMPOTENCY_CONFLICT", lambda: submit(s, params=params, key="occupied"))
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET deleted_at=clock_timestamp() WHERE job_id=%s", (job["job_id"],))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: submit(s, params=params))


def test_sip_r4_pg_scope(world, monkeypatch):
    s, foreign = world(), world()
    before = census(s)
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: submit(s, drawing_id=foreign.drawing))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: submit(s, input_version_id=foreign.parent))
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=clock_timestamp() "
                    "WHERE org_id=%s AND binding_id=%s", (s.org, s.actor))
    refused("SIP_R1_PROJECT_FORBIDDEN", lambda: submit(s))
    assert census(s) == before
    s = world()
    with db.cursor() as cur:
        cur.execute("UPDATE orgs SET tier='restricted' WHERE org_id=%s", (s.org,))
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        submit(s)
    assert exc.value.response.status_code == 403
    # An entitled-to-entitled concurrent tier change must preserve the retryable 409.
    s = world()
    original = entitlements.stored_job_entitlement_verdict
    changed = []
    def tier_change(org, kind):
        verdict = original(org, kind)
        if org == s.org and not changed:
            changed.append(True)
            with db.cursor() as cur:
                cur.execute("UPDATE orgs SET tier='hosted_pro' WHERE org_id=%s", (s.org,))
        return verdict
    with monkeypatch.context() as patch:
        patch.setattr(entitlements, "stored_job_entitlement_verdict", tier_change)
        with pytest.raises(entitlements.EntitlementDenied) as exc:
            submit(s)
        assert exc.value.response.status_code == 409
        assert json.loads(exc.value.response.body)["error"]["retryable"] is True
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s", (s.org,))
        assert cur.fetchone()["n"] == 0
        cur.execute("UPDATE projects SET deleted_at=clock_timestamp() WHERE project_id=%s", (s.project,))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: submit(s))


def test_sip_r4_pg_two_claimers(world):
    s = world()
    job = submit(s)
    unrelated = store.create_job(s.org, s.project, "build", tool_name="solar-settings", params={})
    other_tool = submit(s, tool="solar-feeders", params={"expected_rev": 0}, key="other-tool")
    wrong_schema = store.create_job(s.org, s.project, "run", tool_name="solar-settings", params={})
    # A queued job of another kind that carries the graph discriminator: only the kind filter
    # keeps it out of the graph population, so a claim without that filter takes it.
    graph_build = store.create_job(s.org, s.project, "build", tool_name="solar-settings", params={})
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET request_tenant_id=%s WHERE job_id=%s",
                    (str(s.org), unrelated.job_id))
        cur.execute("UPDATE jobs SET request_tenant_id=%s, execution_context=%s WHERE job_id=%s",
                    (str(s.org), Jsonb({"schema": "leaf.other-job.v1"}), wrong_schema.job_id))
        cur.execute("UPDATE jobs SET request_tenant_id=%s, execution_context=%s WHERE job_id=%s",
                    (str(s.org), Jsonb({"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA}), graph_build.job_id))
    barrier = Barrier(2)
    def take(owner):
        barrier.wait(timeout=10)
        return jobs.claim_project_graph_job(owner, tool_name="solar-settings")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(take, owner) for owner in ("one", "two")]
        results = [f.result(timeout=20) for f in futures]
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and winners[0]["job_id"] == job["job_id"] and winners[0]["attempt"] == 1
    assert jobs.claim_project_graph_job("other", tool_name="solar-string-add") is None
    assert load(unrelated.job_id)["attempt"] == 0 and load(unrelated.job_id)["status"] == "queued"
    assert load(other_tool["job_id"])["status"] == "queued"
    assert load(wrong_schema.job_id)["status"] == "queued"
    assert jobs.claim_project_graph_job("three", tool_name="solar-settings") is None
    assert load(graph_build.job_id)["attempt"] == 0 and load(graph_build.job_id)["status"] == "queued"


def test_sip_r4_pg_lease_attempt(world):
    s = world()
    row = claim(s, submit(s))
    p = prepared(row)
    job_id = UUID(row["job_id"])
    before = census(s)
    assert not jobs.heartbeat_project_graph_job(job_id, "wrong", 1)
    assert jobs.fail_project_graph_job(job_id, "wrong", 1, service._preparation_error(TimeoutError())) == "not_owner"
    assert complete(row, p, owner="wrong") == ("not_owner", None)
    expire_worker(job_id)
    assert not jobs.heartbeat_project_graph_job(job_id, "owner", 1)
    assert jobs.fail_project_graph_job(job_id, "owner", 1, service._preparation_error(TimeoutError())) == "not_owner"
    assert complete(row, p) == ("not_owner", None)
    reclaimed = jobs.claim_project_graph_job("owner", tool_name="solar-settings")
    assert reclaimed["attempt"] == 2 and reclaimed["job_id"] == row["job_id"]
    assert not jobs.heartbeat_project_graph_job(job_id, "owner", 1)
    assert jobs.fail_project_graph_job(job_id, "owner", 1, service._preparation_error(TimeoutError())) == "not_owner"
    assert complete(row, p) == ("not_owner", None)
    assert census(s) == before
    assert jobs.heartbeat_project_graph_job(job_id, "owner", 2)


def test_sip_r4_pg_retry_reclaim(world):
    s = world()
    row = claim(s, submit(s))
    job_id = UUID(row["job_id"])
    p = prepared(row)
    expire_worker(job_id)
    row2 = claim(s, row)
    p2 = prepared(row2)
    assert row2["attempt"] == 2 and p.output_intake_bytes == p2.output_intake_bytes
    assert p.request_sha256 != p2.request_sha256
    error = service._preparation_error(TimeoutError())
    assert jobs.fail_project_graph_job(job_id, "owner", 2, error) == "retry"
    row3 = claim(s, row)
    assert row3["attempt"] == 3
    assert jobs.fail_project_graph_job(job_id, "owner", 3, error) == "failed"
    assert jobs.claim_project_graph_job("owner", tool_name="solar-settings") is None
    s2 = world()
    exhausted = claim(s2, submit(s2))
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET attempt=3, lease_expires_at=clock_timestamp() WHERE job_id=%s",
                    (exhausted["job_id"],))
    assert jobs.claim_project_graph_job("owner", tool_name="solar-feeders") is None
    assert load(exhausted["job_id"])["status"] == "running"
    assert jobs.claim_project_graph_job("owner", tool_name="solar-settings") is None
    assert load(exhausted["job_id"])["error"] == {
        "error_code": "ATTEMPTS_EXHAUSTED", "message": "maximum attempts exhausted", "retryable": False}


def test_sip_r4_pg_atomic_success(world):
    for tool in ("solar-settings", "solar-string-add", "solar-assign-equipment"):
        s = world()
        if tool == "solar-settings":
            params, parent = seed_params(s), s.parent
        elif tool == "solar-string-add":
            ctx = manual(s)
            params, parent = string_params(ctx), ctx.parent_version_id
        else:
            _, _, ctx = through_string(s)
            params, parent = equipment_params(ctx), ctx.parent_version_id
        row = claim(s, submit(s, tool=tool, params=params, parent=parent))
        p = prepared(row)
        before = census(s)
        outcome, result = complete(row, p)
        assert outcome == "applied" and delta(census(s), before) == (1, 1, 1, 0)
        stored = load(row["job_id"])
        receipt = result["graph_commit"]
        assert str(stored["output_version_id"]) == result["output_version_id"] == receipt["output_version_id"]
        assert stored["result"] == result and stored["status"] == "succeeded"
        assert {k: v for k, v in receipt.items() if k != "output_version_id"} == json.loads(p.receipt_bytes)
        assert "history_hash" not in receipt and "solve_id" not in result
        assert result["execution_provenance"] == {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA,
            "attempt": 1, "worker_id": "owner", "execution_path": "local"}
        with db.cursor() as cur:
            cur.execute("SELECT * FROM history_operations WHERE operation_id=%s",
                        (result["history_operation_id"],))
            history = cur.fetchone()
            payload = {"jobId": row["job_id"], "inputVersionId": str(parent),
                "outputVersionId": receipt["output_version_id"], "requestHash": receipt["request_sha256"],
                "graphHash": receipt["graph_sha256"]}
            assert history["operation_type"] == "solar.graph.completed" and history["payload"] == payload
            assert history["idempotency_key"] == f"job:{row['job_id']}:history"
            assert history["hash_value"] == result["history_hash"]
            cur.execute("SELECT * FROM outbox_entries WHERE aggregate_id=%s",
                        (UUID(result["history_operation_id"]),))
            event = cur.fetchone()
            assert event["aggregate_type"] == "history_operation"
            assert event["event_type"] == "history.operation.appended"
            assert event["payload"] == {"operationId": result["history_operation_id"], "jobId": row["job_id"]}
        assert store.verify_history_operation(s.org, UUID(result["history_operation_id"]))
        assert db.run_transaction(lambda conn: adapter.project_graph_job_provenance(
            UUID(row["job_id"]), receipt, expected=p, conn=conn))["output_version_id"] == receipt["output_version_id"]


class CursorFault:
    def __init__(self, cursor, phase):
        self.cursor, self.phase = cursor, phase
    def __enter__(self):
        self.cursor.__enter__()
        return self
    def __exit__(self, *args):
        return self.cursor.__exit__(*args)
    def __getattr__(self, name):
        return getattr(self.cursor, name)
    def execute(self, sql, *args, **kwargs):
        if self.phase == "settlement" and sql.startswith("UPDATE jobs SET status='succeeded'"):
            raise RuntimeError("injected before settlement")
        result = self.cursor.execute(sql, *args, **kwargs)
        if self.phase == "history" and sql.startswith("INSERT INTO history_operations"):
            raise RuntimeError("injected after history")
        return result


class ConnectionFault:
    def __init__(self, conn, phase):
        self.conn, self.phase = conn, phase
    def cursor(self, *args, **kwargs):
        return CursorFault(self.conn.cursor(*args, **kwargs), self.phase)
    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_sip_r4_pg_rollback(world, monkeypatch):
    for phase in ("publication", "proof", "history", "settlement"):
        s = world()
        row = claim(s, submit(s))
        p = prepared(row)
        before = census(s)
        with monkeypatch.context() as patch:
            if phase in {"publication", "proof"}:
                name = ("publish_project_graph_commit" if phase == "publication" else "project_graph_job_provenance")
                original = getattr(adapter, name)
                def fault(*a, **k):
                    original(*a, **k)
                    raise RuntimeError("injected after " + phase)
                patch.setattr(adapter, name, fault)
            else:
                original = db.run_transaction
                patch.setattr(db, "run_transaction", lambda op, **k:
                    original(lambda conn: op(ConnectionFault(conn, phase)), **k))
            with pytest.raises(RuntimeError, match="injected"):
                complete(row, p)
        assert census(s) == before
        stored = load(row["job_id"])
        assert stored["status"] == "running" and stored["output_version_id"] is None and stored["result"] is None
        assert s.blobs.writes  # orphan immutable bytes are permitted; database successors are not.


def test_sip_r4_pg_duplicate_conflict(world, monkeypatch):
    s = world()
    row = claim(s, submit(s))
    p = prepared(row)
    outcome, result = complete(row, p)
    assert outcome == "applied"
    before = census(s), len(s.blobs.reads), len(s.blobs.writes), deepcopy(load(row["job_id"]))
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "publish_project_graph_commit", blocked)
        patch.setattr(adapter, "project_graph_job_provenance", blocked)
        patch.setattr(local, "_load_builtin", blocked)
        patch.setattr(s.blobs, "get", blocked)
        patch.setattr(s.blobs, "put_if_absent_or_verify", blocked)
        assert complete(row, p, owner="different") == ("duplicate", result)
        assert complete(row, p, attempt=2) == ("conflict", None)
        changed = deepcopy(completion(p))
        changed["output_intake_sha256"] = "0" * 64
        assert db.run_transaction(lambda conn: jobs.complete_project_graph_job(
            UUID(row["job_id"]), "owner", 1, changed, publish_and_prove=blocked, conn=conn)) == ("conflict", None)
    assert (census(s), len(s.blobs.reads), len(s.blobs.writes), load(row["job_id"])) == before


def test_sip_r4_pg_publication_admission(world, monkeypatch):
    for code in ("STALE_VERSION", "CHECKOUT_EXPIRED", "CHECKOUT_STALE",
                 "PROJECT_FORBIDDEN", "WRITES_DRAINED"):
        s = world()
        row = claim(s, submit(s), seconds=3600)
        p = prepared(row)
        if code == "STALE_VERSION":
            publish(s, seed(s, attempt=2))
        elif code == "CHECKOUT_STALE":
            db.run_transaction(lambda conn: store_graph.acquire_checkout(s.org, s.project, s.drawing,
                actor_binding_id=s.actor, holder="Replacement", ttl_s=60,
                expected_fence=s.lease.fence, conn=conn))
        elif code == "PROJECT_FORBIDDEN":
            with db.cursor() as cur:
                cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=clock_timestamp() "
                            "WHERE org_id=%s AND binding_id=%s", (s.org, s.actor))
        before = census(s)
        with monkeypatch.context() as patch:
            if code == "CHECKOUT_EXPIRED":
                patch.setattr(store_graph, "_clock", lambda cur: s.lease.expires_at)
            if code == "WRITES_DRAINED":
                @contextmanager
                def drain():
                    yield "drained"
                patch.setattr(project.write_loop, "drawing_mutation_refusal_guard", drain)
            refused("SIP_R1_" + code, lambda: complete(row, p))
        assert census(s) == before and load(row["job_id"])["status"] == "running"


def test_sip_r4_pg_stored_proof(world):
    s = world()
    row = claim(s, submit(s))
    p = prepared(row)
    _, result = complete(row, p)
    receipt = result["graph_commit"]
    def prove(evidence):
        return db.run_transaction(lambda conn: adapter.project_graph_job_provenance(
            UUID(row["job_id"]), evidence, expected=p, conn=conn))
    assert prove(receipt)["output_version_id"] == receipt["output_version_id"]
    version = store.get_drawing_version(s.org, s.project, UUID(receipt["output_version_id"]))
    original = s.blobs.data[version.intake_ref]
    s.blobs.data[version.intake_ref] = original + b" "
    refused("SIP_R1_INTAKE_DIGEST_MISMATCH", lambda: prove(receipt))
    s.blobs.data[version.intake_ref] = original
    for graph_change in (False, True):
        intake = json.loads(original)
        if graph_change:
            intake["solar_design_graph"]["settings"]["num_mppt"] = 9
            intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
        else:
            intake["custom"]["keep"].append(99)
        raw = local.canonical_bytes(intake)
        digest = sha256(raw).hexdigest()
        key = store_graph.publication_intake_key(s.org, s.project, s.drawing, digest)
        s.blobs.data[key] = raw
        provenance = deepcopy(version.provenance)
        provenance["intake"] = {"ref": key, "sha256": digest}
        with db.cursor() as cur:
            cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                        "WHERE org_id=%s AND version_id=%s",
                        (key, Jsonb(provenance), store_graph._publication_fingerprint(provenance),
                         s.org, version.version_id))
        forged = {**receipt, "output_intake_sha256": digest,
                  "graph_sha256": intake["solar_design_graph_sha256"]}
        refused("SIP_R3_PROOF_REJECTED", lambda: prove(receipt))
        refused("SIP_R3_PROOF_REJECTED", lambda: prove(forged))
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=clock_timestamp() "
                    "WHERE org_id=%s AND binding_id=%s", (s.org, s.actor))
    refused("SIP_R1_PROJECT_FORBIDDEN", lambda: prove(receipt))


def test_sip_r4_pg_legacy_isolation(world):
    s = world()
    first = submit(s)
    # A second graph job stays QUEUED through the whole row, so the generic selector is
    # challenged by a row it could take the moment its graph exclusion were removed.
    waiting = submit(s, key="waiting-graph")
    row = claim(s, first)
    job_id = UUID(row["job_id"])
    before = deepcopy(load(job_id))
    idle = deepcopy(load(waiting["job_id"]))
    assert idle["status"] == "queued" and idle["attempt"] == 0
    assert jobs.claim_next("generic", tool_name="solar-settings") is None
    assert not jobs.heartbeat(job_id, "owner")
    assert jobs.fail_or_retry(job_id, "owner", {}, {}) == "conflict"
    assert jobs.complete_solve(job_id, "owner", {}, {}) == "conflict"
    assert load(job_id) == before
    assert load(waiting["job_id"]) == idle
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET attempt=3, lease_expires_at=clock_timestamp() WHERE job_id=%s", (job_id,))
    exhausted = deepcopy(load(job_id))
    assert jobs.claim_next("generic", tool_name="solar-settings") is None
    assert load(job_id) == exhausted
    assert load(waiting["job_id"]) == idle
    ordinary = store.create_job(s.org, s.project, "run", tool_name="ordinary-r4", params={})
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET request_tenant_id=%s WHERE job_id=%s", (str(s.org), ordinary.job_id))
    regular = jobs.claim_next("generic", tool_name="ordinary-r4")
    assert regular["job_id"] == str(ordinary.job_id) and regular["attempt"] == 1
    assert jobs.heartbeat(ordinary.job_id, "generic")
    assert jobs.fail_or_retry(ordinary.job_id, "generic",
        {"message": "retry", "retryable": True}, {"attempt": 1}) == "retry"
    assert load(ordinary.job_id)["status"] == "queued"


def test_sip_r4_pg_final_clock(world, monkeypatch):
    s = world()
    row = claim(s, submit(s), seconds=3)
    p = prepared(row)
    before = census(s)
    original = adapter.project_graph_job_provenance
    proof_ran = []
    def expire_during_proof(*a, **k):
        result = original(*a, **k)
        proof_ran.append(True)
        k["conn"].execute("SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
            "(%s::timestamptz - clock_timestamp()))) + 0.05)", (load(row["job_id"])["lease_expires_at"],))
        return result
    monkeypatch.setattr(adapter, "project_graph_job_provenance", expire_during_proof)
    monkeypatch.setattr(jobs, "_now", lambda *a: (_ for _ in ()).throw(AssertionError("host clock used")))
    assert complete(row, p) == ("not_owner", None)
    assert proof_ran == [True] and census(s) == before
    stored = load(row["job_id"])
    assert stored["status"] == "running" and stored["result"] is None and stored["output_version_id"] is None
