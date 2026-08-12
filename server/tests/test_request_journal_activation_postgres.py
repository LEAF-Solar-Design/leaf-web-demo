"""Real PostgreSQL proofs for fused request and session activation."""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import request_journal  # noqa: E402
import session_store  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def postgres_authority():
    url = os.environ.get("PG_CUSTOMIZATION_TEST_URL")
    if not url:
        pytest.skip("PG_CUSTOMIZATION_TEST_URL is not configured")
    os.environ["DATABASE_URL"] = url
    os.environ["LEAF_SESSIONS_STORE"] = "postgres"
    db = request_journal.platform_db()
    db.reset_pool()
    db.apply_migration()
    yield db
    db.reset_pool()


@pytest.fixture()
def scope(postgres_authority):
    suffix = uuid.uuid4().hex
    tenant = f"p5a-{suffix}"
    drawing = f"drawing-{suffix}"
    session = session_store.get_or_create_session(tenant, drawing)
    yield postgres_authority, tenant, drawing, session["session_id"]
    with postgres_authority.transaction() as conn:
        conn.execute("DELETE FROM app_sessions WHERE session_id=%s", (session["session_id"],))


def _admit(tenant: str, drawing: str, session_id: str, label: str) -> str:
    request_id = str(uuid.uuid4())
    request_journal.admit_request(
        request_id=request_id, tenant_id=tenant, drawing_id=drawing,
        session_id=session_id, principal_key="auth0|p5a",
        digest=request_journal.payload_digest({"text": label}),
    )
    return request_id


def _payload(label: str) -> dict:
    return {
        "text": label, "classifier_hint": None, "model": None,
        "tier": "hosted_pro", "subject": "auth0|p5a",
        "entitlement_tier": "hosted_pro", "entitlement_roles": [],
        "entitlement_elevated": False,
    }


def test_direct_activation_commits_request_and_session_from_one_timestamp(scope, monkeypatch):
    _db, tenant, drawing, session_id = scope
    request_id = _admit(tenant, drawing, session_id, "direct")
    monkeypatch.setattr(request_journal.time, "time", lambda: 1000.0)

    assert request_journal.begin_request_and_turn(
        request_id, "turn-direct", lease_seconds=37, session_id=session_id,
        stale_after_s=300, tier="hosted_pro", subject="auth0|p5a",
    )
    row = request_journal.get_request(request_id)
    session = session_store.get_session(session_id)
    assert row["state"] == "executing" and row["turn_id"] == "turn-direct"
    assert session["active_turn_id"] == "turn-direct"
    assert row["lease_expires_at"] - session["turn_started_at"] == 37


def test_foreign_turn_refuses_without_consuming_queue_payload(scope):
    _db, tenant, drawing, session_id = scope
    request_id = _admit(tenant, drawing, session_id, "queued")
    payload = _payload("queued")
    assert request_journal.queue_request(request_id, payload)
    assert session_store.try_begin_turn(session_id, "foreign", 300)

    assert request_journal.claim_next_queued_and_turn(
        lease_seconds=30, stale_after_s=300, session_id=session_id,
    ) is None
    row = request_journal.get_request(request_id)
    assert row["state"] == "queued"
    assert row["recoverable_json"] == payload
    assert row["response_status"] is None and row["terminal_at"] is None


def test_direct_foreign_turn_leaves_request_admitted(scope):
    _db, tenant, drawing, session_id = scope
    request_id = _admit(tenant, drawing, session_id, "direct-refused")
    assert session_store.try_begin_turn(session_id, "foreign", 300)
    assert not request_journal.begin_request_and_turn(
        request_id, "turn-refused", lease_seconds=30, session_id=session_id,
        stale_after_s=300,
    )
    row = request_journal.get_request(request_id)
    assert row["state"] == "admitted" and row["turn_id"] is None
    assert session_store.get_session(session_id)["active_turn_id"] == "foreign"


def test_session_lock_keeps_first_payload_queued_and_second_slot_full(scope):
    db, tenant, drawing, session_id = scope
    first = _admit(tenant, drawing, session_id, "first")
    second = _admit(tenant, drawing, session_id, "second")
    first_payload = _payload("first")
    assert request_journal.queue_request(first, first_payload)
    entered = threading.Event()

    def claim():
        entered.set()
        return request_journal.claim_next_queued_and_turn(
            lease_seconds=30, stale_after_s=300, session_id=session_id,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.transaction() as conn:
            conn.execute(
                "SELECT session_id FROM app_sessions WHERE session_id=%s FOR UPDATE",
                (session_id,),
            ).fetchone()
            future = pool.submit(claim)
            assert entered.wait(2)
            time.sleep(0.05)
            assert request_journal.queue_request(second, _payload("second")) is False
            row = request_journal.get_request(first)
            assert row["state"] == "queued" and row["recoverable_json"] == first_payload
        claimed = future.result(timeout=5)
    assert claimed and claimed["request_id"] == first


def test_queued_activation_commits_then_allows_one_successor(scope):
    _db, tenant, drawing, session_id = scope
    first = _admit(tenant, drawing, session_id, "first")
    assert request_journal.queue_request(first, _payload("first"))
    claimed = request_journal.claim_next_queued_and_turn(
        lease_seconds=30, stale_after_s=300, session_id=session_id,
    )
    assert claimed and claimed["request_id"] == first
    assert session_store.get_session(session_id)["active_turn_id"] == claimed["turn_id"]
    second = _admit(tenant, drawing, session_id, "second")
    assert request_journal.queue_request(second, _payload("second"))
    assert request_journal.active_counts(tenant, drawing) == {
        "queued": 1, "executing": 1, "active": 2,
    }


def test_concurrent_queue_claimers_have_exactly_one_winner(scope):
    _db, tenant, drawing, session_id = scope
    request_id = _admit(tenant, drawing, session_id, "race")
    assert request_journal.queue_request(request_id, _payload("race"))
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return request_journal.claim_next_queued_and_turn(
            lease_seconds=30, stale_after_s=300, session_id=session_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: claim(), range(2)))
    winners = [row for row in results if row is not None]
    assert len(winners) == 1
    assert request_journal.get_request(request_id)["state"] == "executing"


def test_failure_after_session_lock_rolls_back_both_tables(scope, monkeypatch):
    _db, tenant, drawing, session_id = scope
    request_id = _admit(tenant, drawing, session_id, "rollback")
    original = session_store.pg_try_begin_turn_in_transaction

    def fail_after_update(*args, **kwargs):
        assert original(*args, **kwargs)
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(session_store, "pg_try_begin_turn_in_transaction", fail_after_update)
    with pytest.raises(RuntimeError, match="injected"):
        request_journal.begin_request_and_turn(
            request_id, "turn-rollback", lease_seconds=30, session_id=session_id,
            stale_after_s=300,
        )
    assert request_journal.get_request(request_id)["state"] == "admitted"
    assert session_store.get_session(session_id)["active_turn_id"] is None


def test_tenant_and_drawing_counts_remain_isolated(scope):
    _db, tenant, drawing, session_id = scope
    request_id = _admit(tenant, drawing, session_id, "isolated")
    assert request_journal.queue_request(request_id, _payload("isolated"))
    assert request_journal.active_counts(tenant, drawing)["active"] == 1
    assert request_journal.active_counts("foreign", drawing)["active"] == 0
    assert request_journal.active_counts(tenant, "foreign")["active"] == 0
