"""
Approval policy (chip: "Read-only auto-runs only in auto_approve_reads;
paid/write always confirms").

The safety property is the DEFAULT and the FILTER:
  (a) with no policy set, nothing changes — a drawing.read proposal still
      waits for a human (regression pin for every deployed client);
  (b) under auto_approve_reads, a drawing.read proposal is auto-decided
      (decided_by records the policy) and a confirm turn auto-starts at the
      proposing turn's terminal, built from the STORED row;
  (c) drawing.write / a bogus value / "" / MISSING capability NEVER
      auto-confirm, even
      under the policy (falsification-checked: widening the capability set
      fails this);
  (d) a proposal with no stored dwg binding stays manual (the router's rule);
  (e) route level: POST /api/sessions validates and persists `policy`,
      idempotent re-POST without the field keeps it, and the policy store is
      tenant-scoped at the storage boundary.

Run:  cd server && python -m pytest tests/test_approval_policy.py -q
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="policy-sessions-")) / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

import session_policy  # noqa: E402
import session_store  # noqa: E402
import turn_runner  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402


# --------------------------------------------------------------------------- #
# scripted harness stub: first turn proposes; the confirm turn completes.
# --------------------------------------------------------------------------- #
class _TurnStub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    SCRIPTS: List[List[Dict[str, Any]]] = []   # consumed one per POST
    BODIES: List[Dict[str, Any]] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length)
        cls = type(self)
        try:
            cls.BODIES.append(json.loads(body or b"{}"))
        except Exception:
            cls.BODIES.append({})
        script = cls.SCRIPTS.pop(0) if cls.SCRIPTS else [
            {"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in script:
            raw = (json.dumps(ev) + "\n").encode("utf-8")
            self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *a):
        return


@pytest.fixture
def turn_stub():
    _TurnStub.SCRIPTS = []
    _TurnStub.BODIES = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TurnStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _TurnStub
    finally:
        srv.shutdown()


_counter = [0]


def _new_session(tenant_id: str = "tenant-p") -> Dict[str, Any]:
    _counter[0] += 1
    return session_store.get_or_create_session(
        tenant_id, f"dwg-policy-{_counter[0]}-{time.time()}")


def _wait_until(predicate, timeout_s: float = 5.0, poll_s: float = 0.02):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _proposal_script(cid: str, capability: Optional[str], dwg: Optional[str]):
    data: Dict[str, Any] = {
        "confirmation_id": cid,
        "tool": "panel_count",
        "params": {"layer": "roof"},
        "rationale": "counts panels",
    }
    if capability is not None:
        data["capability"] = capability
    if dwg is not None:
        data["dwg"] = dwg
    return [
        {"type": "proposed_run", "data": data},
        {"type": "confirmation_required",
         "data": {"confirmation_id": cid, "kind": "run_capability"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_confirmation"}},
    ]


def _run_proposing_turn(stub, tenant: str, sid: str, dwg: str, cid: str,
                        capability: Optional[str] = "drawing.read",
                        with_dwg: bool = True):
    stub.SCRIPTS.append(_proposal_script(cid, capability, dwg if with_dwg else None))
    turn_runner.start_turn(tenant, sid, text="count my panels")
    assert _wait_until(
        lambda: session_store.get_session(sid)["active_turn_id"] is None
        and not turn_runner._cancellers)


@pytest.fixture(autouse=True)
def _fast_settle():
    yield
    # let any auto-started confirm turn finish before the stub tears down
    time.sleep(0.05)


# =========================================================================== #
# (a) default: byte-identical, nothing auto-runs
# =========================================================================== #
def test_default_policy_leaves_the_chip_for_a_human(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-default")

    approval = session_store.get_approval("cid-default")
    assert approval is not None
    assert approval["decided"] is False, "the DEFAULT policy auto-decided an approval"
    assert approval["consumed"] is False
    assert len(stub.BODIES) == 1, "a confirm turn was auto-started under the default policy"


# =========================================================================== #
# (b) auto_approve_reads: drawing.read auto-decides and auto-confirms
# =========================================================================== #
def test_auto_approve_reads_confirms_a_drawing_read_proposal(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")

    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-auto")

    ok = _wait_until(lambda: len(stub.BODIES) >= 2)
    assert ok, "no confirm turn auto-started under auto_approve_reads"
    confirm_body = stub.BODIES[1]
    confirm = confirm_body.get("confirm")
    assert confirm and confirm["confirmation_id"] == "cid-auto"
    assert confirm["approved"] is True
    assert confirm["proposal"]["tool"] == "panel_count"
    assert confirm["proposal"]["dwg"] == sess["drawing_id"]

    approval = session_store.get_approval("cid-auto")
    assert approval["decided"] is True and approval["approved"] is True
    assert approval["decided_by"] == "policy:auto_approve_reads", (
        "the audit trail must name the policy, not a human")
    assert approval["consumed"] is True
    assert _wait_until(lambda: session_store.get_session(sid)["active_turn_id"] is None)


# =========================================================================== #
# (c) write/paid/missing capabilities never auto-confirm
# =========================================================================== #
@pytest.mark.parametrize("capability", ["drawing.write", "run_read", "", None])
def test_non_read_capabilities_stay_manual(monkeypatch, turn_stub, capability):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    cid = f"cid-{capability or 'missing'}-{_counter[0]}"

    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], cid,
                        capability=capability)

    time.sleep(0.2)  # give a wrong implementation the chance to fire
    approval = session_store.get_approval(cid)
    assert approval["decided"] is False, (
        f"capability {capability!r} was auto-decided — paid/write must always confirm")
    assert len(stub.BODIES) == 1


# =========================================================================== #
# (d) no stored dwg binding -> manual (the router's own confirm rule)
# =========================================================================== #
def test_missing_dwg_binding_stays_manual(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")

    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-nodwg",
                        with_dwg=False)

    time.sleep(0.2)
    approval = session_store.get_approval("cid-nodwg")
    assert approval["decided"] is False
    assert len(stub.BODIES) == 1


# =========================================================================== #
# (e) route + storage boundary
# =========================================================================== #
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


def test_route_validates_persists_and_keeps_policy(client):
    _counter[0] += 1
    dwg = f"dwg-rp-{_counter[0]}"
    bad = client.post("/api/sessions", json={"drawing_id": dwg, "policy": "yolo"},
                      headers=_h("tenant-rp"))
    assert bad.status_code == 400

    r = client.post("/api/sessions",
                    json={"drawing_id": dwg, "policy": "auto_approve_reads"},
                    headers=_h("tenant-rp"))
    assert r.status_code < 300, r.text
    assert r.json()["policy"] == "auto_approve_reads"

    again = client.post("/api/sessions", json={"drawing_id": dwg}, headers=_h("tenant-rp"))
    assert again.json()["policy"] == "auto_approve_reads", (
        "an idempotent re-POST without the field reset the stored policy")


def test_policy_store_is_tenant_scoped():
    sess = _new_session("tenant-scope-a")
    session_policy.set_policy(sess["session_id"], "tenant-scope-a", "auto_approve_reads")
    assert session_policy.get_policy(sess["session_id"], "tenant-scope-a") == "auto_approve_reads"
    assert session_policy.get_policy(sess["session_id"], "tenant-scope-b") == "confirm_all", (
        "a foreign tenant read another tenant's policy")


# =========================================================================== #
# round 2: the gate is the AUTHORITY, and a lost race must be resumable
# =========================================================================== #
def test_gate_is_granted_before_the_session_row(monkeypatch, turn_stub):
    """Review round 1, blocker 2: the human path lands the decision in the
    section-18 gate FIRST (routers/agent.py) because the resume turn consults
    THAT record. Deciding only the session mirror leaves the gate pending and
    the resumed tool never dispatches."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    order: List[str] = []

    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: ({"confirmation_id": cid}, "ok"))

    def _grant(cid, *, by="tenant"):
        order.append(f"gate:{by}")
        return (True, {"confirmation_id": cid}, "granted")
    monkeypatch.setattr(turn_runner.agent_gate, "grant_approval", _grant)

    real_decide = turn_runner.session_store.decide_approval

    def _decide(cid, approved, by=None):
        order.append(f"session:{by}")
        return real_decide(cid, approved, by=by)
    monkeypatch.setattr(turn_runner.session_store, "decide_approval", _decide)

    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-gate")

    assert _wait_until(lambda: len(order) >= 2), f"order={order}"
    assert order[0].startswith("gate:"), f"the session row was decided BEFORE the gate: {order}"
    assert order[0] == f"gate:{turn_runner.POLICY_DECIDER}"
    assert order[1] == f"session:{turn_runner.POLICY_DECIDER}"


