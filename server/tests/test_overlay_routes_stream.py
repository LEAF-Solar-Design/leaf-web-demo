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
        self.proposed = []

    def document(self, tenant_id):
        return {"tenant_id": tenant_id, "version": 3, "tokens": {}}

    def create_proposal(self, **kw):
        self.proposed.append(kw)
        return {**kw, "state": "pending", "revision": 0,
                "lease_expires_at": "2026-08-05T00:00:00Z"}

    def latest_proposal(self, proposal_id, tenant_id):
        return {
            "proposal_id": proposal_id,
            "tenant_id": tenant_id,
            "session_id": self.session_id,
        }

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


@pytest.fixture()
def project_overlay(monkeypatch):
    """Real active-turn lookup and session guard, with a fresh membership seam."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps

    org_id = "11111111-1111-4111-8111-111111111111"
    project_id = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    session_id = session_store.get_or_create_session(
        org_id, f"dwg-{os.urandom(4).hex()}")["session_id"]
    turn_id = "overlay-active-turn"
    subject = "auth0|project-editor"
    assert session_store.try_begin_turn(
        session_id, turn_id, 300, tier="pro", subject=subject)
    original_get = session_store.get_session

    def get_session(target):
        row = original_get(target)
        if row is not None and target == session_id:
            return {**row, "org_id": org_id, "project_id": project_id}
        return row

    monkeypatch.setattr(session_store, "get_session", get_session)
    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority",
        lambda actor: (org_id, "pro") if actor == subject else ("foreign", "pro"),
    )
    membership = {"role": "editor", "checks": []}

    def require_project_access(tenant, target_project, *, write):
        # This is the existing guard's fresh-membership boundary, not a role
        # asserted by the transport or saved on the active turn.
        assert tenant.subject == subject
        assert str(tenant) == org_id
        assert target_project == project_id
        assert write is True
        membership["checks"].append(membership["role"])
        if membership["role"] not in {"owner", "editor"}:
            raise overlay_router.platform_link.ProjectSessionForbidden("write refused")
        return org_id

    monkeypatch.setattr(overlay_router.platform_link, "require_project_access",
                        require_project_access)
    store = FakeStore()
    store.session_id = session_id
    monkeypatch.setattr(overlay_router, "_store", lambda: store)
    context = deps.TenantContext(org_id, org_id=org_id, backedge=True)
    app = FastAPI()
    app.include_router(overlay_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: context
    client = TestClient(app)
    yield {
        "client": client, "app": app, "store": store,
        "session": session_id, "turn": turn_id, "org": org_id,
        "subject": subject, "membership": membership,
        "headers": {"X-Authority-Session-Id": session_id,
                    "X-Authority-Turn-Id": turn_id},
    }
    session_store.end_turn(session_id, turn_id)


def _project_preview(project, *, headers=None, target=None):
    return project["client"].post(
        "/api/overlay/proposals",
        json={"session_id": target or project["session"],
              "tokens": {"color.border": "#123456"}, "request_text": "private preview"},
        headers=project["headers"] if headers is None else headers,
    )


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_dispatch_overlay_uses_active_author_and_fresh_project_write_role(project_overlay, role):
    project = project_overlay
    project["membership"]["role"] = role
    response = _project_preview(project)
    assert response.status_code == 200, response.text
    assert project["membership"]["checks"] == [role]
    assert len(project["store"].proposed) == 1
    assert project["store"].proposed[0]["session_id"] == project["session"]
    assert project["store"].proposed[0]["tenant_id"] == project["org"]
    assert [e["type"] for e in _events(project["session"])] == ["overlay_proposed"]


@pytest.mark.parametrize("failure", [
    "missing", "session_only", "turn_only", "mismatch", "superseded",
    "completed", "stale", "foreign_turn", "foreign_binding", "binding_outage",
    "unknown_target", "foreign_target",
])
def test_invalid_dispatch_overlay_authority_never_mutates(project_overlay, monkeypatch, failure):
    import deps
    import turn_runner

    project = project_overlay
    headers = dict(project["headers"])
    target = project["session"]
    foreign = session_store.get_or_create_session(
        "foreign-org", f"dwg-{os.urandom(4).hex()}")["session_id"]
    if failure == "missing":
        headers = {}
    elif failure == "session_only":
        headers.pop("X-Authority-Turn-Id")
    elif failure == "turn_only":
        headers.pop("X-Authority-Session-Id")
    elif failure == "mismatch":
        other = session_store.get_or_create_session(
            project["org"], f"dwg-{os.urandom(4).hex()}")["session_id"]
        assert session_store.try_begin_turn(other, "other-turn", 300, subject=project["subject"])
        headers = {"X-Authority-Session-Id": other, "X-Authority-Turn-Id": "other-turn"}
    elif failure == "superseded":
        headers["X-Authority-Turn-Id"] = "previous-turn"
    elif failure == "completed":
        session_store.end_turn(target, project["turn"])
    elif failure == "stale":
        monkeypatch.setattr(turn_runner, "turn_max_s", lambda: -1)
    elif failure == "foreign_turn":
        assert session_store.try_begin_turn(foreign, "foreign-turn", 300, subject=project["subject"])
        target = foreign
        headers["X-Authority-Session-Id"] = foreign
        headers["X-Authority-Turn-Id"] = "foreign-turn"
    elif failure == "foreign_binding":
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                            lambda subject: ("foreign-org", "pro"))
    elif failure == "binding_outage":
        def unavailable(subject):
            raise RuntimeError("binding unavailable")
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", unavailable)
    elif failure in {"unknown_target", "foreign_target"}:
        target = "sess-does-not-exist" if failure == "unknown_target" else foreign
        headers["X-Authority-Session-Id"] = target

    response = _project_preview(project, headers=headers, target=target)
    assert response.status_code == 404, response.text
    assert "session_not_found" in response.text
    assert project["store"].proposed == []
    assert project["membership"]["checks"] == []
    assert _events(project["session"]) == []
    assert _events(foreign) == []


@pytest.mark.parametrize("role", ["revoked", "read_only", "reviewer"])
def test_dispatch_overlay_does_not_restore_project_write_access(project_overlay, role):
    project = project_overlay
    project["membership"]["role"] = role
    response = _project_preview(project)
    assert response.status_code == 403, response.text
    assert project["membership"]["checks"] == [role]
    assert project["store"].proposed == []
    assert _events(project["session"]) == []


@pytest.mark.parametrize("subject", [None, "auth0|project-editor"])
def test_direct_overlay_does_not_borrow_turn_authority(project_overlay, monkeypatch, subject):
    import deps

    project = project_overlay
    context = deps.TenantContext(project["org"], org_id=project["org"], subject=subject)
    project["app"].dependency_overrides[deps.require_tenant] = lambda: context
    session_store.end_turn(project["session"], project["turn"])
    monkeypatch.setattr(deps, "stage_author_identity",
                        lambda *args: pytest.fail("a direct caller must not borrow an actor"))
    if subject is None:
        def forbidden(*args, **kwargs):
            raise overlay_router.platform_link.ProjectSessionForbidden("no verified subject")
        monkeypatch.setattr(overlay_router.platform_link, "require_project_access", forbidden)
    response = _project_preview(project, headers={} if subject else project["headers"])
    assert response.status_code == (200 if subject else 403), response.text
    assert len(project["store"].proposed) == (1 if subject else 0)
    assert len(_events(project["session"])) == (1 if subject else 0)


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


@pytest.fixture()
def decision_identity(monkeypatch):
    """Exercise the canonical caller resolver, replacing only its store IO."""
    import uuid
    from types import SimpleNamespace
    import deps

    state = SimpleNamespace(
        binding=SimpleNamespace(platform_tenant_id=TENANT, binding_id=uuid.uuid4()),
        lookups=[], error=None,
    )

    def resolve(authority, subject):
        state.lookups.append((authority, subject))
        if state.error is not None:
            raise state.error
        return state.binding

    binding_store = SimpleNamespace(resolve_active_identity_binding=resolve)
    monkeypatch.setattr(overlay_router.platform_link, "_load_platform",
                        lambda: (binding_store, None, None))
    state.tenant = deps.TenantContext(
        TENANT, subject="auth0|overlay-reviewer", authority_resolved=True)
    return state


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("actor", [None, "spoofed@browser"])
def test_verified_decision_records_canonical_binding(store, session, decision_identity,
                                                    approve, actor):
    identity = decision_identity
    kwargs = {} if actor is None else {"x_actor": actor}
    out = overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=approve,
                   decision_key="verified-key", document_version=3),
        tenant=identity.tenant, **kwargs)
    assert out["state"] == ("approved" if approve else "denied")
    assert identity.lookups == [("auth0", identity.tenant.subject)]
    assert store.decided == ("approve" if approve else "deny", {
        "proposal_id": "p-1", "tenant_id": TENANT,
        "actor": str(identity.binding.binding_id), "decision_key": "verified-key",
        "expected_version": 3,
    })


@pytest.mark.parametrize("failure", ["revoked", "foreign", "outage"])
def test_verified_decision_never_falls_back_to_supplied_actor(
        store, session, decision_identity, failure):
    identity = decision_identity
    if failure == "revoked":
        identity.binding = None
    elif failure == "foreign":
        identity.binding.platform_tenant_id = "foreign-tenant"
    else:
        identity.error = RuntimeError("private binding outage")
    res = overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=False,
                   decision_key="verified-key", document_version=3),
        x_actor="fallback@browser", tenant=identity.tenant)
    assert res.status_code == (503 if failure == "outage" else 403)
    assert store.decided is None
    assert store.proposed == []
    assert _events(session) == []
    assert b"private binding outage" not in res.body


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("code", ["version_conflict", "decision_key_reused"])
def test_verified_decision_preserves_store_witness_refusals(
        store, session, decision_identity, monkeypatch, approve, code):
    class WitnessRefused(Exception):
        status_code = 409

    def refuse(**kwargs):
        assert kwargs["expected_version"] == 3
        assert kwargs["decision_key"] == "verified-key"
        error = WitnessRefused()
        error.code = code
        raise error

    monkeypatch.setattr(store, "approve" if approve else "deny", refuse)
    res = overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=approve,
                   decision_key="verified-key", document_version=3),
        tenant=decision_identity.tenant)
    assert res.status_code == 409
    assert code.encode() in res.body
    assert store.decided is None
    assert _events(session) == []


@pytest.mark.parametrize("kind", ["plain", "unresolved", "subjectless", "backedge", "demo"])
def test_unverified_decision_does_not_gain_an_actor(store, session, monkeypatch, kind):
    import deps

    contexts = {
        "plain": TENANT,
        "unresolved": deps.TenantContext(TENANT, subject="auth0|unverified"),
        "subjectless": deps.TenantContext(TENANT, authority_resolved=True),
        "backedge": deps.TenantContext(TENANT, subject="auth0|backedge",
                                     authority_resolved=True, backedge=True),
        "demo": deps.TenantContext(TENANT, subject="demo|subject",
                                 authority_resolved=True, verified_demo=True),
    }
    monkeypatch.setattr(
        overlay_router.platform_link, "resolve_caller_binding",
        lambda *_: pytest.fail("this caller has no normal verified authority"))
    res = overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=False,
                   decision_key="verified-key", document_version=3),
        tenant=contexts[kind])
    assert res.status_code == 400
    assert b"actor_required" in res.body
    assert store.decided is None
    assert _events(session) == []


@pytest.mark.parametrize("failure", ["revoked_role", "foreign_session"])
def test_verified_decision_rechecks_project_session(
        project_overlay, decision_identity, monkeypatch, failure):
    import deps

    project = project_overlay
    tenant = deps.TenantContext(project["org"], subject=project["subject"],
                                authority_resolved=True)
    decision_identity.binding.platform_tenant_id = project["org"]
    monkeypatch.setattr(
        overlay_router.platform_link, "resolve_caller_binding",
        lambda *_: pytest.fail("forbidden project must be rejected before actor lookup"))
    if failure == "revoked_role":
        project["membership"]["role"] = "revoked"
    else:
        foreign = session_store.get_or_create_session(
            "foreign-org", f"dwg-{os.urandom(4).hex()}")["session_id"]
        project["store"].session_id = foreign
    res = overlay_router.decide_overlay(
        DecideBody(proposal_id="p-1", approve=False,
                   decision_key="verified-key", document_version=3), tenant=tenant)
    assert res.status_code == (403 if failure == "revoked_role" else 404)
    assert project["store"].decided is None
    assert project["store"].proposed == []
    assert _events(project["session"]) == []
    if failure == "foreign_session":
        assert _events(foreign) == []


@pytest.mark.parametrize("actor", [None, "spoofed@browser"])
def test_http_authenticated_denial_without_browser_actor(
        store, session, decision_identity, monkeypatch, actor):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import auth
    import deps
    import tenancy

    identity = decision_identity
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")

    def verify(authorization):
        assert authorization == "Bearer test"
        ns = auth.claim_ns()
        return {"sub": identity.tenant.subject, ns + "tenant_id": TENANT,
                ns + "org_id": TENANT, ns + "tier": "pro"}

    monkeypatch.setattr(auth, "verify_platform_token", verify)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda subject: (TENANT, "pro"))
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(
        resolve_workspace=lambda _: None))
    app = FastAPI()
    app.include_router(overlay_router.router)
    headers = {"Authorization": "Bearer test"}
    if actor is not None:
        headers["X-Actor"] = actor
    with TestClient(app) as client:
        response = client.post("/api/overlay/decisions", headers=headers, json={
            "proposal_id": "p-1", "approve": False,
            "decision_key": "verified-key", "document_version": 3,
        })
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "denied"
    assert store.decided[1]["actor"] == str(identity.binding.binding_id)
    assert identity.lookups == [("auth0", identity.tenant.subject)]


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
    # Clear the package AND every submodule: leaving leaf_platform.overlay_store
    # or leaf_platform.db cached lets the loader appear to work off a warm
    # cache, so the "cold load" claim would be false (sol-critic PR #439 r7).
    for name in [n for n in list(sys.modules)
                 if n == "leaf_platform" or n.startswith("leaf_platform.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

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
