"""
Busy-turn queue (cap 1) — chip acceptance: "One pending prompt is persisted
and starts after the terminal event; a second is refused."

Engine level (real turn_runner against a scripted NDJSON harness stub, the
tests/test_turn_runner.py pattern):
  (a) a prompt queued behind a live turn STARTS at that turn's terminal event,
      and a second enqueue while one is parked answers "full";
  (b) enqueueing against a session that is actually free starts immediately
      (the enqueue/terminal handoff race is closed by the post-insert check);
  (c) the ORPHAN cancel path kicks the queue (no relay exists to do it);
  (d) a kick whose start_turn is REJECTED (harness unreachable) closes the
      transcript it opened — turn_started followed by a terminal `error` for
      the same turn_id — and empties the slot (no silent loss, no retry loop);
  (e) a kick that loses the CAS to a foreign turn RE-INSERTS the prompt — the
      structural property that makes "that turn's terminal will kick" true.
      Removing the re-insert makes this fail (falsification-checked).

Route level (real engine, orphan-held CAS, TestClient over ONLY the sessions
router — the tests/test_sessions_routes.py pattern):
  (f) busy + queue:true text -> 202 {status:"queued"}, and the durable
      `turn_queued` event is on the transcript;
  (g) a second queue:true while one is parked -> the byte-identical busy 409;
  (h) busy WITHOUT queue -> 409 exactly as before (opt-in regression pin);
  (i) queue:true + confirm -> 400 BEFORE the approval consume (the approval
      stays undecided-consumable); queue:true + credential_grant -> 400.

Run:  cd server && python -m pytest tests/test_turn_queue.py -q
"""
from __future__ import annotations

import http.server
import json
import os
import socket
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

# Redirect the sessions DB BEFORE session_store is imported (read once at
# import time — same posture as tests/test_turn_runner.py).
os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="turnqueue-sessions-")) / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

import deps  # noqa: E402
import entitlements  # noqa: E402
import requests  # noqa: E402
import session_policy  # noqa: E402
import session_store  # noqa: E402
import turn_runner  # noqa: E402
from envelopes import ErrorCode  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402


# --------------------------------------------------------------------------- #
# scripted harness stub (test_turn_runner.py pattern) + a GATE so the test —
# not the scheduler — decides when the first turn's terminal event lands.
# --------------------------------------------------------------------------- #
class _TurnStub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    SCRIPT: List[Dict[str, Any]] = []
    GATE: Optional[threading.Event] = None   # held BEFORE the final scripted event
    BODIES: List[Dict[str, Any]] = []

    def _write_chunk(self, raw: bytes) -> None:
        self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
        self.wfile.flush()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length)
        cls = type(self)
        try:
            cls.BODIES.append(json.loads(body or b"{}"))
        except Exception:
            cls.BODIES.append({})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i, ev in enumerate(cls.SCRIPT):
            if cls.GATE is not None and i == len(cls.SCRIPT) - 1:
                cls.GATE.wait(timeout=10)
            self._write_chunk((json.dumps(ev) + "\n").encode("utf-8"))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *a):  # silence
        return


@pytest.fixture
def turn_stub():
    _TurnStub.SCRIPT = [
        {"type": "text_delta", "data": {"text": "working"}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]
    _TurnStub.GATE = None
    _TurnStub.BODIES = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TurnStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _TurnStub
    finally:
        srv.shutdown()


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_counter = [0]


def _new_session(tenant_id: str = "tenant-q") -> Dict[str, Any]:
    _counter[0] += 1
    return session_store.get_or_create_session(
        tenant_id, f"dwg-queue-{_counter[0]}-{time.time()}")


def _wait_until(predicate, timeout_s: float = 5.0, poll_s: float = 0.02):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _types(session_id: str) -> List[str]:
    return [e["type"] for e in session_store.recent_events(session_id, 100)]


@pytest.fixture(autouse=True)
def _clean_queue():
    yield
    with turn_runner._queued_lock:
        turn_runner._queued.clear()


# =========================================================================== #
# (a) queued prompt starts at the terminal event; second enqueue refused
# =========================================================================== #
def test_queued_prompt_starts_after_terminal_and_second_is_refused(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    gate = threading.Event()
    stub.GATE = gate

    sess = _new_session()
    sid = sess["session_id"]
    turn_runner.start_turn("tenant-q", sid, text="first prompt")

    status, queued_id = turn_runner.try_enqueue_turn("tenant-q", sid, text="second prompt")
    assert (status, bool(queued_id)) == ("queued", True)
    # durable record, cap enforced
    assert "turn_queued" in _types(sid)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="third") == ("full", None)
    # the queued prompt must NOT have started while turn 1 is live
    assert sum(1 for t in _types(sid) if t == "turn_started") == 1

    gate.set()  # let turn 1 emit its terminal event

    ok = _wait_until(lambda: sum(1 for t in _types(sid) if t == "turn_started") == 2)
    assert ok, f"queued prompt never started; events: {_types(sid)}"
    started = [e for e in session_store.recent_events(sid, 100) if e["type"] == "turn_started"]
    assert started[1]["data"].get("text") == "second prompt"
    # The slot empties AFTER start_turn returns to the (async, drain-thread)
    # kicker — turn_started becoming durable precedes it by design (the claim
    # is held until the outcome is known). Wait, don't race the kicker.
    assert _wait_until(lambda: turn_runner.queued_prompt(sid) is None), (
        "the started prompt's slot was never released")
    # both turns end cleanly (stub's gate is already open for turn 2)
    assert _wait_until(lambda: session_store.get_session(sid)["active_turn_id"] is None)


# =========================================================================== #
# (b) the enqueue/terminal handoff race is closed
# =========================================================================== #
def test_enqueue_on_a_free_session_starts_immediately(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    # No active turn: the post-insert fence check must start the prompt itself
    # (this is exactly the state a request lands in when the busy turn
    # terminalized between the CAS loss and the enqueue registration).
    status, _ = turn_runner.try_enqueue_turn("tenant-q", sid, text="raced prompt")
    assert status == "queued"
    ok = _wait_until(lambda: "turn_started" in _types(sid))
    assert ok, f"handoff race not closed; events: {_types(sid)}"
    assert turn_runner.queued_prompt(sid) is None


# =========================================================================== #
# (c) the orphan cancel path kicks the queue
# =========================================================================== #
def test_orphan_cancel_kicks_queued_prompt(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    # An orphaned active turn: CAS held, but no relay in this process.
    assert session_store.try_begin_turn(sid, "orphan-turn-1", 300)
    status, _ = turn_runner.try_enqueue_turn("tenant-q", sid, text="after cancel")
    assert status == "queued"

    assert turn_runner.request_cancel("tenant-q", sid, "orphan-turn-1") == "cancelled"

    ok = _wait_until(
        lambda: any(e["type"] == "turn_started" and e["data"].get("text") == "after cancel"
                    for e in session_store.recent_events(sid, 100)))
    assert ok, f"orphan cancel did not kick; events: {_types(sid)}"
    assert turn_runner.queued_prompt(sid) is None


# =========================================================================== #
# (d) a rejected kick closes the transcript it opened and empties the slot
# =========================================================================== #
def test_rejected_kick_appends_error_terminal_and_drops_prompt(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL",
                       f"http://127.0.0.1:{_free_closed_port()}")
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-turn-2", 300)
    status, _ = turn_runner.try_enqueue_turn("tenant-q", sid, text="doomed prompt")
    assert status == "queued"

    assert turn_runner.request_cancel("tenant-q", sid, "orphan-turn-2") == "cancelled"

    events = session_store.recent_events(sid, 100)
    started = [e for e in events if e["type"] == "turn_started"
               and e["data"].get("text") == "doomed prompt"]
    assert started, f"queued start never appended turn_started; events: {_types(sid)}"
    turn_id = started[0]["turn_id"]
    errors = [e for e in events if e["type"] == "error" and e["turn_id"] == turn_id]
    assert errors, ("rejected queued start left a dangling turn_started "
                    f"(no terminal error); events: {_types(sid)}")
    assert errors[0]["data"]["error"]["error_code"] == ErrorCode.BROKER_UNREACHABLE
    assert turn_runner.queued_prompt(sid) is None
    # and the failed start released the CAS — the session is not wedged
    assert session_store.get_session(sid)["active_turn_id"] is None


# =========================================================================== #
# (e) a kick that loses the CAS re-inserts the prompt
# =========================================================================== #
def test_busy_kick_reinserts_the_prompt(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")  # never reached
    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-turn-3", 300)
    status, queued_id = turn_runner.try_enqueue_turn("tenant-q", sid, text="parked")
    assert status == "queued"

    # A racing kick (e.g. a stale terminal thread) while the CAS is genuinely
    # held: start_turn raises TurnBusy, and the prompt must survive in the
    # slot — otherwise "that turn's terminal will kick" points at nothing.
    turn_runner._kick_queued(sid)

    parked = turn_runner.queued_prompt(sid)
    assert parked is not None and parked["queued_id"] == queued_id, (
        "TurnBusy kick dropped the queued prompt instead of re-inserting it")
    session_store.end_turn(sid, "orphan-turn-3")


# =========================================================================== #
# round-2 guards: the slot is CLAIMED (not popped) for the whole attempt
# =========================================================================== #
def test_enqueue_during_handoff_answers_full_and_nothing_is_stranded(monkeypatch):
    """Review round 1, findings 1 and 3 in one deterministic interleaving.

    While the kicker's start_turn is mid-flight (harness connection pending),
    a concurrent request tries to enqueue. Popping the slot let that request
    be 202'd — and then the first start's failure left the newcomer parked on
    a FREE session with no future kick (stranded), or the retry dropped an
    accepted prompt as "superseded". With the claim design the newcomer gets
    "full" (cap-1 stays a promise to at most ONE 202), and a failed start
    closes its own transcript and empties the slot.
    """
    sess = _new_session()
    sid = sess["session_id"]
    inner_results = []

    real_post = requests.post

    def _post_that_races_an_enqueue(*args, **kwargs):
        # A concurrent POST /messages with queue:true lands EXACTLY here —
        # after the kicker committed to this start, before its outcome.
        inner_results.append(
            turn_runner.try_enqueue_turn("tenant-q", sid, text="newcomer"))
        raise requests.exceptions.ConnectionError("harness down mid-handoff")

    monkeypatch.setattr(turn_runner.requests, "post", _post_that_races_an_enqueue)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(turn_runner.requests, "post", _post_that_races_an_enqueue)

    assert session_store.try_begin_turn(sid, "orphan-h1", 300)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="first")[0] == "queued"
    assert turn_runner.request_cancel("tenant-q", sid, "orphan-h1") == "cancelled"

    monkeypatch.setattr(turn_runner.requests, "post", real_post)
    assert inner_results == [("full", None)], (
        "a prompt enqueued during the handoff window was accepted — the slot "
        f"was emptied mid-attempt: {inner_results}")
    assert turn_runner.queued_prompt(sid) is None
    assert session_store.get_session(sid)["active_turn_id"] is None
    # the failed start closed its own transcript
    events = session_store.recent_events(sid, 100)
    started = [e for e in events if e["type"] == "turn_started"
               and e["data"].get("text") == "first"]
    assert started and any(
        e["type"] == "error" and e["turn_id"] == started[0]["turn_id"]
        for e in events)
    # and no turn_queue_dropped was emitted — nothing was superseded
    assert not any(e["type"] == "turn_queue_dropped" for e in events)


def test_busy_kick_retries_after_the_session_frees(monkeypatch, turn_stub):
    """Round-1 finding 2's stable-state requirement: a kick that loses the CAS
    leaves the prompt PARKED, and the next kick (here: after the foreign turn
    releases) starts it — no strand."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-h2", 300)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="parked")[0] == "queued"

    turn_runner._kick_queued(sid)  # TurnBusy: must revert to parked, not drop
    parked = turn_runner.queued_prompt(sid)
    assert parked is not None and parked["starting"] is False

    session_store.end_turn(sid, "orphan-h2")
    turn_runner._kick_queued(sid)
    ok = _wait_until(
        lambda: any(e["type"] == "turn_started" and e["data"].get("text") == "parked"
                    for e in session_store.recent_events(sid, 100)))
    assert ok, "the freed session's kick did not start the parked prompt"
    assert turn_runner.queued_prompt(sid) is None


def test_entitlement_revoked_during_wait_drops_the_prompt(monkeypatch):
    """Review round 1, finding 4: the router's entitlement gate re-runs at
    START time. A revocation during the wait yields a durable
    turn_queue_dropped{entitlement_denied} and NO turn_started — the queue
    cannot launder a paid turn past a policy change."""
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")
    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-h3", 300)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="revoked")[0] == "queued"

    monkeypatch.setattr(turn_runner.entitlements, "entitlements_for",
                        lambda tier, *_roles: {"converse": False})
    assert turn_runner.request_cancel("tenant-q", sid, "orphan-h3") == "cancelled"

    events = session_store.recent_events(sid, 100)
    dropped = [e for e in events if e["type"] == "turn_queue_dropped"]
    assert dropped and dropped[0]["data"].get("reason") == "entitlement_denied"
    assert not any(e["type"] == "turn_started" and e["data"].get("text") == "revoked"
                   for e in events)
    assert turn_runner.queued_prompt(sid) is None


def test_kicker_passes_role_snapshot_into_the_promoted_start(monkeypatch):
    """sol-critic round 1, finding 4: the kicker re-checks the queued payload's
    role snapshot but then must also HAND it to start_turn — the promoted turn
    holds only the flattened tenant string, whose live resolution would erase
    a role-only principal's converse authority mid-promotion."""
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")
    sess = _new_session()
    sid = sess["session_id"]

    class _RolePrincipal(str):
        pass
    principal = _RolePrincipal("tenant-q")
    principal.tier = "restricted"
    principal.subject = "auth0|role-only"
    principal.roles = ("converse_granter",)
    principal.elevated = False

    # Entitlements that grant converse ONLY through the role — the tier alone
    # would drop the prompt at kick time, so a green start proves both the
    # payload snapshot and the pass-through.
    monkeypatch.setattr(
        turn_runner.entitlements, "entitlements_for",
        lambda tier, roles=(), elevated=False:
            {"converse": "converse_granter" in tuple(roles)})

    assert session_store.try_begin_turn(sid, "orphan-r1", 300)
    assert turn_runner.try_enqueue_turn(principal, sid, text="role-only")[0] == "queued"

    captured = {}

    def _fake_start(tenant_id, session_id, **kwargs):
        captured["tenant_id"] = tenant_id
        captured.update(kwargs)
        return "turn-fake"

    monkeypatch.setattr(turn_runner, "start_turn", _fake_start)
    session_store.end_turn(sid, "orphan-r1")
    turn_runner._kick_queued(sid)

    ok = _wait_until(lambda: "tenant_id" in captured)
    assert ok, "the kick never promoted the role-only prompt (dropped at re-check?)"
    assert captured["entitlement_roles"] == ("converse_granter",)
    assert captured["entitlement_elevated"] is False
    assert captured["tier"] == "restricted"
    assert turn_runner.queued_prompt(sid) is None


def test_role_only_principal_survives_the_full_promotion_chain(monkeypatch, turn_stub):
    """sol-critic round 2 (PR #414, MINOR): the pass-through test above stubs
    start_turn, so it pins only the kicker handoff. This exercises the REAL
    chain — enqueue while busy → kick → start_turn → _spawn_relay → terminal
    auto-confirm — with nothing stubbed but the entitlement policy (a spy that
    grants converse ONLY through the role AND elevation together, so tier
    alone drops the prompt at the kick re-check and again at the terminal
    gate: losing the roles OR the elevated flag at any checkpoint kills the
    chain there instead of finishing green — elevated must be pinned True
    because False is also every fall-back default, sol-critic #416 round 1).

    The spy also records every (tier, roles, elevated) triple the policy is
    consulted with, which pins the OTHER half of the snapshot: resolving the
    kicker's flattened string falls open to the "demo" tier, so without the
    entitlement_tier pass-through the terminal auto-confirm consulted
    demo-tier entitlements for a restricted-tier principal's promoted turn.
    """
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]

    class _RolePrincipal(str):
        pass
    principal = _RolePrincipal("tenant-q")
    principal.tier = "restricted"
    principal.subject = "auth0|role-only-e2e"
    principal.roles = ("converse_granter",)
    principal.elevated = True

    calls = []

    def _spy(tier, roles=(), elevated=False):
        calls.append((tier, tuple(roles), elevated))
        return {"converse": "converse_granter" in tuple(roles) and elevated is True}

    monkeypatch.setattr(turn_runner.entitlements, "entitlements_for", _spy)
    # auto_approve_reads is what makes the terminal auto-confirm consult the
    # entitlement policy at all — that consultation is checkpoint 4's probe.
    session_policy.set_policy(sid, "tenant-q", "auto_approve_reads")

    snapshot = ("restricted", ("converse_granter",), True)

    # 1. enqueue while busy: the payload snapshots the principal's authority,
    #    and enqueueing consults no policy at all.
    assert session_store.try_begin_turn(sid, "orphan-e2e", 300)
    assert turn_runner.try_enqueue_turn(principal, sid, text="role-only e2e")[0] == "queued"
    with turn_runner._queued_lock:
        payload = dict(turn_runner._queued[sid])
    assert payload["entitlement_tier"] == "restricted"
    assert payload["entitlement_roles"] == ["converse_granter"]
    assert payload["entitlement_elevated"] is True
    assert calls == []

    # 2. kick (orphan cancel path, cancelling AS the principal — the cancel
    #    runs its own follow-ups under the canceller's identity) → REAL
    #    start_turn against the stub harness.
    assert turn_runner.request_cancel(principal, sid, "orphan-e2e") == "cancelled"
    ok = _wait_until(
        lambda: any(e["type"] == "turn_started" and e["data"].get("text") == "role-only e2e"
                    for e in session_store.recent_events(sid, 100)))
    assert ok, (f"role-only prompt never started — its authority was lost "
                f"before the kick re-check; events: {_types(sid)}, calls: {calls}")
    assert not any(e["type"] == "turn_queue_dropped"
                   for e in session_store.recent_events(sid, 100)), (
        "the kick re-check dropped a prompt whose role grants converse")

    # 3. relay: the promoted prompt reached the harness wire and its scripted
    #    turn_complete terminalizes the turn.
    started = [e for e in session_store.recent_events(sid, 100)
               if e["type"] == "turn_started" and e["data"].get("text") == "role-only e2e"]
    turn_id = started[0]["turn_id"]
    assert _wait_until(
        lambda: any(e["type"] == "turn_complete" and e["turn_id"] == turn_id
                    for e in session_store.recent_events(sid, 100))), (
        f"the promoted turn never terminalized; events: {_types(sid)}")
    assert stub.BODIES and "role-only e2e" in json.dumps(stub.BODIES), (
        "the promoted prompt never reached the harness wire")

    # 4. terminal auto-confirm: consulted with the SAME snapshot — tier and
    #    roles both — not a re-resolution of the flattened tenant string
    #    (which would say ("demo", (), False)). Three consultations total:
    #    the cancel's own follow-up pass, the kick re-check, and the promoted
    #    turn's terminal auto-confirm — and EVERY one carries the principal's
    #    full authority. The last is the terminal's, by _finalize_terminal's
    #    documented order (auto-confirm first, then the queue kick).
    assert _wait_until(lambda: len(calls) >= 3), (
        f"the terminal auto-confirm never consulted the entitlement policy: {calls}")
    assert calls[-1] == snapshot, (
        f"the terminal auto-confirm ran against a different identity: {calls[-1]}")
    assert all(c == snapshot for c in calls), (
        f"a checkpoint consulted the policy with degraded authority: {calls}")

    assert _wait_until(lambda: turn_runner.queued_prompt(sid) is None)
    assert _wait_until(
        lambda: session_store.get_session(sid)["active_turn_id"] is None)
    assert sum(1 for t in _types(sid) if t == "turn_started") == 1


def test_direct_turns_sync_rejection_kicks_the_parked_prompt(monkeypatch):
    """Review round 2, finding 1: a DIRECT turn that wins the CAS and then
    rejects synchronously (401/429/conn-refused/no-URL) releases the CAS
    outside the relay's terminal path — without a kick there, a parked prompt
    is stranded on a free session forever. Every synchronous rejection site
    now kicks (_release_cas)."""
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL",
                       f"http://127.0.0.1:{_free_closed_port()}")
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    # Park a prompt (behind an orphan turn), then release that turn RAW —
    # simulating the kicker having stood down on "genuinely busy" while the
    # foreign turn's terminal kick never fires on the synchronous path.
    assert session_store.try_begin_turn(sid, "orphan-d1", 300)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="parked-behind")[0] == "queued"
    session_store.end_turn(sid, "orphan-d1")
    assert turn_runner.queued_prompt(sid) is not None  # stranded state, pre-kick

    with pytest.raises(turn_runner.TurnRejected):
        turn_runner.start_turn("tenant-q", sid, text="direct turn")

    # The direct turn's rejection must have kicked: the parked prompt was
    # attempted (and, harness-down, closed with a terminal error) — not left.
    assert turn_runner.queued_prompt(sid) is None, (
        "a synchronous rejection released the CAS without kicking — the "
        "parked prompt is stranded")
    events = session_store.recent_events(sid, 100)
    started = [e for e in events if e["type"] == "turn_started"
               and e["data"].get("text") == "parked-behind"]
    assert started and any(
        e["type"] == "error" and e["turn_id"] == started[0]["turn_id"]
        for e in events)
    assert session_store.get_session(sid)["active_turn_id"] is None


