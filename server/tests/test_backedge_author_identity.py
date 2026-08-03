"""The harness back-edge authors as the human who opened the turn.

The back edge authenticates as a TENANT (dispatch secret + X-Tenant-Id) and can
never assert a user, so protected authoring — which requires a verified
owner/editor binding — could not resolve a subject and always failed closed with
`tenant_identity_binding_unavailable`. These tests pin the resolution: the
harness names the app-owned session and turn that authenticated it, and the app
reads the author from its OWN record of who opened that turn.
"""
from __future__ import annotations

import pytest

import agent_gate
import deps
import session_store
from routers.author import _subject_scoped_key


ALICE = "auth0|alice"
MALLORY = "auth0|mallory"


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    monkeypatch.setattr(
        deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: ("tenant-a", "hosted_pro"),
    )
    session_store.ensure_started()
    return session_store.get_or_create_session("tenant-a", "drawing-a")


def backedge(tenant_id="tenant-a"):
    """The identity a verified back-edge call resolves to: no subject."""
    return deps.TenantContext(tenant_id, tier="hosted_pro", backedge=True)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_backedge_authors_as_the_subject_that_opened_the_turn(session):
    session_id = session["session_id"]
    assert session_store.try_begin_turn(session_id, "turn-1", 60,
                                        tier="hosted_pro", subject=ALICE)

    elevated = deps.backedge_author_identity(backedge(), session_id, "turn-1")

    assert elevated.subject == ALICE
    assert str(elevated) == "tenant-a"
    # The rest of the verified identity survives — _binding compares org_id.
    assert elevated.tier == "hosted_pro"


def test_backedge_restores_the_current_platform_tier(session):
    """A stale broker tenant row must not downgrade current authority.

    Protected authoring comes back through the subjectless harness back edge.
    The app-owned turn supplies the subject, then the current platform binding
    supplies the tier that is authoritative at the moment of authoring.
    """
    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-tier", 60, tier="hosted_pro", subject=ALICE,
    )
    restricted_backedge = deps.TenantContext(
        "tenant-a", tier="restricted", backedge=True,
    )

    elevated = deps.backedge_author_identity(
        restricted_backedge, session_id, "turn-tier",
    )

    assert elevated.subject == ALICE
    assert elevated.tier == "hosted_pro"
    assert elevated.backedge is True


def test_backedge_run_uses_the_active_turns_current_platform_tier(
    session, monkeypatch
):
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-run", 60, tier="restricted", subject=ALICE,
    )
    caller = deps.TenantContext("tenant-a", tier="restricted", backedge=True)

    resolved = deps.backedge_run_identity(caller, session_id, "turn-run")

    assert resolved is not None
    assert resolved.subject == ALICE
    assert resolved.tier == "hosted_pro"
    assert resolved.backedge is True


@pytest.mark.parametrize("session_id,turn_id,tenant_id", [
    (None, "turn-run", "tenant-a"),
    ("missing-session", "turn-run", "tenant-a"),
    ("real", "wrong-turn", "tenant-a"),
    ("real", "turn-run", "tenant-b"),
])
def test_backedge_run_fails_closed_without_the_exact_active_turn(
    session, session_id, turn_id, tenant_id, monkeypatch
):
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    real_session = session["session_id"]
    assert session_store.try_begin_turn(
        real_session, "turn-run", 60, tier="hosted_pro", subject=ALICE,
    )
    if session_id == "real":
        session_id = real_session

    assert deps.backedge_run_identity(
        backedge(tenant_id), session_id, turn_id
    ) is None


def test_backedge_run_authority_outage_fails_closed(session, monkeypatch):
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-run", 60, tier="hosted_pro", subject=ALICE,
    )
    monkeypatch.setattr(
        session_store,
        "active_turn_subject",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )

    assert deps.backedge_run_identity(
        backedge(), session_id, "turn-run"
    ) is None


def test_backedge_run_rejects_a_superseded_turn(session, monkeypatch):
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-old", 60, tier="hosted_pro", subject=ALICE,
    )
    assert session_store.try_begin_turn(
        session_id, "turn-new", 0, tier="hosted_pro", subject=MALLORY,
    )

    assert deps.backedge_run_identity(
        backedge(), session_id, "turn-old"
    ) is None
    resolved = deps.backedge_run_identity(
        backedge(), session_id, "turn-new"
    )
    assert resolved is not None and resolved.subject == MALLORY


def test_direct_jwt_run_identity_never_borrows_turn_authority(session):
    caller = deps.TenantContext(
        "tenant-a", tier="hosted_pro", subject=MALLORY,
    )
    assert deps.backedge_run_identity(
        caller, session["session_id"], "any-turn"
    ) is caller


