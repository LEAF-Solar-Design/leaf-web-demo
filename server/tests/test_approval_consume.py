"""
Binary acceptance for session_store.consume_approval() (merge-gate finding
#1 — approvals bypassable/replayable). Store-level coverage only; the HTTP
router wiring (routers/sessions.py's messages{confirm} path) and the four
attack-case scenarios from the review are covered end-to-end in
tests/test_sessions_routes.py.

SESSIONS_DB is redirected to a fresh tmp path BEFORE the first import of
session_store (the module reads the env var once, at import time — identical
posture to tests/test_session_store.py / jobs.py's own JOBS_DB precedent).
This guarantees the real server/sessions.db is never touched by this suite.

Run:  cd server && python -m pytest tests/test_approval_consume.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="approval-consume-test-"))
_TMP_DB = _TMP_DIR / "sessions.db"
os.environ.setdefault("SESSIONS_DB", str(_TMP_DB))

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import session_store  # noqa: E402  (import after env redirect, by design)

REAL_DB_PATH = SERVER_DIR / "sessions.db"

# The live :8130 stack legitimately owns a real server/sessions.db on a dev
# machine — its EXISTENCE is not test pollution. The invariant is that this
# SUITE never touches it: snapshot (mtime, size) at import, assert unchanged.
def _real_db_snapshot():
    try:
        st = REAL_DB_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None
_REAL_DB_AT_IMPORT = _real_db_snapshot()


def test_sessions_db_isolated_to_tmp_path():
    assert session_store.DB_PATH != REAL_DB_PATH
    session_store.ensure_started()
    assert session_store.DB_PATH.exists()
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_counter = [0]


def _new_session(tenant_id: str = "tenant-consume") -> dict:
    _counter[0] += 1
    return session_store.get_or_create_session(tenant_id, f"drawing-consume-{_counter[0]}")


def _seed_approval(session_id: str, tenant_id: str, *, ttl_s: float = 300,
                    tool: str = "write_home_run", params=None,
                    capability: str = "drawing.write") -> str:
    _counter[0] += 1
    confirmation_id = f"confirm-consume-{_counter[0]}"
    session_store.create_approval(
        confirmation_id, session_id, tenant_id, turn_id=f"turn-{_counter[0]}",
        tool=tool, params=params if params is not None else {"length_ft": 12},
        capability=capability, rationale="adds a home-run", kind="proposed_run",
        payload={"preview": "ok"}, ttl_s=ttl_s,
    )
    return confirmation_id


# =========================================================================== #
# success — returns the STORED approved value, marks consumed
# =========================================================================== #
def test_consume_approval_success_returns_stored_approved_true():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    outcome = session_store.decide_approval(cid, True, by="user-1")
    assert outcome == "recorded"

    result = session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
    assert result["confirmation_id"] == cid
    assert result["approved"] is True
    assert result["consumed"] is True
    assert result["tool"] == "write_home_run"
    assert result["params"] == {"length_ft": 12}
    assert result["capability"] == "drawing.write"

    # durably persisted, not just returned
    got = session_store.get_approval(cid)
    assert got["consumed"] is True


def test_consume_approval_success_returns_stored_approved_false():
    """A REJECTED approval consumes cleanly too — the caller (router) decides
    what to do with approved=False; the store just tells the truth."""
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, False, by="user-1")

    result = session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
    assert result["approved"] is False
    assert result["consumed"] is True


# =========================================================================== #
# not_found — unknown / wrong-session / wrong-tenant all collapse identically
# =========================================================================== #
def test_consume_approval_unknown_confirmation_id_not_found():
    sess = _new_session()
    try:
        session_store.consume_approval("no-such-confirmation-id", sess["session_id"], sess["tenant_id"])
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "not_found"


def test_consume_approval_wrong_session_not_found():
    """Approval created under session A, consumed against session B (same
    tenant) — must fail exactly like an unknown confirmation_id."""
    sess_a = _new_session(tenant_id="tenant-wrongsession")
    sess_b = _new_session(tenant_id="tenant-wrongsession")
    cid = _seed_approval(sess_a["session_id"], "tenant-wrongsession")
    session_store.decide_approval(cid, True, by="user-1")

    try:
        session_store.consume_approval(cid, sess_b["session_id"], "tenant-wrongsession")
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "not_found"

    # the approval is untouched — a legitimate consume against session A still works
    result = session_store.consume_approval(cid, sess_a["session_id"], "tenant-wrongsession")
    assert result["consumed"] is True


def test_consume_approval_wrong_tenant_not_found():
    sess = _new_session(tenant_id="tenant-owner-2")
    cid = _seed_approval(sess["session_id"], "tenant-owner-2")
    session_store.decide_approval(cid, True, by="user-1")

    try:
        session_store.consume_approval(cid, sess["session_id"], "tenant-intruder-2")
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "not_found"


# =========================================================================== #
# undecided — the skip-the-approvals-endpoint attack
# =========================================================================== #
def test_consume_approval_undecided():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])  # never decided

    try:
        session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "undecided"

    # untouched — still consumable once actually decided
    session_store.decide_approval(cid, True, by="user-1")
    result = session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
    assert result["consumed"] is True


# =========================================================================== #
# already_consumed — the replay attack
# =========================================================================== #
def test_consume_approval_replay_already_consumed():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")

    first = session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
    assert first["consumed"] is True

    try:
        session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "already_consumed"


# =========================================================================== #
# expired
# =========================================================================== #
def test_consume_approval_expired():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"], ttl_s=0.05)
    session_store.decide_approval(cid, True, by="user-1")
    time.sleep(0.15)

    try:
        session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "expired"


def test_consume_approval_undecided_takes_priority_over_expired():
    """An undecided-AND-expired approval must report 'undecided', not
    'expired' -- matches consume_approval's documented check order."""
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"], ttl_s=0.05)
    time.sleep(0.15)  # expires, but never decided

    try:
        session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "undecided"