def test_entitlement_evaluator_crash_neither_raises_nor_leaks_the_claim(monkeypatch):
    """Review round 2, finding 2: an UNEXPECTED evaluator exception (not just
    EntitlementsError) previously escaped _kick_queued with `starting` still
    True. It must fail closed: no raise, slot emptied, durable drop."""
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")
    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-d2", 300)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="crash-eval")[0] == "queued"

    def _boom(tier, *_roles):
        raise RuntimeError("evaluator crashed")
    monkeypatch.setattr(turn_runner.entitlements, "entitlements_for", _boom)

    # must not raise (request_cancel runs the kick on this thread)
    assert turn_runner.request_cancel("tenant-q", sid, "orphan-d2") == "cancelled"

    assert turn_runner.queued_prompt(sid) is None, (
        "the claim leaked: the slot still exists after an evaluator crash")
    events = session_store.recent_events(sid, 100)
    assert any(e["type"] == "turn_queue_dropped"
               and e["data"].get("reason") == "entitlement_denied" for e in events)
    assert not any(e["type"] == "turn_started"
                   and e["data"].get("text") == "crash-eval" for e in events)


def test_promoted_turns_started_event_carries_the_queued_id(monkeypatch, turn_stub):
    """The client reconciles its "Queued" note by IDENTITY: the promoted
    turn's turn_started must carry the queued_id the 202 returned. Text
    matching was race-prone with identical texts (PR #305 round 2)."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-qid", 300)
    status, queued_id = turn_runner.try_enqueue_turn("tenant-q", sid, text="identified")
    assert status == "queued"
    assert turn_runner.request_cancel("tenant-q", sid, "orphan-qid") == "cancelled"

    started = [e for e in session_store.recent_events(sid, 100)
               if e["type"] == "turn_started" and e["data"].get("text") == "identified"]
    assert started, "queued prompt never started"
    assert started[0]["data"].get("queued_id") == queued_id, (
        "the promoted turn_started does not carry the queued_id — the client "
        "cannot reconcile by identity")
    # a DIRECT turn's turn_started must NOT carry the field
    assert _wait_until(lambda: session_store.get_session(sid)["active_turn_id"] is None)
    turn_runner.start_turn("tenant-q", sid, text="direct")
    direct = [e for e in session_store.recent_events(sid, 100)
              if e["type"] == "turn_started" and e["data"].get("text") == "direct"]
    assert direct and "queued_id" not in direct[0]["data"]


def test_promoted_queue_preserves_the_authenticated_subject(monkeypatch):
    """A queued author request must still bind to the user who submitted it."""
    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-subject", 300)
    tenant = deps.TenantContext(
        "tenant-q", tier="hosted_pro", subject="auth0|queued-author",
    )
    status, queued_id = turn_runner.try_enqueue_turn(
        tenant, sid, text="author after the active turn",
    )
    assert status == "queued"
    assert "subject" not in turn_runner.queued_prompt(sid)
    captured = {}

    def capture_start(tenant_id, session_id, **kwargs):
        captured.update(
            tenant_id=tenant_id,
            session_id=session_id,
            subject=kwargs.get("subject"),
            tier=kwargs.get("tier"),
            queued_id=kwargs.get("queued_id"),
        )
        return "queued-turn"

    monkeypatch.setattr(turn_runner, "start_turn", capture_start)
    assert turn_runner.request_cancel(
        "tenant-q", sid, "orphan-subject",
    ) == "cancelled"

    assert captured == {
        "tenant_id": "tenant-q",
        "session_id": sid,
        "subject": "auth0|queued-author",
        "tier": "hosted_pro",
        "queued_id": queued_id,
    }


# =========================================================================== #
# route level
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


def _route_session(client, tenant: str) -> str:
    _counter[0] += 1
    r = client.post("/api/sessions", json={"drawing_id": f"dwg-rq-{_counter[0]}"},
                    headers=_h(tenant))
    assert r.status_code < 300, r.text
    return r.json()["session_id"]


def test_route_busy_queue_true_parks_then_second_bounces(client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")
    sid = _route_session(client, "tenant-rq")
    assert session_store.try_begin_turn(sid, "orphan-rq-1", 300)
    try:
        # (f) parked
        r = client.post(f"/api/sessions/{sid}/messages",
                        json={"text": "park me", "queue": True}, headers=_h("tenant-rq"))
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "queued" and r.json()["queued_id"]
        assert "turn_queued" in _types(sid)
        # (g) second refused with the ordinary busy envelope
        r2 = client.post(f"/api/sessions/{sid}/messages",
                         json={"text": "again", "queue": True}, headers=_h("tenant-rq"))
        assert r2.status_code == 409, r2.text
        assert r2.json()["error"]["error_code"] == ErrorCode.TURN_IN_PROGRESS
        assert r2.json()["error"]["retryable"] is True
        # (h) opt-in pin: no queue flag -> plain busy 409, nothing parked extra
        r3 = client.post(f"/api/sessions/{sid}/messages",
                         json={"text": "no flag"}, headers=_h("tenant-rq"))
        assert r3.status_code == 409
        assert r3.json()["error"]["error_code"] == ErrorCode.TURN_IN_PROGRESS
    finally:
        with turn_runner._queued_lock:
            turn_runner._queued.pop(sid, None)
        session_store.end_turn(sid, "orphan-rq-1")


def test_route_queue_with_confirm_or_grant_is_400_before_consume(client):
    sid = _route_session(client, "tenant-rq2")
    # queue + confirm -> 400, and the approval was never consumed
    _counter[0] += 1
    cid = f"confirm-q-{_counter[0]}"
    sess = session_store.get_session(sid)
    session_store.create_approval(
        cid, sid, "tenant-rq2", turn_id="t-x", tool="write_home_run",
        params={"length_ft": 1}, capability="drawing.write", rationale="r",
        kind="proposed_run", payload={"dwg": sess["drawing_id"]}, ttl_s=300)
    session_store.decide_approval(cid, True)

    r = client.post(f"/api/sessions/{sid}/messages",
                    json={"confirm": {"confirmationId": cid, "approved": True},
                          "queue": True},
                    headers=_h("tenant-rq2"))
    assert r.status_code == 400, r.text
    assert "cannot be queued" in r.json()["error"]["message"]
    # the 400 fired BEFORE the consume: the approval is still consumable
    consumed = session_store.consume_approval(cid, sid, "tenant-rq2")
    assert consumed["approved"] is True

    r2 = client.post(f"/api/sessions/{sid}/messages",
                     json={"text": "hi", "queue": True,
                           "credential_grant": {"kind": "api_key",
                                                "api_key": "k" * 30}},
                     headers=_h("tenant-rq2"))
    assert r2.status_code == 400, r2.text
    assert "cannot be queued" in r2.json()["error"]["message"]