def test_a_mid_turn_platform_downgrade_takes_effect(
    session, monkeypatch,
):
    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-downgraded", 60,
        tier="hosted_pro", subject=ALICE,
    )
    monkeypatch.setattr(
        deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: ("tenant-a", "restricted"),
    )

    elevated = deps.backedge_author_identity(
        backedge(), session_id, "turn-downgraded",
    )

    assert elevated.subject == ALICE
    assert elevated.tier == "restricted"


def test_an_account_move_cannot_elevate_the_old_tenant(
    session, monkeypatch,
):
    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-moved", 60,
        tier="hosted_pro", subject=ALICE,
    )
    monkeypatch.setattr(
        deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: ("tenant-b", "hosted_pro"),
    )

    unchanged = deps.backedge_author_identity(
        backedge(), session_id, "turn-moved",
    )

    assert unchanged.subject is None
    assert unchanged.tier == "hosted_pro"


def test_a_direct_user_call_keeps_its_own_subject(session):
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    caller = deps.TenantContext("tenant-a", tier="hosted_pro", subject=MALLORY)

    # A caller that already authenticated as a user is never re-attributed.
    assert deps.backedge_author_identity(
        caller, session_id, "turn-1").subject == MALLORY


@pytest.mark.parametrize("session_id,turn_id", [
    (None, "turn-1"),
    ("some-session", None),
    (None, None),
])
def test_without_an_authority_tuple_nothing_is_elevated(session, session_id,
                                                        turn_id):
    session_store.try_begin_turn(session["session_id"], "turn-1", 60,
                                 subject=ALICE)
    assert deps.backedge_author_identity(
        backedge(), session_id, turn_id).subject is None


def test_a_finished_turn_yields_no_subject(session):
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    session_store.end_turn(session_id, "turn-1")

    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-1").subject is None


def test_a_terminal_event_releases_the_subject(session):
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    session_store.append_event(session_id, "turn-1", "turn_complete", {})

    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-1").subject is None


def test_a_superseded_turn_yields_the_new_subject_not_the_old(session):
    """A stale takeover must overwrite the subject in the same CAS."""
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    # stale_after_s=0 makes the live turn immediately stale, so this wins.
    assert session_store.try_begin_turn(session_id, "turn-2", 0,
                                        subject=MALLORY)

    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-1").subject is None
    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-2").subject == MALLORY


def test_a_foreign_tenant_cannot_read_the_subject(session):
    """Naming a real session from the wrong tenant resolves nothing."""
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)

    assert deps.backedge_author_identity(
        backedge("tenant-b"), session_id, "turn-1").subject is None


def test_an_authority_outage_never_elevates(session, monkeypatch):
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)

    def explode(*_args, **_kwargs):
        raise RuntimeError("authority down")

    monkeypatch.setattr(session_store, "active_turn_subject", explode)
    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-1").subject is None


def test_current_platform_authority_outage_never_elevates(session, monkeypatch):
    session_id = session["session_id"]
    session_store.try_begin_turn(
        session_id, "turn-1", 60, tier="hosted_pro", subject=ALICE,
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("tier authority down")

    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority", explode,
    )
    unchanged = deps.backedge_author_identity(
        backedge(), session_id, "turn-1",
    )
    assert unchanged.subject is None


def test_the_subject_is_not_exposed_through_the_session_projection(session):
    """It is identity data, not session state — same posture as the tier."""
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)

    row = session_store.get_session(session_id)
    assert "active_turn_subject" not in row
    assert ALICE not in repr(row)


# --------------------------------------------------------------------------- #
# one shared session, two people
# --------------------------------------------------------------------------- #
def test_a_confirm_once_grant_does_not_cross_users():
    """Sessions are per tenant+drawing, so a grant must name the person.

    author_tool takes no `tool` argument, so its target is always ["none"]. A
    grant keyed only on tenant+session+action would let Alice's approval
    authorize Mallory's later, different authoring request.
    """
    alice = agent_gate.grant_target({"description": "a"}, ALICE)
    mallory = agent_gate.grant_target({"description": "b"}, MALLORY)

    assert alice != mallory
    # The unbound key still exists for the auth-off demo, but once auth is live
    # a call that cannot resolve a subject must refuse to use it.
    assert agent_gate.grant_target({"description": "a"}) == ["none"]


def test_a_grant_still_distinguishes_the_target_tool():
    assert agent_gate.grant_target({"tool": "none"}, ALICE) != \
        agent_gate.grant_target(None, ALICE)


