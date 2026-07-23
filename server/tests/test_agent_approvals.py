"""
Binary acceptance for S4: POST /api/agent/approvals/{confirmation_id}
(sessions wire spec, gap audit §2.1.5 / CONTRACT-ADDENDUM sessions addendum).

Covers:
  * record-only: 200 {resolved:true, approved} on a fresh confirmation, and
    it NEVER starts a turn (try_begin_turn is never invoked; active_turn_id
    is untouched).
  * a `confirmation_resolved` event is appended into the approval's OWN
    session log with {confirmation_id, approved, by}.
  * unknown confirmation_id -> 404 bad_params.
  * cross-tenant confirmation_id -> 404 bad_params, IDENTICAL shape to the
    unknown case (no existence leak).
  * a second decision on an already-decided confirmation_id -> 409
    bad_params, and the original decision is left unchanged.
  * an expired-but-undecided confirmation is still recorded (200, not 410 --
    the 410 belongs to the subsequent /messages{confirm} call, not this one).
  * approved=false (reject) round-trips too.

Hermetic: in-process TestClient wrapping only routers/agent.py. SESSIONS_DB is
redirected to a fresh tmp path BEFORE the first import of session_store (read
once at import time, identical posture to tests/test_session_store.py) so the
real server/sessions.db is never touched.

Run:  cd server && python -m pytest tests/test_agent_approvals.py -q
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="agent-approvals-test-"))
_TMP_DB = _TMP_DIR / "sessions.db"
os.environ.setdefault("SESSIONS_DB", str(_TMP_DB))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")  # legacy X-Tenant-Id header stub

import sys  # noqa: E402
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import agent_gate  # noqa: E402  (reads its env paths per call — safe to import here)
import session_store  # noqa: E402  (import after env redirect, by design)
from agent_policy import AgentAction  # noqa: E402
from routers import agent as agent_router  # noqa: E402

REAL_DB_PATH = SERVER_DIR / "sessions.db"

# The live :8130 stack legitimately owns a real server/sessions.db on a dev
# machine — its EXISTENCE is not test pollution (same posture as
# test_session_store.py). The invariant is that this SUITE never touches it:
# snapshot (mtime, size) at import, assert unchanged.
def _real_db_snapshot():
    try:
        st = REAL_DB_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None
_REAL_DB_AT_IMPORT = _real_db_snapshot()


def test_sessions_db_isolated_to_tmp_path():
    # In a combined multi-file run, whichever test module imported session_store
    # FIRST won the os.environ.setdefault race and bound SESSIONS_DB to ITS tmp
    # dir — so assert the redirect is real (never the repo's own sessions.db),
    # not that it equals THIS file's tmp path.
    assert session_store.DB_PATH != REAL_DB_PATH
    assert "sessions.db" in session_store.DB_PATH.name
    session_store.ensure_started()
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )


# --------------------------------------------------------------------------- #
# HTTP harness: minimal FastAPI app wrapping ONLY the agent (approvals) router
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agent_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


_counter = [0]


def _seed_approval(tenant_id: str = "tenant-a", ttl_s: float = 300,
                    tool: str = "write_wire_run") -> dict:
    """Create a session + a pending approval row, return {session_id,
    turn_id, confirmation_id, tenant_id}."""
    _counter[0] += 1
    n = _counter[0]
    sess = session_store.get_or_create_session(tenant_id, f"drawing-{n}")
    turn_id = f"turn-{n}"
    confirmation_id = f"confirm-{n}"
    session_store.create_approval(
        confirmation_id, sess["session_id"], tenant_id, turn_id,
        tool=tool, params={"length_ft": 12}, capability="drawing.write",
        rationale="adds a home-run", kind="proposed_run",
        payload={"preview": "ok"}, ttl_s=ttl_s,
    )
    return {
        "session_id": sess["session_id"],
        "turn_id": turn_id,
        "confirmation_id": confirmation_id,
        "tenant_id": tenant_id,
    }


# --------------------------------------------------------------------------- #
# happy path: record-only, never starts a turn
# --------------------------------------------------------------------------- #
def test_decide_approved_records_and_returns_resolved_shape(client):
    seed = _seed_approval()
    r = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": True}, headers=_h(seed["tenant_id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True
    assert body["approved"] is True

    stored = session_store.get_approval(seed["confirmation_id"])
    assert stored["decided"] is True
    assert stored["approved"] is True
    assert stored["decided_by"] == seed["tenant_id"]


def test_decide_rejected_round_trips_false(client):
    seed = _seed_approval()
    r = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": False}, headers=_h(seed["tenant_id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True
    assert body["approved"] is False

    stored = session_store.get_approval(seed["confirmation_id"])
    assert stored["decided"] is True
    assert stored["approved"] is False


def test_decide_never_starts_a_turn(client, monkeypatch):
    seed = _seed_approval()

    def _boom(*a, **kw):
        raise AssertionError("decide_approval must NEVER call try_begin_turn")

    monkeypatch.setattr(session_store, "try_begin_turn", _boom)

    before = session_store.get_session(seed["session_id"])
    assert before["active_turn_id"] is None

    r = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": True}, headers=_h(seed["tenant_id"]),
    )
    assert r.status_code == 200, r.text

    after = session_store.get_session(seed["session_id"])
    assert after["active_turn_id"] is None  # unchanged -- no turn was started


def test_decide_appends_confirmation_resolved_event_to_session_log(client):
    seed = _seed_approval()
    before_last_seq = session_store.get_session(seed["session_id"])["last_seq"]

    r = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": True}, headers=_h(seed["tenant_id"]),
    )
    assert r.status_code == 200, r.text

    events = session_store.events_after(seed["session_id"], after_seq=before_last_seq)
    matching = [e for e in events if e["type"] == "confirmation_resolved"]
    assert len(matching) == 1, f"expected exactly one confirmation_resolved event, got {events}"
    ev = matching[0]
    assert ev["session_id"] == seed["session_id"]
    assert ev["turn_id"] == seed["turn_id"]
    assert ev["data"] == {
        "confirmation_id": seed["confirmation_id"],
        "approved": True,
        "by": seed["tenant_id"],
    }


# --------------------------------------------------------------------------- #
# no existence leak: unknown and cross-tenant collapse to the SAME 404
# --------------------------------------------------------------------------- #
def test_decide_unknown_confirmation_id_404_bad_params(client):
    r = client.post(
        "/api/agent/approvals/no-such-confirmation",
        json={"approved": True}, headers=_h("tenant-a"),
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["error"]["error_code"] == "BAD_PARAMS"


def test_decide_cross_tenant_404_bad_params_same_shape_as_unknown(client):
    seed = _seed_approval(tenant_id="tenant-owner")

    unknown_r = client.post(
        "/api/agent/approvals/no-such-confirmation-xyz",
        json={"approved": True}, headers=_h("tenant-owner"),
    )
    cross_r = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": True}, headers=_h("tenant-intruder"),
    )

    assert unknown_r.status_code == 404
    assert cross_r.status_code == 404
    assert unknown_r.json()["error"]["error_code"] == cross_r.json()["error"]["error_code"] == "BAD_PARAMS"

    # the cross-tenant attempt must NOT have decided the approval
    stored = session_store.get_approval(seed["confirmation_id"])
    assert stored["decided"] is False


# --------------------------------------------------------------------------- #
# already-decided -> 409, original decision preserved
# --------------------------------------------------------------------------- #
def test_decide_already_decided_409_bad_params_preserves_original(client):
    seed = _seed_approval()
    first = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": True}, headers=_h(seed["tenant_id"]),
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": False}, headers=_h(seed["tenant_id"]),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["error_code"] == "BAD_PARAMS"

    # the original 'approved: True' decision must be unchanged
    stored = session_store.get_approval(seed["confirmation_id"])
    assert stored["approved"] is True

    # and only ONE confirmation_resolved event was ever appended
    events = session_store.events_after(seed["session_id"], after_seq=0)
    matching = [e for e in events if e["type"] == "confirmation_resolved"]
    assert len(matching) == 1


# --------------------------------------------------------------------------- #
# expired-but-undecided is still recorded (the 410 lives on /messages{confirm})
# --------------------------------------------------------------------------- #
def test_decide_expired_but_undecided_still_records_200_not_410(client):
    seed = _seed_approval(ttl_s=0.05)
    time.sleep(0.15)

    pre = session_store.get_approval(seed["confirmation_id"])
    assert pre["expired"] is True
    assert pre["decided"] is False

    r = client.post(
        f"/api/agent/approvals/{seed['confirmation_id']}",
        json={"approved": True}, headers=_h(seed["tenant_id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True
    assert body["approved"] is True

    stored = session_store.get_approval(seed["confirmation_id"])
    assert stored["decided"] is True
    assert stored["approved"] is True


# --------------------------------------------------------------------------- #
# spine unification (census #12 chip 1): a recorded decision bridges into the
# §18 gate store, whose args-bound record the resume turn's gate consult
# redeems before any dispatch.
# --------------------------------------------------------------------------- #
_WRITE_ACTION = AgentAction(
    name="run_write_tool", description="", rung=3,
    required_capability="converse", policy="always_confirm", dispatch={},
)


def _seed_both_stores(tmp_path, monkeypatch, tenant_id: str) -> str:
    """Seed the gate's pending record (it mints the id in the live flow) plus the
    matching session_store row; return the shared confirmation_id."""
    monkeypatch.setenv("LEAF_AGENT_APPROVALS_DIR", str(tmp_path / "gate-approvals"))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "gate-audit.jsonl"))
    sess = session_store.get_or_create_session(tenant_id, f"drawing-{tenant_id}")
    record = agent_gate.create_pending(
        tenant_id=tenant_id, session_id=sess["session_id"], turn_id="turn-bridge",
        action=_WRITE_ACTION, args={"tool": "add-panel", "params": {"col": 2}}, ttl_s=300,
    )
    cid = record["confirmation_id"]
    session_store.create_approval(
        cid, sess["session_id"], tenant_id, "turn-bridge",
        tool="add-panel", params={"col": 2}, capability="drawing.write",
        rationale="approval required by policy", kind="run_capability",
        payload=None, ttl_s=300,
    )
    return cid


def test_decide_approved_bridges_grant_into_gate_store(client, tmp_path, monkeypatch):
    cid = _seed_both_stores(tmp_path, monkeypatch, "tenant-bridge-a")
    r = client.post(f"/api/agent/approvals/{cid}",
                    headers=_h("tenant-bridge-a"), json={"approved": True})
    assert r.status_code == 200, r.text
    gate_rec = agent_gate.read_pending(cid)
    assert gate_rec is not None
    assert gate_rec["granted"] is True
    assert gate_rec["denied"] is False
    assert gate_rec["decided_by"] == "tenant-bridge-a"


def test_decide_rejected_bridges_deny_into_gate_store(client, tmp_path, monkeypatch):
    cid = _seed_both_stores(tmp_path, monkeypatch, "tenant-bridge-d")
    r = client.post(f"/api/agent/approvals/{cid}",
                    headers=_h("tenant-bridge-d"), json={"approved": False})
    assert r.status_code == 200, r.text
    gate_rec = agent_gate.read_pending(cid)
    assert gate_rec is not None
    assert gate_rec["granted"] is False
    assert gate_rec["denied"] is True


def test_decide_gate_write_failure_aborts_before_recording_then_retry_succeeds(
        client, tmp_path, monkeypatch):
    """Gate-first ordering (review round 1 finding): a gate-store write failure
    must abort the request BEFORE the session_store decision is recorded — the
    click stays retryable and the stuck divergence 'session row consumed / gate
    record pending' (which would re-propose an already-consumed id) can never
    form."""
    cid = _seed_both_stores(tmp_path, monkeypatch, "tenant-bridge-f")

    def _boom(*_a, **_k):
        raise OSError("gate store write failed")

    orig = agent_gate.grant_approval
    agent_gate.grant_approval = _boom
    try:
        r = client.post(f"/api/agent/approvals/{cid}",
                        headers=_h("tenant-bridge-f"), json={"approved": True})
    finally:
        agent_gate.grant_approval = orig

    assert r.status_code == 503, r.text
    assert session_store.get_approval(cid)["decided"] is False  # nothing recorded
    gate_rec = agent_gate.read_pending(cid)
    assert gate_rec["granted"] is False and gate_rec["denied"] is False

    # The retry lands both stores.
    r2 = client.post(f"/api/agent/approvals/{cid}",
                     headers=_h("tenant-bridge-f"), json={"approved": True})
    assert r2.status_code == 200, r2.text
    assert agent_gate.read_pending(cid)["granted"] is True
    assert session_store.get_approval(cid)["decided"] is True


def test_decide_without_gate_record_still_records(client, tmp_path, monkeypatch):
    """A sessions-wire-only confirmation_id (pre-spine turn) has no gate record:
    the bridge is a silent not_found and record-only behavior is unchanged."""
    monkeypatch.setenv("LEAF_AGENT_APPROVALS_DIR", str(tmp_path / "gate-approvals-empty"))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "gate-audit.jsonl"))
    seeded = _seed_approval("tenant-nogate")
    r = client.post(f"/api/agent/approvals/{seeded['confirmation_id']}",
                    headers=_h("tenant-nogate"), json={"approved": True})
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True
    assert agent_gate.read_pending(seeded["confirmation_id"]) is None


def test_zzz_real_sessions_db_still_absent():
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )
