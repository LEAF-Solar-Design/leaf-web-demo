"""Hermetic S11 capture checks: synthetic credentials, no DB/KMS/provider calls."""
from dataclasses import replace
from pathlib import Path
import json
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deps
import integration_turn_grants as grants
import platform_link
import session_store
import turn_runner

platform_link._ensure_platform_package()
from leaf_platform.integration_turn_grant_store import (
    Authority, Binding, LEGACY_OPERATIONS, CURRENT_OPERATIONS,
)

ORG = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
BINDING = "44444444-4444-4444-8444-444444444444"
TOKEN = "S11-SYNTHETIC-BEARER-NEVER-FORWARD"
ENVELOPE = b"S11-SYNTHETIC-SEALED-ENVELOPE"


def request():
    return Request({"type": "http", "method": "POST", "path": "/api/sessions/s/messages",
                    "headers": [(b"authorization", ("Bearer " + TOKEN).encode())]})


@pytest.fixture
def custody(monkeypatch):
    for name, value in {
        "LEAF_INTEGRATION_TURN_GRANTS_ENABLED": "1",
        "LEAF_INTEGRATION_SERVICE_ORIGIN": "https://integration.example",
        "LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID": "alias/test",
        "AWS_REGION": "us-east-1", "DATABASE_URL": "synthetic-unused",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(grants, "_references", {})
    h = SimpleNamespace(now=1000., puts=[], revokes=[], seals=[], after_put=None,
                        access=True, author="auth0|capture")
    h.tenant = deps.TenantContext(ORG, org_id=ORG, subject=h.author,
                                  authority_resolved=True)
    h.snapshot = deps._VerifiedPlatformExpiry(h.author, 1250.)
    h.session = dict(session_id="s", tenant_id=f"project:{ORG}:{PROJECT}",
                     org_id=ORG, project_id=PROJECT, drawing_id="d",
                     active_turn_id="t", turn_started_at=990.)
    h.identity = SimpleNamespace(
        binding_id=UUID(BINDING), platform_user_id=UUID(USER),
        platform_tenant_id=UUID(ORG), external_authority="auth0",
        external_subject=h.author, status="active")
    monkeypatch.setattr(grants, "time", SimpleNamespace(time=lambda: h.now))
    monkeypatch.setattr(session_store, "get_session", lambda sid: dict(h.session))
    def active(sid, tid, tenant, max_age_s=None):
        assert tenant == ORG and max_age_s == 300
        return h.author if tid == h.session["active_turn_id"] else None
    monkeypatch.setattr(session_store, "active_turn_subject", active)
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda tenant: h.identity)
    def access(session, tenant, *, write, binding):
        assert write is True and binding is h.identity
        return session if h.access else None
    monkeypatch.setattr(platform_link, "require_project_session_access", access)
    class Store:
        def put_once(self, binding, envelope):
            assert envelope == ENVELOPE
            h.puts.append(binding)
            if h.after_put:
                h.after_put()
            return "created"
        def revoke_turn(self, authority, now):
            h.revokes.append((authority, now))
    h.store = Store()
    def seal(token, binding):
        assert token == TOKEN.encode()
        h.seals.append(binding)
        return ENVELOPE
    monkeypatch.setattr(grants, "_adapters", lambda: (Authority, Binding, h.store, seal))
    h.capture = lambda: grants._capture(TOKEN, h.snapshot, h.tenant, "s", "t", 300)
    return h


def test_capture_uses_current_canonical_authority_and_fixed_deadline(custody):
    h = custody
    result = h.capture()
    assert result.code == "captured" and result.grant_id == h.puts[0].grant_id
    b = h.puts[0]
    assert b.authority == Authority(ORG, PROJECT, USER, BINDING, "auth0", h.author,
                                    "s", "t", "https://integration.example",
                                    operations=CURRENT_OPERATIONS)
    assert b.authority.operations == CURRENT_OPERATIONS
    assert (b.issued_at, b.verified_jwt_exp, b.turn_deadline, b.expires_at) == (
        1000, 1250, 1290, 1120)
    assert TOKEN not in repr(grants._references) and ENVELOPE.decode() not in repr(grants._references)
    assert h.capture().code == "unavailable"  # Never renew the same turn.
    assert len(h.puts) == len(h.seals) == 1


def test_duplicate_capture_cannot_upgrade_reserved_legacy_turn(custody):
    h = custody
    legacy = Binding(Authority(ORG, PROJECT, USER, BINDING, "auth0", h.author,
                              "s", "t", "https://integration.example"),
                     "55555555-5555-4555-8555-555555555555", 1000, 1250, 1290, 1120)
    original = legacy.canonical_bytes()
    assert grants._reserve(legacy, h.now) is not None
    assert h.capture().code == "unavailable"
    assert not h.puts and not h.seals and not h.revokes
    assert grants._references[("s", "t")].authority.operations == LEGACY_OPERATIONS
    assert legacy.canonical_bytes() == original


@pytest.mark.parametrize("exp,started,expected", [(1050, 990, 1050), (1250, 710, 1010)])
def test_expiry_uses_verified_exp_and_stored_start(custody, exp, started, expected):
    h = custody
    h.snapshot = replace(h.snapshot, expires_at=exp)
    h.session["turn_started_at"] = started
    assert h.capture().code == "captured"
    assert h.puts[0].expires_at == expected


@pytest.mark.parametrize("field,value", [
    ("org_id", None), ("org_id", USER), ("project_id", None),
    ("project_id", "model-selected"), ("active_turn_id", "new-turn"),
    ("turn_started_at", None), ("turn_started_at", float("nan")),
    ("turn_started_at", float("inf")), ("turn_started_at", 1001),
    ("turn_started_at", 700), ("session_id", "foreign"),
])
def test_missing_foreign_or_stale_session_cannot_capture(custody, field, value):
    custody.session[field] = value
    assert custody.capture().code == "unavailable"
    assert not custody.seals and not custody.puts


@pytest.mark.parametrize("field,value", [
    ("status", "revoked"), ("external_subject", "auth0|foreign"),
    ("external_authority", "other"), ("platform_tenant_id", UUID(USER)),
    ("platform_user_id", None), ("binding_id", None),
])
def test_binding_is_current_and_subject_agrees(custody, field, value):
    setattr(custody.identity, field, value)
    assert custody.capture().code == "unavailable"
    assert not custody.puts


@pytest.mark.parametrize("case", ["access", "author", "tenant", "expired", "nan"])
def test_authority_and_snapshot_fail_closed(custody, case):
    h = custody
    if case == "access": h.access = False
    if case == "author": h.author = "auth0|other"
    if case == "tenant":
        h.tenant = deps.TenantContext(ORG, subject="auth0|other", authority_resolved=True)
    if case in {"expired", "nan"}:
        h.snapshot = replace(h.snapshot, expires_at=1000 if case == "expired" else float("nan"))
    assert h.capture().code == "unavailable"
    assert not h.puts


@pytest.mark.parametrize("name", [
    "LEAF_INTEGRATION_TURN_GRANTS_ENABLED", "LEAF_INTEGRATION_SERVICE_ORIGIN",
    "LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID", "AWS_REGION", "DATABASE_URL",
])
def test_missing_configuration_does_no_adapter_work(custody, monkeypatch, name):
    monkeypatch.delenv(name)
    monkeypatch.setattr(grants, "_adapters", lambda: pytest.fail("adapter touched"))
    assert custody.capture().code == "unavailable"


@pytest.mark.parametrize("origin", ["http://unsafe.example", "https://example/path", "https://EXAMPLE"])
def test_invalid_origin_does_no_authority_or_crypto_work(custody, monkeypatch, origin):
    monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", origin)
    monkeypatch.setattr(session_store, "get_session", lambda *a: pytest.fail("authority read"))
    assert custody.capture().code == "unavailable"
    assert not custody.puts and not custody.seals


@pytest.mark.parametrize("failure", ["import", "seal", "put"])
def test_failures_are_sanitized_and_ambiguous_put_is_revoked(custody, monkeypatch, capsys, caplog, failure):
    h = custody
    def boom(*args): raise RuntimeError(TOKEN + ENVELOPE.decode())
    if failure == "import": monkeypatch.setattr(grants, "_adapters", boom)
    elif failure == "put": monkeypatch.setattr(h.store, "put_once", boom)
    else: monkeypatch.setattr(grants, "_adapters", lambda: (Authority, Binding, h.store, boom))
    result = h.capture()
    assert result == grants.CaptureOutcome("unavailable")
    assert bool(h.revokes) == (failure != "import")
    captured = capsys.readouterr()
    assert TOKEN not in repr(result) + captured.out + captured.err + caplog.text


def test_callback_is_single_use_and_snapshot_has_no_bearer(custody):
    req = request()
    req.state._integration_verified_expiry = custody.snapshot
    callback = grants.for_request(req, custody.tenant)
    assert callback("s", "t", 300).code == "captured"
    assert callback("s", "t", 300).code == "unavailable"
    assert TOKEN not in repr(req.state._integration_verified_expiry)
    req.state._integration_verified_expiry = None
    assert grants.for_request(req, custody.tenant) is None


def test_discard_releases_uninvoked_credential(custody):
    req = request()
    req.state._integration_verified_expiry = custody.snapshot
    callback = grants.for_request(req, custody.tenant)
    callback.discard()
    assert callback("s", "t", 300).code == "unavailable"
    assert not custody.puts


@pytest.fixture
def live_snapshot(monkeypatch):
    import demo_sessions
    import tenancy
    h = SimpleNamespace(calls=0, payload={"sub": "auth0|snapshot", "exp": 4102444800})
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(demo_sessions, "resolve_demo_session", lambda **kw: None)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda sub: (ORG, "pro"))
    monkeypatch.setattr(deps, "require_matching_platform_tenant_claim", lambda *a: None)
    monkeypatch.setattr(deps, "admin_elevated_tier", lambda claim, sub, tier: tier)
    monkeypatch.setattr(deps, "admin_subjects", lambda: set())
    monkeypatch.setattr(tenancy.get_store(), "resolve_workspace", lambda tenant: None)
    def verify(header):
        h.calls += 1
        return h.payload
    monkeypatch.setitem(sys.modules, "auth", SimpleNamespace(
        verify_platform_token=verify, extract_tenant_claims=lambda p: {"tier": "pro"}))
    return h


@pytest.mark.parametrize("exp", [4102444800, None, "4102444800", True, float("nan"), float("inf"), 1])
def test_live_auth_stamps_once_only_for_finite_verified_metadata(live_snapshot, exp):
    live_snapshot.payload["exp"] = exp
    req = request()
    tenant = deps.require_tenant(req, None, "Bearer " + TOKEN, None, None, None)
    assert live_snapshot.calls == 1 and str(tenant) == ORG
    snapshot = req.state._integration_verified_expiry
    assert (snapshot is not None) == (type(exp) is int and exp == 4102444800)
    assert TOKEN not in repr(snapshot) and "expires_at" not in tenant.__dict__


@pytest.mark.parametrize("case", ["off", "dispatch", "guest", "demo", "failed"])
def test_alternate_and_failed_auth_clear_prior_snapshot(live_snapshot, monkeypatch, case):
    import demo_sessions
    req = request()
    req.state._integration_verified_expiry = deps._VerifiedPlatformExpiry("old", 4102444800)
    kwargs = dict(x_tenant_id=None, authorization=None, x_dispatch_secret=None,
                  x_guest_session=None, x_demo_session=None)
    if case == "off": monkeypatch.setattr(deps, "auth_live", lambda: False)
    if case == "dispatch":
        kwargs.update(x_tenant_id=ORG, x_dispatch_secret="synthetic")
        monkeypatch.setattr(deps, "_dispatch_backedge_route", lambda *a: True)
        monkeypatch.setattr(deps, "_dispatch_secret_ok", lambda *a: True)
        monkeypatch.setattr(deps, "backedge_tenant", lambda *a: "dispatch")
    if case == "guest":
        req = Request({"type": "http", "method": "POST", "path": "/guest", "headers": []})
        req.state._integration_verified_expiry = "old"
        kwargs["x_guest_session"] = "synthetic"
        monkeypatch.setitem(sys.modules, "guest_uploads", SimpleNamespace(verify_guest_session=lambda t: "guest-x"))
        monkeypatch.setattr(deps, "_guest_route_allowed", lambda *a: True)
    if case == "demo":
        monkeypatch.setattr(demo_sessions, "resolve_demo_session", lambda **kw: object())
        monkeypatch.setattr(demo_sessions, "require_demo_route", lambda r: None)
        monkeypatch.setattr(deps, "demo_tenant_context", lambda r: "demo")
    if case == "failed":
        from fastapi import HTTPException
        def fail(*a): raise HTTPException(403, "refused")
        monkeypatch.setattr(deps, "require_matching_platform_tenant_claim", fail)
        with pytest.raises(HTTPException): deps.require_tenant(req, **kwargs)
    else:
        deps.require_tenant(req, **kwargs)
    assert req.state._integration_verified_expiry is None
    assert live_snapshot.calls == (1 if case == "failed" else 0)


@pytest.fixture
def dispatch(custody, monkeypatch):
    h = custody
    h.events, h.wire, h.relay, h.order = [], [], [], []
    monkeypatch.setattr(turn_runner, "_require_session", lambda *a: dict(h.session))
    monkeypatch.setattr(turn_runner, "turn_max_s", lambda: 300)
    monkeypatch.setattr(turn_runner.entitlements, "resolve_tier", lambda t: "pro")
    monkeypatch.setattr(turn_runner.entitlements, "resolve_roles", lambda t: ((), False))
    monkeypatch.setattr(turn_runner.entitlements, "entitlements_for", lambda *a: {})
    monkeypatch.setattr(turn_runner.request_journal, "enabled", lambda: False)
    def admit(sid, tid, *a, **kw):
        h.order.append("admit")
        h.session.update(active_turn_id=tid, turn_started_at=990.)
        return True
    monkeypatch.setattr(session_store, "try_begin_turn", admit)
    monkeypatch.setattr(session_store, "append_event", lambda *a: h.events.append(a))
    monkeypatch.setattr(turn_runner, "_prior_messages", lambda *a, **kw: [])
    monkeypatch.setattr(turn_runner, "_harness_url", lambda: "https://harness.invalid")
    monkeypatch.setattr(turn_runner.context_packet, "build_packet", lambda *a, **kw: {})
    monkeypatch.setattr(turn_runner.instant_execution, "assignment_for_session", lambda *a: None)
    monkeypatch.setattr(turn_runner.session_policy, "get_policy", lambda *a: "assist")
    monkeypatch.setattr(turn_runner.broker_client, "harness_headers", lambda: {})
    monkeypatch.setattr(turn_runner, "_spawn_relay", lambda *a, **kw: h.relay.append((a, kw)))
    monkeypatch.setattr(turn_runner, "_auto_confirm_reads", lambda *a, **kw: None)
    monkeypatch.setattr(turn_runner, "_kick_queued", lambda *a: None)
    monkeypatch.setattr(session_store, "_end_turn_with_store_mode", lambda *a: None)
    def post(url, **kw):
        h.order.append("post")
        h.wire.append(kw)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(turn_runner.requests, "post", post)
    def callback(sid, tid, max_s):
        h.order.append("capture")
        return grants._capture(TOKEN, h.snapshot, h.tenant, sid, tid, max_s)
    h.callback = callback
    h.start = lambda **kw: turn_runner.start_turn(h.tenant, "s", text="hello", turn_id="t", **kw)
    return h


@pytest.mark.parametrize("journal", [False, True])
def test_direct_capture_after_admission_before_post_never_on_wire(dispatch, monkeypatch, journal):
    h = dispatch
    if journal:
        monkeypatch.setattr(turn_runner.request_journal, "enabled", lambda: True)
        def begin(*a, **kw):
            h.order.append("admit")
            return True
        monkeypatch.setattr(turn_runner.request_journal, "begin_request_and_turn", begin)
    assert h.start(integration_capture=h.callback, request_id="req" if journal else None) == "t"
    assert h.order == ["admit", "capture", "post"]
    assert len(h.puts) == 1
    observable = repr(h.wire) + repr(h.relay) + repr(h.events)
    assert TOKEN not in observable and ENVELOPE.decode() not in observable
    assert "integration_capture" not in observable