def test_idempotency_keys_do_not_collide_across_users():
    """The harness cannot know the user, so its key omits one.

    Two members of a tenant sharing a session and asking for the same tool
    present the SAME key; the store rejects a second author_subject under one
    key as a replay conflict, so they must be separated app-side.
    """
    shared = "author:deadbeef"
    alice = _subject_scoped_key(shared, deps.TenantContext("t", subject=ALICE))
    mallory = _subject_scoped_key(shared,
                                  deps.TenantContext("t", subject=MALLORY))

    assert alice != mallory
    # Stable for one subject, so a retry inside a turn still dedupes.
    assert alice == _subject_scoped_key(
        shared, deps.TenantContext("t", subject=ALICE))
    # Unchanged when there is no subject to scope by.
    assert _subject_scoped_key(shared, deps.TenantContext("t")) == shared


def test_shared_session_approval_can_only_be_consumed_by_its_decider(session):
    session_id = session["session_id"]
    session_store.create_approval(
        "confirm-subject", session_id, "tenant-a", "turn-1",
        "author_tool", {}, "platform_customize", "review", "tool", {},
        300,
    )
    assert session_store.decide_approval(
        "confirm-subject", True, by=ALICE) == "recorded"

    with pytest.raises(session_store.ApprovalConsumeError) as exc:
        session_store.consume_approval(
            "confirm-subject", session_id, "tenant-a", decided_by=MALLORY)
    assert exc.value.reason == "not_found"

    consumed = session_store.consume_approval(
        "confirm-subject", session_id, "tenant-a", decided_by=ALICE)
    assert consumed["approved"] is True
    assert consumed["decided_by"] == ALICE


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #
def test_a_stale_turn_stops_resolving_its_author(session):
    """try_begin_turn hands a stale turn to the next caller, so a turn past that
    bound is one nothing is guarding. Its author must stop resolving with it,
    or an app restart that skipped the watchdog would leave the last turn's
    identity usable indefinitely."""
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)

    # Inside the bound it resolves; past it, nothing.
    assert session_store.active_turn_subject(
        session_id, "turn-1", "tenant-a", 300) == ALICE
    assert session_store.active_turn_subject(
        session_id, "turn-1", "tenant-a", 0) is None


def test_a_turn_with_no_start_time_is_treated_as_stale(session):
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    session_store._exec(
        "UPDATE sessions SET turn_started_at = NULL WHERE session_id = ?",
        (session_id,),
    )

    assert session_store.active_turn_subject(
        session_id, "turn-1", "tenant-a", 300) is None


def test_an_unbounded_read_keeps_the_old_behaviour(session):
    """max_age_s=None is the explicit opt out, used by callers with no bound."""
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)

    assert session_store.active_turn_subject(
        session_id, "turn-1", "tenant-a") == ALICE


def test_the_elevation_helper_applies_the_staleness_bound(session, monkeypatch):
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    monkeypatch.setenv("TURN_MAX_S", "0")

    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-1").subject is None


