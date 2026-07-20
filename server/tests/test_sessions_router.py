"""
Binary acceptance for server/routers/sessions.py (agent spine §18 app surface).

Pins the wire-contract §2 behaviours against a MONKEYPATCHED harness HTTP layer
(no network, no real harness):

  * POST /api/sessions requires the `converse` entitlement (403 shape) and is an
    idempotent create/attach passthrough ({tenantId, drawingId} forwarded);
  * POST /api/sessions/{id}/messages: 202 happy path with an assembled
    ContextPacket; exactly-one-of text/confirm; and the outcome mappings
    409 -> turn_in_progress · 401 -> GRANT_REQUIRED · 429 short -> llm_rate_limited ·
    429 long -> llm_quota_exhausted (degraded) · connection refused ->
    BROKER_UNREACHABLE 502 (degraded);
  * cross-tenant access -> 404 session_not_found (no existence oracle);
  * SSE relay passes bytes + after_seq through.

Run:  cd server && python -m pytest tests/test_sessions_router.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path (mirrors wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

import jsonschema  # noqa: E402
import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs`/`app` import anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="sess-jobs-")) / "jobs.db"))

ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))

import entitlements  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402

HARNESS = "http://harness.test"


def _client():
    from fastapi.testclient import TestClient

    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# --------------------------------------------------------------------------- #
# fake harness HTTP layer (monkeypatches requests.request — no network)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code: int, body: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None,
                 chunks: Optional[List[bytes]] = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        return None


class FakeHarness:
    """Programmable stand-in for requests.request. Records every forwarded call."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.responder = None  # (method, url, kwargs) -> FakeResponse | raise

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.responder is not None:
            return self.responder(method, url, kwargs)
        # default: idempotent create + accepted message
        if method == "POST" and url.endswith("/converse/sessions"):
            drawing = kwargs["json"]["drawingId"]
            return FakeResponse(200, {"sessionId": f"sess-{drawing}", "status": "idle",
                                      "createdAt": "2026-07-20T00:00:00+00:00"})
        if method == "POST" and "/messages" in url:
            return FakeResponse(202, {"turnId": "turn-1"})
        return FakeResponse(404, {"error": "session_not_found"})


@pytest.fixture
def fake_harness(monkeypatch, tmp_path):
    """Hermetic env: converse harness URL set, requests.request faked, throwaway
    drawing store, no grant-store env (grant read degrades without network)."""
    fh = FakeHarness()
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", HARNESS)
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr("requests.request", fh)
    monkeypatch.setattr(sessions_router, "_SESSIONS", {})
    yield fh


def _create(c, tenant="t-alpha", drawing="demo"):
    r = c.post("/api/sessions", json={"drawing_id": drawing}, headers=_h(tenant))
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# --------------------------------------------------------------------------- #
# create: entitlement gate + idempotent passthrough
# --------------------------------------------------------------------------- #
def test_create_denied_for_restricted_tier(fake_harness, monkeypatch):
    """The restricted tier lacks `converse` (§9 policy) -> 403 entitlement shape,
    and the harness is never contacted."""
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "restricted")
    c = _client()
    r = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-restricted"))
    assert r.status_code == 403, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["entitlement_required"] is True
    assert b["required"] == "converse" and b["tier"] == "restricted"
    assert b["error"]["error_code"] == "ENTITLEMENT_REQUIRED"
    assert fake_harness.calls == []  # gate fires BEFORE any harness traffic


def test_create_idempotent_passthrough(fake_harness):
    c = _client()
    r1 = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-alpha"))
    r2 = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-alpha"))
    assert r1.status_code == 200 and r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    jsonschema.validate(b1, ENVELOPE_SCHEMA)
    assert b1["session_id"] == b2["session_id"] == "sess-demo"  # harness keys (tenant, drawing)
    assert b1["status"] == "idle" and isinstance(b1["created_at"], str)
    # forwarded body per wire §1: {tenantId, drawingId} to POST /converse/sessions
    call = fake_harness.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{HARNESS}/converse/sessions"
    assert call["json"] == {"tenantId": "t-alpha", "drawingId": "demo"}


def test_create_unconfigured_harness_is_502_degraded(fake_harness, monkeypatch):
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    c = _client()
    r = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-alpha"))
    assert r.status_code == 502
    b = r.json()
    assert b["error"]["error_code"] == "BROKER_UNREACHABLE" and b["degraded_mode"] is True


def test_list_sessions_own_tenant_only(fake_harness):
    c = _client()
    _create(c, tenant="t-alpha", drawing="demo")
    _create(c, tenant="t-beta", drawing="other")
    r = c.get("/api/sessions", headers=_h("t-alpha"))
    assert r.status_code == 200
    rows = r.json()["sessions"]
    assert [s["session_id"] for s in rows] == ["sess-demo"]
    assert rows[0]["drawing_id"] == "demo"


# --------------------------------------------------------------------------- #
# messages: happy path + validation
# --------------------------------------------------------------------------- #
def test_message_202_happy_path_with_context_packet(fake_harness):
    c = _client()
    sid = _create(c)
    hint = {"lane": "run", "tool": "count-by-layer", "confidence": 0.62, "rationale": "match"}
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"text": "how many panels?", "classifier_hint": hint},
               headers=_h("t-alpha"))
    assert r.status_code == 202, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["turn_id"] == "turn-1" and b["status"] == "started"

    fwd = fake_harness.calls[-1]
    assert fwd["url"] == f"{HARNESS}/converse/sessions/{sid}/messages"
    payload = fwd["json"]
    assert payload["tenantId"] == "t-alpha"
    assert payload["text"] == "how many panels?"
    assert payload["classifierHint"] == hint
    packet = payload["contextPacket"]  # assembled §4 ContextPacket rides along
    for key in ("catalog", "catalog_hash", "drawing", "versions", "checkout",
                "entitlements", "active_jobs", "grant", "classifier_hint"):
        assert key in packet, f"ContextPacket missing {key!r}"
    assert packet["classifier_hint"] == hint
    assert len(json.dumps(packet, separators=(",", ":"))) < 8000


def test_message_confirm_variant_forwarded(fake_harness):
    c = _client()
    sid = _create(c)
    confirm = {"confirmationId": "conf-9", "approved": True}
    r = c.post(f"/api/sessions/{sid}/messages", json={"confirm": confirm},
               headers=_h("t-alpha"))
    assert r.status_code == 202, r.text
    payload = fake_harness.calls[-1]["json"]
    assert payload["confirm"] == confirm and "text" not in payload


def test_message_requires_exactly_one_of_text_confirm(fake_harness):
    c = _client()
    sid = _create(c)
    n_calls = len(fake_harness.calls)
    both = c.post(f"/api/sessions/{sid}/messages",
                  json={"text": "hi", "confirm": {"confirmationId": "x", "approved": True}},
                  headers=_h("t-alpha"))
    neither = c.post(f"/api/sessions/{sid}/messages", json={}, headers=_h("t-alpha"))
    assert both.status_code == 400 and neither.status_code == 400
    assert both.json()["error"]["error_code"] == "BAD_PARAMS"
    assert len(fake_harness.calls) == n_calls  # nothing forwarded


# --------------------------------------------------------------------------- #
# messages: harness outcome mappings
# --------------------------------------------------------------------------- #
def _respond_with(fake_harness, factory):
    """Route ONLY the /messages POST through `factory`; defaults elsewhere."""
    default = FakeHarness()

    def responder(method, url, kwargs):
        if method == "POST" and "/messages" in url:
            return factory()
        return default(method, url)
    fake_harness.responder = responder


def test_message_409_maps_turn_in_progress(fake_harness):
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness,
                  lambda: FakeResponse(409, {"error": "turn_in_progress", "turnId": "turn-busy"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 409, r.text
    b = r.json()
    # NOTE: envelope_schema.json's error_code enum predates the §18 codes (see
    # final-report conflict note) — shape-assert manually here.
    assert set(b["error"]) == {"error_code", "message", "retryable"}
    assert b["error"]["error_code"] == "turn_in_progress"
    assert b["error"]["retryable"] is True
    assert b["turn_id"] == "turn-busy"  # additive: the blocking turn


def test_message_401_maps_grant_required(fake_harness):
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness, lambda: FakeResponse(401, {"error": "grant_required"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 401, r.text
    b = r.json()
    assert b["error"]["error_code"] == "GRANT_REQUIRED"
    assert b["grant_required"] is True


def test_message_429_short_horizon_maps_rate_limited(fake_harness):
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness,
                  lambda: FakeResponse(429, {"error": "rate_limited"}, headers={"Retry-After": "30"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 429, r.text
    b = r.json()
    assert set(b["error"]) == {"error_code", "message", "retryable"}
    assert b["error"]["error_code"] == "llm_rate_limited"
    assert b["error"]["retryable"] is True
    assert b["degraded_mode"] is False  # short horizon: retry through it


def test_message_429_long_horizon_maps_quota_exhausted(fake_harness):
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness,
                  lambda: FakeResponse(429, {"error": "rate_limited"}, headers={"Retry-After": "3600"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 429, r.text
    b = r.json()
    assert b["error"]["error_code"] == "llm_quota_exhausted"
    assert b["error"]["retryable"] is True
    assert b["degraded_mode"] is True  # conversational lane down; §12 floor is the product


def test_message_429_exact_horizon_boundary_is_rate_limited(fake_harness):
    """The quota horizon is `horizon > QUOTA_HORIZON_S` (300.0) — EXACTLY 300
    stays llm_rate_limited (a `>=` drift must fail here), 301 tips over to
    llm_quota_exhausted. Pins the same 300/301 split the harness
    classifyRateLimit uses, so the two layers cannot silently disagree."""
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness,
                  lambda: FakeResponse(429, {"error": "rate_limited"}, headers={"Retry-After": "300"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 429, r.text
    b = r.json()
    assert b["error"]["error_code"] == "llm_rate_limited"
    assert b["degraded_mode"] is False

    _respond_with(fake_harness,
                  lambda: FakeResponse(429, {"error": "rate_limited"}, headers={"Retry-After": "301"}))
    r2 = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r2.status_code == 429, r2.text
    b2 = r2.json()
    assert b2["error"]["error_code"] == "llm_quota_exhausted"
    assert b2["degraded_mode"] is True


def test_message_429_non_numeric_retry_after_defaults_short(fake_harness):
    """An HTTP-date Retry-After is unparseable as seconds — parse failure
    defaults to the SHORT horizon (rate-limited), never quota-exhausted."""
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness,
                  lambda: FakeResponse(429, {"error": "rate_limited"},
                                       headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 429, r.text
    b = r.json()
    assert b["error"]["error_code"] == "llm_rate_limited"
    assert b["degraded_mode"] is False


def test_message_429_no_horizon_defaults_to_rate_limited(fake_harness):
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness, lambda: FakeResponse(429, {"error": "rate_limited"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.json()["error"]["error_code"] == "llm_rate_limited"


def test_message_connection_refused_maps_broker_unreachable(fake_harness):
    c = _client()
    sid = _create(c)

    def _refuse():
        import requests
        raise requests.ConnectionError("connection refused")
    _respond_with(fake_harness, _refuse)
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 502, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert b["error"]["retryable"] is True
    assert b["degraded_mode"] is True


def test_message_harness_400_maps_bad_params_not_outage(fake_harness):
    """A harness 400 (BadMessageError) is the CALLER's problem — it must map to
    BAD_PARAMS with the harness detail, never 502 BROKER_UNREACHABLE with the
    degraded banner."""
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness,
                  lambda: FakeResponse(400, {"error": "bad_message",
                                             "message": "malformed confirm payload"}))
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"confirm": {"confirmationId": "x", "approved": True}},
               headers=_h("t-alpha"))
    assert r.status_code == 400, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["error"]["error_code"] == "BAD_PARAMS"
    assert "malformed confirm payload" in b["error"]["message"]
    assert b["degraded_mode"] is False


def test_message_harness_410_maps_confirmation_expired(fake_harness):
    """A harness 410 (ConfirmationInvalidError) means the approval chip is
    stale — actionable BAD_PARAMS ('re-propose'), not a fake outage."""
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness, lambda: FakeResponse(410, {"error": "confirmation_invalid"}))
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"confirm": {"confirmationId": "stale", "approved": True}},
               headers=_h("t-alpha"))
    assert r.status_code == 410, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["error"]["error_code"] == "BAD_PARAMS"
    assert "expired" in b["error"]["message"]
    assert b["degraded_mode"] is False


def test_message_oversized_classifier_hint_is_400(fake_harness):
    """classifier_hint is client-supplied and otherwise unbounded — an oversized
    dict is rejected app-side (BAD_PARAMS) before any packet assembly, so it can
    never blow the ContextPacket budget on its own."""
    c = _client()
    sid = _create(c)
    n_calls = len(fake_harness.calls)
    hint = {"lane": "run", "rationale": "y" * 4000}
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"text": "hi", "classifier_hint": hint}, headers=_h("t-alpha"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert len(fake_harness.calls) == n_calls  # nothing forwarded


def test_message_harness_404_maps_session_not_found(fake_harness):
    c = _client()
    sid = _create(c)
    _respond_with(fake_harness, lambda: FakeResponse(404, {"error": "session_not_found"}))
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-alpha"))
    assert r.status_code == 404
    assert r.json()["error"]["error_code"] == "session_not_found"


def test_cross_tenant_message_is_404_no_oracle(fake_harness):
    """Tenant B posting to tenant A's session gets the SAME 404 an unknown
    session would — never a 403 that confirms existence."""
    c = _client()
    sid = _create(c, tenant="t-alpha")
    n_calls = len(fake_harness.calls)
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=_h("t-beta"))
    assert r.status_code == 404
    assert r.json()["error"]["error_code"] == "session_not_found"
    assert len(fake_harness.calls) == n_calls  # short-circuited app-side


# --------------------------------------------------------------------------- #
# stream / transcript / archive passthrough
# --------------------------------------------------------------------------- #
def test_stream_relays_bytes_and_passes_after_seq(fake_harness):
    c = _client()
    sid = _create(c)
    chunks = [b'data: {"seq":7,"type":"turn_started"}\n\n',
              b'data: {"seq":8,"type":"turn_complete"}\n\n']

    def responder(method, url, kwargs):
        assert method == "GET" and url.endswith(f"/converse/sessions/{sid}/stream")
        assert kwargs["params"] == {"afterSeq": 6, "tenantId": "t-alpha"}
        return FakeResponse(200, chunks=chunks)
    fake_harness.responder = responder

    r = c.get(f"/api/sessions/{sid}/stream?after_seq=6", headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == b"".join(chunks)  # verbatim byte relay


def test_cross_tenant_stream_transcript_archive_short_circuit_app_side(fake_harness):
    """The ownership 404s on stream/transcript/archive must fire APP-SIDE:
    same session_not_found shape as /messages, and NOTHING forwarded to the
    harness (deleting the short-circuits would leak the request + tenant
    header upstream while the FakeHarness default 404 masked it)."""
    c = _client()
    sid = _create(c, tenant="t-alpha")
    n = len(fake_harness.calls)
    r_stream = c.get(f"/api/sessions/{sid}/stream", headers=_h("t-beta"))
    r_transcript = c.get(f"/api/sessions/{sid}/transcript", headers=_h("t-beta"))
    r_archive = c.request("DELETE", f"/api/sessions/{sid}", headers=_h("t-beta"))
    for r in (r_stream, r_transcript, r_archive):
        assert r.status_code == 404, r.text
        assert r.json()["error"]["error_code"] == "session_not_found"
    assert len(fake_harness.calls) == n  # short-circuited app-side, zero forwarded


def test_stream_meters_completed_turns_into_agent_ledger(fake_harness, monkeypatch, tmp_path):
    """The relay's metering observer appends ONE agent_ledger line per completed
    turn (turn_usage joined with turn_complete) — and a replayed reconnect of
    the same events does not double-count."""
    ledger = tmp_path / "agent_ledger.jsonl"
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(ledger))
    monkeypatch.setattr(sessions_router, "_METERED_TURNS", set())
    c = _client()
    sid = _create(c)
    events = [
        {"v": 1, "session_id": sid, "turn_id": "turn-9", "seq": 1,
         "type": "turn_started", "data": {"model": "m"}},
        {"v": 1, "session_id": sid, "turn_id": "turn-9", "seq": 2,
         "type": "turn_usage",
         "data": {"turns": 1, "input_tokens": 10, "output_tokens": 5,
                  "cost_tokens": 1500, "total_cost_usd": 0.03}},
        {"v": 1, "session_id": sid, "turn_id": "turn-9", "seq": 3,
         "type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]
    chunks = [f"data: {json.dumps(ev)}\n\n".encode("utf-8") for ev in events]
    fake_harness.responder = lambda m, u, k: FakeResponse(200, chunks=list(chunks))

    r = c.get(f"/api/sessions/{sid}/stream", headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.content == b"".join(chunks)  # observer never touches the relay bytes

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "turn"
    assert row["tenant_id"] == "t-alpha" and row["session_id"] == sid
    assert row["turn_id"] == "turn-9" and row["cost_tokens"] == 1500
    assert row["usd_est"] == 0.03 and row["stop_reason"] == "end_turn"

    # reconnect replays the same events (after_seq semantics) — no double line
    r2 = c.get(f"/api/sessions/{sid}/stream", headers=_h("t-alpha"))
    assert r2.status_code == 200
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_transcript_passthrough(fake_harness):
    c = _client()
    sid = _create(c)
    events = [{"seq": 1, "type": "turn_started", "data": {}}]
    fake_harness.responder = lambda m, u, k: FakeResponse(200, {"events": events})
    r = c.get(f"/api/sessions/{sid}/transcript?limit=50", headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["events"] == events
    assert fake_harness.calls[-1]["params"] == {"limit": 50, "tenantId": "t-alpha"}


def test_archive_passthrough_and_forgets_session(fake_harness):
    c = _client()
    sid = _create(c)
    fake_harness.responder = lambda m, u, k: FakeResponse(200, {"archived": True})
    r = c.request("DELETE", f"/api/sessions/{sid}", headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is True
    assert sid not in sessions_router._SESSIONS
    assert fake_harness.calls[-1]["method"] == "DELETE"


def test_unexpected_harness_4xx_is_an_error_not_a_fake_success(fake_harness):
    """A harness-secret mismatch (401) on create/transcript/archive must surface as
    an upstream failure — never 200 {session_id: null} / an empty transcript / a
    falsely archived session."""
    c = _client()
    sid = _create(c)
    fake_harness.responder = lambda m, u, k: FakeResponse(401, {"error": "unauthorized"})

    r_create = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-alpha"))
    r_transcript = c.get(f"/api/sessions/{sid}/transcript", headers=_h("t-alpha"))
    r_archive = c.request("DELETE", f"/api/sessions/{sid}", headers=_h("t-alpha"))
    for r in (r_create, r_transcript, r_archive):
        assert r.status_code == 502, r.text
        b = r.json()
        jsonschema.validate(b, ENVELOPE_SCHEMA)
        assert b["error"]["error_code"] == "BROKER_UNREACHABLE"
        assert b["degraded_mode"] is True
    assert sid in sessions_router._SESSIONS  # a failed archive must not forget it


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