def test_a_refusing_gate_leaves_the_chip_manual(monkeypatch, turn_stub):
    """A gate that will not grant (already decided, expired) must NOT produce a
    session decision — the two stores may never record opposite outcomes."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: ({"confirmation_id": cid}, "ok"))
    monkeypatch.setattr(turn_runner.agent_gate, "grant_approval",
                        lambda cid, *, by="tenant": (False, None, "already_decided"))

    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-refused")

    time.sleep(0.2)
    approval = session_store.get_approval("cid-refused")
    assert approval["decided"] is False, (
        "the session row was decided even though the gate refused")
    assert len(stub.BODIES) == 1


def test_an_unreadable_gate_fails_closed(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "io_error"))
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-ioerr")

    time.sleep(0.2)
    assert session_store.get_approval("cid-ioerr")["decided"] is False
    assert len(stub.BODIES) == 1


def test_a_policy_decision_left_unconsumed_is_resumable(monkeypatch, turn_stub):
    """Review round 1, finding 3: a lost CAS used to strand the approval —
    decided-by-policy forever, so a human could neither re-decide nor (under
    live auth) consume it. Our own unconsumed decision must be finishable at a
    later terminal."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))

    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")

    # Simulate the stranded state the old code produced: our decision recorded,
    # nothing consumed, no confirm turn started.
    _run_proposing_turn(stub, "tenant-p", sid, sess["drawing_id"], "cid-resume",
                        capability="drawing.write")  # ineligible -> no auto path
    session_store.create_approval(
        confirmation_id="cid-resume-2", session_id=sid, tenant_id="tenant-p",
        turn_id="t-old", tool="panel_count", params={"layer": "roof"},
        capability="drawing.read", rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)
    session_store.decide_approval("cid-resume-2", True,
                                  by=turn_runner.POLICY_DECIDER)

    turn_runner._auto_confirm_reads(
        "tenant-p", sid,
        {"cid-resume-2": {"capability": "drawing.read"}}, None, "demo")

    assert _wait_until(
        lambda: session_store.get_approval("cid-resume-2")["consumed"] is True), (
        "our own unconsumed policy decision was not resumed — it is stranded")