def test_real_author_route_uses_current_platform_tier(
    session, monkeypatch,
):
    """Drive the live failure shape through the actual POST route."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import customization_flags
    import customization_service
    from routers import author as author_router

    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-route", 60,
        tier="hosted_pro", subject=ALICE,
    )
    seen = {}

    class _RecordingService:
        def stage(self, *, tenant, description, mode, idempotency_key):
            seen["tenant"] = str(tenant)
            seen["tier"] = tenant.tier
            seen["subject"] = tenant.subject
            return {"change_set_id": "cs-route", "state": "staged"}

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(customization_flags, "enabled", lambda wave, tenant: True)
    monkeypatch.setattr(author_router, "customization_enabled",
                        lambda wave, tenant: True)
    monkeypatch.setattr(
        customization_service.CustomizationService,
        "configured",
        classmethod(lambda cls: _RecordingService()),
    )

    app = FastAPI()
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier="restricted", backedge=True,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/author",
        json={"description": "build a deterministic sphere drape", "mode": "build"},
        headers={
            "Idempotency-Key": "author:route-tier",
            "X-Authority-Session-Id": session_id,
            "X-Authority-Turn-Id": "turn-route",
        },
    )

    assert response.status_code == 200, response.text
    assert seen == {
        "tenant": "tenant-a",
        "tier": "hosted_pro",
        "subject": ALICE,
    }


def test_real_run_route_uses_active_turn_authority_not_stale_broker_tier(
    session, monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import jobs as jobs_router

    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-run-route", 60,
        tier="hosted_pro", subject=ALICE,
    )
    tool = {
        "name": "drape-onto-spheres",
        "description": "Drape arrays onto spheres",
        "capabilities": ["drawing.write"],
    }
    digest = deps.catalog_tool_digest(tool)
    catalog_pin = {
        "catalog_commit": "a" * 40,
        "effective_catalog_digest": "b" * 64,
    }
    seen = {}

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "find_tool", lambda name, tenant: tool)
    def entitlements_for(tier, *_roles):
        seen["tier"] = tier
        return {"run_write": True}

    monkeypatch.setattr(
        jobs_router.entitlements, "entitlements_for", entitlements_for,
    )
    monkeypatch.setattr(
        jobs_router.entitlements,
        "tool_required_capability",
        lambda _tool: "run_write",
    )
    monkeypatch.setattr(
        jobs_router, "_checkout_identity", lambda *_args: ("anonymous", None)
    )
    monkeypatch.setattr(
        jobs_router.jobs.platform_link,
        "resolve_submission_context",
        lambda *_args: None,
    )
    monkeypatch.setattr(jobs_router, "_legacy_drawing_head", lambda *_args: 7)
    monkeypatch.setattr(
        jobs_router.customization_service,
        "effective_catalog_pin",
        lambda _tenant: catalog_pin,
    )

    class _LegacyStore:
        @staticmethod
        def authority_mode():
            return "legacy"

    monkeypatch.setattr(jobs_router, "_store", lambda: _LegacyStore())
    def submit_job(tenant, *_args, **_kwargs):
        seen["tenant"] = str(tenant)
        return "job-run-route"

    monkeypatch.setattr(jobs_router.jobs, "submit_job", submit_job)

    app = FastAPI()
    app.include_router(jobs_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier="restricted", backedge=True,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/run",
        json={
            "tool": "drape-onto-spheres",
            "params": {},
            "dwg": "drawing-a",
            "dwg_version": 7,
            "expected_drawing_head": 7,
            "catalog_digest": digest,
            "tool_manifest_sha256": digest,
            **catalog_pin,
        },
        headers={
            "X-Authority-Session-Id": session_id,
            "X-Authority-Turn-Id": "turn-run-route",
        },
    )

    assert response.status_code == 202, response.text
    assert seen == {"tier": "hosted_pro", "tenant": "tenant-a"}


def test_real_resolved_backedge_write_without_exact_pins_submits_no_job(
    session, monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import jobs as jobs_router

    session_id = session["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-write-without-pins", 60,
        tier="hosted_pro", subject=ALICE,
    )
    tool = {
        "name": "drape-onto-spheres",
        "description": "Drape arrays onto spheres",
        "capabilities": ["drawing.write"],
    }
    digest = deps.catalog_tool_digest(tool)
    submitted = []

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "find_tool", lambda *_args: tool)
    monkeypatch.setattr(
        jobs_router.entitlements, "entitlements_for",
        lambda _tier, *_roles: {"run_write": True},
    )
    monkeypatch.setattr(
        jobs_router.entitlements, "tool_required_capability",
        lambda _tool: "run_write",
    )
    monkeypatch.setattr(
        jobs_router, "_checkout_identity", lambda *_args: ("anonymous", None)
    )
    monkeypatch.setattr(
        jobs_router.jobs.platform_link,
        "resolve_submission_context",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        jobs_router.jobs, "submit_job",
        lambda *_args, **_kwargs: submitted.append(True) or "unexpected-job",
    )

    app = FastAPI()
    app.include_router(jobs_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier="restricted", backedge=True,
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/run",
        json={
            "tool": "drape-onto-spheres",
            "params": {},
            "dwg": "drawing-a",
            "catalog_digest": digest,
        },
        headers={
            "X-Authority-Session-Id": session_id,
            "X-Authority-Turn-Id": "turn-write-without-pins",
        },
    )

    assert response.status_code == 409, response.text
    assert "requires exact catalog and drawing pins" in response.text
    assert submitted == []


def test_real_run_route_refuses_missing_turn_before_catalog_lookup(
    session, monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import jobs as jobs_router

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(
        deps,
        "find_tool",
        lambda *_args: pytest.fail("unbound run reached catalog lookup"),
    )
    app = FastAPI()
    app.include_router(jobs_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", tier="restricted", backedge=True,
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/run",
        json={
            "tool": "drape-onto-spheres",
            "params": {},
            "dwg": "drawing-a",
            "catalog_digest": "a" * 64,
        },
    )

    assert response.status_code == 403
    assert "active same-account turn authority" in response.text


# --------------------------------------------------------------------------- #
# postgres parity — the SQL guards, not just the tuple comparison
# --------------------------------------------------------------------------- #
def test_every_production_caller_applies_the_staleness_bound():
    """Both callers, not just the elevation helper.

    The internal gate resolves the same subject for grant binding; dropping the
    bound there alone would leave a stale turn authorizing new grants.
    """
    import inspect

    from routers import agent as agent_router

    gate_src = inspect.getsource(agent_router.internal_gate)
    assert "active_turn_subject(" in gate_src
    assert "turn_runner.turn_max_s()" in gate_src, (
        "the internal gate resolves a subject without a staleness bound"
    )
    helper_src = inspect.getsource(deps.backedge_author_identity)
    assert "turn_max_s()" in helper_src


def test_postgres_subject_lookup_guards_session_turn_and_tenant():
    """The Postgres statement must carry the same three-way guard as SQLite.

    The suite above runs on SQLite, so without this a Postgres-only mutation
    (dropping `active_turn_id` from the WHERE clause) would leave every test
    green while letting a superseded tuple resolve the current subject.
    """
    import inspect

    sql = inspect.getsource(session_store._pg_active_turn_subject)
    assert "active_turn_subject" in sql
    # The whole conjunction, not three separate substrings: checking the parts
    # individually still passes when an AND is flipped to an OR.
    assert (
        "WHERE session_id = %s AND active_turn_id = %s AND tenant_id = %s"
        in " ".join(sql.split()).replace('" "', "")
    ), "postgres subject lookup lost or loosened its three-way guard"
    # and it must read the start time, or it cannot apply the staleness bound
    assert "turn_started_at" in sql
    assert "_turn_is_stale" in sql


def test_postgres_terminal_event_releases_the_subject():
    """SQLite clears the subject with the terminal event; Postgres must too, or
    the two authorities disagree and dual-write shadow comparison blocks the
    next turn."""
    import inspect

    sql = inspect.getsource(session_store._pg_append_event)
    terminal = " ".join(sql[sql.index("turn_complete"):].split()).replace('" "', "")
    assert "active_turn_subject = NULL" in terminal
    # and it must still only clear the turn it names
    assert "WHERE session_id = %s AND active_turn_id = %s" in terminal, (
        "the postgres terminal clear lost its active-turn guard"
    )


# --------------------------------------------------------------------------- #
# origin
# --------------------------------------------------------------------------- #
def test_the_active_resolver_leaves_a_backedge_identity_alone(monkeypatch):
    """No subject and no org means the resolver returns the context untouched,
    so it never looks up a platform binding for a caller that has no user."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    context = deps.TenantContext("tenant-a", tier="hosted_pro")

    assert deps.resolve_active_tenant_context(context) is context


