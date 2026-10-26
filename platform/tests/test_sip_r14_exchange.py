"""Thirty-one PostgreSQL proofs of scoped sizing exchange custody."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
_admission_spec = importlib.util.spec_from_file_location(
    "_sip_r14_pg_admission_helpers", ROOT / "platform/tests/test_sip_r5_admission.py")
admission = importlib.util.module_from_spec(_admission_spec)
_admission_spec.loader.exec_module(admission)
pg = admission.pg
_chain_spec = importlib.util.spec_from_file_location(
    "_sip_r14_pg_chain_helpers", ROOT / "platform/tests/test_sip_r3b_chain.py")
chain = importlib.util.module_from_spec(_chain_spec)
_chain_spec.loader.exec_module(chain)

from leaf_platform import project_sizing_store as exchanges

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

REQUEST = b'{"mode":"global"}'
RESPONSE = b'{"sizing":"captured"}'
EVIDENCE = b'{"proof":"opaque"}'


@pytest.fixture
def world(make_org, monkeypatch):
    generator = admission.world.__wrapped__(make_org, monkeypatch)
    fresh = next(generator)

    def sizing():
        s = fresh()
        # Reuse the existing graph import helper without submitting any other jobs.
        ctx = chain.commit.__globals__["panels_parent"](s)
        s.parent = ctx.parent_version_id
        params = {"expected_rev": ctx.graph["rev"], "mode": "global",
                  "drawing_id": str(s.drawing)}
        manifest = pg.manifest("solar-size-strings")
        execution = {
            "schema": pg.jobs.PROJECT_GRAPH_JOB_SCHEMA,
            "authority_mode": "postgres_canonical", "execution_path": "local",
            "drawing_id": str(s.drawing), "actor_binding_id": str(s.actor),
            "checkout_fence": str(s.lease.fence), "tool_manifest_sha256": manifest,
            "parent_intake_sha256": ctx.intake_sha256, "initialized": False,
        }
        fingerprint = pg.jobs._fingerprint("r14-test-submission", {
            "nonce": str(uuid4()), "parameters": params, "context": execution})
        job = pg.db.run_transaction(lambda conn: pg.jobs.submit_project_graph_job(
            s.org, s.project, str(s.org), "solar-size-strings", params,
            f"r14-{uuid4()}", input_version_id=s.parent, execution_context=execution,
            submission_fingerprint=fingerprint, pinned_tier="hosted_pro", conn=conn))
        s.job = UUID(str(job["job_id"]))
        owner = f"r14-worker-{uuid4()}"
        row = pg.jobs.claim_project_graph_job(owner, tool_name="solar-size-strings",
                                              lease_seconds=120)
        assert row is not None and UUID(str(row["job_id"])) == s.job
        s.row = row
        s.scope = exchanges.ExchangeScope(s.org, s.project, s.drawing, s.job, s.actor,
            f"leaf:settings:{uuid4()}", str(s.org))
        s.attempt = exchanges.JobAttempt(owner, row["attempt"])
        s.grant = f"grant_{uuid4().hex}"
        s.context = ctx
        s.publication_request_id = uuid4()
        return s

    try:
        yield sizing
    finally:
        with pytest.raises(StopIteration):
            next(generator)


def _prepare(s, *, raw=REQUEST, classification="live", grant=None):
    return exchanges.prepare_exchange(s.scope, s.attempt, request_bytes=raw,
        request_sha256=sha256(raw).hexdigest(), grant_ref=s.grant if grant is None else grant,
        classification=classification)


def _capture(s, raw=RESPONSE):
    return exchanges.capture_response(s.scope, s.attempt, response_bytes=raw,
        response_sha256=sha256(raw).hexdigest())


def _evidence(s, raw=EVIDENCE):
    return exchanges.attach_evidence(s.scope, s.attempt, evidence_bytes=raw,
        evidence_sha256=sha256(raw).hexdigest())


def _respond(s):
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    assert _capture(s) == "stored"


def _ready(s):
    _respond(s)
    assert _evidence(s) == "stored"


def _read(s, *, conn=None):
    return exchanges.get_exchange(s.scope, s.attempt, conn=conn)


def _reclaim(s, *, owner=None):
    pg.expire_worker(s.job)
    row = pg.jobs.claim_project_graph_job(owner or f"r14-reclaim-{uuid4()}",
        tool_name="solar-size-strings", lease_seconds=120)
    assert row is not None and UUID(str(row["job_id"])) == s.job
    assert row["attempt"] == s.attempt.attempt + 1
    s.row = row
    s.attempt = exchanges.JobAttempt(row["lease_owner"], row["attempt"])
    return row


def _before_sql(monkeypatch, operation):
    with monkeypatch.context() as patch:
        patch.setattr(exchanges.db, "run_transaction", pg.blocked)
        patch.setattr(exchanges.db, "connection", pg.blocked)
        with pytest.raises(exchanges.ExchangeInputError) as exc:
            operation()
        assert str(exc.value) == "SIP_R14_EXCHANGE_INPUT_INVALID"


def _insert(s, **changes):
    args = {**exchanges._parameters(s.scope, s.attempt), "grant_ref": s.grant,
            "classification": "live", "request_bytes": REQUEST,
            "request_sha256": sha256(REQUEST).hexdigest(), **changes}
    names = ("job_id", "org_id", "project_id", "drawing_id", "actor_binding_id",
             "target_settings_id", "admitted_tenant", "grant_ref", "classification",
             "request_bytes", "request_sha256")
    sql = ("INSERT INTO project_sizing_exchanges (" + ",".join(names) + ") VALUES ("
           + ",".join("%(" + name + ")s" for name in names) + ")")
    with pg.db.transaction() as conn:
        conn.execute(sql, args)


def _check_failure(operation, constraint):
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        operation()
    assert exc.value.sqlstate == "23514"
    assert exc.value.diag.constraint_name == constraint


def _update(s, assignments, values):
    with pg.db.transaction() as conn:
        conn.execute("UPDATE project_sizing_exchanges SET " + assignments + " WHERE job_id=%s",
                     (*values, s.job))


def _immutable(s, assignments, values, message):
    with pytest.raises(psycopg.errors.RaiseException, match=message):
        _update(s, assignments, values)


def _completion(s):
    row = pg.load(s.job)
    execution = row["execution_context"]
    request = {
        "schema": "leaf.solar-project-graph-commit.v1",
        "organization_id": str(s.org), "project_id": str(s.project),
        "drawing_id": str(s.drawing), "parent_version_id": str(s.parent),
        "actor_binding_id": str(s.actor), "checkout_fence": execution["checkout_fence"],
        "job_id": str(s.job), "attempt": row["attempt"], "tool": row["tool_name"],
        "tool_manifest_sha256": execution["tool_manifest_sha256"],
        "parameters": row["params"], "parent_intake_sha256": execution["parent_intake_sha256"],
        "initialized": False,
    }
    output = deepcopy(s.context.intake)
    output["solar_design_graph"]["rev"] += 1
    output["solar_design_graph_sha256"] = pg.local.digest(output["solar_design_graph"])
    raw = pg.local.canonical_bytes(output)
    digest = sha256(raw).hexdigest()
    receipt = {**request, "output_intake_sha256": digest,
               "request_sha256": sha256(pg.local.canonical_bytes(request)).hexdigest(),
               "graph_sha256": output["solar_design_graph_sha256"]}
    s.output_bytes = raw
    return {"request": request, "output_intake_sha256": digest, "receipt": receipt}


def _publisher(s, completion):
    def publish_and_prove(row, conn):
        pg.jobs._validate_graph_request(row, completion["request"])
        digest = completion["output_intake_sha256"]
        key = pg.store_graph.publication_intake_key(s.org, s.project, s.drawing, digest)
        version = pg.store_graph.publish_version(s.org, s.project, s.drawing,
            expected_parent_version_id=s.parent, actor_binding_id=s.actor,
            expected_fence=s.lease.fence, request_id=s.publication_request_id,
            intake_sha256=digest, conn=conn,
            before_new=lambda: s.blobs.put_if_absent_or_verify(key, s.output_bytes))
        # The proof is in-process and checks the real R2 row and stored bytes.
        stored = pg.store.get_drawing_version(s.org, s.project, version.version_id, conn=conn)
        assert stored.drawing_id == s.drawing and stored.intake_ref == key
        assert sha256(s.blobs.data[key]).hexdigest() == digest
        assert stored.provenance["intake"]["sha256"] == digest
        return {**completion["receipt"], "output_version_id": str(version.version_id)}
    return publish_and_prove


def _complete(s, completion=None, publisher=None):
    completion = completion or _completion(s)
    return pg.db.run_transaction(lambda conn: exchanges.complete_exchange(
        s.scope, s.attempt, completion,
        publish_and_prove=publisher or _publisher(s, completion), conn=conn))


def _snapshot(s):
    return pg.census(s), deepcopy(pg.load(s.job)), _read(s)


@contextmanager
def _borrow(conn):
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def test_only_send_transition_winner_dispatches(world, monkeypatch):
    s = world()
    _prepare(s)
    barrier, mutex = Barrier(2), Lock()
    pids, dispatches = [], []
    transaction = pg.db.run_transaction

    def separate_transaction(operation, **kwargs):
        with pg.db.connection() as conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            conn.commit()
            with mutex:
                pids.append(pid)
            barrier.wait(timeout=10)
            with conn.transaction():
                result = operation(conn)
        return result

    def dispatch():
        won = exchanges.begin_send(s.scope, s.attempt)
        if won:
            # External dispatch occurs only after the committed ownership result.
            with mutex:
                dispatches.append(s.job)
        return won

    with monkeypatch.context() as patch:
        patch.setattr(exchanges.db, "run_transaction", separate_transaction)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == [False, True]
    assert len(pids) == 2 and len(set(pids)) == 2
    assert dispatches == [s.job]
    assert _read(s)["state"] == "send_started"
    assert transaction is pg.db.run_transaction


def test_reclaim_uses_stored_response(world):
    s = world()
    _respond(s)
    sender = s.attempt
    _reclaim(s)
    resumed = exchanges.resume_exchange(s.scope, s.attempt)
    assert resumed.action == "use_response"
    assert resumed.exchange["response_bytes"] == RESPONSE
    assert resumed.exchange["response_sha256"] == sha256(RESPONSE).hexdigest()
    assert (resumed.exchange["sender_worker"], resumed.exchange["sender_attempt"]) == (
        sender.worker_id, sender.attempt)
    assert not exchanges.begin_send(s.scope, s.attempt)
    with pytest.raises(exchanges.ExchangeLeaseLost):
        _capture(s)
    assert _evidence(s) == "stored"
    completion = _completion(s)
    assert _complete(s, completion)[0] == "applied"
    published = _read(s)
    assert (published["published_worker"], published["published_attempt"]) == (
        s.attempt.worker_id, s.attempt.attempt)
    assert published["sender_worker"] == sender.worker_id
    assert _complete(s, completion, pg.blocked)[0] == "duplicate"


def test_started_without_response_is_unknown(world):
    s = world()
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    sender = s.attempt
    _reclaim(s)
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "outcome_unknown"
    with pg.db.connection() as conn:
        row = _read(s, conn=conn)
        assert row["state"] == "outcome_unknown"
        assert row["refusal_code"] == "SIZING_OUTCOME_UNKNOWN"
        assert row["sender_worker"] == sender.worker_id and row["response_bytes"] is None
    assert not exchanges.begin_send(s.scope, s.attempt)
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "outcome_unknown"


def test_response_is_write_once(world):
    s = world()
    _respond(s)
    before = _read(s)
    with pytest.raises(exchanges.ExchangeConflict):
        _capture(s, b"different")
    _immutable(s, "response_bytes=%s,response_sha256=%s",
        (b"different", sha256(b"different").hexdigest()), "sizing response is immutable")
    assert _read(s) == before


def test_exchange_scope_is_job_scope(world, monkeypatch):
    s, other = world(), world()
    _prepare(s)
    valid = _read(s)
    for name, value in (
        ("org_id", other.org), ("project_id", other.project), ("drawing_id", other.drawing),
        ("job_id", other.job), ("actor_binding_id", other.actor),
        ("target_settings_id", other.scope.target_settings_id)):
        changes = {name: value}
        if name == "org_id":
            changes["admitted_tenant"] = str(other.org)
        assert exchanges.get_exchange(replace(s.scope, **changes), s.attempt) is None
    _before_sql(monkeypatch, lambda: exchanges.get_exchange(
        replace(s.scope, admitted_tenant=str(other.org)), s.attempt))
    assert exchanges.get_exchange(s.scope, replace(s.attempt, worker_id=other.attempt.worker_id)) is None
    assert exchanges.get_exchange(s.scope, replace(s.attempt, attempt=s.attempt.attempt + 1)) is None
    for scope, lease in (
        (replace(s.scope, job_id=str(s.job)), s.attempt),
        (replace(s.scope, target_settings_id="leaf:settings:invalid"), s.attempt),
        (s.scope, replace(s.attempt, attempt=True)),
        (s.scope, replace(s.attempt, attempt=2147483648)),
        (s.scope, replace(s.attempt, worker_id="bad\nworker")),
        (s.scope, replace(s.attempt, worker_id=" ")),
        (s.scope, replace(s.attempt, worker_id="w" * 201))):
        _before_sql(monkeypatch, lambda: exchanges.get_exchange(scope, lease))
    assert _read(s) == valid


def test_stale_attempt_cannot_publish(world, monkeypatch):
    s = world()
    _ready(s)
    old = s.attempt
    completion = _completion(s)
    with pg.db.connection() as first, pg.db.connection() as second:
        pids = [conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                for conn in (first, second)]
        assert pids[0] != pids[1]
        first.commit()
        second.commit()
        first.execute("UPDATE jobs SET lease_expires_at=clock_timestamp() WHERE job_id=%s", (s.job,))
        first.commit()
        with monkeypatch.context() as patch:
            patch.setattr(pg.jobs, "connection", lambda: _borrow(first))
            row = pg.jobs.claim_project_graph_job(f"r14-reclaim-{uuid4()}",
                tool_name="solar-size-strings", lease_seconds=120)
        assert UUID(str(row["job_id"])) == s.job and row["attempt"] == old.attempt + 1
        s.row = row
        s.attempt = exchanges.JobAttempt(row["lease_owner"], row["attempt"])
        before = _snapshot(s)
        with pytest.raises(exchanges.ExchangeLeaseLost):
            with second.transaction():
                exchanges.complete_exchange(s.scope, old, completion,
                    publish_and_prove=pg.blocked, conn=second)
        assert _snapshot(s) == before
        assert exchanges.get_exchange(s.scope, old, conn=second) is None


def test_request_byte_bounds(world, monkeypatch):
    s = world()
    maximum = b"x" * 65536
    assert _prepare(s, raw=maximum)["request_bytes"] == maximum
    for raw in (b"", b"x" * 65537, bytearray(b"x")):
        _before_sql(monkeypatch, lambda: exchanges.prepare_exchange(s.scope, s.attempt,
            request_bytes=raw, request_sha256=sha256(raw).hexdigest(), grant_ref=s.grant,
            classification="live"))
    fresh = world()
    _check_failure(lambda: _insert(fresh, request_bytes=b"x" * 65537,
        request_sha256=sha256(b"x" * 65537).hexdigest()), "project_sizing_exchanges_request_check")


def test_response_byte_bounds(world, monkeypatch):
    s = world()
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    before = _read(s)
    for raw in (b"", b"x" * 65537, bytearray(b"x")):
        _before_sql(monkeypatch, lambda: exchanges.capture_response(s.scope, s.attempt,
            response_bytes=raw, response_sha256=sha256(raw).hexdigest()))
    assert _read(s) == before and before["state"] == "send_started"
    raw = b"x" * 65537
    _check_failure(lambda: _update(s,
        "state='response_stored',response_bytes=%s,response_sha256=%s",
        (raw, sha256(raw).hexdigest())), "project_sizing_exchanges_response_check")
    raw = b"x" * 65536
    assert _capture(s, raw) == "stored" and _read(s)["response_bytes"] == raw


def test_evidence_byte_bounds(world, monkeypatch):
    s = world()
    _respond(s)
    before = _read(s)
    for raw in (b"", b"x" * 262145, bytearray(b"x")):
        _before_sql(monkeypatch, lambda: exchanges.attach_evidence(s.scope, s.attempt,
            evidence_bytes=raw, evidence_sha256=sha256(raw).hexdigest()))
    assert _read(s) == before
    raw = b"x" * 262145
    _check_failure(lambda: _update(s, "evidence_bytes=%s,evidence_sha256=%s",
        (raw, sha256(raw).hexdigest())), "project_sizing_exchanges_evidence_check")
    raw = b"x" * 262144
    assert _evidence(s, raw) == "stored" and _read(s)["evidence_bytes"] == raw


def test_public_grant_reference_refuses_token_shape(world, monkeypatch):
    s = world()
    for grant in ("Bearer secret", "eyJhbGciOiJIUzI1NiJ9.payload.signature", "", "x" * 65):
        _before_sql(monkeypatch, lambda: _prepare(s, grant=grant))
    assert _prepare(s)["grant_ref"] == s.grant
    assert "token" not in _read(s) and "authorization" not in _read(s)


def test_classification_check(world, monkeypatch):
    for label in ("live", "synthetic"):
        s = world()
        assert _prepare(s, classification=label)["classification"] == label
    s = world()
    for label in ("production", "LIVE", "", None):
        _before_sql(monkeypatch, lambda: _prepare(s, classification=label))
    _check_failure(lambda: _insert(s, classification="production"),
                   "project_sizing_exchanges_classification_check")
    assert _read(s) is None


def test_identical_response_capture_is_idempotent(world):
    s = world()
    _ready(s)
    before = _read(s)
    assert _capture(s) == "duplicate"
    assert _read(s) == before
    assert before["evidence_bytes"] == EVIDENCE
    assert before["updated_at"].tzinfo is not None


def test_outcome_unknown_is_terminal(world):
    s = world()
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    assert exchanges.record_outcome_unknown(s.scope, s.attempt) == "outcome_unknown"
    before = _read(s)
    assert exchanges.record_outcome_unknown(s.scope, s.attempt) == "duplicate"
    assert not exchanges.begin_send(s.scope, s.attempt)
    for operation in (lambda: _capture(s), lambda: _evidence(s),
        lambda: exchanges.refuse_exchange(s.scope, s.attempt, reason_code="cloud_upstream_failure"),
        lambda: _complete(s, publisher=pg.blocked)):
        with pytest.raises(exchanges.ExchangeStateError):
            operation()
    _immutable(s, "state='prepared',sender_worker=NULL,sender_attempt=NULL,refusal_code=NULL",
               (), "sizing exchange terminal state is immutable")
    assert _read(s) == before
    error = {"error_code": "GRAPH_JOB_FAILED", "reason_code": "SIZING_OUTCOME_UNKNOWN",
             "retryable": False}
    assert pg.jobs.fail_project_graph_job(s.job, s.attempt.worker_id, s.attempt.attempt, error) == "failed"
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "outcome_unknown"


def test_prepare_is_idempotent_and_conflicts(world):
    s = world()
    before = _prepare(s)
    assert _prepare(s) == before
    for changes in ({"raw": b"changed"}, {"grant": f"grant_{uuid4().hex}"},
                    {"classification": "synthetic"}):
        with pytest.raises(exchanges.ExchangeConflict):
            _prepare(s, **changes)
    with pytest.raises(exchanges.ExchangeNotFound):
        exchanges.prepare_exchange(
            replace(s.scope, target_settings_id=f"leaf:settings:{uuid4()}"), s.attempt,
            request_bytes=REQUEST, request_sha256=sha256(REQUEST).hexdigest(),
            grant_ref=s.grant, classification="live")
    with pg.db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM project_sizing_exchanges WHERE job_id=%s", (s.job,))
        assert cur.fetchone()["n"] == 1
    assert _read(s) == before


def test_reclaim_before_send_can_send_once(world):
    s = world()
    _prepare(s)
    old = s.attempt
    # Reusing a worker name still requires the new attempt.
    _reclaim(s, owner=old.worker_id)
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "send_ready"
    assert not exchanges.begin_send(s.scope, old)
    assert exchanges.begin_send(s.scope, s.attempt)
    assert not exchanges.begin_send(s.scope, s.attempt)
    assert _read(s)["sender_attempt"] == old.attempt + 1
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "awaiting_response"


def test_evidence_is_write_once(world):
    s = world()
    _ready(s)
    before = _read(s)
    assert _evidence(s) == "duplicate" and _read(s) == before
    with pytest.raises(exchanges.ExchangeConflict):
        _evidence(s, b"different evidence")
    _immutable(s, "evidence_bytes=%s,evidence_sha256=%s",
        (b"different evidence", sha256(b"different evidence").hexdigest()),
        "sizing evidence is immutable")
    assert _read(s) == before


def test_publication_failure_preserves_response(world, monkeypatch):
    s = world()
    _ready(s)
    completion = _completion(s)
    before = _snapshot(s)
    insert_outbox = pg.jobs._insert_outbox
    observed = []

    def fail_after_publication(cur, *args, **kwargs):
        cur.execute("SELECT state,output_version_id FROM project_sizing_exchanges WHERE job_id=%s",
                    (s.job,))
        row = cur.fetchone()
        assert row["state"] == "published" and row["output_version_id"] is not None
        cur.execute("SELECT count(*) AS n FROM history_operations WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        assert cur.fetchone()["n"] == before[0][1] + 1
        observed.append(row["output_version_id"])
        raise RuntimeError("failure after exchange publication")

    with monkeypatch.context() as patch:
        patch.setattr(pg.jobs, "_insert_outbox", fail_after_publication)
        with pytest.raises(RuntimeError, match="failure after exchange publication"):
            _complete(s, completion)
    assert len(observed) == 1 and pg.jobs._insert_outbox is insert_outbox
    assert _snapshot(s) == before
    assert _read(s)["response_bytes"] == RESPONSE
    assert _complete(s, completion)[0] == "applied"


def test_lease_expiry_during_publication_still_settles(world, monkeypatch):
    s = world()
    _ready(s)
    completion = _completion(s)
    insert_outbox = pg.jobs._insert_outbox
    observed = []

    def expire_after_publication(cur, *args, **kwargs):
        cur.execute("SELECT state FROM project_sizing_exchanges WHERE job_id=%s", (s.job,))
        assert cur.fetchone()["state"] == "published"
        insert_outbox(cur, *args, **kwargs)
        cur.execute("UPDATE jobs SET lease_expires_at=clock_timestamp() - interval '1 second' "
                    "WHERE job_id=%s", (s.job,))
        observed.append(True)

    with monkeypatch.context() as patch:
        patch.setattr(pg.jobs, "_insert_outbox", expire_after_publication)
        assert _complete(s, completion)[0] == "applied"
    assert observed == [True]
    assert pg.load(s.job)["status"] == "succeeded"
    assert _read(s)["response_bytes"] == RESPONSE and _read(s)["state"] == "published"


def test_lease_expired_before_completion_is_refused(world):
    s = world()
    _ready(s)
    completion = _completion(s)
    with pg.db.transaction() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=clock_timestamp() - interval '1 second' "
                     "WHERE job_id=%s", (s.job,))
    before = _snapshot(s)
    with pytest.raises(exchanges.ExchangeLeaseLost):
        _complete(s, completion)
    assert _snapshot(s) == before
    assert _read(s)["response_bytes"] == RESPONSE and _read(s)["state"] == "response_stored"


def test_known_refusal_is_terminal(world):
    s = world()
    _respond(s)
    assert exchanges.refuse_exchange(s.scope, s.attempt,
        reason_code="cloud_response_invalid") == "refused"
    before = _snapshot(s)
    assert exchanges.refuse_exchange(s.scope, s.attempt,
        reason_code="cloud_response_invalid") == "duplicate"
    assert not exchanges.begin_send(s.scope, s.attempt)
    for operation in (lambda: _capture(s), lambda: _evidence(s),
        lambda: exchanges.record_outcome_unknown(s.scope, s.attempt),
        lambda: exchanges.refuse_exchange(s.scope, s.attempt, reason_code="cloud_upstream_failure"),
        lambda: _complete(s, publisher=pg.blocked)):
        with pytest.raises(exchanges.ExchangeStateError):
            operation()
    _immutable(s, "state='prepared',refusal_code=NULL", (),
               "sizing exchange terminal state is immutable")
    assert _snapshot(s) == before
    assert _read(s)["response_bytes"] == RESPONSE
    # A reclaimed unresolved send cannot be recast as a definite refusal.
    pending = world()
    _prepare(pending)
    assert exchanges.begin_send(pending.scope, pending.attempt)
    _reclaim(pending)
    with pytest.raises(exchanges.ExchangeLeaseLost):
        exchanges.refuse_exchange(pending.scope, pending.attempt,
                                  reason_code="cloud_upstream_failure")
    assert _read(pending)["state"] == "send_started"


def test_migration_is_idempotent(world):
    s = world()
    _ready(s)
    before = _read(s)
    sql = (ROOT / "platform/migrations/0074_project_sizing_exchanges.sql").read_text(
        encoding="utf-8")
    for _ in range(2):
        with pg.db.transaction() as conn:
            for statement in pg.db._split_sql_statements(sql):
                conn.execute(statement)
    assert _read(s) == before
    with pg.db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM pg_class WHERE oid='project_sizing_exchanges'::regclass")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM pg_indexes WHERE tablename='project_sizing_exchanges' "
                    "AND indexname='idx_project_sizing_exchanges_scope'")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM pg_trigger WHERE "
                    "tgrelid='project_sizing_exchanges'::regclass "
                    "AND tgname='project_sizing_exchanges_guard' AND tgenabled='O'")
        assert cur.fetchone()["n"] == 1


def test_byte_digests_are_verified(world, monkeypatch):
    s = world()
    for digest in ("0" * 64, sha256(REQUEST).hexdigest().upper(), "invalid"):
        _before_sql(monkeypatch, lambda: exchanges.prepare_exchange(s.scope, s.attempt,
            request_bytes=REQUEST, request_sha256=digest, grant_ref=s.grant, classification="live"))
    _check_failure(lambda: _insert(s, request_sha256="0" * 64),
                   "project_sizing_exchanges_digest_check")
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    for digest in ("0" * 64, sha256(RESPONSE).hexdigest().upper(), "invalid"):
        _before_sql(monkeypatch, lambda: exchanges.capture_response(s.scope, s.attempt,
            response_bytes=RESPONSE, response_sha256=digest))
    _check_failure(lambda: _update(s,
        "state='response_stored',response_bytes=%s,response_sha256=%s",
        (RESPONSE, "0" * 64)), "project_sizing_exchanges_digest_check")
    assert _capture(s) == "stored"
    for digest in ("0" * 64, sha256(EVIDENCE).hexdigest().upper(), "invalid"):
        _before_sql(monkeypatch, lambda: exchanges.attach_evidence(s.scope, s.attempt,
            evidence_bytes=EVIDENCE, evidence_sha256=digest))
    _check_failure(lambda: _update(s, "evidence_bytes=%s,evidence_sha256=%s",
        (EVIDENCE, "0" * 64)), "project_sizing_exchanges_digest_check")
    assert _evidence(s) == "stored"


def test_exchange_requires_canonical_global_job(world):
    for field, value in (
        ("kind", "build"), ("tool_name", "solar-settings"),
        ("schema", "leaf.other-job.v1"), ("mode", "manual-global"),
        ("actor_binding_id", str(uuid4())), ("drawing_id", str(uuid4())),
        ("request_tenant_id", str(uuid4()))):
        s = world()
        row = pg.load(s.job)
        context, params = deepcopy(row["execution_context"]), deepcopy(row["params"])
        if field in ("kind", "tool_name", "request_tenant_id"):
            with pg.db.cursor() as cur:
                cur.execute("UPDATE jobs SET " + field + "=%s WHERE job_id=%s", (value, s.job))
        else:
            if field == "mode":
                params[field] = value
            else:
                context[field] = value
            with pg.db.cursor() as cur:
                cur.execute("UPDATE jobs SET execution_context=%s,params=%s WHERE job_id=%s",
                            (Jsonb(context), Jsonb(params), s.job))
        with pytest.raises(exchanges.ExchangeLeaseLost):
            _prepare(s)
        assert not exchanges.begin_send(s.scope, s.attempt)
        assert _read(s) is None


def test_python_worker_strip(world, monkeypatch):
    s = world()
    worker = s.attempt.worker_id
    whitespace = [0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
                  0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0x85]
    for codepoint in whitespace:
        for value in (chr(codepoint) + worker, worker + chr(codepoint)):
            _before_sql(monkeypatch, lambda: exchanges.get_exchange(
                s.scope, replace(s.attempt, worker_id=value)))
    # Non-control whitespace is allowed inside a worker, including U+3000.
    for value in (worker, worker + "\u3000x", worker + " x"):
        assert exchanges._parameters(s.scope, replace(s.attempt, worker_id=value))["worker"] == value


def test_python_worker_length(world, monkeypatch):
    s = world()
    for length in (0, 201):
        value = (s.attempt.worker_id * 5)[:length]
        assert len(value) == length
        _before_sql(monkeypatch, lambda: exchanges.get_exchange(
            s.scope, replace(s.attempt, worker_id=value)))
    for length in (1, 200):
        value = (s.attempt.worker_id * 5)[:length]
        assert len(value) == length
        assert exchanges._parameters(s.scope, replace(s.attempt, worker_id=value))["worker"] == value


def test_python_worker_cc(world, monkeypatch):
    s = world()

    class Worker(str):
        pass

    invalid = [None, 1, b"worker", Worker(s.attempt.worker_id)]
    # All Cc values (including embedded tab and U+0085), plus Cs endpoints.
    for codepoint in (*range(0x20), *range(0x7F, 0xA0), 0xD800, 0xDFFF):
        invalid.append(s.attempt.worker_id + chr(codepoint) + "x")
    for value in invalid:
        _before_sql(monkeypatch, lambda: exchanges.get_exchange(
            s.scope, replace(s.attempt, worker_id=value)))
    # Accepted neighbours of the Cc and Cs ranges; Cf is not prohibited.
    for codepoint in (0x20, 0x7E, 0xA0, 0xD7FF, 0xE000, 0x200B):
        value = s.attempt.worker_id + chr(codepoint) + "x"
        assert exchanges._parameters(s.scope, replace(s.attempt, worker_id=value))["worker"] == value


def _db_worker_cases(world, invalid, accepted):
    for field in ("sender_worker", "published_worker"):
        for index, worker in enumerate(accepted):
            s = world()
            if field == "sender_worker":
                _prepare(s)
                assignments = "state='send_started',sender_worker=%s,sender_attempt=1"
                values = lambda value: (value,)
            else:
                _ready(s)
                # Satisfy completion so a worker CHECK failure is isolated.
                with pg.db.transaction() as conn:
                    conn.execute("UPDATE jobs SET status='succeeded',output_version_id=%s "
                                 "WHERE job_id=%s", (s.parent, s.job))
                assignments = ("state='published',published_worker=%s,published_attempt=1,"
                               "output_version_id=%s")
                values = lambda value: (value, s.parent)
            if index == 0:
                for value in invalid:
                    _check_failure(lambda: _update(s, assignments, values(value)),
                                   "project_sizing_exchanges_worker_check")
            _update(s, assignments, values(worker))
            with pg.db.cursor() as cur:
                cur.execute("SELECT sender_worker,published_worker FROM project_sizing_exchanges "
                            "WHERE job_id=%s", (s.job,))
                assert cur.fetchone()[field] == worker


def test_db_worker_strip(world):
    worker = f"r14-strip-{uuid4()}"
    whitespace = [0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
                  0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0x85]
    invalid = [value for cp in whitespace for value in (chr(cp) + worker, worker + chr(cp))]
    interior = worker + "".join(chr(cp) for cp in whitespace[:-1]) + "x"
    _db_worker_cases(world, invalid, (worker, interior))


def test_db_worker_cc(world):
    worker = f"r14-cc-{uuid4()}"
    # NUL and surrogates cannot be encoded as PostgreSQL text. All other Cc
    # characters must be refused by the CHECK itself with SQLSTATE 23514.
    invalid = [worker + chr(cp) + "x" for cp in (*range(1, 0x20), *range(0x7F, 0xA0))]
    accepted = worker + "\u007e\u00a0\ud7ff\ue000\u200bx"
    _db_worker_cases(world, invalid, (accepted,))


def test_db_worker_length(world):
    worker = f"r14-length-{uuid4()}" * 5
    _db_worker_cases(world, ("", worker[:201]), (worker[:1], worker[:200]))


def _direct_publish(s, conn, output_version_id):
    row = conn.execute(
        "UPDATE project_sizing_exchanges SET state='published',output_version_id=%s,"
        "published_worker=%s,published_attempt=%s WHERE job_id=%s RETURNING state",
        (output_version_id, s.attempt.worker_id, s.attempt.attempt, s.job)).fetchone()
    assert row["state"] == "published"


def _completion_commit_failure(conn):
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        conn.commit()
    assert exc.value.sqlstate == "23514"
    assert exc.value.diag.message_primary == "project sizing exchange completion rule violated"
    conn.rollback()


def test_db_publish_running(world):
    s = world()
    _ready(s)
    before = _snapshot(s)
    assert before[1]["status"] == "running"
    with pg.db.connection() as conn:
        _direct_publish(s, conn, s.parent)
        _completion_commit_failure(conn)
    assert _snapshot(s) == before
    assert _read(s)["state"] == "response_stored"


def test_db_publish_wrong_output(world):
    s = world()
    _ready(s)
    completion = _completion(s)
    # Complete through R4 without publishing the exchange, then attempt to
    # publish it against another existing version in the same drawing.
    result = pg.db.run_transaction(lambda conn: pg.jobs.complete_project_graph_job(
        s.job, s.attempt.worker_id, s.attempt.attempt, completion,
        publish_and_prove=_publisher(s, completion), conn=conn))
    assert result[0] == "applied"
    before = _snapshot(s)
    assert before[1]["status"] == "succeeded"
    assert UUID(str(before[1]["output_version_id"])) != s.parent
    with pg.db.connection() as conn:
        _direct_publish(s, conn, s.parent)
        _completion_commit_failure(conn)
    assert _snapshot(s) == before
    assert _read(s)["state"] == "response_stored"


def test_db_job_invalidates_publication(world):
    s = world()
    _ready(s)
    assert _complete(s)[0] == "applied"
    before = _snapshot(s)
    assert before[2]["state"] == "published"
    assert before[1]["status"] == "succeeded"
    assert UUID(str(before[1]["output_version_id"])) != s.parent
    for assignments, values in (
        ("status=%s", ("running",)),
        ("output_version_id=%s", (s.parent,)),
    ):
        with pg.db.connection() as conn:
            changed = conn.execute("UPDATE jobs SET " + assignments + " WHERE job_id=%s",
                                   (*values, s.job))
            assert changed.rowcount == 1
            _completion_commit_failure(conn)
        assert _snapshot(s) == before