@pytest.mark.parametrize("case", ["queued", "journal_claimed", "authority", "busy", "replay", "oversize", "no_harness"])
def test_ineligible_starts_never_call_capture(dispatch, monkeypatch, case):
    h = dispatch
    kw = {"integration_capture": lambda *a: pytest.fail("ineligible capture")}
    expected = None
    if case == "queued": kw["queued_id"] = "q"
    if case == "journal_claimed": kw["journal_claimed"] = True
    if case == "authority":
        kw["classifier_hint"] = {"source": "author_panel", "purpose": "stage_authority"}
        monkeypatch.setattr(turn_runner, "_hold_stage_authority", lambda *a: None)
        monkeypatch.setattr(session_store, "turn_started_data", lambda *a: None)
    if case == "busy":
        monkeypatch.setattr(session_store, "try_begin_turn", lambda *a, **kw: False)
        expected = turn_runner.TurnBusy
    if case == "replay":
        kw["request_id"] = "req"
        monkeypatch.setattr(turn_runner.request_journal, "enabled", lambda: True)
        monkeypatch.setattr(turn_runner.request_journal, "begin_request_and_turn", lambda *a, **kw: False)
        expected = turn_runner.TurnBusy
    if case == "oversize":
        monkeypatch.setattr(turn_runner, "MAX_FORWARD_BYTES", 1)
        expected = turn_runner.TurnRejected
    if case == "no_harness":
        monkeypatch.setattr(turn_runner, "_harness_url", lambda: None)
        expected = turn_runner.TurnRejected
    if expected:
        with pytest.raises(expected): h.start(**kw)
    else: h.start(**kw)
    assert not h.puts


@pytest.mark.parametrize("case", ["disabled", "import", "seal", "put", "callback"])
def test_optional_failure_preserves_dispatch_body_and_response(dispatch, monkeypatch, case):
    h = dispatch
    h.start()
    before = h.wire[-1]
    def boom(*a): raise RuntimeError(TOKEN)
    if case == "disabled": monkeypatch.delenv("LEAF_INTEGRATION_TURN_GRANTS_ENABLED")
    if case == "import": monkeypatch.setattr(grants, "_adapters", boom)
    if case == "seal": monkeypatch.setattr(grants, "_adapters", lambda: (Authority, Binding, h.store, boom))
    if case == "put": monkeypatch.setattr(h.store, "put_once", boom)
    assert h.start(integration_capture=boom if case == "callback" else h.callback) == "t"
    assert h.wire[-1] == before


def test_existing_llm_credential_forwarding_is_unchanged(dispatch):
    llm = {"access_token": "synthetic-unrelated-llm"}
    dispatch.start(integration_capture=dispatch.callback, credential_grant=llm)
    assert json.loads(dispatch.wire[0]["data"])["credential_grant"] == llm


@pytest.mark.parametrize("parked", [False, True])
def test_queue_option_captures_only_when_direct_admission_wins(dispatch, monkeypatch, parked):
    from routers import sessions
    h = dispatch
    req = request()
    req.state._integration_verified_expiry = h.snapshot
    monkeypatch.setattr(sessions, "_require_owned_session", lambda *a: dict(h.session))
    monkeypatch.setattr(sessions.entitlements, "entitlements_for", lambda *a: {"converse": True})
    queued = []
    if parked:
        monkeypatch.setattr(session_store, "try_begin_turn", lambda *a, **kw: False)
    def enqueue(*a, **kw):
        queued.append((a, kw))
        return "queued", "q"
    monkeypatch.setattr(turn_runner, "try_enqueue_turn", enqueue)
    response = sessions.post_message("s", sessions.MessageRequest(text="hello", queue=True), req, h.tenant)
    assert response.status_code == 202
    assert len(h.puts) == (0 if parked else 1)
    assert bool(queued) == parked
    observable = repr(queued) + repr(h.events) + repr(h.wire) + response.body.decode()
    assert TOKEN not in observable and ENVELOPE.decode() not in observable
    assert "integration_capture" not in repr(queued)


def test_router_journal_replay_exits_before_callback_creation(dispatch, monkeypatch):
    from routers import sessions
    h = dispatch
    req = request()
    req.state._integration_verified_expiry = h.snapshot
    monkeypatch.setattr(sessions, "_require_owned_session", lambda *a: dict(h.session))
    monkeypatch.setattr(sessions.entitlements, "entitlements_for", lambda *a: {"converse": True})
    monkeypatch.setattr(sessions.request_journal, "enabled", lambda: True)
    monkeypatch.setattr(sessions.request_journal, "canonical_request_id", lambda rid: "req")
    monkeypatch.setattr(sessions.request_journal, "payload_digest", lambda payload: "digest")
    monkeypatch.setattr(sessions.request_journal, "admit_request", lambda **kw: ({"state": "completed"}, False))
    monkeypatch.setattr(sessions, "_journal_response", lambda row, tenant: "replayed")
    monkeypatch.setattr(grants, "for_request", lambda *a: pytest.fail("replay capture callback"))
    assert sessions.post_message("s", sessions.MessageRequest(text="hello"), req, h.tenant) == "replayed"
    assert not h.puts and not h.wire
