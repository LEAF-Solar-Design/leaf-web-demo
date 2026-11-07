"""PostgreSQL proofs of scoped solve proposal custody."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
_admission_spec = importlib.util.spec_from_file_location(
    "_sip_w1c_pg_admission_helpers", ROOT / "platform/tests/test_sip_r5_admission.py")
admission = importlib.util.module_from_spec(_admission_spec)
_admission_spec.loader.exec_module(admission)
pg = admission.pg
_chain_spec = importlib.util.spec_from_file_location(
    "_sip_w1c_pg_chain_helpers", ROOT / "platform/tests/test_sip_r3b_chain.py")
chain = importlib.util.module_from_spec(_chain_spec)
_chain_spec.loader.exec_module(chain)

from leaf_platform import project_solve_store as exchanges

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

REQUEST = b'{"phase":"initial"}'
RESPONSE = b'{"solve":"captured"}'
CANDIDATE = b'{"candidate":"opaque"}'
PROOF = b'{"proof":"opaque"}'


def _claim(s, owner):
    # Tool-wide claims can otherwise pick an expired sibling from an earlier
    # subcase or a previous suite run. Keep the real claim/lease SQL, restricting
    # both its exhausted-attempt sweep and candidate selection to this job.
    population = pg.jobs._graph_population
    job_id = UUID(str(s.job))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pg.jobs, "_graph_population",
            lambda: population() + f" AND j.job_id='{job_id}'::uuid")
        return pg.jobs.claim_project_graph_job(owner,
            tool_name="solar-solve-proposal", lease_seconds=120)


@pytest.fixture
def world(make_org, monkeypatch):
    generator = admission.world.__wrapped__(make_org, monkeypatch)
    fresh = next(generator)

    def solve():
        s = fresh()
        # Reuse the existing graph import helper without submitting any other jobs.
        ctx = chain.commit.__globals__["panels_parent"](s)
        s.parent = ctx.parent_version_id
        params = {"expected_rev": ctx.graph["rev"], "mode": "global",
                  "drawing_id": str(s.drawing)}
        manifest = sha256(b"synthetic solve test manifest").hexdigest()
        execution = {
            "schema": pg.jobs.PROJECT_GRAPH_JOB_SCHEMA,
            "authority_mode": "postgres_canonical", "execution_path": "local",
            "drawing_id": str(s.drawing), "actor_binding_id": str(s.actor),
            "checkout_fence": str(s.lease.fence), "tool_manifest_sha256": manifest,
            "parent_intake_sha256": ctx.intake_sha256, "initialized": False,
            "solve_binding": {"frame_id": "world", "phase": "initial",
                "expected_rev": ctx.graph["rev"], "graph_sha256": pg.local.digest(ctx.graph),
                "request": {}},
        }
        fingerprint = pg.jobs._fingerprint("w1c-test-submission", {
            "nonce": str(uuid4()), "parameters": params, "context": execution})
        job = pg.db.run_transaction(lambda conn: pg.jobs.submit_project_graph_job(
            s.org, s.project, str(s.org), "solar-solve-proposal", params,
            f"w1c-{uuid4()}", input_version_id=s.parent, execution_context=execution,
            submission_fingerprint=fingerprint, pinned_tier="hosted_pro", conn=conn))
        s.job = UUID(str(job["job_id"]))
        owner = f"w1c-worker-{uuid4()}"
        row = _claim(s, owner)
        assert row is not None and UUID(str(row["job_id"])) == s.job
        s.row = row
        s.scope = exchanges.ExchangeScope(s.org, s.project, s.drawing, s.job)
        s.attempt = exchanges.JobAttempt(owner, row["attempt"])
        s.grant = f"grant_{uuid4().hex}"
        s.context = ctx
        return s

    try:
        yield solve
    finally:
        with pytest.raises(StopIteration):
            next(generator)


def _prepare(s, **changes):
    args = dict(request_bytes=REQUEST, request_sha256=sha256(REQUEST).hexdigest(),
                grant_ref=s.grant, classification="live", input_version_id=s.parent,
                parent_intake_sha256=s.context.intake_sha256,
                graph_revision=s.context.graph["rev"],
                graph_sha256=pg.local.digest(s.context.graph), frame_id="world", phase="initial")
    return exchanges.prepare_exchange(s.scope, s.attempt, **{**args, **changes})


def _capture(s, **changes):
    args = dict(response_bytes=RESPONSE, response_sha256=sha256(RESPONSE).hexdigest(),
                solver_endpoint="https://solver.example/solve", solver_version="test-v1")
    return exchanges.capture_response(s.scope, s.attempt, **{**args, **changes})


def _complete(s, **changes):
    args = dict(candidate_bytes=CANDIDATE, candidate_sha256=sha256(CANDIDATE).hexdigest(),
                proof_bytes=PROOF, proof_sha256=sha256(PROOF).hexdigest())
    return exchanges.complete_exchange(s.scope, s.attempt, **{**args, **changes})


def _respond(s):
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    assert _capture(s) == "stored"


def _read(s):
    return exchanges.get_exchange(s.scope, s.attempt)


def _completed(s, scope=None):
    scope = scope or s.scope
    with pg.db.connection() as conn:
        return exchanges.read_completed_exchange(
            scope.org_id, scope.project_id, scope.drawing_id, scope.job_id, conn=conn)


def _reclaim(s):
    pg.expire_worker(s.job)
    row = _claim(s, f"w1c-reclaim-{uuid4()}")
    assert row is not None and UUID(str(row["job_id"])) == s.job
    assert row["attempt"] == s.attempt.attempt + 1
    s.attempt = exchanges.JobAttempt(row["lease_owner"], row["attempt"])


def test_claims_ignore_expired_sibling_jobs(world):
    sibling = world()
    pg.expire_worker(sibling.job)
    before = pg.load(sibling.job)

    s = world()
    assert s.job != sibling.job
    _reclaim(s)

    assert pg.load(sibling.job) == before


def _before_sql(monkeypatch, operation):
    with monkeypatch.context() as patch:
        patch.setattr(exchanges.db, "run_transaction", pg.blocked)
        patch.setattr(exchanges.db, "connection", pg.blocked)
        with pytest.raises(exchanges.ExchangeInputError):
            operation()


def test_only_committed_send_winner(world, monkeypatch):
    s = world()
    _prepare(s)
    barrier, mutex = Barrier(2), Lock()
    pids, dispatches = [], []

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
            # The winner is visible on a separate connection after commit.
            assert _read(s)["state"] == "sending"
            with mutex:
                dispatches.append(s.job)
        return won

    with monkeypatch.context() as patch:
        patch.setattr(exchanges.db, "run_transaction", separate_transaction)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == [False, True]
    assert len(set(pids)) == 2 and dispatches == [s.job]
    assert _capture(s) == "stored"


def test_only_sender_captures_after_reclaim(world):
    s = world()
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    old = s.attempt
    _reclaim(s)
    before = _read(s)
    for lease in (old, s.attempt):
        with pytest.raises(exchanges.ExchangeLeaseLost):
            exchanges.capture_response(s.scope, lease, response_bytes=RESPONSE,
                response_sha256=sha256(RESPONSE).hexdigest(),
                solver_endpoint="https://solver.example/solve", solver_version="test-v1")
    assert _read(s) == before


def test_resume_reuses_captured_response(world):
    s = world()
    _respond(s)
    sender = s.attempt
    _reclaim(s)
    result = exchanges.resume_exchange(s.scope, s.attempt)
    assert result.action == "use_response"
    assert result.exchange["response_bytes"] == RESPONSE
    assert result.exchange["sender_worker"] == sender.worker_id
    assert not exchanges.begin_send(s.scope, s.attempt)
    assert _complete(s) == "completed"


def test_resume_settles_uncaptured_send_even_same_sender(world):
    for reclaim in (False, True):
        s = world()
        _prepare(s)
        assert exchanges.begin_send(s.scope, s.attempt)
        if reclaim:
            _reclaim(s)
        result = exchanges.resume_exchange(s.scope, s.attempt)
        assert result.action == "outcome_unknown"
        assert result.exchange["response_bytes"] is None
        assert not exchanges.begin_send(s.scope, s.attempt)
        assert exchanges.resume_exchange(s.scope, s.attempt).action == "outcome_unknown"


def test_prepared_reclaim_sends_once(world):
    s = world()
    _prepare(s)
    old = s.attempt
    _reclaim(s)
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "send_ready"
    assert not exchanges.begin_send(s.scope, old)
    assert exchanges.begin_send(s.scope, s.attempt)
    assert not exchanges.begin_send(s.scope, s.attempt)


def test_complete_freezes_candidate_and_proof(world):
    s = world()
    _respond(s)
    assert _complete(s) == "completed"
    before = _read(s)
    assert before["candidate_bytes"] == CANDIDATE and before["proof_bytes"] == PROOF
    for name in ("candidate", "proof"):
        raw = b"different"
        with pytest.raises(exchanges.ExchangeConflict):
            _complete(s, **{name + "_bytes": raw, name + "_sha256": sha256(raw).hexdigest()})
    assert _read(s) == before and _completed(s) == before


def test_terminal_rows_reject_all_transitions(world):
    for state in ("completed", "refused", "outcome_unknown"):
        s = world()
        _prepare(s)
        if state == "refused":
            exchanges.refuse_exchange(s.scope, s.attempt, reason_code="cloud_auth_missing")
        else:
            assert exchanges.begin_send(s.scope, s.attempt)
            if state == "completed":
                _capture(s)
                _complete(s)
            else:
                exchanges.record_outcome_unknown(s.scope, s.attempt)
        before = _read(s)
        assert not exchanges.begin_send(s.scope, s.attempt)
        for operation in (lambda: _capture(s), lambda: _complete(s),
                lambda: exchanges.refuse_exchange(s.scope, s.attempt, reason_code="cloud_response_invalid"),
                lambda: exchanges.record_outcome_unknown(s.scope, s.attempt)):
            with pytest.raises((exchanges.ExchangeStateError, exchanges.ExchangeLeaseLost)):
                operation()
        assert _read(s) == before
        assert exchanges.resume_exchange(s.scope, s.attempt).action == state


def test_expired_and_lost_lease_cannot_write(world):
    for reclaim in (False, True):
        for stage in ("prepared", "sending", "responded"):
            s = world()
            _prepare(s)
            if stage != "prepared":
                assert exchanges.begin_send(s.scope, s.attempt)
            if stage == "responded":
                _capture(s)
            old = s.attempt
            before = _read(s)
            if reclaim:
                _reclaim(s)
                s.attempt = old
            else:
                pg.expire_worker(s.job)
            assert not exchanges.begin_send(s.scope, old)
            for operation in (lambda: _prepare(s), lambda: _capture(s), lambda: _complete(s),
                    lambda: exchanges.refuse_exchange(s.scope, old, reason_code="cloud_auth_missing"),
                    lambda: exchanges.record_outcome_unknown(s.scope, old),
                    lambda: exchanges.resume_exchange(s.scope, old)):
                with pytest.raises((exchanges.ExchangeLeaseLost, exchanges.ExchangeNotFound)):
                    operation()
            with pg.db.connection() as conn:
                row = conn.execute("SELECT * FROM project_solve_exchanges WHERE job_id=%s",
                                   (s.job,)).fetchone()
            assert exchanges._exchange(row) == before


def test_request_bounds_and_digest_before_sql(world, monkeypatch):
    s = world()
    for raw in (b"", b"x" * (exchanges.MAX_REQUEST_BYTES + 1)):
        _before_sql(monkeypatch, lambda: _prepare(s, request_bytes=raw,
            request_sha256=sha256(raw).hexdigest()))
    for digest in ("0" * 64, "INVALID", sha256(REQUEST).hexdigest().upper()):
        _before_sql(monkeypatch, lambda: _prepare(s, request_sha256=digest))
    for raw in (bytearray(REQUEST), memoryview(REQUEST), None):
        _before_sql(monkeypatch, lambda: _prepare(s, request_bytes=raw))
    with pytest.raises(exchanges.ExchangeNotFound):
        _read(s)


def test_response_bounds_and_digest_before_sql(world, monkeypatch):
    s = world()
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    before = _read(s)
    for raw in (b"", b"x" * (exchanges.MAX_RESPONSE_BYTES + 1)):
        _before_sql(monkeypatch, lambda: _capture(s, response_bytes=raw,
            response_sha256=sha256(raw).hexdigest()))
    for digest in ("0" * 64, "bad"):
        _before_sql(monkeypatch, lambda: _capture(s, response_sha256=digest))
    assert _read(s) == before


def test_candidate_and_proof_bounds_and_digests_before_sql(world, monkeypatch):
    s = world()
    _respond(s)
    before = _read(s)
    for name, bound in (("candidate", exchanges.MAX_CANDIDATE_BYTES), ("proof", exchanges.MAX_PROOF_BYTES)):
        for raw in (b"", b"x" * (bound + 1)):
            _before_sql(monkeypatch, lambda: _complete(s,
                **{name + "_bytes": raw, name + "_sha256": sha256(raw).hexdigest()}))
        for digest in ("0" * 64, "bad"):
            _before_sql(monkeypatch, lambda: _complete(s, **{name + "_sha256": digest}))
    assert _read(s) == before


def test_exact_byte_bounds_are_accepted(world):
    s = world()
    raw = b"x" * exchanges.MAX_REQUEST_BYTES
    _prepare(s, request_bytes=raw, request_sha256=sha256(raw).hexdigest())
    assert exchanges.begin_send(s.scope, s.attempt)
    raw = b"x" * exchanges.MAX_RESPONSE_BYTES
    _capture(s, response_bytes=raw, response_sha256=sha256(raw).hexdigest())
    candidate, proof = b"x" * exchanges.MAX_CANDIDATE_BYTES, b"x" * exchanges.MAX_PROOF_BYTES
    _complete(s, candidate_bytes=candidate, candidate_sha256=sha256(candidate).hexdigest(),
              proof_bytes=proof, proof_sha256=sha256(proof).hexdigest())
    assert _completed(s)["proof_bytes"] == proof
    before = _completed(s)
    # Exercise the SQL CHECKs independently of the Python validation. The
    # enclosing transaction restores the deleted original after each refusal.
    for name, bound in (("request", exchanges.MAX_REQUEST_BYTES),
                        ("response", exchanges.MAX_RESPONSE_BYTES),
                        ("candidate", exchanges.MAX_CANDIDATE_BYTES),
                        ("proof", exchanges.MAX_PROOF_BYTES)):
        for raw in (b"", b"x" * (bound + 1)):
            changed = {**before, name + "_bytes": raw,
                       name + "_sha256": sha256(raw).hexdigest()}
            with pytest.raises(psycopg.errors.CheckViolation):
                with pg.db.transaction() as conn:
                    conn.execute("DELETE FROM project_solve_exchanges WHERE job_id=%s", (s.job,))
                    names = tuple(changed)
                    conn.execute("INSERT INTO project_solve_exchanges (" + ",".join(names) +
                        ") VALUES (" + ",".join("%(" + key + ")s" for key in names) + ")", changed)
            assert _completed(s) == before


def test_metadata_validation_before_sql(world, monkeypatch):
    s = world()
    for changes in ({"graph_revision": -1}, {"graph_revision": True},
            {"graph_revision": 2147483648}, {"parent_intake_sha256": "bad"},
            {"graph_sha256": "X" * 64}, {"frame_id": ""}, {"frame_id": "x" * 201},
            {"frame_id": "world\u0001"}, {"phase": "other"}, {"phase": None},
            {"grant_ref": "secret token"}, {"classification": "production"},
            {"input_version_id": str(s.parent)}):
        _before_sql(monkeypatch, lambda: _prepare(s, **changes))
    for name, bound in (("solver_endpoint", 2048), ("solver_version", 200)):
        for value in ("", "x" * (bound + 1), " x", "x\u0001"):
            _before_sql(monkeypatch, lambda: _capture(s, **{name: value}))


def test_prepare_is_idempotent_and_conflicts(world):
    s = world()
    before = _prepare(s)
    assert _prepare(s) == before
    for changes in ({"grant_ref": "another"}, {"classification": "synthetic"},
            {"graph_revision": before["graph_revision"] + 1}, {"graph_sha256": "0" * 64},
            {"frame_id": "other"}, {"phase": "background"},
            {"input_version_id": uuid4()}, {"parent_intake_sha256": "0" * 64},
            {"request_bytes": b"other", "request_sha256": sha256(b"other").hexdigest()}):
        with pytest.raises(exchanges.ExchangeConflict):
            _prepare(s, **changes)
    assert _read(s) == before


def test_response_is_write_once(world):
    s = world()
    _respond(s)
    before = _read(s)
    assert _capture(s) == "duplicate"
    for changes in ({"response_bytes": b"other", "response_sha256": sha256(b"other").hexdigest()},
                    {"solver_endpoint": "https://other.example"}, {"solver_version": "v2"}):
        with pytest.raises(exchanges.ExchangeConflict):
            _capture(s, **changes)
    assert _read(s) == before


def test_all_functions_isolate_org_project_drawing_and_job(world):
    s = world()
    _respond(s)
    _complete(s)
    before = _read(s)
    for name in ("org_id", "project_id", "drawing_id", "job_id"):
        scope = replace(s.scope, **{name: uuid4()})
        other = SimpleNamespace(**{**vars(s), "scope": scope})
        for operation in (lambda: _read(other), lambda: _prepare(other), lambda: _capture(other),
                lambda: _complete(other), lambda: _completed(other),
                lambda: exchanges.resume_exchange(scope, s.attempt),
                lambda: exchanges.refuse_exchange(scope, s.attempt, reason_code="cloud_auth_missing"),
                lambda: exchanges.record_outcome_unknown(scope, s.attempt)):
            with pytest.raises(exchanges.ExchangeNotFound):
                operation()
        assert not exchanges.begin_send(scope, s.attempt)
    assert _read(s) == before


def test_reader_requires_completed_and_no_lease(world):
    s = world()
    _prepare(s)
    with pytest.raises(exchanges.ExchangeStateError):
        _completed(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    _capture(s)
    with pytest.raises(exchanges.ExchangeStateError):
        _completed(s)
    _complete(s)
    before = _completed(s)
    pg.expire_worker(s.job)
    assert _completed(s) == before


def test_reader_rejects_corrupt_stored_digests(world):
    s = world()
    _respond(s)
    _complete(s)
    for name in ("request", "response", "candidate", "proof"):
        with pg.db.connection() as conn:
            try:
                # Simulate damaged custody, then roll back both corruption and
                # temporary guard removal; the reader must check actual bytes.
                conn.execute("ALTER TABLE project_solve_exchanges DISABLE TRIGGER "
                             "project_solve_exchanges_guard")
                conn.execute("ALTER TABLE project_solve_exchanges DROP CONSTRAINT "
                             "project_solve_exchanges_digest_check")
                conn.execute("UPDATE project_solve_exchanges SET " + name +
                             "_sha256=%s WHERE job_id=%s", ("0" * 64, s.job))
                with pytest.raises(exchanges.ExchangeInputError):
                    exchanges.read_completed_exchange(
                        s.org, s.project, s.drawing, s.job, conn=conn)
            finally:
                conn.rollback()
    assert _completed(s)["candidate_bytes"] == CANDIDATE


def test_enclosing_transaction_rolls_back_prepare_capture_and_complete(world):
    s = world()
    with pytest.raises(RuntimeError, match="rollback"):
        with pg.db.transaction() as conn:
            _prepare(s, conn=conn)
            raise RuntimeError("rollback")
    with pytest.raises(exchanges.ExchangeNotFound):
        _read(s)
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    before = _read(s)
    with pytest.raises(RuntimeError, match="rollback"):
        with pg.db.transaction() as conn:
            _capture(s, conn=conn)
            raise RuntimeError("rollback")
    assert _read(s) == before
    _capture(s)
    before = _read(s)
    with pytest.raises(RuntimeError, match="rollback"):
        with pg.db.transaction() as conn:
            _complete(s, conn=conn)
            raise RuntimeError("rollback")
    assert _read(s) == before


def test_database_guards_immutable_bytes_and_terminal_rows(world):
    s = world()
    _respond(s)
    before = _read(s)
    with pytest.raises(psycopg.errors.RaiseException, match="response is immutable"):
        with pg.db.transaction() as conn:
            conn.execute("UPDATE project_solve_exchanges SET response_bytes=%s,response_sha256=%s "
                         "WHERE job_id=%s", (b"other", sha256(b"other").hexdigest(), s.job))
    assert _read(s) == before
    _complete(s)
    before = _read(s)
    for assignments in ("state='prepared'", "updated_at=clock_timestamp()",
                        "candidate_bytes='other'::bytea", "proof_bytes='other'::bytea"):
        with pytest.raises(psycopg.errors.RaiseException, match="terminal state is immutable"):
            with pg.db.transaction() as conn:
                conn.execute("UPDATE project_solve_exchanges SET " + assignments + " WHERE job_id=%s", (s.job,))
    assert _read(s) == before


def test_refusal_from_prepared_sending_and_responded(world):
    for state, reason in (("prepared", "cloud_auth_missing"),
                          ("sending", "cloud_upstream_failure"),
                          ("responded", "cloud_response_invalid")):
        s = world()
        _prepare(s)
        if state != "prepared":
            assert exchanges.begin_send(s.scope, s.attempt)
        if state == "responded":
            _capture(s)
        before = _read(s)
        assert exchanges.refuse_exchange(s.scope, s.attempt, reason_code=reason) == "refused"
        refused = _read(s)
        assert refused == {**before, "state": "refused", "refusal_code": reason,
                           "updated_at": refused["updated_at"]}
        assert exchanges.refuse_exchange(s.scope, s.attempt, reason_code=reason) == "duplicate"
        with pytest.raises(exchanges.ExchangeStateError):
            exchanges.refuse_exchange(s.scope, s.attempt, reason_code="cloud_request_invalid")
        result = exchanges.resume_exchange(s.scope, s.attempt)
        assert result.action == "refused" and result.exchange == refused
        assert _read(s) == refused


def test_only_sender_can_refuse_sending_after_reclaim(world):
    s = world()
    _prepare(s)
    assert exchanges.begin_send(s.scope, s.attempt)
    old = s.attempt
    _reclaim(s)
    before = _read(s)
    for lease in (old, s.attempt):
        with pytest.raises(exchanges.ExchangeLeaseLost):
            exchanges.refuse_exchange(s.scope, lease, reason_code="cloud_upstream_failure")
        assert _read(s) == before


def test_reclaimed_lease_can_refuse_responded(world):
    s = world()
    _respond(s)
    before = _read(s)
    _reclaim(s)
    assert exchanges.refuse_exchange(s.scope, s.attempt,
        reason_code="cloud_response_invalid") == "refused"
    refused = _read(s)
    assert refused["sender_worker"] == before["sender_worker"]
    assert refused["sender_attempt"] == before["sender_attempt"]
    assert refused["response_bytes"] == before["response_bytes"]
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "refused"


def test_database_guards_refused_rows_from_all_three_states(world):
    for state in ("prepared", "sending", "responded"):
        s = world()
        _prepare(s)
        if state != "prepared":
            assert exchanges.begin_send(s.scope, s.attempt)
        if state == "responded":
            _capture(s)
        exchanges.refuse_exchange(s.scope, s.attempt, reason_code="cloud_response_invalid")
        before = _read(s)
        for assignments in ("state='prepared'", "updated_at=clock_timestamp()",
                "refusal_code='cloud_auth_missing'", "sender_worker='other'",
                "response_bytes='other'::bytea", "candidate_bytes='other'::bytea",
                "proof_bytes='other'::bytea"):
            with pytest.raises(psycopg.errors.RaiseException, match="terminal state is immutable"):
                with pg.db.transaction() as conn:
                    conn.execute("UPDATE project_solve_exchanges SET " + assignments +
                                 " WHERE job_id=%s", (s.job,))
        assert _read(s) == before


def test_migration_is_idempotent(world):
    s = world()
    _respond(s)
    before = _read(s)
    sql = (ROOT / "platform/migrations/0078_project_solve_exchanges.sql").read_text(encoding="utf-8")
    for _ in range(2):
        with pg.db.transaction() as conn:
            for statement in pg.db._split_sql_statements(sql):
                conn.execute(statement)
    assert _read(s) == before


PRINCIPAL = sha256(b"test-issuer|test-subject").hexdigest()


def _begin_async(s, *, aged=False):
    _prepare(s)
    if aged:
        # Construct historical custody at the legal send transition. Updating
        # sent_at afterwards is deliberately forbidden by the new guard.
        with pg.db.transaction() as conn:
            conn.execute("UPDATE project_solve_exchanges SET state='sending',"
                "sender_worker=%s,sender_attempt=%s,send_protocol='async-idempotent-v1',"
                "sent_at=now()-interval '4 hours',send_principal_sha256=%s WHERE job_id=%s",
                (s.attempt.worker_id, s.attempt.attempt, PRINCIPAL, s.job))
    else:
        assert exchanges.begin_send(s.scope, s.attempt, send_protocol="async-idempotent-v1",
                                    send_principal_sha256=PRINCIPAL)
    return _read(s)


def test_async_send_custody_and_resume_without_handle(world):
    s = world()
    row = _begin_async(s)
    assert row["send_protocol"] == "async-idempotent-v1"
    assert row["sent_at"] is not None and row["send_principal_sha256"] == PRINCIPAL
    assert row["grid_id"] is None
    assert exchanges.resume_exchange(s.scope, s.attempt,
        send_principal_sha256=PRINCIPAL).action == "resubmit_same_key"
    with pytest.raises(exchanges.ExchangeConflict):
        exchanges.resume_exchange(s.scope, s.attempt, send_principal_sha256="0" * 64)
    assert _read(s) == row


def test_async_record_grid_is_live_scoped_and_write_once(world):
    s = world()
    _begin_async(s)
    grid_id = uuid4()
    row = exchanges.record_grid(s.scope, s.attempt, grid_id)
    assert row["grid_id"] == grid_id
    assert exchanges.record_grid(s.scope, s.attempt, grid_id) == row
    with pytest.raises(exchanges.ExchangeConflict):
        exchanges.record_grid(s.scope, s.attempt, uuid4())
    with pytest.raises(exchanges.ExchangeInputError):
        exchanges.record_grid(s.scope, s.attempt, str(grid_id))
    for field in ("org_id", "project_id", "drawing_id", "job_id"):
        with pytest.raises(exchanges.ExchangeNotFound):
            exchanges.record_grid(replace(s.scope, **{field: uuid4()}), s.attempt, grid_id)
    old = s.attempt
    _reclaim(s)
    with pytest.raises(exchanges.ExchangeLeaseLost):
        exchanges.record_grid(s.scope, old, grid_id)
    assert exchanges.record_grid(s.scope, s.attempt, grid_id) == row
    assert exchanges.resume_exchange(s.scope, s.attempt).action == "poll_result"
    pg.expire_worker(s.job)
    with pytest.raises(exchanges.ExchangeLeaseLost):
        exchanges.record_grid(s.scope, s.attempt, grid_id)


def test_record_grid_requires_async_sending(world):
    s = world()
    _prepare(s)
    with pytest.raises(exchanges.ExchangeStateError):
        exchanges.record_grid(s.scope, s.attempt, uuid4())
    assert exchanges.begin_send(s.scope, s.attempt)
    assert _read(s)["sent_at"] is None and _read(s)["send_principal_sha256"] is None
    with pytest.raises(exchanges.ExchangeStateError):
        exchanges.record_grid(s.scope, s.attempt, uuid4())


def test_async_reclaimed_lease_captures_and_preserves_sender(world):
    s = world()
    before = _begin_async(s)
    _reclaim(s)
    grid_id = uuid4()
    exchanges.record_grid(s.scope, s.attempt, grid_id)
    assert _capture(s) == "stored"
    row = _read(s)
    assert row["sender_worker"] == before["sender_worker"]
    assert row["sender_attempt"] == before["sender_attempt"]
    assert row["grid_id"] == grid_id and row["sent_at"] == before["sent_at"]
    assert _capture(s) == "duplicate"
    with pytest.raises(exchanges.ExchangeStateError):
        exchanges.record_grid(s.scope, s.attempt, grid_id)
    assert _complete(s) == "completed"


def test_async_reclaimed_lease_can_refuse(world):
    s = world()
    before = _begin_async(s)
    _reclaim(s)
    assert exchanges.refuse_exchange(s.scope, s.attempt,
        reason_code="cloud_upstream_failure") == "refused"
    assert _read(s)["sender_worker"] == before["sender_worker"]


def test_async_no_handle_cutoff_resumes_unknown(world):
    s = world()
    _begin_async(s, aged=True)
    assert exchanges.resume_exchange(s.scope, s.attempt,
        send_principal_sha256="0" * 64).action == "outcome_unknown"
    assert _read(s)["grid_id"] is None


@pytest.mark.parametrize("aged,with_grid", [(False, False), (False, True), (True, True)])
def test_async_unknown_guard_blocks_old_workers(world, aged, with_grid):
    s = world()
    _begin_async(s, aged=aged)
    if with_grid:
        exchanges.record_grid(s.scope, s.attempt, uuid4())
    before = _read(s)
    with pytest.raises(exchanges.ExchangeStateError):
        exchanges.record_outcome_unknown(s.scope, s.attempt)
    with pytest.raises(psycopg.errors.RaiseException, match="outcome remains recoverable"):
        with pg.db.transaction() as conn:
            conn.execute("UPDATE project_solve_exchanges SET state='outcome_unknown',"
                "refusal_code='SOLVE_OUTCOME_UNKNOWN' WHERE job_id=%s", (s.job,))
    assert _read(s) == before
    if with_grid:
        assert exchanges.resume_exchange(s.scope, s.attempt).action == "poll_result"


def test_async_custody_columns_are_immutable(world):
    s = world()
    _begin_async(s)
    exchanges.record_grid(s.scope, s.attempt, uuid4())
    before = _read(s)
    for assignments in ("grid_id=NULL", "grid_id='" + str(uuid4()) + "'::uuid",
            "send_protocol=NULL", "sent_at=sent_at-interval '1 hour'",
            "send_principal_sha256='" + "0" * 64 + "'"):
        with pytest.raises(psycopg.errors.RaiseException):
            with pg.db.transaction() as conn:
                conn.execute("UPDATE project_solve_exchanges SET " + assignments + " WHERE job_id=%s", (s.job,))
        assert _read(s) == before


def test_async_shape_checks_independent_of_update_guard(world):
    s = world()
    before = _begin_async(s)
    for changes in ({"send_protocol": "unsupported"}, {"sent_at": None},
            {"send_principal_sha256": None}, {"send_principal_sha256": "bad"},
            {"send_protocol": None}, {"send_protocol": None, "sent_at": None,
                "send_principal_sha256": None, "grid_id": uuid4()},
            {"state": "outcome_unknown", "refusal_code": "SOLVE_OUTCOME_UNKNOWN", "grid_id": uuid4()},
            {"state": "prepared", "sender_worker": None, "sender_attempt": None, "grid_id": uuid4()}):
        changed = {**before, **changes}
        with pytest.raises(psycopg.errors.CheckViolation):
            with pg.db.transaction() as conn:
                conn.execute("DELETE FROM project_solve_exchanges WHERE job_id=%s", (s.job,))
                names = tuple(changed)
                conn.execute("INSERT INTO project_solve_exchanges (" + ",".join(names) +
                    ") VALUES (" + ",".join("%(" + key + ")s" for key in names) + ")", changed)
        assert _read(s) == before


def test_async_custody_requires_prepared_send_transition(world):
    s = world()
    _prepare(s)
    with pytest.raises(psycopg.errors.RaiseException, match="custody is immutable"):
        with pg.db.transaction() as conn:
            conn.execute("UPDATE project_solve_exchanges SET send_protocol='async-idempotent-v1',"
                "sent_at=now(),send_principal_sha256=%s WHERE job_id=%s", (PRINCIPAL, s.job))


def test_async_migration_reapplication_preserves_custody(world):
    s = world()
    _begin_async(s)
    exchanges.record_grid(s.scope, s.attempt, uuid4())
    before = _read(s)
    sql = (ROOT / "platform/migrations/0094_project_solve_async.sql").read_text(encoding="utf-8")
    for _ in range(2):
        with pg.db.transaction() as conn:
            for statement in pg.db._split_sql_statements(sql):
                conn.execute(statement)
    assert _read(s) == before