# =========================================================================== #
# unconsume_approval — give back a consume that never redeemed anything
# (routers/sessions.py's TurnBusy leg; see that module's APPROVAL GIVE-BACK)
# =========================================================================== #
def test_unconsume_approval_makes_an_unredeemed_consume_consumable_again():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")
    session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])

    assert session_store.unconsume_approval(
        cid, sess["session_id"], sess["tenant_id"]) is True
    assert session_store.get_approval(cid)["consumed"] is False

    # consumable again, and the stored decision/proposal survived the round trip
    again = session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
    assert again["approved"] is True
    assert again["consumed"] is True
    assert again["tool"] == "write_home_run"
    assert again["params"] == {"length_ft": 12}


def test_unconsume_approval_is_still_single_redeem():
    """A give-back reopens the approval EXACTLY once. Consuming it again and
    NOT giving that one back leaves it spent for good."""
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")

    session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
    session_store.unconsume_approval(cid, sess["session_id"], sess["tenant_id"])
    session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])

    try:
        session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
        assert False, "expected ApprovalConsumeError"
    except session_store.ApprovalConsumeError as exc:
        assert exc.reason == "already_consumed"


def test_unconsume_approval_never_consumed_is_a_false_noop():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")

    assert session_store.unconsume_approval(
        cid, sess["session_id"], sess["tenant_id"]) is False
    assert session_store.get_approval(cid)["consumed"] is False
    # and the row is otherwise untouched — still decided, still consumable
    assert session_store.consume_approval(
        cid, sess["session_id"], sess["tenant_id"])["approved"] is True


def test_unconsume_approval_unknown_confirmation_id_is_a_false_noop():
    sess = _new_session()
    assert session_store.unconsume_approval(
        "no-such-confirmation-id", sess["session_id"], sess["tenant_id"]) is False


def test_unconsume_approval_wrong_session_cannot_unspend():
    """Session B must not be able to un-spend session A's approval — the same
    ownership guard consume_approval enforces."""
    sess_a = _new_session(tenant_id="tenant-unconsume-xsession")
    sess_b = _new_session(tenant_id="tenant-unconsume-xsession")
    cid = _seed_approval(sess_a["session_id"], "tenant-unconsume-xsession")
    session_store.decide_approval(cid, True, by="user-1")
    session_store.consume_approval(cid, sess_a["session_id"], "tenant-unconsume-xsession")

    assert session_store.unconsume_approval(
        cid, sess_b["session_id"], "tenant-unconsume-xsession") is False
    assert session_store.get_approval(cid)["consumed"] is True


def test_unconsume_approval_wrong_tenant_cannot_unspend():
    sess = _new_session(tenant_id="tenant-unconsume-owner")
    cid = _seed_approval(sess["session_id"], "tenant-unconsume-owner")
    session_store.decide_approval(cid, True, by="user-1")
    session_store.consume_approval(cid, sess["session_id"], "tenant-unconsume-owner")

    assert session_store.unconsume_approval(
        cid, sess["session_id"], "tenant-unconsume-intruder") is False
    assert session_store.get_approval(cid)["consumed"] is True


def test_unconsume_approval_only_the_consumer_can_give_back():
    """Two racers, one winner, and only the WINNER's give-back can fire: the
    loser never consumed, so the row it would hand back is not its to hand.
    This is what keeps the give-back from becoming a second redemption — a
    consumed=1 row has exactly one writer, the caller that set it."""
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")

    n = 8
    barrier = threading.Barrier(n)
    won = []
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            try:
                session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
                won.append(True)
            except session_store.ApprovalConsumeError:
                pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"unexpected errors: {errors}"
    assert len(won) == 1
    # the single winner gives back; every loser's give-back would be a no-op
    assert session_store.unconsume_approval(
        cid, sess["session_id"], sess["tenant_id"]) is True
    assert session_store.unconsume_approval(
        cid, sess["session_id"], sess["tenant_id"]) is False  # already given back


# =========================================================================== #
# atomicity — 2-thread race on the SAME confirmation_id -> exactly one winner
# =========================================================================== #
def test_consume_approval_concurrent_exactly_one_winner():
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")

    n = 2
    barrier = threading.Barrier(n)
    outcomes = [None] * n
    errors = []

    def worker(idx):
        try:
            barrier.wait(timeout=5)
            try:
                session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
                outcomes[idx] = "won"
            except session_store.ApprovalConsumeError as exc:
                outcomes[idx] = exc.reason
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"unexpected errors: {errors}"
    assert outcomes.count("won") == 1, outcomes
    assert outcomes.count("already_consumed") == n - 1, outcomes


def test_consume_approval_concurrent_hammer_exactly_one_winner():
    """Wider hammer (10 threads) — same invariant, more concurrency pressure."""
    sess = _new_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by="user-1")

    n = 10
    barrier = threading.Barrier(n)
    outcomes = [None] * n
    errors = []

    def worker(idx):
        try:
            barrier.wait(timeout=5)
            try:
                session_store.consume_approval(cid, sess["session_id"], sess["tenant_id"])
                outcomes[idx] = "won"
            except session_store.ApprovalConsumeError as exc:
                outcomes[idx] = exc.reason
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"unexpected errors: {errors}"
    assert outcomes.count("won") == 1, outcomes
    assert outcomes.count("already_consumed") == n - 1, outcomes


def test_zzz_real_sessions_db_still_absent():
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )
