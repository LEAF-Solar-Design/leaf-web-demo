"""
Binary acceptance for S2: the four client-facing session routes
(server/routers/sessions.py), matching console/converse.js byte-for-byte
(sessions wire spec, gap audit §2.1 / CONTRACT-ADDENDUM sessions addendum).

Covers:
  POST /api/sessions                  -> idempotent create/attach
  POST /api/sessions/{id}/messages    -> the full 9-outcome error taxonomy
                                          converse.js's classifyAgentError()
                                          switches on: busy, not_found, grant,
                                          entitlement, quota, rate_limited,
                                          confirmation_expired, approval_stale
                                          (409 bad_params), unreachable (502)
  GET  /api/sessions/{id}/stream      -> SSE replay from after_seq
  GET  /api/sessions/{id}/transcript  -> ascending events, limit clamp

Hermetic: in-process TestClient wrapping ONLY routers/sessions.py. SESSIONS_DB
is redirected to a fresh tmp path BEFORE the first import of session_store
(read once at import time — same posture as tests/test_agent_approvals.py /
tests/test_turn_runner.py) so the real server/sessions.db is never touched.
This suite never performs a real HTTP call to a harness. Most tests monkeypatch
turn_runner.start_turn outright; the few that must exercise the REAL dispatch
(the turn-CAS race and the pre-harness give-back) instead run start_turn with
no harness URL configured, so it rejects before any POST is attempted.

Run:  cd server && python -m pytest tests/test_sessions_routes.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

_TMP_DIR = Path(tempfile.mkdtemp(prefix="sessions-routes-test-"))
_TMP_DB = _TMP_DIR / "sessions.db"
os.environ.setdefault("SESSIONS_DB", str(_TMP_DB))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")  # legacy X-Tenant-Id header stub

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import entitlements  # noqa: E402
import session_store  # noqa: E402  (import after env redirect, by design)
import turn_runner  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402

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
    # Full-suite runs race multiple modules' SESSIONS_DB setdefaults — whichever
    # won, the invariant that matters is "redirected somewhere, never the real
    # server/sessions.db" (same relaxation as tests/test_session_store.py).
    assert session_store.DB_PATH != REAL_DB_PATH
    session_store.ensure_started()
    assert session_store.DB_PATH.exists()
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )


# --------------------------------------------------------------------------- #
# HTTP harness: minimal FastAPI app wrapping ONLY the sessions router
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


_counter = [0]


def _new_drawing() -> str:
    _counter[0] += 1
    return f"drawing-{_counter[0]}"


def _seed_session(tenant_id: str = "tenant-a") -> Dict[str, Any]:
    return session_store.get_or_create_session(tenant_id, _new_drawing())


def _seed_approval(session_id: str, tenant_id: str, *, ttl_s: float = 300,
                    tool: str = "write_home_run", params: Optional[Dict[str, Any]] = None,
                    capability: str = "drawing.write") -> str:
    _counter[0] += 1
    confirmation_id = f"confirm-{_counter[0]}"
    session_store.create_approval(
        confirmation_id, session_id, tenant_id, turn_id=f"turn-{_counter[0]}",
        tool=tool, params=params if params is not None else {"length_ft": 12},
        capability=capability, rationale="adds a home-run", kind="proposed_run",
        payload={"preview": "ok"}, ttl_s=ttl_s,
    )
    return confirmation_id


# =========================================================================== #
# POST /api/sessions — idempotent create/attach
# =========================================================================== #
def test_create_session_returns_session_id_status_created_at(client):
    r = client.post("/api/sessions", json={"drawing_id": "dwg-1"}, headers=_h("tenant-a"))
    assert 200 <= r.status_code < 300, r.text
    body = r.json()
    assert body["session_id"]
    assert body["status"]
    assert body["created_at"] is not None


def test_create_session_idempotent_per_tenant_drawing(client):
    r1 = client.post("/api/sessions", json={"drawing_id": "dwg-idem"}, headers=_h("tenant-b"))
    r2 = client.post("/api/sessions", json={"drawing_id": "dwg-idem"}, headers=_h("tenant-b"))
    assert r1.json()["session_id"] == r2.json()["session_id"]


def test_create_session_different_tenants_get_different_sessions(client):
    r1 = client.post("/api/sessions", json={"drawing_id": "dwg-shared"}, headers=_h("tenant-c"))
    r2 = client.post("/api/sessions", json={"drawing_id": "dwg-shared"}, headers=_h("tenant-d"))
    assert r1.json()["session_id"] != r2.json()["session_id"]


# =========================================================================== #
# POST /api/sessions/{id}/messages — error taxonomy (9 outcomes)
# =========================================================================== #
def test_messages_unknown_session_404_session_not_found(client):
    r = client.post("/api/sessions/no-such-session/messages",
                    json={"text": "hi"}, headers=_h("tenant-a"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "session_not_found"


def test_messages_cross_tenant_session_404_same_shape_as_unknown(client):
    sess = _seed_session(tenant_id="tenant-owner")
    unknown_r = client.post("/api/sessions/no-such-session-xyz/messages",
                            json={"text": "hi"}, headers=_h("tenant-owner"))
    cross_r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                          json={"text": "hi"}, headers=_h("tenant-intruder"))
    assert unknown_r.status_code == cross_r.status_code == 404
    assert unknown_r.json()["error"]["error_code"] == cross_r.json()["error"]["error_code"] == "session_not_found"


def test_messages_neither_text_nor_confirm_409_bad_params(client):
    sess = _seed_session()
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_messages_both_text_and_confirm_409_bad_params_approval_stale_shape(client):
    """Both fields set -> 409 BAD_PARAMS. Same (status, lowercased error_code)
    combo converse.js's classifyAgentError() reads as 'approval_stale' (the
    ONLY http-shape signal that classification uses — see converse.js:90)."""
    sess = _seed_session()
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"text": "hi", "confirm": {"confirmationId": "x", "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert r.json()["error"]["error_code"].lower() == "bad_params"


def test_messages_entitlement_denied_403(client, monkeypatch):
    sess = _seed_session()
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "restricted")
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "converse"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"


def test_messages_text_success_202_calls_start_turn(client, monkeypatch):
    sess = _seed_session()
    captured = {}

    def _fake_start_turn(tenant_id, session_id, *, text=None, confirm=None,
                         classifier_hint=None, model=None, credential_grant=None):
        captured.update(tenant_id=tenant_id, session_id=session_id, text=text,
                        confirm=confirm, classifier_hint=classifier_hint,
                        model=model, credential_grant=credential_grant)
        return "turn-abc"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start_turn)
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "add a home run"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["turn_id"] == "turn-abc"
    assert body["status"] == "started"
    assert captured["tenant_id"] == sess["tenant_id"]
    assert captured["session_id"] == sess["session_id"]
    assert captured["text"] == "add a home run"
    assert captured["confirm"] is None


def test_messages_busy_409_turn_in_progress(client, monkeypatch):
    sess = _seed_session()

    def _boom(*a, **kw):
        raise turn_runner.TurnBusy("already active")

    monkeypatch.setattr(turn_runner, "start_turn", _boom)
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["error_code"] == "turn_in_progress"


def test_messages_grant_required_401_passthrough(client, monkeypatch):
    from envelopes import ErrorCode
    sess = _seed_session()

    def _boom(*a, **kw):
        raise turn_runner.TurnRejected(
            401, ErrorCode.GRANT_REQUIRED, "sign in with Claude",
            extra={"grant_required": True},
        )

    monkeypatch.setattr(turn_runner, "start_turn", _boom)
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["grant_required"] is True
    assert body["error"]["error_code"] == "GRANT_REQUIRED"


def test_messages_llm_quota_exhausted_429(client, monkeypatch):
    from envelopes import ErrorCode
    sess = _seed_session()

    def _boom(*a, **kw):
        raise turn_runner.TurnRejected(429, ErrorCode.LLM_QUOTA_EXHAUSTED, "quota hard cap")

    monkeypatch.setattr(turn_runner, "start_turn", _boom)
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"]["error_code"] == "llm_quota_exhausted"


def test_messages_llm_rate_limited_429(client, monkeypatch):
    from envelopes import ErrorCode
    sess = _seed_session()

    def _boom(*a, **kw):
        raise turn_runner.TurnRejected(429, ErrorCode.LLM_RATE_LIMITED, "slow down")

    monkeypatch.setattr(turn_runner, "start_turn", _boom)
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"]["error_code"] == "llm_rate_limited"


def test_messages_broker_unreachable_502(client, monkeypatch):
    from envelopes import ErrorCode
    sess = _seed_session()

    def _boom(*a, **kw):
        raise turn_runner.TurnRejected(502, ErrorCode.BROKER_UNREACHABLE, "harness down")

    monkeypatch.setattr(turn_runner, "start_turn", _boom)
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 502, r.text
    assert r.json()["error"]["error_code"] == "BROKER_UNREACHABLE"


def test_messages_confirm_missing_confirmation_id_400(client):
    sess = _seed_session()
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"confirm": {"approved": True}}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_messages_confirm_unknown_confirmation_id_404_bad_params(client):
    sess = _seed_session()
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": "no-such-confirm", "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_messages_confirm_cross_tenant_confirmation_404_bad_params(client):
    sess = _seed_session(tenant_id="tenant-owner")
    cid = _seed_approval(sess["session_id"], "tenant-owner")
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h("tenant-intruder"),
    )
    # cross-tenant on the SESSION itself is caught first (ownership guard) --
    # still collapses to a 404, just via session_not_found rather than the
    # approval-cross-tenant path (that path is only reachable when the caller
    # DOES own the session but the confirmation_id belongs to someone else).
    assert r.status_code == 404, r.text


def test_messages_confirm_cross_tenant_approval_same_session_404_bad_params(client, monkeypatch):
    """A caller who owns the session but references a confirmation_id created
    under a DIFFERENT tenant_id (defensive -- shouldn't happen via the real
    flow, but the guard must hold) -> 404 BAD_PARAMS, not a crash."""
    sess = _seed_session(tenant_id="tenant-a")
    # approval row tagged to a different tenant than the session's owner
    cid = _seed_approval(sess["session_id"], "tenant-other")
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h("tenant-a"),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_messages_confirm_expired_410_confirmation_expired(client):
    sess = _seed_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"], ttl_s=0.05)
    # consume_approval requires `decided` BEFORE it ever looks at expiry (see
    # session_store.consume_approval's check order) — decide it first via the
    # real /api/agent/approvals precedent (session_store.decide_approval
    # directly here, same as the other seeded-approval tests in this file)
    # so this test still isolates the EXPIRY outcome specifically.
    session_store.decide_approval(cid, True, by=sess["tenant_id"])
    time.sleep(0.15)
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r.status_code == 410, r.text
    assert r.json()["error"]["error_code"] == "confirmation_expired"


def test_messages_confirm_valid_builds_frozen_proposal_shape_from_approval_row(client, monkeypatch):
    sess = _seed_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"],
                         tool="write_home_run", params={"length_ft": 12},
                         capability="drawing.write")
    # consume_approval now REQUIRES a prior decision (mirrors the real
    # POST /api/agent/approvals call converse.js always makes before sending
    # the confirm message).
    session_store.decide_approval(cid, True, by=sess["tenant_id"])
    captured = {}

    def _fake_start_turn(tenant_id, session_id, *, text=None, confirm=None,
                         classifier_hint=None, model=None, credential_grant=None):
        captured["confirm"] = confirm
        return "turn-resume-1"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start_turn)
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r.status_code == 202, r.text
    assert r.json()["turn_id"] == "turn-resume-1"
    assert captured["confirm"] == {
        "confirmation_id": cid,
        "approved": True,
        "proposal": {
            "tool": "write_home_run",
            "params": {"length_ft": 12},
            "capability": "drawing.write",
        },
    }


# =========================================================================== #
# Merge-gate finding #1 — approvals bypassable/replayable. The four attack
# cases from the review, exercised at the HTTP router layer (store-level
# atomicity + the full outcome matrix live in tests/test_approval_consume.py).
# =========================================================================== #
def test_messages_confirm_skip_approvals_endpoint_409_undecided(client):
    """A client that jumps straight to the confirm message WITHOUT ever
    POSTing /api/agent/approvals first (i.e. an undecided row) must be
    rejected -- 409 BAD_PARAMS, the same (status, error_code) shape
    converse.js's classifyAgentError() already reads as 'approval_stale'."""
    sess = _seed_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])  # never decided

    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_messages_confirm_reverse_a_rejection_stored_approved_false_wins(client, monkeypatch):
    """The approval was DECIDED as rejected (approved=False). A client that
    sends {approved: true} on the confirm message must NOT be able to flip
    it -- the turn must receive the STORED approved=False, not the client's
    claim."""
    sess = _seed_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    outcome = session_store.decide_approval(cid, False, by=sess["tenant_id"])
    assert outcome == "recorded"

    captured = {}

    def _fake_start_turn(tenant_id, session_id, *, text=None, confirm=None,
                         classifier_hint=None, model=None, credential_grant=None):
        captured["confirm"] = confirm
        return "turn-resume-reversed"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start_turn)
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},  # client LIES
        headers=_h(sess["tenant_id"]),
    )
    assert r.status_code == 202, r.text
    assert captured["confirm"]["approved"] is False  # the STORED decision wins


def test_messages_confirm_cross_session_use_404_bad_params(client, monkeypatch):
    """An approval created under session A, presented on session B (same
    tenant owns both) -- must 404, never resolve to session B's turn."""
    sess_a = _seed_session(tenant_id="tenant-xsession")
    sess_b = _seed_session(tenant_id="tenant-xsession")
    cid = _seed_approval(sess_a["session_id"], "tenant-xsession")
    session_store.decide_approval(cid, True, by="tenant-xsession")

    def _boom(*a, **kw):
        raise AssertionError("start_turn must never be called for a cross-session approval")

    monkeypatch.setattr(turn_runner, "start_turn", _boom)
    r = client.post(
        f"/api/sessions/{sess_b['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h("tenant-xsession"),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_messages_confirm_replay_second_confirm_409_bad_params(client, monkeypatch):
    """A confirm message replayed against an already-consumed approval must
    be rejected -- the FIRST confirm resumes the turn exactly once; the
    SECOND must 409, never start a second turn from the same approval."""
    sess = _seed_session()
    cid = _seed_approval(sess["session_id"], sess["tenant_id"])
    session_store.decide_approval(cid, True, by=sess["tenant_id"])

    calls = []

    def _fake_start_turn(tenant_id, session_id, *, text=None, confirm=None,
                         classifier_hint=None, model=None, credential_grant=None):
        calls.append(confirm)
        return f"turn-resume-{len(calls)}"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start_turn)

    r1 = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r1.status_code == 202, r1.text

    r2 = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"confirm": {"confirmationId": cid, "approved": True}},
        headers=_h(sess["tenant_id"]),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["error_code"] == "BAD_PARAMS"
    assert len(calls) == 1  # the replay never reached start_turn a second time


# =========================================================================== #
# Consume-vs-CAS race — a confirm that loses the turn CAS must not eat the
# approval. consume_approval() runs BEFORE start_turn's try_begin_turn CAS
# (the CAS is inside start_turn), so the 409 TURN_IN_PROGRESS leg has to give
# the consume back or the retry it invites can only fail 'already_consumed'.
# =========================================================================== #
def test_messages_confirm_busy_gives_the_approval_back(client, monkeypatch):
    """Hold the session's REAL turn lock, send a confirm, and assert the full
    round trip: 409 -> approval still unconsumed -> the SAME approval redeems
    once the lock clears -> a replay after that redemption is still rejected.

    The busy leg runs the REAL turn_runner.start_turn on purpose — TurnBusy is
    raised by the CAS before any harness I/O, so this stays hermetic while
    exercising the actual race site rather than a stub of it."""
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    cid = _seed_approval(sid, tid, tool="write_home_run",
                         params={"length_ft": 12}, capability="drawing.write")
    session_store.decide_approval(cid, True, by=tid)

    # A concurrent turn holds the session lock (what try_begin_turn sees).
    assert session_store.try_begin_turn(sid, "turn-concurrent-holder", 300) is True

    confirm_body = {"confirm": {"confirmationId": cid, "approved": True}}
    busy = client.post(f"/api/sessions/{sid}/messages", json=confirm_body, headers=_h(tid))
    assert busy.status_code == 409, busy.text
    assert busy.json()["error"]["error_code"] == "turn_in_progress"
    assert busy.json()["error"]["retryable"] is True

    # THE REGRESSION: the losing confirm must not have spent the approval.
    assert session_store.get_approval(cid)["consumed"] is False, (
        "the approval was consumed by a confirm whose turn never started — "
        "the retry the 409 invites can now only fail 'already_consumed'"
    )

    # The concurrent turn finishes and the client retries, exactly as the
    # retryable 409 tells it to.
    session_store.end_turn(sid, "turn-concurrent-holder")

    calls = []

    def _fake_start_turn(tenant_id, session_id, *, text=None, confirm=None,
                         classifier_hint=None, model=None, credential_grant=None):
        calls.append(confirm)
        return f"turn-after-busy-{len(calls)}"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start_turn)
    retry = client.post(f"/api/sessions/{sid}/messages", json=confirm_body, headers=_h(tid))
    assert retry.status_code == 202, retry.text
    assert retry.json()["turn_id"] == "turn-after-busy-1"
    # the SAME approval resumed, with its stored proposal intact
    assert calls == [{
        "confirmation_id": cid,
        "approved": True,
        "proposal": {
            "tool": "write_home_run",
            "params": {"length_ft": 12},
            "capability": "drawing.write",
        },
    }]

    # ...and single-redeem is still single-redeem: the give-back reopened the
    # approval for a turn that never ran, it did NOT make it reusable.
    replay = client.post(f"/api/sessions/{sid}/messages", json=confirm_body, headers=_h(tid))
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["error_code"] == "BAD_PARAMS"
    assert len(calls) == 1, "the replay started a SECOND turn from one approval"


def test_messages_busy_response_keeps_the_full_run_envelope(client):
    """The busy 409 is a §3 run envelope, and adding the give-back must not
    have quietly reshaped it. Pin every field: a body assembled from
    `with_envelope_fields` instead of `err_envelope` drops the seven
    ok/tool/version/result/overlay/timing_ms/cost keys that clients parse."""
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    assert session_store.try_begin_turn(sid, "turn-holder-envelope", 300) is True
    try:
        r = client.post(f"/api/sessions/{sid}/messages",
                        json={"text": "hi"}, headers=_h(tid))
        assert r.status_code == 409, r.text
        body = r.json()
        assert set(body) == {
            "ok", "tool", "version", "result", "overlay", "timing_ms", "cost",
            "error", "degraded_mode",
        }, body
        assert body["ok"] is False
        assert body["degraded_mode"] is False
        # no give-back happened, so the honesty field stays absent
        assert "approval_recovered" not in body
    finally:
        session_store.end_turn(sid, "turn-holder-envelope")


def test_messages_busy_text_turn_needs_no_give_back(client):
    """The give-back is confirm-only: a busy TEXT turn has no approval to
    return, and must still answer a plain retryable 409."""
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    assert session_store.try_begin_turn(sid, "turn-holder-text", 300) is True
    try:
        r = client.post(f"/api/sessions/{sid}/messages",
                        json={"text": "hi"}, headers=_h(tid))
        assert r.status_code == 409, r.text
        assert r.json()["error"]["error_code"] == "turn_in_progress"
    finally:
        session_store.end_turn(sid, "turn-holder-text")


def test_messages_confirm_busy_give_back_failure_answers_409_honestly(client, monkeypatch):
    """A store failure inside the give-back must not become a 500 — but it must
    not be papered over either. The approval really is still spent, so the
    response must NOT advertise a retry that could only fail
    'already_consumed'."""
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    cid = _seed_approval(sid, tid)
    session_store.decide_approval(cid, True, by=tid)
    assert session_store.try_begin_turn(sid, "turn-holder-boom", 300) is True

    def _boom(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(session_store, "unconsume_approval", _boom)
    try:
        r = client.post(f"/api/sessions/{sid}/messages",
                        json={"confirm": {"confirmationId": cid, "approved": True}},
                        headers=_h(tid))
        assert r.status_code == 409, r.text
        body = r.json()
        # The honesty assertions. NOT turn_in_progress: converse.js maps that
        # to 'busy' and tells the user to wait, which can never help once the
        # approval is gone. 409 + BAD_PARAMS is the shape that client already
        # classifies as 'approval_stale' -> "ask the assistant to propose it
        # again", which IS the user's real next step.
        assert body["error"]["error_code"] == "BAD_PARAMS"
        assert body["error"]["retryable"] is False, (
            "give-back failed, so the approval is spent — telling the client to "
            "retry sends it into a loop that can only fail 'already_consumed'"
        )
        assert body["approval_recovered"] is False
        assert session_store.get_approval(cid)["consumed"] is True
        # the full run envelope survives on this leg too
        assert set(body) >= {"ok", "tool", "version", "result", "overlay",
                             "timing_ms", "cost", "error", "degraded_mode"}
    finally:
        session_store.end_turn(sid, "turn-holder-boom")


def test_messages_confirm_busy_give_back_noop_answers_409_honestly(client, monkeypatch):
    """Same honesty rule when the give-back RETURNS FALSE rather than raising:
    no row moved 1 -> 0, so the approval is still spent."""
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    cid = _seed_approval(sid, tid)
    session_store.decide_approval(cid, True, by=tid)
    assert session_store.try_begin_turn(sid, "turn-holder-noop", 300) is True

    monkeypatch.setattr(session_store, "unconsume_approval", lambda *a, **kw: False)
    try:
        r = client.post(f"/api/sessions/{sid}/messages",
                        json={"confirm": {"confirmationId": cid, "approved": True}},
                        headers=_h(tid))
        assert r.status_code == 409, r.text
        assert r.json()["error"]["error_code"] == "BAD_PARAMS"
        assert r.json()["error"]["retryable"] is False
        assert r.json()["approval_recovered"] is False
    finally:
        session_store.end_turn(sid, "turn-holder-noop")


def test_give_back_failure_emits_an_alarmable_metric(client, monkeypatch):
    """A destroyed approval must reach the STRUCTURED reporting path, not just
    a stderr string: an operator cannot alarm on prose."""
    emitted = []
    monkeypatch.setattr(
        sessions_router.emf_metrics, "emit_approval_give_back_failed",
        lambda reason, **kw: emitted.append((reason, kw)),
    )
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    cid = _seed_approval(sid, tid)
    session_store.decide_approval(cid, True, by=tid)
    assert session_store.try_begin_turn(sid, "turn-holder-metric", 300) is True

    monkeypatch.setattr(session_store, "unconsume_approval", lambda *a, **kw: False)
    try:
        client.post(f"/api/sessions/{sid}/messages",
                    json={"confirm": {"confirmationId": cid, "approved": True}},
                    headers=_h(tid))
    finally:
        session_store.end_turn(sid, "turn-holder-metric")

    assert len(emitted) == 1, emitted
    reason, kw = emitted[0]
    assert reason == "not_released"
    assert kw["confirmation_id"] == cid
    assert kw["session_id"] == sid


# =========================================================================== #
# The SECOND consume-vs-dispatch burn site: a TurnRejected raised BEFORE the
# harness could see the turn. BROKER_UNREACHABLE is advertised retryable, so
# without a give-back the invited retry can only fail 'already_consumed' --
# the same defect as the busy path, on a different leg.
# =========================================================================== #
def test_messages_confirm_pre_harness_rejection_gives_the_approval_back(client, monkeypatch):
    """Run the REAL start_turn with no harness configured. It wins the CAS,
    then rejects with BROKER_UNREACHABLE before any POST is attempted
    (pre_harness=True), so the approval must go back on the shelf and redeem
    successfully once a harness exists."""
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    cid = _seed_approval(sid, tid, tool="write_home_run",
                         params={"length_ft": 12}, capability="drawing.write")
    session_store.decide_approval(cid, True, by=tid)

    confirm_body = {"confirm": {"confirmationId": cid, "approved": True}}
    r = client.post(f"/api/sessions/{sid}/messages", json=confirm_body, headers=_h(tid))
    assert r.status_code == 502, r.text
    assert r.json()["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert r.json()["error"]["retryable"] is True

    # THE REGRESSION: nothing reached the harness, so the approval is not spent.
    assert session_store.get_approval(cid)["consumed"] is False, (
        "a rejection raised before the harness was ever contacted consumed the "
        "approval anyway — the retryable 502 now invites a doomed retry"
    )

    # and it really does still redeem.
    calls = []

    def _fake_start_turn(tenant_id, session_id, *, text=None, confirm=None,
                         classifier_hint=None, model=None, credential_grant=None):
        calls.append(confirm)
        return "turn-after-pre-harness"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start_turn)
    retry = client.post(f"/api/sessions/{sid}/messages", json=confirm_body, headers=_h(tid))
    assert retry.status_code == 202, retry.text
    assert calls[0]["confirmation_id"] == cid
    assert calls[0]["approved"] is True


def test_messages_confirm_ambiguous_rejection_keeps_the_approval_spent(client, monkeypatch):
    """The negative control, and the reason `pre_harness` defaults False: when
    the engine CANNOT prove the harness stayed untouched (a read timeout, or a
    rejection the harness itself returned), the approval must stay spent. The
    harness may already be running the tool call, and un-spending it there is
    the double-execution consume-once exists to prevent."""
    from envelopes import ErrorCode
    sess = _seed_session()
    sid, tid = sess["session_id"], sess["tenant_id"]
    cid = _seed_approval(sid, tid)
    session_store.decide_approval(cid, True, by=tid)

    def _ambiguous(*a, **kw):
        # no pre_harness -> the fail-safe default
        raise turn_runner.TurnRejected(
            502, ErrorCode.BROKER_UNREACHABLE, "harness read timed out")

    monkeypatch.setattr(turn_runner, "start_turn", _ambiguous)
    r = client.post(f"/api/sessions/{sid}/messages",
                    json={"confirm": {"confirmationId": cid, "approved": True}},
                    headers=_h(tid))
    assert r.status_code == 502, r.text
    assert session_store.get_approval(cid)["consumed"] is True, (
        "an approval was un-spent on a leg where the harness may already have "
        "acted on it — this is the double-execution consume-once prevents"
    )
    assert "approval_recovered" not in r.json()


# =========================================================================== #
# GET /api/sessions/{id}/stream — SSE, event-per-frame, after_seq replay
# =========================================================================== #
def test_stream_unknown_session_404(client):
    r = client.get("/api/sessions/no-such-session/stream", headers=_h("tenant-a"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "session_not_found"


def test_stream_replays_only_events_after_cursor(client, monkeypatch):
    # keep the loop's own cadence fast + bounded so a bug can never hang the suite
    monkeypatch.setattr(sessions_router, "STREAM_POLL_S", 0.01)
    monkeypatch.setattr(sessions_router, "STREAM_PING_S", 60.0)
    monkeypatch.setattr(sessions_router, "STREAM_DEADLINE_S", 5.0)

    sess = _seed_session()
    sid = sess["session_id"]
    seq1 = session_store.append_event(sid, "turn-1", "turn_started", {"text": "hi"})
    seq2 = session_store.append_event(sid, "turn-1", "text_delta", {"text": "hello"})
    seq3 = session_store.append_event(sid, "turn-1", "turn_complete", {"stop_reason": "end_turn"})
    assert seq1 < seq2 < seq3

    seen_types = []
    seen_seqs = []
    with client.stream("GET", f"/api/sessions/{sid}/stream?after_seq={seq1}",
                       headers=_h(sess["tenant_id"])) as resp:
        assert resp.status_code == 200
        pending_type = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                pending_type = line[len("event: "):]
            elif line.startswith("data: "):
                import json as _json
                env = _json.loads(line[len("data: "):])
                seen_types.append(pending_type)
                seen_seqs.append(env["seq"])
                if len(seen_seqs) >= 2:
                    break

    assert seen_types == ["text_delta", "turn_complete"]
    assert seen_seqs == [seq2, seq3]
    assert seq1 not in seen_seqs


# =========================================================================== #
# GET /api/sessions/{id}/transcript — ascending, limit clamp
# =========================================================================== #
def test_transcript_unknown_session_404(client):
    r = client.get("/api/sessions/no-such-session/transcript", headers=_h("tenant-a"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "session_not_found"


def test_transcript_returns_events_ascending(client):
    sess = _seed_session()
    sid = sess["session_id"]
    session_store.append_event(sid, "turn-1", "turn_started", {"text": "hi"})
    session_store.append_event(sid, "turn-1", "text_delta", {"text": "hello"})
    session_store.append_event(sid, "turn-1", "turn_complete", {"stop_reason": "end_turn"})

    r = client.get(f"/api/sessions/{sid}/transcript?limit=200", headers=_h(sess["tenant_id"]))
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert [e["type"] for e in events] == ["turn_started", "text_delta", "turn_complete"]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)


def test_transcript_clamps_oversized_limit_to_max(client, monkeypatch):
    sess = _seed_session()
    captured = {}
    real_recent_events = session_store.recent_events

    def _spy(session_id, limit):
        captured["limit"] = limit
        return real_recent_events(session_id, limit)

    monkeypatch.setattr(session_store, "recent_events", _spy)
    r = client.get(f"/api/sessions/{sess['session_id']}/transcript?limit=999999999",
                   headers=_h(sess["tenant_id"]))
    assert r.status_code == 200, r.text
    assert captured["limit"] == sessions_router.TRANSCRIPT_MAX_LIMIT


def test_transcript_clamps_nonpositive_limit_to_one(client, monkeypatch):
    sess = _seed_session()
    captured = {}
    real_recent_events = session_store.recent_events

    def _spy(session_id, limit):
        captured["limit"] = limit
        return real_recent_events(session_id, limit)

    monkeypatch.setattr(session_store, "recent_events", _spy)
    r = client.get(f"/api/sessions/{sess['session_id']}/transcript?limit=0",
                   headers=_h(sess["tenant_id"]))
    assert r.status_code == 200, r.text
    assert captured["limit"] == 1


# =========================================================================== #
# Merge-gate finding #6 — /api/site/capabilities must be BUILTIN-only (no
# authored/runtime tool leak). routers/site.py has no dedicated test file of
# its own (checked: none exists anywhere in this repo) and isn't part of this
# suite's WRITE-SET scope for a new file, so this coverage lives here instead
# -- see routers/site.py's own BUILTIN-ONLY docstring note for the loader
# split this test asserts against.
# =========================================================================== #
def test_site_capabilities_never_leaks_an_authored_tool():
    import deps
    from routers import site as site_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    fake_tool = {
        "name": "zzz-test-authored-leak-probe",
        "version": "1.0.0",
        "description": "a fake authored tool that must never reach the public site catalog",
        "kind": "script",
        "engine_op": "zzz_test_authored_leak_probe",
        "params": {"type": "object", "properties": {}},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent", "created": "2026-07-20T00:00:00Z"},
    }
    deps._AUTHORED.append(fake_tool)
    try:
        # sanity: it DOES show up in the full (authenticated) tool union --
        # proves this probe would have failed loudly had the filter been
        # broken, rather than passing vacuously because the tool never
        # existed anywhere.
        assert any(t["name"] == fake_tool["name"] for t in deps.all_tools(deps.DEFAULT_TENANT))

        site_app = FastAPI()
        install_error_handlers(site_app)
        site_app.include_router(site_router.router)
        site_client = TestClient(site_app, raise_server_exceptions=False)

        r = site_client.get("/api/site/capabilities")
        assert r.status_code == 200, r.text
        families = r.json()["families"]
        all_names = {cap["name"] for fam in families for cap in fam["capabilities"]}
        assert fake_tool["name"] not in all_names, (
            "an authored/runtime tool leaked into the public /api/site/capabilities catalog"
        )
    finally:
        deps._AUTHORED[:] = [t for t in deps._AUTHORED if t["name"] != fake_tool["name"]]


def test_zzz_real_sessions_db_still_absent():
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )
