"""
Unit acceptance for server/checkout_capability.py — the opaque proof of a
drawing checkout.

The HTTP suites (tests/test_hardening_3b.py, tests/test_write_loop.py) drive the
routes end to end, but they run with LEAF_AUTH_LIVE=0, where every caller on a
tenant is the same anonymous identity. The SUBJECT binding — the property that
stops one authenticated member of a workspace using a colleague's capability —
only exists when auth is live, so it is pinned here against `deps.TenantContext`
directly rather than left untested.

Run:  cd server && python -m pytest tests/test_checkout_capability.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import checkout_capability as cc  # noqa: E402
import deps  # noqa: E402

SECRET = "test-checkout-capability-secret"
TENANT = "acme"
DRAWING = "demo"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", SECRET)
    monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    yield


def lock(fence=1, holder="sess-a", expires="2999-01-01T00:00:00+00:00"):
    """A manifest checkout dict in the shape `load_manifest` returns."""
    return {"holder": holder, "acquired": "2026-01-01T00:00:00+00:00",
            "expires": expires, "fence": fence}


def subject_ctx(subject):
    return deps.TenantContext(TENANT, subject=subject)


# --------------------------------------------------------------------------- #
# the token itself
# --------------------------------------------------------------------------- #
def test_the_token_carries_no_readable_payload():
    """Opaque, not merely signed: there is nothing in it to decode, edit and
    re-sign, and nothing that discloses the holder, subject or generation."""
    token = cc.mint(TENANT, DRAWING, 7)
    scheme, _, tag = token.partition(".")
    assert scheme == "lco1"
    assert len(tag) == 64 and int(tag, 16) >= 0        # a bare hex MAC, no payload
    for leak in (TENANT, DRAWING, "sess-a"):
        assert leak not in tag
    # and a one-field change rewrites the whole tag, so nothing about it is
    # positional or editable.
    assert cc.mint(TENANT, DRAWING, 8) != token
    assert cc.mint(TENANT, DRAWING + "x", 7) != token


def test_a_valid_capability_returns_the_lock_s_own_identity():
    """The route acts on what the MANIFEST says, never on what the caller sent."""
    co = lock(fence=4, holder="sess-a")
    holder, fence = cc.verify(cc.mint(TENANT, DRAWING, 4), TENANT, DRAWING, co)
    assert (holder, fence) == ("sess-a", 4)


# --------------------------------------------------------------------------- #
# what must NOT verify
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("presented", [None, "", "   ", "not-a-token",
                                       "lco1." + "0" * 64, "lco1.short"])
def test_absent_and_forged_capabilities_are_refused(presented):
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(presented, TENANT, DRAWING, lock(fence=1))


def test_a_capability_for_another_generation_is_refused():
    """Fencing: re-acquiring bumps the generation, and everything minted for the
    previous lease — including the same holder's own token — stops verifying."""
    stale = cc.mint(TENANT, DRAWING, 1)
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(stale, TENANT, DRAWING, lock(fence=2))


def test_a_capability_for_another_drawing_or_tenant_is_refused():
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(cc.mint(TENANT, "other-drawing", 1), TENANT, DRAWING, lock())
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(cc.mint("other-tenant", DRAWING, 1), TENANT, DRAWING, lock())


def test_a_colleagues_capability_is_refused_when_auth_is_live():
    """THE reason the subject is bound. Two members of ONE workspace pass every
    tenant check, so without this a capability leaked to a colleague — or lifted
    from a shared log — would be a working credential for someone else's lease."""
    alice, bob = subject_ctx("auth0|alice"), subject_ctx("auth0|bob")
    alices = cc.mint(alice, DRAWING, 3)

    holder, fence = cc.verify(alices, alice, DRAWING, lock(fence=3))
    assert (holder, fence) == ("sess-a", 3)
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(alices, bob, DRAWING, lock(fence=3))


def test_an_unfenced_lock_cannot_be_proven():
    """A lock taken before generations were stamped. Nothing can verify against
    it, and inventing a generation would make every capability for the drawing
    interchangeable, so it is refused until the lease expires."""
    co = lock()
    co.pop("fence")
    with pytest.raises(cc.CapabilityRejected, match="expire"):
        cc.verify(cc.mint(TENANT, DRAWING, 1), TENANT, DRAWING, co)


def test_a_rotated_secret_invalidates_outstanding_capabilities(monkeypatch):
    """Rotating the signing key must revoke, not silently keep honouring."""
    issued = cc.mint(TENANT, DRAWING, 1)
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "a-different-secret")
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(issued, TENANT, DRAWING, lock(fence=1))


# --------------------------------------------------------------------------- #
# operator posture
# --------------------------------------------------------------------------- #
def test_production_without_a_configured_secret_fails_closed(monkeypatch):
    """A per-process random secret is fine for a dev checkout and wrong for
    production: a second replica could not verify what the first minted, which
    would surface as sporadic 403s on legitimate writes. Fail loudly instead, and
    as an OPERATOR fault (503 at the route) rather than an authorization one."""
    monkeypatch.delenv("LEAF_CHECKOUT_CAP_SECRET", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    with pytest.raises(cc.CapabilityUnavailable, match="LEAF_CHECKOUT_CAP_SECRET"):
        cc.mint(TENANT, DRAWING, 1)


def test_a_dev_checkout_works_with_no_configuration(monkeypatch):
    """The other half: no env var off production must not break `docker compose
    up` or a fresh clone."""
    monkeypatch.delenv("LEAF_CHECKOUT_CAP_SECRET", raising=False)
    monkeypatch.setattr(cc, "_EPHEMERAL_SECRET", None)
    token = cc.mint(TENANT, DRAWING, 1)
    assert cc.verify(token, TENANT, DRAWING, lock(fence=1)) == ("sess-a", 1)
