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

SECRET = "test-checkout-capability-secret-32-bytes"
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


def test_auth_live_subjectless_callers_cannot_share_one_capability(monkeypatch):
    """A verified token without ``sub`` must not collapse every member of a
    tenant onto the anonymous binding used only when authentication is off."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    first = subject_ctx(None)
    second = subject_ctx(None)

    with pytest.raises(cc.CapabilityUnavailable, match="authenticated subject"):
        cc.mint(first, DRAWING, 3)

    # Verification must fail closed too, including for a token minted while
    # authentication was off before the posture changed.
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    anonymous = cc.mint(first, DRAWING, 3)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    with pytest.raises(cc.CapabilityUnavailable, match="authenticated subject"):
        cc.verify(anonymous, second, DRAWING, lock(fence=3))


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


def test_production_rejects_a_short_configured_secret(monkeypatch):
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "short-secret")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    with pytest.raises(cc.CapabilityUnavailable, match="at least 32 bytes"):
        cc.mint(TENANT, DRAWING, 1)


def test_production_accepts_a_32_byte_configured_secret(monkeypatch):
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "x" * 32)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    token = cc.mint(TENANT, DRAWING, 1)
    assert cc.verify(token, TENANT, DRAWING, lock(fence=1)) == ("sess-a", 1)


def test_a_dev_checkout_works_with_no_configuration(monkeypatch):
    """The other half: no env var off production must not break `docker compose
    up` or a fresh clone."""
    monkeypatch.delenv("LEAF_CHECKOUT_CAP_SECRET", raising=False)
    monkeypatch.setattr(cc, "_EPHEMERAL_SECRET", None)
    token = cc.mint(TENANT, DRAWING, 1)
    assert cc.verify(token, TENANT, DRAWING, lock(fence=1)) == ("sess-a", 1)


# --------------------------------------------------------------------------- #
# sol-critic r3 BLOCKER: the generation must come from the acquire that wrote it
#
# The acquire route used to write the lock, RELOAD the manifest, and mint against
# whatever generation the reload found. Those are not one operation. `ttl_s` only
# has to be positive, so a caller can take a lease that lapses immediately, be
# descheduled, and have a second session acquire in the gap — the reload then
# returns the SECOND session's generation and the first caller is minted a valid
# capability for it. The subject binding cannot catch this: verification
# recomputes the tag with the presenter's own subject, so a capability Alice
# minted for herself against Bob's generation verifies for Alice and returns
# Bob's holder.
# --------------------------------------------------------------------------- #
def test_a_capability_minted_against_a_later_lease_would_authorize_it():
    """The defect, stated as the property that must NOT hold.

    This is what the route did when it minted from a reloaded generation. It is
    pinned here so the binding is never mistaken for protection against it: the
    fix is that the route no longer LEARNS this generation, not that the token
    would somehow refuse it.
    """
    alice = subject_ctx("auth0|alice")
    bobs_lease = lock(fence=2, holder="bob-session")
    minted_against_bobs_generation = cc.mint(alice, DRAWING, 2)
    assert cc.verify(minted_against_bobs_generation, alice, DRAWING,
                     bobs_lease) == ("bob-session", 2)


def test_the_generation_the_acquire_returned_is_the_only_safe_one(tmp_path):
    """End to end over the real store: a lease that lapses before a second
    session acquires must not yield a capability that authorizes the second
    lease. Mirrors the route's fixed sequence — mint from the RETURNED
    generation, never from a re-read one."""
    da_dir = str(SERVER_DIR.parent / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store  # noqa: E402

    backend = store.InMemoryBackend()
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))

    alice = subject_ctx("auth0|alice")
    granted = store.acquire_checkout_fence(
        backend, TENANT, DRAWING, "alice-session", 1e-9,
        expected_fence=None, strict_owner=True)
    assert granted == 1

    # Alice is descheduled here; her lease has already lapsed.
    assert store.acquire_checkout_fence(
        backend, TENANT, DRAWING, "bob-session", 300,
        expected_fence=None, strict_owner=True) == 2
    co = store.load_manifest(backend, TENANT, DRAWING)["checkout"]
    assert co["holder"] == "bob-session"

    # The route mints from `granted`, so Alice gets a capability for a lease
    # that no longer exists — and it authorizes nothing.
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(cc.mint(alice, DRAWING, int(granted)), alice, DRAWING, co)

    # Re-reading instead would have handed her authority over Bob's lease.
    reloaded = int(co["fence"])
    assert reloaded != granted


def test_acquire_reports_its_own_generation_and_none_when_refused(tmp_path):
    da_dir = str(SERVER_DIR.parent / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store  # noqa: E402

    backend = store.InMemoryBackend()
    store.save_manifest(backend, TENANT, "d1", store._new_manifest(TENANT, "d1"))
    assert store.acquire_checkout_fence(backend, TENANT, "d1", "s1", 300) == 1
    # a live lease refuses a second holder, and reports it as None
    assert store.acquire_checkout_fence(backend, TENANT, "d1", "s2", 300) is None
    # the bool view every other caller uses is unchanged
    assert store.acquire_checkout(backend, TENANT, "d1", "s2", 300) is False
    assert store.acquire_checkout(backend, TENANT, "d1", "s1", 300) is True


# --------------------------------------------------------------------------- #
# sol-critic r3: a deployment that cannot mint must refuse BEFORE it locks
# --------------------------------------------------------------------------- #
def test_ensure_mintable_refuses_production_without_a_secret(monkeypatch):
    """Otherwise the route takes the lock, THEN fails to mint, and leaves an
    active lease nobody can prove — a misconfiguration that locks the drawing
    for its whole TTL."""
    monkeypatch.delenv("LEAF_CHECKOUT_CAP_SECRET", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    with pytest.raises(cc.CapabilityUnavailable):
        cc.ensure_mintable(TENANT, DRAWING)


def test_ensure_mintable_refuses_a_subjectless_caller_when_auth_is_live(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    with pytest.raises(cc.CapabilityUnavailable, match="authenticated subject"):
        cc.ensure_mintable(subject_ctx(None), DRAWING)


def test_ensure_mintable_passes_in_the_ordinary_posture():
    cc.ensure_mintable(subject_ctx("auth0|alice"), DRAWING)
    cc.ensure_mintable(TENANT, DRAWING)


def test_ensure_mintable_refuses_a_subject_mint_cannot_encode():
    """sol-critic r4. The first precheck re-listed the reasons minting fails
    (`_secret`, `_subject_of`) instead of trying it, and so missed that `mint`
    UTF-8 encodes the binding. A verified `sub` carrying an unpaired surrogate
    passed the check, then raised UnicodeEncodeError from inside `mint` — a 500
    with the lock already taken, which is the exact failure the precheck exists
    to prevent. It now trial-mints, so anything `mint` can raise is raised here.
    """
    lone_surrogate = subject_ctx("auth0|\ud800")
    with pytest.raises(cc.CapabilityUnavailable):
        cc.ensure_mintable(lone_surrogate, DRAWING)
    # and the underlying mint really would have failed, so the check is not
    # rejecting something that would otherwise have worked
    with pytest.raises(UnicodeEncodeError):
        cc.mint(lone_surrogate, DRAWING, 1)


# --------------------------------------------------------------------------- #
# sol-critic r4: pin the ROUTE's choice, not just the primitive's contract
#
# The store tests above prove `acquire_checkout_fence` reports the generation it
# stamped, and the unit tests prove a capability for a different generation is
# refused. Neither fails if the ROUTE goes back to minting from a reloaded
# manifest — which was the actual defect. This forces the two apart: the store
# reports one generation while the manifest holds another, exactly the state the
# race produces, and asserts which one the route signed.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def drawings_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("APS_LIVE", "0")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")

    from routers import drawings as drawings_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings_router.router)
    return TestClient(app, raise_server_exceptions=False)


def test_the_route_signs_the_granted_generation_not_the_current_one(
        drawings_client, monkeypatch):
    tenant = "route-fence-tenant"
    headers = {"X-Tenant-Id": tenant}

    first = drawings_client.post("/api/drawings/demo/checkout",
                                 json={"holder": "sess-a", "ttl_s": 300},
                                 headers=headers)
    assert first.status_code == 200, first.text
    granted_generation = 1

    sys.path.insert(0, str(SERVER_DIR.parent / "da"))
    import store  # noqa: E402
    from routers import drawings as drawings_router  # noqa: E402

    backend = drawings_router._backend(tenant)

    # A second session takes the drawing, moving the manifest to generation 2.
    # This is the state the race leaves behind: the generation THIS caller's
    # acquire stamped is no longer the one a reload finds.
    assert store.acquire_checkout_fence(
        backend, tenant, "demo", "sess-b", 300,
        expected_fence=granted_generation, strict_owner=True) == 2

    # Our acquire reports the generation it wrote, as the fixed primitive does.
    monkeypatch.setattr(
        store, "acquire_checkout_fence",
        lambda *a, **k: granted_generation, raising=True)

    second = drawings_client.post("/api/drawings/demo/checkout",
                                  json={"holder": "sess-a", "ttl_s": 300},
                                  headers=headers)
    assert second.status_code == 200, second.text
    issued = second.json()["checkout_capability"]

    co = store.load_manifest(backend, tenant, "demo")["checkout"]
    assert int(co["fence"]) != granted_generation, (
        "fixture failed to separate the granted generation from the current one")

    # The route must have signed the GRANTED generation. Minting from the
    # reloaded manifest instead would make this capability verify against the
    # current lock — which is the bypass.
    with pytest.raises(cc.CapabilityRejected):
        cc.verify(issued, tenant, "demo", co)
    assert issued == cc.mint(tenant, "demo", granted_generation)


def test_concurrent_legacy_acquires_cannot_share_one_generation(monkeypatch):
    """sol-critic r4. The legacy manifest is a load-edit-save with nothing held
    in between, so two concurrent acquires of a FREE drawing both read
    generation N, both compute N+1, and the second save wins. Two callers are
    each told they took the lock, ONE lease persists, and both capabilities
    verify against it because the generations are equal — the bypass the fence
    exists to prevent. Only one acquire may win, and the winner's generation
    must be unique.
    """
    import threading
    import time

    da_dir = str(SERVER_DIR.parent / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store  # noqa: E402

    backend = store.InMemoryBackend()
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))

    # The window is between the load and the save. In memory those are adjacent
    # with no yield point, so a plain thread race almost never lands on it — a
    # test built that way passes with the serialization REMOVED and proves
    # nothing. Force the interleaving instead: the first loader announces itself
    # and then holds its snapshot briefly, and the second waits for that
    # announcement before loading. Without serialization both threads therefore
    # certainly hold generation N and both certainly write N+1. With it, the
    # second thread cannot reach the load until the first has saved.
    real_load = store.load_manifest
    first_has_loaded = threading.Event()

    def interleaved_load(*a, **k):
        m = real_load(*a, **k)
        if not first_has_loaded.is_set():
            first_has_loaded.set()
            time.sleep(0.25)        # hold the snapshot, inviting the collision
        else:
            first_has_loaded.wait(timeout=5)
        return m

    monkeypatch.setattr(store, "load_manifest", interleaved_load)
    granted: list[int] = []
    granted_guard = threading.Lock()

    def acquire(n):
        fence = store.acquire_checkout_fence(
            backend, TENANT, DRAWING, f"sess-{n}", 300,
            expected_fence=None, strict_owner=True)
        if fence is not None:
            with granted_guard:
                granted.append(fence)

    threads = [threading.Thread(target=acquire, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one caller may be told it holds the free drawing, and no two
    # callers may ever be handed the same generation.
    assert len(granted) == 1, f"{len(granted)} callers were granted the same lock"
    assert len(set(granted)) == len(granted)
    persisted = store.load_manifest(backend, TENANT, DRAWING)["checkout"]
    assert int(persisted["fence"]) == granted[0]


def test_a_deployment_that_cannot_mint_leaves_no_lock_behind(
        drawings_client, monkeypatch):
    """sol-critic r3 gap: the `ensure_mintable` unit tests pin the helper, but
    none of them fails if the ROUTE stops calling it. This drives the route in
    the posture that used to leave a lock nobody could prove — production with
    no signing secret — and asserts the two things that matter: the caller is
    told it is an operator fault, and the drawing is still FREE afterwards.
    """
    da_dir = str(SERVER_DIR.parent / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store  # noqa: E402
    from routers import drawings as drawings_router  # noqa: E402

    tenant = "no-mint-tenant"
    monkeypatch.delenv("LEAF_CHECKOUT_CAP_SECRET", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")

    res = drawings_client.post("/api/drawings/demo/checkout",
                               json={"holder": "sess-a", "ttl_s": 300},
                               headers={"X-Tenant-Id": tenant})
    assert res.status_code == 503, res.text

    # THE point: no lease was taken. Acquiring after the misconfiguration is
    # fixed must still be possible, rather than blocked for the whole TTL by a
    # lock whose capability was never issued.
    backend = drawings_router._backend(tenant)
    co = store.load_manifest(backend, tenant, "demo").get("checkout")
    assert not store.checkout_active(co), (
        "the route took the lock before discovering it could not mint")


def test_concurrent_legacy_release_and_acquire_cannot_repeat_a_generation(
        monkeypatch):
    """sol-critic r3 gap: the acquire-side interleaving test still passes if only
    the RELEASE lock is removed. A release that loads a snapshot, then saves it
    after a concurrent acquire has written the next generation, loses that
    write — and a repeated generation lets a capability from the earlier lease
    verify against the later one.
    """
    import threading
    import time

    da_dir = str(SERVER_DIR.parent / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store  # noqa: E402

    backend = store.InMemoryBackend()
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    first = store.acquire_checkout_fence(backend, TENANT, DRAWING, "sess-a",
                                         0.05, expected_fence=None,
                                         strict_owner=True)
    assert first == 1
    time.sleep(0.1)                      # the lease lapses; release is now free

    real_load = store.load_manifest
    releaser_loaded = threading.Event()

    def interleaved_load(*a, **k):
        m = real_load(*a, **k)
        if not releaser_loaded.is_set():
            releaser_loaded.set()
            time.sleep(0.25)             # releaser holds its stale snapshot
        else:
            releaser_loaded.wait(timeout=5)
        return m

    monkeypatch.setattr(store, "load_manifest", interleaved_load)
    granted: list[int] = []

    def release():
        store.release_checkout(backend, TENANT, DRAWING, holder="sess-a")

    def acquire():
        fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "sess-b",
                                             300, expected_fence=None,
                                             strict_owner=True)
        if fence is not None:
            granted.append(fence)

    threads = [threading.Thread(target=release), threading.Thread(target=acquire)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Whatever order they land in, the generation counter must never go
    # backwards: a later acquire can never be handed a generation already used.
    assert granted, "the second session was never granted the lapsed lock"
    assert granted[0] > first, (
        f"generation {granted[0]} repeats or precedes {first}: a capability "
        f"from the earlier lease would verify against the later one")
    m = store.load_manifest(backend, TENANT, DRAWING)
    assert int(m.get("checkout_fence") or 0) >= granted[0]
