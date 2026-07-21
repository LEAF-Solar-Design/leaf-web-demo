"""
End-to-end acceptance for the sessions wire spec (gap audit §2.1 / CONTRACT-
ADDENDUM sessions addendum) — the E2E lane. Drives the REAL FastAPI app
(server/app.py, with routers.sessions + routers.agent registered by this same
lane's app.py edit) through a TestClient, backed by a REAL compiled harness
subprocess (harness/dist/scripts/serve.js) running with LEAF_AGENT_MOCK=1's
scripted FakeTurnRunner.

Zero LLM credit, zero contact with the live stack:
  * the harness subprocess is booted on an EPHEMERAL port with its own random
    LEAF_HARNESS_SECRET — never the real :8150.
  * LEAF_AGENT_MOCK=1 means the harness never loads the Agent SDK / touches
    Anthropic — FakeTurnRunner (harness/src/ports/fakes/fakeTurnRunner.ts) is a
    scripted, deterministic stand-in.
  * SESSIONS_DB is redirected to a fresh tmp path before `app` (and therefore
    `session_store`) is ever imported, so the real server/sessions.db is never
    touched.
  * the app under test is exercised purely in-process via FastAPI's
    TestClient (ASGI) — no socket is ever opened on :8130/:8140.

`npm run build` runs ONCE, as a subprocess, at module setup (the sanctioned
exception to the "don't run the harness build yourself" rule — this lane owns
integration). The compiled `node dist/scripts/serve.js` is then booted as a
long-lived subprocess for the whole module and torn down at the end (or
earlier, by test_harness_down_returns_502_broker_unreachable, which is why
that test is deliberately the LAST one defined below and teardown itself is
idempotent about an already-dead process).

Run:  cd server && python -m pytest tests/test_sessions_e2e.py -q
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
HARNESS_DIR = REPO_ROOT / "harness"

# --------------------------------------------------------------------------- #
# env setup — BEFORE `app` (and therefore `session_store`, which reads
# SESSIONS_DB once at import time — see session_store.py's own docstring) is
# ever imported. Same posture as tests/test_sessions_routes.py /
# tests/test_agent_approvals.py, just applied to the whole app this time.
# --------------------------------------------------------------------------- #
_TMP_DIR = Path(tempfile.mkdtemp(prefix="sessions-e2e-test-"))
_TMP_DB = _TMP_DIR / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ["LEAF_AUTH_LIVE"] = "0"  # legacy X-Tenant-Id header stub
os.environ.setdefault("TURN_MAX_S", "30")
os.environ.setdefault("SESSIONS_APPROVAL_TTL_S", "600")

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

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


def _free_port() -> int:
    """Bind :0 to let the OS pick a free ephemeral port, then release it. A
    (tiny, standard) TOCTOU race exists between this and the harness actually
    binding it, but is acceptable for a hermetic test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


HARNESS_SECRET = secrets.token_hex(16)
HARNESS_PORT = _free_port()
HARNESS_URL = f"http://127.0.0.1:{HARNESS_PORT}"

# turn_runner reads these at CALL time, not import time (see turn_runner.py's
# own docstring), so it is safe to set them now even though the harness
# process itself only boots later, inside the `harness` fixture.
os.environ["LEAF_AUTHOR_HARNESS_URL"] = HARNESS_URL
os.environ["LEAF_HARNESS_SECRET"] = HARNESS_SECRET


# --------------------------------------------------------------------------- #
# harness build + boot (module-scoped, once)
# --------------------------------------------------------------------------- #
def _run_npm_build() -> subprocess.CompletedProcess:
    """`npm run build` in harness/. Windows' `npm` is a .cmd shim (not a raw
    PE executable) — shell=True is the standard, reliable way to invoke it
    from Python's subprocess without hunting for the resolved npm.cmd path."""
    if os.name == "nt":
        return subprocess.run(
            "npm run build", cwd=str(HARNESS_DIR), shell=True,
            capture_output=True, text=True, timeout=240,
        )
    return subprocess.run(
        ["npm", "run", "build"], cwd=str(HARNESS_DIR),
        capture_output=True, text=True, timeout=240,
    )


def _wait_health(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/health", timeout=2)
            if r.status_code == 200:
                return
        except requests.RequestException as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.2)
    raise RuntimeError(f"harness never became healthy at {url}/health: {last_err}")