def test_a_humans_decision_is_never_resumed_by_the_policy(monkeypatch, turn_stub):
    """The resume key is decided_by == POLICY_DECIDER. A human's decision
    belongs to that human to redeem."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    session_store.create_approval(
        confirmation_id="cid-human", session_id=sid, tenant_id="tenant-p",
        turn_id="t-h", tool="panel_count", params={}, capability="drawing.read",
        rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)
    session_store.decide_approval("cid-human", True, by="auth0|alice")

    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-human": {"capability": "drawing.read"}}, None, "demo")

    time.sleep(0.15)
    assert session_store.get_approval("cid-human")["consumed"] is False, (
        "the policy consumed a decision a HUMAN made")


def test_a_lost_cas_is_retried_at_the_next_terminal(monkeypatch, turn_stub):
    """Review round 2, finding 1: the resume path had NO reachable trigger —
    each relay owns a fresh proposals map, so a later terminal could not
    rediscover the stranded id. A lost CAS now PARKS the id, and the next
    terminal (any turn, unrelated) retries it."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    session_store.create_approval(
        confirmation_id="cid-park", session_id=sid, tenant_id="tenant-p",
        turn_id="t-park", tool="panel_count", params={}, capability="drawing.read",
        rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    # Force the CAS loss: hold the session with an orphan turn.
    assert session_store.try_begin_turn(sid, "orphan-park", 300)
    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-park": {"capability": "drawing.read"}}, None, "demo")

    with turn_runner._pending_policy_lock:
        assert turn_runner._pending_policy.get(sid) == ["cid-park"], (
            "a lost CAS did not park the decision — nothing will retry it")
    assert session_store.get_approval("cid-park")["consumed"] is False

    # A LATER terminal with completely unrelated proposals must still finish it.
    session_store.end_turn(sid, "orphan-park")
    turn_runner._auto_confirm_reads("tenant-p", sid, {}, None, "demo")

    assert _wait_until(
        lambda: session_store.get_approval("cid-park")["consumed"] is True), (
        "the parked decision was never retried at a later terminal")
    with turn_runner._pending_policy_lock:
        assert sid not in turn_runner._pending_policy


@pytest.fixture(autouse=True)
def _clean_parked():
    yield
    with turn_runner._pending_policy_lock:
        turn_runner._pending_policy.clear()


def test_two_overlapping_strands_are_both_retried(monkeypatch, turn_stub):
    """Review round 3 HIGH: terminals overlap (the CAS is released before
    auto-confirm runs), so two of them can each park a different id. A single
    slot let the second overwrite the first and the displaced row was lost."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    for cid in ("cid-two-a", "cid-two-b"):
        session_store.create_approval(
            confirmation_id=cid, session_id=sid, tenant_id="tenant-p",
            turn_id=f"t-{cid}", tool="panel_count", params={},
            capability="drawing.read", rationale="r", kind="run_capability",
            payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    # Both terminals lose the CAS: each must park its own id.
    assert session_store.try_begin_turn(sid, "orphan-two", 300)
    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-two-a": {"capability": "drawing.read"}}, None, "demo")
    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-two-b": {"capability": "drawing.read"}}, None, "demo")

    with turn_runner._pending_policy_lock:
        parked = list(turn_runner._pending_policy.get(sid) or ())
    assert parked == ["cid-two-a", "cid-two-b"], (
        f"a second strand displaced the first: {parked}")

    # Draining: each later terminal finishes one, and none is lost.
    session_store.end_turn(sid, "orphan-two")
    turn_runner._auto_confirm_reads("tenant-p", sid, {}, None, "demo")
    assert _wait_until(
        lambda: session_store.get_approval("cid-two-a")["consumed"] is True)
    assert _wait_until(
        lambda: session_store.get_session(sid)["active_turn_id"] is None)
    turn_runner._auto_confirm_reads("tenant-p", sid, {}, None, "demo")
    assert _wait_until(
        lambda: session_store.get_approval("cid-two-b")["consumed"] is True), (
        "the second stranded decision was never retried")


def test_at_capacity_the_policy_stops_deciding_instead_of_evicting(monkeypatch, turn_stub):
    """Review round 4 HIGH: a cap enforced by EVICTION strands whichever end it
    drops — the oldest decided-unconsumed row becomes unreachable, and dropping
    the newest strands the row just decided. Capacity is reserved BEFORE any
    decision, so at the cap the policy simply leaves chips manual."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    monkeypatch.setattr(turn_runner, "MAX_PENDING_POLICY", 2)

    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    ids = ["cid-cap-a", "cid-cap-b", "cid-cap-c"]
    for cid in ids:
        session_store.create_approval(
            confirmation_id=cid, session_id=sid, tenant_id="tenant-p",
            turn_id=f"t-{cid}", tool="panel_count", params={},
            capability="drawing.read", rationale="r", kind="run_capability",
            payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    assert session_store.try_begin_turn(sid, "orphan-cap", 300)
    for cid in ids:
        turn_runner._auto_confirm_reads(
            "tenant-p", sid, {cid: {"capability": "drawing.read"}}, None, "demo")

    with turn_runner._pending_policy_lock:
        parked = list(turn_runner._pending_policy.get(sid) or ())
    assert parked == ["cid-cap-a", "cid-cap-b"], f"cap not honoured: {parked}"
    # the third was never DECIDED, so it is an ordinary manual chip — not a
    # decided row nobody can reach.
    third = session_store.get_approval("cid-cap-c")
    assert third["decided"] is False, (
        "a row was decided with no capacity to track it — it is stranded")
    # and both parked rows are still decided-and-recoverable
    for cid in ("cid-cap-a", "cid-cap-b"):
        assert session_store.get_approval(cid)["decided"] is True
    session_store.end_turn(sid, "orphan-cap")


def test_concurrent_terminals_cannot_exceed_the_cap(monkeypatch, turn_stub):
    """Review round 5 HIGH: checking the length and appending later are two
    lock acquisitions, so N concurrent terminals all saw room, all decided, and
    all parked — over the cap, with rows decided that should have stayed
    manual. The slot must be TAKEN in the same acquisition as the check."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    monkeypatch.setattr(turn_runner, "MAX_PENDING_POLICY", 2)

    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    ids = [f"cid-conc-{i}" for i in range(5)]
    for cid in ids:
        session_store.create_approval(
            confirmation_id=cid, session_id=sid, tenant_id="tenant-p",
            turn_id=f"t-{cid}", tool="panel_count", params={},
            capability="drawing.read", rationale="r", kind="run_capability",
            payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    # Hold the session so every terminal loses the CAS, and widen the window
    # the old code was vulnerable in: between reading the capacity and parking.
    # A DELAY, not a barrier — only the threads that pass the capacity check
    # reach this point, so a barrier sized to the thread count can never
    # complete (it raised BrokenBarrierError and manufactured a different
    # failure). The delay is enough: with a check-without-take, every thread
    # observes room before any of them parks.
    assert session_store.try_begin_turn(sid, "orphan-conc", 300)
    real_decide = session_store.decide_approval

    def _slow_decide(cid, approved, by=None):
        time.sleep(0.15)
        return real_decide(cid, approved, by=by)
    monkeypatch.setattr(turn_runner.session_store, "decide_approval", _slow_decide)

    threads = [threading.Thread(
        target=turn_runner._auto_confirm_reads,
        args=("tenant-p", sid, {cid: {"capability": "drawing.read"}}, None, "demo"))
        for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    with turn_runner._pending_policy_lock:
        parked = list(turn_runner._pending_policy.get(sid) or ())
    assert len(parked) <= 2, f"the cap was exceeded under concurrency: {parked}"
    decided = [c for c in ids if session_store.get_approval(c)["decided"]]
    assert len(decided) <= 2, (
        f"more rows were decided than the cap can track: {decided}")
    assert set(decided) <= set(parked), (
        f"a decided row is not tracked: decided={decided} parked={parked}")
    session_store.end_turn(sid, "orphan-conc")


def test_policy_switched_off_drains_the_park(monkeypatch, turn_stub):
    """Review round 6, finding 2: parked entries were never inspected again
    once the policy was confirm_all — the drain drops them (the rows expire by
    their own TTL) instead of finishing them, because finishing one is still an
    auto-run the operator just switched off."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    session_store.create_approval(
        confirmation_id="cid-drain", session_id=sid, tenant_id="tenant-p",
        turn_id="t-drain", tool="panel_count", params={},
        capability="drawing.read", rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    assert session_store.try_begin_turn(sid, "orphan-drain", 300)
    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-drain": {"capability": "drawing.read"}}, None, "demo")
    with turn_runner._pending_policy_lock:
        assert turn_runner._pending_policy.get(sid) == ["cid-drain"]

    session_policy.set_policy(sid, "tenant-p", "confirm_all")
    session_store.end_turn(sid, "orphan-drain")
    turn_runner._auto_confirm_reads("tenant-p", sid, {}, None, "demo")

    with turn_runner._pending_policy_lock:
        assert sid not in turn_runner._pending_policy, (
            "policy-off left the parked entry orphaned")
    row = session_store.get_approval("cid-drain")
    assert row["consumed"] is False, "policy-off FINISHED a parked auto-run"


def test_persistent_give_back_failure_is_alarmed_and_ttl_bounded(monkeypatch, turn_stub):
    """Review round 6, finding 1: an unconsume that keeps failing leaves the
    row consumed-by-policy (a human cannot redeem it under live auth). The
    failure must reach the ALARMABLE channel, and the slot must free itself at
    the row's TTL via the top check's expired branch — bounded, never silent,
    never a permanently occupied cap."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    alarms = []
    monkeypatch.setattr(turn_runner.emf_metrics, "emit_approval_give_back_failed",
                        lambda reason, **kw: alarms.append((reason, kw)))
    monkeypatch.setattr(turn_runner.session_store, "unconsume_approval",
                        lambda *a, **k: False)
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    session_store.create_approval(
        confirmation_id="cid-gbf", session_id=sid, tenant_id="tenant-p",
        turn_id="t-gbf", tool="panel_count", params={},
        capability="drawing.read", rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    assert session_store.try_begin_turn(sid, "orphan-gbf", 300)
    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-gbf": {"capability": "drawing.read"}}, None, "demo")

    assert alarms and alarms[0][0] == "policy_auto_confirm", (
        "a destroyed approval did not reach the alarmable channel")
    row = session_store.get_approval("cid-gbf")
    assert row["consumed"] is True  # the stranded state, honestly present

    # TTL bound: age the row past expiry; the next terminal frees the slot.
    with session_store._lock:
        conn = session_store._db()
        conn.execute("UPDATE approvals SET expires_at = ? WHERE confirmation_id = ?",
                     (1.0, "cid-gbf"))
        conn.commit()
    session_store.end_turn(sid, "orphan-gbf")
    turn_runner._auto_confirm_reads("tenant-p", sid, {}, None, "demo")
    with turn_runner._pending_policy_lock:
        assert sid not in turn_runner._pending_policy, (
            "the slot never freed after the row expired — a permanent cap leak")


def test_a_reservation_release_retries_the_parked_decision(monkeypatch, turn_stub):
    """PR #311 round 8: a checkpoint-restore RESERVATION can win the slot just
    as the terminal's auto-confirm runs — the decision parks, and the restore's
    release used to kick only the queue, so nothing retried the park until an
    unrelated terminal or expiry. Every slot releaser now runs BOTH follow-ups
    (drain_session_followups), in the relay's order."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-p", "auto_approve_reads")
    session_store.create_approval(
        confirmation_id="cid-resv", session_id=sid, tenant_id="tenant-p",
        turn_id="t-resv", tool="panel_count", params={},
        capability="drawing.read", rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    # The reservation wins the slot; the terminal's auto-confirm loses and parks.
    assert session_store.try_begin_turn(
        sid, "restore-conflict-probe", session_store.RESERVATION_STALE_S)
    turn_runner._auto_confirm_reads(
        "tenant-p", sid, {"cid-resv": {"capability": "drawing.read"}}, None, "demo")
    with turn_runner._pending_policy_lock:
        assert turn_runner._pending_policy.get(sid) == ["cid-resv"]

    # The restore finishes: its release must retry the park, not just the queue.
    session_store.end_turn(sid, "restore-conflict-probe")
    turn_runner.drain_session_followups("tenant-p", sid)

    assert _wait_until(
        lambda: session_store.get_approval("cid-resv")["consumed"] is True), (
        "the reservation's release never retried the parked policy decision")
    with turn_runner._pending_policy_lock:
        assert sid not in turn_runner._pending_policy