def test_a_subjectless_jwt_cannot_borrow_the_turns_author(session):
    """Origin must be marked, never inferred from a missing subject.

    JWT validation does not require a `sub` claim, so require_tenant can return
    an authenticated context whose subject is None. Inferring "no subject means
    back edge" would let such a caller supply authority headers and author as
    whoever opened the turn.

    The turn below is REAL and resolvable, so this only passes because the
    caller is not marked as a back edge.
    """
    session_id = session["session_id"]
    session_store.try_begin_turn(session_id, "turn-1", 60, subject=ALICE)
    # The same tuple does elevate a genuine back edge, so the tuple is not why
    # this is refused.
    assert deps.backedge_author_identity(
        backedge(), session_id, "turn-1").subject == ALICE

    jwt_like = deps.TenantContext("tenant-a", org_id="tenant-a",
                                  tier="hosted_pro", workspace="/w")
    assert getattr(jwt_like, "backedge", False) is False
    assert deps.backedge_author_identity(
        jwt_like, session_id, "turn-1").subject is None


def test_only_the_dispatch_backedge_is_marked(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    assert deps.backedge_tenant("tenant-a").backedge is True
    assert deps.TenantContext("tenant-a").backedge is False


# --------------------------------------------------------------------------- #
# an unresolvable subject must not ride a shared grant
# --------------------------------------------------------------------------- #
def test_live_auth_refuses_unbound_grants(monkeypatch):
    """A stale, foreign, or pre-migration turn resolves no subject. The only key
    then available is the legacy unbound one, which any pre-existing shared
    grant matches, so it must be refused rather than reused."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    assert agent_gate._subject_bound_grants_required(None) is True
    assert agent_gate._subject_bound_grants_required(ALICE) is False


def test_auth_off_keeps_the_open_demo_behaviour(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert agent_gate._subject_bound_grants_required(None) is False
