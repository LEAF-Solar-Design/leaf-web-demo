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
    session_store.ensure_started()
    return session_store.get_or_create_session("tenant-a", "drawing-a")


def backedge(tenant_id="tenant-a"):
    """The identity a verified back-edge call resolves to: no subject."""
    return deps.TenantContext(tenant_id, tier="hosted_pro")


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
    # And without a resolvable subject the old shared key is unchanged.
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
# the raw claim is not the identity
# --------------------------------------------------------------------------- #
def test_the_binding_check_resolves_and_never_falls_back_to_the_claim():
    """_binding must resolve the active identity, with NO claim fallback.

    Resolution does more than translate an id: it also asserts the org is
    ACTIVE. Degrading to the presented claim on an authority failure would
    therefore ADMIT a request against an inactive org, so "the fallback can
    only refuse more" was wrong and the fallback is gone.

    Observed on staging before the fix: claim "acceptance-tenant-a-20260728"
    compared against active binding "bccb0d64-04c9-4108-bcc1-f27b8bb3924d".
    """
    import inspect

    import customization_service
    from routers import author as author_router

    src = inspect.getsource(customization_service._binding)
    assert "_active_context" in src, "_binding no longer resolves the identity"
    assert 'getattr(tenant, "org_id"' not in src, (
        "_binding still reads the unresolved claim for its org comparison"
    )
    assert "except Exception" not in src, (
        "a swallowed resolver failure would re-open the claim fallback"
    )
    # Resolution stays out of the route dependency so a disabled tenant keeps
    # its cheap 404 ahead of any authority lookup.
    assert "Depends(deps.require_tenant)" in inspect.getsource(author_router)


def test_authorization_and_mutation_target_the_same_tenant():
    """_binding returns the RESOLVED tenant, and stage mutates under it.

    Returning the raw claim would let authorization pass on the subject's
    current role in one tenant while the change set was written under the
    tenant named by a stale claim.
    """
    import inspect

    import customization_service

    binding_src = inspect.getsource(customization_service._binding)
    assert "TenantBinding(_tenant_id(active)" in binding_src, (
        "_binding hands back the raw claim instead of the resolved tenant"
    )
    stage_src = inspect.getsource(customization_service.CustomizationService.stage)
    assert "tenant_id = binding.tenant_id" in stage_src, (
        "stage mutates under the presented id rather than the authorized one"
    )
    # and the entitlement decision uses the resolved tenant's tier
    assert "resolve_tier(_active_context(tenant))" in stage_src


def test_the_active_resolver_leaves_a_backedge_identity_alone(monkeypatch):
    """The harness path must survive the swap: no subject and no org means the
    resolver returns the context untouched, so it never tries to look up a
    platform binding for a caller that has no user."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    context = deps.TenantContext("tenant-a", tier="hosted_pro")

    assert deps.resolve_active_tenant_context(context) is context