def _kill_harness(handle: Dict[str, Any]) -> None:
    """Idempotent: a no-op if the process is already dead (harness-down test
    calls this explicitly; the module teardown then finds `alive=False`)."""
    if not handle["alive"]:
        return
    proc: subprocess.Popen = handle["proc"]
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    handle["alive"] = False


@pytest.fixture(scope="module")
def harness():
    build = _run_npm_build()
    if build.returncode != 0:
        pytest.fail(
            f"npm run build failed (rc={build.returncode}) in {HARNESS_DIR}:\n"
            f"STDOUT:\n{build.stdout}\nSTDERR:\n{build.stderr}"
        )

    dist_entry = HARNESS_DIR / "dist" / "scripts" / "serve.js"
    if not dist_entry.exists():
        pytest.fail(f"npm run build succeeded but {dist_entry} is missing")

    env = dict(os.environ)
    env.update({
        "HARNESS_PORT": str(HARNESS_PORT),
        "LEAF_AGENT_MOCK": "1",         # scripted FakeTurnRunner — no SDK, no network, no credit
        "LEAF_HARNESS_AUTH": "1",       # shared-secret gate ON
        "LEAF_HARNESS_SECRET": HARNESS_SECRET,
    })
    proc = subprocess.Popen(
        ["node", "dist/scripts/serve.js"], cwd=str(HARNESS_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    log_lines: List[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            log_lines.append(line)

    threading.Thread(target=_drain, daemon=True).start()

    try:
        _wait_health(HARNESS_URL, timeout_s=30)
    except Exception:  # noqa: BLE001
        proc.kill()
        pytest.fail(f"harness subprocess never became healthy. Log:\n{''.join(log_lines[-200:])}")

    handle: Dict[str, Any] = {"proc": proc, "log": log_lines, "alive": True}
    yield handle
    _kill_harness(handle)


@pytest.fixture(scope="module")
def client(harness):  # noqa: ARG001  (dependency enforces build+boot happens first)
    """The REAL app (server/app.py) — sessions.router + agent.router included
    by this lane's 2-line app.py edit — wrapped in a TestClient. Imported only
    now, after SESSIONS_DB/LEAF_AUTHOR_HARNESS_URL/LEAF_HARNESS_SECRET are all
    set and the harness is confirmed healthy."""
    import app as app_module  # noqa: PLC0415
    from fastapi.testclient import TestClient

    return TestClient(app_module.app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _h(tenant: str) -> Dict[str, str]:
    return {"X-Tenant-Id": tenant}


def _create_session(client, tenant: str, drawing_id: str) -> Dict[str, Any]:
    r = client.post("/api/sessions", json={"drawing_id": drawing_id}, headers=_h(tenant))
    assert r.status_code == 200, r.text
    return r.json()


def _transcript(client, session_id: str, tenant: str, limit: int = 500) -> List[Dict[str, Any]]:
    r = client.get(f"/api/sessions/{session_id}/transcript?limit={limit}", headers=_h(tenant))
    assert r.status_code == 200, r.text
    return r.json()["events"]


def _poll_transcript(
    client, session_id: str, tenant: str, predicate: Callable[[List[Dict[str, Any]]], bool],
    timeout_s: float = 15.0, poll_s: float = 0.05,
) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout_s
    last: List[Dict[str, Any]] = []
    while time.time() < deadline:
        last = _transcript(client, session_id, tenant)
        if predicate(last):
            return last
        time.sleep(poll_s)
    raise AssertionError(f"transcript predicate not satisfied within {timeout_s}s; last={last}")


def _wait_turn_complete(client, session_id: str, tenant: str, turn_id: str,
                        timeout_s: float = 15.0) -> List[Dict[str, Any]]:
    return _poll_transcript(
        client, session_id, tenant,
        lambda evs: any(e["turn_id"] == turn_id and e["type"] == "turn_complete" for e in evs),
        timeout_s=timeout_s,
    )


# =========================================================================== #
# 0. isolation sanity (mirrors test_sessions_routes.py's own guard)
# =========================================================================== #
def test_sessions_db_isolated_to_tmp_path():
    import session_store

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


# =========================================================================== #
# 1. session idempotency
# =========================================================================== #
def test_session_create_idempotent_and_tenant_scoped(client):
    r1 = _create_session(client, "tenant-e2e-a", "e2e-dwg-idem")
    r2 = _create_session(client, "tenant-e2e-a", "e2e-dwg-idem")
    assert r1["session_id"] == r2["session_id"]

    r3 = _create_session(client, "tenant-e2e-b", "e2e-dwg-idem")
    assert r3["session_id"] != r1["session_id"]


# =========================================================================== #
# 2. text turn -> turn_complete, seq gapless
# =========================================================================== #
def test_text_turn_completes_with_gapless_seq(client):
    sess = _create_session(client, "tenant-e2e-text", "e2e-dwg-text")
    sid = sess["session_id"]

    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "hello there"},
                    headers=_h("tenant-e2e-text"))
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]
    assert r.json()["status"] == "started"

    events = _wait_turn_complete(client, sid, "tenant-e2e-text", turn_id)
    turn_events = [e for e in events if e["turn_id"] == turn_id]
    types = [e["type"] for e in turn_events]
    assert types[0] == "turn_started"
    assert types[-1] == "turn_complete"
    assert "text_delta" in types
    assert turn_events[-1]["data"]["stop_reason"] == "end_turn"

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq not gapless: {seqs}"


# =========================================================================== #
# 3. after_seq replay exact via transcript + SSE (events_after)
# =========================================================================== #
def test_after_seq_replay_matches_transcript_tail(client, monkeypatch):
    # Keep the stream loop's own cadence + deadline small (matches
    # tests/test_sessions_routes.py's precedent) — otherwise the TestClient's
    # early `break`-and-close only stops the generator once its own internal
    # STREAM_DEADLINE_S (300s default) elapses: a sync generator run via
    # Starlette's threadpool iteration can't be force-cancelled mid-sleep, so
    # an early client disconnect alone doesn't bound the wall-clock cost.
    from routers import sessions as sessions_router

    monkeypatch.setattr(sessions_router, "STREAM_POLL_S", 0.01)
    monkeypatch.setattr(sessions_router, "STREAM_PING_S", 60.0)
    monkeypatch.setattr(sessions_router, "STREAM_DEADLINE_S", 5.0)

    tenant = "tenant-e2e-replay"
    sess = _create_session(client, tenant, "e2e-dwg-replay")
    sid = sess["session_id"]

    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "just checking in"}, headers=_h(tenant))
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]

    events = _wait_turn_complete(client, sid, tenant, turn_id)
    assert len(events) >= 3, events
    cursor = events[1]["seq"]
    expected_tail = [e for e in events if e["seq"] > cursor]
    assert expected_tail  # sanity: cursor didn't swallow everything

    seen: List[Dict[str, Any]] = []
    with client.stream("GET", f"/api/sessions/{sid}/stream?after_seq={cursor}",
                       headers=_h(tenant)) as resp:
        assert resp.status_code == 200
        pending_type: Optional[str] = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                pending_type = line[len("event: "):]
            elif line.startswith("data: "):
                env = json.loads(line[len("data: "):])
                seen.append(env)
                assert pending_type == env["type"]
                if len(seen) >= len(expected_tail):
                    break

    assert [e["seq"] for e in seen] == [e["seq"] for e in expected_tail]
    assert [e["type"] for e in seen] == [e["type"] for e in expected_tail]
    assert all(e["seq"] > cursor for e in seen)


# =========================================================================== #
# 4. approval round trip: approve -> proposed_run/confirmation_required ->
#    POST /api/agent/approvals -> confirmation_resolved -> messages{confirm}
#    -> resumed turn completes
# =========================================================================== #
def test_approval_round_trip(client):
    tenant = "tenant-e2e-approve"
    sess = _create_session(client, tenant, "e2e-dwg-approve")
    sid = sess["session_id"]

    r1 = client.post(f"/api/sessions/{sid}/messages", json={"text": "please approve this run"},
                     headers=_h(tenant))
    assert r1.status_code == 202, r1.text
    turn1 = r1.json()["turn_id"]

    events = _wait_turn_complete(client, sid, tenant, turn1)
    turn1_events = [e for e in events if e["turn_id"] == turn1]
    assert turn1_events[-1]["type"] == "turn_complete"
    assert turn1_events[-1]["data"]["stop_reason"] == "awaiting_approval"

    proposed = [e for e in turn1_events if e["type"] == "proposed_run"]
    required = [e for e in turn1_events if e["type"] == "confirmation_required"]
    assert len(proposed) == 1 and len(required) == 1
    confirmation_id = required[0]["data"]["confirmation_id"]
    assert confirmation_id and proposed[0]["data"]["confirmation_id"] == confirmation_id

    # POST /api/agent/approvals/{confirmation_id} -> 200 {resolved, approved}, and a
    # confirmation_resolved event lands in the session log.
    ap = client.post(f"/api/agent/approvals/{confirmation_id}", json={"approved": True},
                     headers=_h(tenant))
    assert ap.status_code == 200, ap.text
    assert ap.json()["resolved"] is True
    assert ap.json()["approved"] is True

    resolved_events = [e for e in _transcript(client, sid, tenant) if e["type"] == "confirmation_resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0]["data"] == {"confirmation_id": confirmation_id, "approved": True, "by": tenant}

    # a second decision on the same confirmation_id -> 409
    ap2 = client.post(f"/api/agent/approvals/{confirmation_id}", json={"approved": False},
                      headers=_h(tenant))
    assert ap2.status_code == 409, ap2.text
    assert ap2.json()["error"]["error_code"] == "BAD_PARAMS"

    # messages{confirm} resumes the halted turn as a NEW turn_id
    r2 = client.post(
        f"/api/sessions/{sid}/messages",
        json={"confirm": {"confirmationId": confirmation_id, "approved": True}},
        headers=_h(tenant),
    )
    assert r2.status_code == 202, r2.text
    turn2 = r2.json()["turn_id"]
    assert turn2 != turn1

    events2 = _wait_turn_complete(client, sid, tenant, turn2)
    turn2_events = [e for e in events2 if e["turn_id"] == turn2]
    types2 = [e["type"] for e in turn2_events]
    assert types2[0] == "turn_started"
    assert "tool_result" in types2
    assert types2[-1] == "turn_complete"
    assert turn2_events[-1]["data"]["stop_reason"] == "end_turn"


# =========================================================================== #
# 5. busy 409 while a turn is in flight
# =========================================================================== #
def test_messages_busy_409_while_turn_in_flight(client):
    tenant = "tenant-e2e-busy"
    sess = _create_session(client, tenant, "e2e-dwg-busy")
    sid = sess["session_id"]

    # "count" text drives the longer scripted flow (8 events @ ~10ms apart) so
    # the race window for the second POST to observe the still-active CAS is
    # comfortably wide.
    r1 = client.post(f"/api/sessions/{sid}/messages", json={"text": "count entities please"},
                     headers=_h(tenant))
    assert r1.status_code == 202, r1.text
    turn1 = r1.json()["turn_id"]

    r2 = client.post(f"/api/sessions/{sid}/messages", json={"text": "a second message"},
                     headers=_h(tenant))
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["error_code"] == "turn_in_progress"

    _wait_turn_complete(client, sid, tenant, turn1)  # drain so nothing bleeds into later tests


# =========================================================================== #
# 6. entitlement 403 (monkeypatched entitlements.resolve_tier)
# =========================================================================== #
def test_messages_entitlement_denied_403(client, monkeypatch):
    import entitlements

    tenant = "tenant-e2e-restricted"
    sess = _create_session(client, tenant, "e2e-dwg-restricted")
    sid = sess["session_id"]

    monkeypatch.setattr(entitlements, "resolve_tier", lambda t: "restricted")
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h(tenant))
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "converse"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"


# =========================================================================== #
# 7. harness-down -> TurnRejected 502 BROKER_UNREACHABLE
#
# MUST be the LAST test in this file — it kills the module-scoped harness
# subprocess. Depends on `harness` (not just `client`) so it can reach into
# the handle and terminate the process directly.
# =========================================================================== #
def test_harness_down_returns_502_broker_unreachable(client, harness):
    tenant = "tenant-e2e-down"
    sess = _create_session(client, tenant, "e2e-dwg-down")
    sid = sess["session_id"]

    _kill_harness(harness)

    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "hello"}, headers=_h(tenant))
    assert r.status_code == 502, r.text
    assert r.json()["error"]["error_code"] == "BROKER_UNREACHABLE"


def test_zzz_real_sessions_db_still_absent():
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )
