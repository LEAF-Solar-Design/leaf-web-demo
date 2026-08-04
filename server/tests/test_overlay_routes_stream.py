"""T1 overlay routes -> the stream, wired through the durable transcript.

There is no in-memory broadcaster in this app: GET /api/sessions/{id}/stream
POLLS events_after on the transcript. So "wiring publish to the broadcaster"
means one thing — the routes must append their events via
session_store.append_event, which both feeds the live SSE poll and is the
replay source on reconnect. These tests prove the append actually happens,
with the durable seq, against a real sqlite transcript.

Run:  cd server && python -m pytest tests/test_overlay_routes_stream.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# A throwaway transcript DB, set BEFORE session_store first imports.
os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="overlay-routes-")) / "sessions.db"))

import session_store  # noqa: E402
from routers import overlay as overlay_router  # noqa: E402
from routers.overlay import DecideBody, ProposeBody  # noqa: E402

TENANT = "tenant-t1"


class FakeStore:
    """Stands in for platform.overlay_store, which needs Postgres. What is
    under test here is the ROUTE's wiring to the transcript, not the SQL."""

    def __init__(self):
        self.decided = None

    def document(self, tenant_id):
        return {"tenant_id": tenant_id, "version": 3, "tokens": {}}

    def create_proposal(self, **kw):
        return {**kw, "state": "pending", "revision": 0,
                "lease_expires_at": "2026-08-05T00:00:00Z"}

    def approve(self, **kw):
        self.decided = ("approve", kw)
        return ({"proposal_id": kw["proposal_id"], "state": "approved",
                 "session_id": self.session_id},
                {"version": 4, "tokens": {"color.border": "#123456"}})

    def deny(self, **kw):
        self.decided = ("deny", kw)
        return {"proposal_id": kw["proposal_id"], "state": "denied",
                "session_id": self.session_id}


@pytest.fixture()
def session():
    row = session_store.get_or_create_session(TENANT, f"dwg-{os.urandom(4).hex()}")
    return row["session_id"]


@pytest.fixture()
def store(monkeypatch, session):
    fake = FakeStore()
    fake.session_id = session
    monkeypatch.setattr(overlay_router, "_store", lambda: fake)
    return fake


def _events(session_id):
    return session_store.events_after(session_id, 0, 100)


# --------------------------------------------------------------------------- #
# Propose
# --------------------------------------------------------------------------- #
def test_propose_appends_overlay_proposed_to_the_transcript(store, session):
    out = overlay_router.propose_overlay(
        ProposeBody(tokens={"color.border": "#123456"},
                    request_text="lighter please", session_id=session),
        tenant=TENANT)
    assert out["error"] is None

    evs = [e for e in _events(session) if e["type"] == "overlay_proposed"]
    assert len(evs) == 1, "the proposal was stored but never announced"
    assert evs[0]["data"]["token_ids"] == ["color.border"]
    assert evs[0]["seq"] >= 1, "the envelope must carry the DURABLE seq"


def test_an_unknown_session_is_refused_BEFORE_any_write(store):
    """append_event raises KeyError on an unknown session, and by publish time
    the proposal row is committed — the caller would get an error for a
    proposal that exists, then retry into pending_proposal_exists and be
    stuck. Refusing first means everything happens or nothing does."""
    res = overlay_router.propose_overlay(
        ProposeBody(tokens={"color.border": "#123456"},
                    session_id="sess-does-not-exist"),
        tenant=TENANT)
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #
def _decide(session, approve):
    return overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=approve,
                   decision_key="k1234567", document_version=3),
        x_actor="op@example.com", tenant=TENANT)


def test_a_decision_announces_overlay_decided_with_the_durable_seq(store, session):
    out = _decide(session, approve=True)
    assert out["state"] == "approved"

    evs = [e for e in _events(session) if e["type"] == "overlay_decided"]
    assert len(evs) == 1, "the requester's card would only clear on a full re-read"
    assert evs[0]["data"]["state"] == "approved"
    assert evs[0]["data"]["document_version"] == 4
    assert evs[0]["seq"] >= 1


def test_a_denial_is_announced_too(store, session):
    _decide(session, approve=False)
    evs = [e for e in _events(session) if e["type"] == "overlay_decided"]
    assert evs and evs[0]["data"]["state"] == "denied"


def test_a_dead_requester_session_does_not_fail_the_decision(store, monkeypatch):
    """The decision is durable in overlay_proposals before the announce runs.
    If the requester's transcript is gone there is nobody left to notify —
    failing the OPERATOR's tap over that would report a decision as failed
    when it fully happened."""
    store.session_id = "sess-vanished"
    out = _decide("ignored", approve=True)
    assert out["state"] == "approved", "the announce failure leaked into the decision"


def test_a_blank_actor_never_reaches_the_store(store, session):
    res = overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=True,
                   decision_key="k1234567", document_version=3),
        x_actor="   ", tenant=TENANT)
    assert res.status_code == 400
    assert store.decided is None


# --------------------------------------------------------------------------- #
# Revoke
# --------------------------------------------------------------------------- #
def test_a_revoke_announces_overlay_revoked_with_ids_never_values(store, session):
    store.reverted = None

    def revert(**kw):
        store.reverted = kw
        return ({"proposal_id": kw["proposal_id"], "state": "reverted",
                 "session_id": store.session_id,
                 "tokens": {"color.border": "#123456"}},
                {"version": 5, "tokens": {}})
    store.revert = revert

    out = overlay_router.revoke_overlay(
        overlay_router.RevokeBody(proposal_id="p-1", decision_key="k1234567",
                                  document_version=4),
        x_actor="op@example.com", tenant=TENANT)
    assert out["state"] == "reverted"
    assert store.reverted["expected_version"] == 4

    evs = [e for e in _events(session) if e["type"] == "overlay_revoked"]
    assert len(evs) == 1, "THE event the stream contract exists for never fired"
    assert evs[0]["data"]["token_ids"] == ["color.border"]
    assert evs[0]["data"]["reason"] == "operator_reverted"
    assert "#123456" not in repr(evs[0]["data"]), "a token VALUE leaked into the event"
    assert evs[0]["seq"] >= 1


def test_a_blank_actor_cannot_revoke(store, session):
    res = overlay_router.revoke_overlay(
        overlay_router.RevokeBody(proposal_id="p-1", decision_key="k1234567",
                                  document_version=4),
        x_actor="", tenant=TENANT)
    assert res.status_code == 400


def test_store_import_survives_the_stdlib_platform_shadow(monkeypatch):
    """The shipped container resolves `import platform` to the STDLIB module
    (site-packages precedes the repo root), so the router's original
    `from platform import overlay_store` 500ed every overlay route — confirmed
    live on staging 2026-08-04, from a real signed-in chat turn. The router now
    goes through platform_link's file-located `leaf_platform` alias, which no
    sys.path order can shadow. This test recreates the hostile state
    explicitly: the colliding name in sys.modules IS the stdlib module, and
    the alias cache is cold."""
    import importlib.util
    import sys
    import sysconfig
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "platform", Path(sysconfig.get_paths()["stdlib"]) / "platform.py")
    stdlib_platform = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stdlib_platform)
    assert not hasattr(stdlib_platform, "overlay_store")
    monkeypatch.setitem(sys.modules, "platform", stdlib_platform)
    monkeypatch.delitem(sys.modules, "leaf_platform", raising=False)

    import routers.overlay as overlay_router
    store = overlay_router._store()

    assert Path(store.__file__).resolve().parent.name == "platform"
    assert hasattr(store, "approve") and hasattr(store, "revert")


def test_a_foreign_tenants_session_is_refused_BEFORE_any_write(store, monkeypatch):
    """Existence alone was not enough (sol-critic PR #439 round 1, MAJOR).

    A valid caller for tenant A could name tenant B's session: the proposal row
    lands under A but keyed to B's session, and the overlay_proposed announce
    lands in B's transcript — a cross-tenant write plus a foreign card, and
    repeatable across guessed session ids. Now the session must BELONG to the
    caller, answered 404-not-403 so a prober cannot tell "gone" from "not
    yours" (session_store.get_session's own stated contract).

    This route only became reachable from the harness back edge in this PR, so
    the guard ships with the allowlist entry.
    """
    other = session_store.get_or_create_session(
        "tenant-other", f"dwg-{os.urandom(4).hex()}")["session_id"]
    proposed = []
    monkeypatch.setattr(store, "create_proposal",
                        lambda **kw: proposed.append(kw))

    res = overlay_router.propose_overlay(
        ProposeBody(tokens={"color.border": "#123456"},
                    request_text="not mine", session_id=other),
        tenant=TENANT)

    assert res.status_code == 404
    assert "session_not_found" in res.body.decode()
    assert proposed == [], "a foreign session must not reach the store"
    assert _events(other) == [], "nothing may be appended to a foreign transcript"


def test_every_decision_route_passes_the_callers_tenant_to_the_store(store, session):
    """sol-critic PR #439 round 2, MAJOR. The store's locked lookup filtered on
    proposal_id ALONE and then acted on the tenant read off the PROPOSAL row,
    so knowing another tenant's proposal id was enough to approve, deny or
    revert it — and the mutation landed on THEIR document. The store now scopes
    the lookup by tenant (a foreign id reads exactly like a missing one); this
    pins the other half, that the routes actually hand it the CALLER's tenant
    rather than dropping it.
    """
    seen = {}
    store.approve = lambda **kw: (seen.setdefault("approve", kw),
                                  ({"proposal_id": kw["proposal_id"],
                                    "state": "approved", "session_id": session},
                                   {"version": 5, "tokens": {}}))[1]
    store.deny = lambda **kw: (seen.setdefault("deny", kw),
                               {"proposal_id": kw["proposal_id"],
                                "state": "denied", "session_id": session})[1]
    store.revert = lambda **kw: (seen.setdefault("revert", kw),
                                 ({"proposal_id": kw["proposal_id"],
                                   "state": "reverted", "session_id": session,
                                   "tokens": {"color.border": "#123456"}},
                                  {"version": 6, "tokens": {}}))[1]

    overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=True, decision_key="k1234567",
                   document_version=3), x_actor="op@leaf", tenant=TENANT)
    overlay_router.decide_overlay(
        DecideBody(proposal_id="p-2", approve=False, decision_key="k7654321",
                   document_version=3), x_actor="op@leaf", tenant=TENANT)
    overlay_router.revoke_overlay(
        overlay_router.RevokeBody(proposal_id="p-3", decision_key="k1112223",
                                  document_version=3),
        x_actor="op@leaf", tenant=TENANT)

    assert set(seen) == {"approve", "deny", "revert"}
    for name, kw in seen.items():
        assert kw.get("tenant_id") == TENANT, f"{name} dropped the caller's tenant"
