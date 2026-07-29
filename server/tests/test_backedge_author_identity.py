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
