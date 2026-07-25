"""da/test_store.py — OFFLINE gate for the persistent versioned drawing store.

Every test runs against the in-memory backend and makes ZERO network/APS calls.
An autouse fixture (a) stubs the identity/token helpers so no call needs creds and
(b) replaces client.requests with a fake that RAISES on any real network use — so
if any exercised code path tried to reach APS, the suite would fail loudly.

Run:  cd C:/tmp/leaf-web-demo && python -m pytest da/test_store.py -q
"""
import inspect
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
import store  # noqa: E402

DWG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "rooftop_demo.dwg")
VERSION_KEY_RE = re.compile(r"^tenants/[a-z0-9_-]+/drawings/[a-z0-9_-]+/v/\d{8}\.dwg$")

# Six tests below never reach the network, but they still build a signed client,
# so client._load_creds() runs and needs SOME credential source. It does not need
# a REAL one: _load_creds only rejects missing/PASTE_ME values, and the only thing
# derived from the id is bucket_key()'s suffix, which these tests assert
# structurally (prefix / self-consistency), never by value.
#
# So the fixture below injects a dummy credential instead of skipping. That keeps
# all 15 tests executing on a clean CI runner, and it also pins the operator box
# to the SAME inputs -- previously ~/.aps/credentials.json silently fed real
# values in here, so the suite behaved differently depending on the host.
_DUMMY_CREDS = json.dumps({"client_id": "testclientid0000", "client_secret": "testsecret"})


# --------------------------------------------------------------------------- #
# Zero-network guardrails
# --------------------------------------------------------------------------- #
class _NoNetwork:
    """Stand-in for the requests module: any real HTTP verb -> hard failure."""

    def _boom(self, *a, **k):
        raise AssertionError("offline test attempted a real network call")

    get = post = put = delete = request = _boom


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # identity/token helpers must not need creds or the network in an offline test
    monkeypatch.setattr(client, "auth_token", lambda: "offline-faketoken", raising=True)
    monkeypatch.setattr(client, "nickname", lambda: "TESTOWNER", raising=True)
    # any code path that still tries requests.get/post/put fails the test
    monkeypatch.setattr(client, "requests", _NoNetwork(), raising=True)
    # deterministic dummy creds everywhere: satisfies _load_creds() without a real
    # secret, and blanks CRED_PATH so a host credential file cannot leak in.
    monkeypatch.setenv("APS_CREDENTIALS_JSON", _DUMMY_CREDS)
    monkeypatch.setattr(client, "CRED_PATH", str(Path(__file__).parent / "no-such-creds.json"),
                        raising=True)
    yield


# --------------------------------------------------------------------------- #
# Bucket policy
# --------------------------------------------------------------------------- #
def test_create_bucket_default_policy_is_persistent(monkeypatch):
    # 1) the signature default itself is "persistent"
    assert inspect.signature(client.create_bucket).parameters["policy"].default == "persistent"

    # 2) the POST body it builds carries policyKey == "persistent" (no live call)
    captured = {}

    class _Resp:
        status_code = 409  # short-circuits create_bucket to the "already exists" branch

        def raise_for_status(self):  # pragma: no cover - not hit on 409
            raise AssertionError

        def json(self):  # pragma: no cover
            return {}

    class _Cap:
        def post(self, url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["body"] = json.loads(data)
            return _Resp()

        get = put = None

    monkeypatch.setattr(client, "requests", _Cap(), raising=True)
    res = client.create_bucket()  # default policy
    assert res.get("existed") is True
    assert captured["body"]["policyKey"] == "persistent"
    assert captured["body"]["bucketKey"] == client.bucket_key()
    assert captured["url"].endswith("/oss/v2/buckets")
    # store bucket uses the fresh persistent stem, not the abandoned transient one
    assert client.bucket_key().startswith("leaf-web-store-")


def test_workitem_scratch_bucket_is_transient(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 409

        def raise_for_status(self):  # pragma: no cover
            raise AssertionError

    class _Cap:
        def post(self, url, headers=None, data=None, timeout=None):
            captured["body"] = json.loads(data)
            return _Resp()

    monkeypatch.setattr(client, "requests", _Cap(), raising=True)
    client._ensure_scratch_bucket()

    assert captured["body"] == {
        "bucketKey": client.scratch_bucket_key(),
        "policyKey": "transient",
    }
    assert client.scratch_bucket_key().endswith("-scratch")


# --------------------------------------------------------------------------- #
# Key scheme
# --------------------------------------------------------------------------- #
def test_version_keys_distinct_deterministic_and_regex():
    # F13: ids are now REJECT-don't-collapse, so the store no longer normalises
    # `Tenant_1`/`Draw-A` — a caller must pass already-canonical ids. (The collision
    # this closes is proven in da/test_hardening_1f.py.)
    t, d = "tenant-1", "draw-a"
    k1 = store.drawing_version_key(t, d, 1)
    k2 = store.drawing_version_key(t, d, 2)
    assert k1 != k2
    assert VERSION_KEY_RE.match(k1), k1
    assert VERSION_KEY_RE.match(k2), k2
    # deterministic
    assert store.drawing_version_key(t, d, 1) == k1
    # sanitized, zero-padded to 8 digits
    assert k1 == "tenants/tenant-1/drawings/draw-a/v/00000001.dwg"
    assert k2 == "tenants/tenant-1/drawings/draw-a/v/00000002.dwg"


def test_sanitize_id_rejects_empty():
    with pytest.raises(ValueError):
        store.sanitize_id("!!!")


# --------------------------------------------------------------------------- #
# Immutability + version chain
# --------------------------------------------------------------------------- #
def _tmpfile(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_immutable_versions_and_manifest_chain(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"DWG-BYTES-V1")
    b = _tmpfile(tmp_path, "v2.dwg", b"DWG-BYTES-V2-different")

    ing = store.ingest_drawing(be, "acme-corp", a)
    did = ing["drawing_id"]
    assert ing["version"] == 1

    v2 = store.put_drawing(be, "acme-corp", did, b, parent_version=1,
                           meta={"tool": "add-panel-row", "workitem_id": "wi-123"})
    assert v2 == 2

    k1 = store.drawing_version_key("acme-corp", did, 1)
    k2 = store.drawing_version_key("acme-corp", did, 2)
    # both version objects still resolve; v1 was NOT overwritten
    assert be.exists(k1) and be.exists(k2)
    assert be.get(k1) == b"DWG-BYTES-V1"
    assert be.get(k2) == b"DWG-BYTES-V2-different"

    m = store.load_manifest(be, "acme-corp", did)
    assert m["head"] == 2 and m["latest"] == 2
    by_v = {e["v"]: e for e in m["versions"]}
    assert by_v[1]["parent"] is None
    assert by_v[2]["parent"] == 1
    assert by_v[2]["tool"] == "add-panel-row"
    assert by_v[2]["workitem_id"] == "wi-123"
    # sha256 recorded and correct
    import hashlib
    assert by_v[1]["sha256"] == hashlib.sha256(b"DWG-BYTES-V1").hexdigest()


def test_ingest_refuses_to_clobber_existing_drawing(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"X")
    store.ingest_drawing(be, "t", a, drawing_id="fixed-id")
    with pytest.raises(ValueError):
        store.ingest_drawing(be, "t", a, drawing_id="fixed-id")


# --------------------------------------------------------------------------- #
# resolve + undo/redo
# --------------------------------------------------------------------------- #
def test_resolve_head_undo_keeps_latest_and_object(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    b = _tmpfile(tmp_path, "v2.dwg", b"V2")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    store.put_drawing(be, "t", did, b, parent_version=1)

    v, key = store.resolve_version(be, "t", did, "head")
    assert v == 2 and key == store.drawing_version_key("t", did, 2)

    # undo repoints head to 1; latest stays 2; the v2 object still exists (redo-able)
    assert store.undo(be, "t", did) == 1
    v, key = store.resolve_version(be, "t", did, "head")
    assert v == 1 and key == store.drawing_version_key("t", did, 1)

    m = store.load_manifest(be, "t", did)
    assert m["head"] == 1 and m["latest"] == 2
    assert be.exists(store.drawing_version_key("t", did, 2))
    # explicit resolve of v2 still works (object present) => redo is possible
    v2, _ = store.resolve_version(be, "t", did, 2)
    assert v2 == 2


def test_undo_at_root_raises(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    with pytest.raises(ValueError):
        store.undo(be, "t", did)


def test_resolve_unknown_version_raises(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    with pytest.raises(ValueError):
        store.resolve_version(be, "t", did, 9)


# --------------------------------------------------------------------------- #
# Checkout lock
# --------------------------------------------------------------------------- #
def test_checkout_lock_blocks_second_holder_and_frees_on_release(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    # same holder can refresh
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    # a different holder is blocked while the lock is active
    assert store.acquire_checkout(be, "t", did, holder="s2", ttl_s=300) is False
    # after release, a re-acquire by the other holder succeeds
    assert store.release_checkout(be, "t", did, holder="s1") is True
    assert store.acquire_checkout(be, "t", did, holder="s2", ttl_s=300) is True


def test_checkout_expired_lock_is_free(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    # force the lock to look expired (deterministic — no sleep, no wall-clock race)
    m = store.load_manifest(be, "t", did)
    m["checkout"]["expires"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.save_manifest(be, "t", did, m)

    # a DIFFERENT holder can now take it because the prior lock is past its TTL
    assert store.acquire_checkout(be, "t", did, holder="s2", ttl_s=300) is True
    m2 = store.load_manifest(be, "t", did)
    assert m2["checkout"]["holder"] == "s2"


# --------------------------------------------------------------------------- #
# Checkout AUTHORIZATION of a write (put_drawing holder/fence)
#
# acquire_checkout has always refused a second holder, but put_drawing never
# asked who was calling: it published whatever version it was handed as long as
# the manifest was readable. So the lock stopped a second session from TAKING the
# lock and did nothing to stop it from WRITING. These pin the caller check.
# --------------------------------------------------------------------------- #
def test_put_drawing_refuses_a_writer_that_does_not_hold_the_checkout(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    b = _tmpfile(tmp_path, "v2.dwg", b"V2")

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    with pytest.raises(store.CheckoutDenied):
        store.put_drawing(be, "t", did, b, parent_version=1, holder="s2")
    # refused means NOTHING was published: still one version, head still v1
    m = store.load_manifest(be, "t", did)
    assert m["head"] == 1 and m["latest"] == 1
    assert [v["v"] for v in m["versions"]] == [1]


def test_put_drawing_allows_the_holder_and_leaves_the_lock_intact(tmp_path):
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    b = _tmpfile(tmp_path, "v2.dwg", b"V2")

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    assert store.put_drawing(be, "t", did, b, parent_version=1, holder="s1") == 2
    m = store.load_manifest(be, "t", did)
    assert m["head"] == 2
    assert m["checkout"]["holder"] == "s1"   # writing does not consume the lock


def test_put_drawing_unnamed_caller_keeps_the_pre_existing_contract(tmp_path):
    """holder=None is the ingest/harness/test path and must stay permitted, or
    every existing caller of this primitive breaks. The product write path never
    reaches here unnamed: POST /api/run defaults an absent holder to the tenant
    id (server/routers/jobs.py), so a real request always carries an identity."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    b = _tmpfile(tmp_path, "v2.dwg", b"V2")

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    assert store.put_drawing(be, "t", did, b, parent_version=1) == 2


def test_put_drawing_allowed_when_no_lock_or_an_expired_one(tmp_path):
    """An unlocked drawing publishes as before, and an EXPIRED lock is free —
    the same rule acquire_checkout applies. Without this a forgotten lock would
    wedge the drawing for its whole TTL."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    # (a) no lock at all
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                             parent_version=1, holder="s2") == 2

    # (b) someone else's lock, forced past its TTL (deterministic, no sleep)
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    m = store.load_manifest(be, "t", did)
    m["checkout"]["expires"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.save_manifest(be, "t", did, m)
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v3.dwg", b"V3"),
                             parent_version=2, holder="s2") == 3


def test_authorize_checkout_preflight_matches_the_commit_decision(tmp_path):
    """The pre-flight run_write_live uses before spending APS engine seconds must
    agree with what put_drawing would decide at commit, or a caller pays for a
    WorkItem whose result is then refused."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    store.authorize_checkout(be, "t", did, "s1")       # holder: allowed
    store.authorize_checkout(be, "t", did, None)       # unnamed: not this check's job
    with pytest.raises(store.CheckoutDenied):
        store.authorize_checkout(be, "t", did, "s2")


def test_unnamed_writer_cannot_ride_a_lock_taken_with_the_default_holder(tmp_path):
    """sol-critic BLOCKER, PR #141. The route used to default an absent holder to
    the TENANT id, which looked fail-closed only because the lock was assumed to
    be `sess-` shaped. POST .../checkout defaults its holder to the tenant id
    too, so a drawing locked with the documented empty body is held by the tenant
    id — and the unnamed writer matched it exactly. Reproduced before the fix:
    session B published v2 under session A's lease without naming anything."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "acme", a)["drawing_id"]

    # A takes the lock with the empty body -> holder IS the tenant id
    assert store.acquire_checkout(be, "acme", did, holder="acme", ttl_s=300) is True
    assert store.load_manifest(be, "acme", did)["checkout"]["holder"] == "acme"

    # B names nobody. The route now sends the reserved anonymous id, not "acme".
    with pytest.raises(store.CheckoutDenied):
        store.put_drawing(be, "acme", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                          parent_version=1, holder=store.ANONYMOUS_HOLDER)
    m = store.load_manifest(be, "acme", did)
    assert m["head"] == 1 and m["latest"] == 1


def test_anonymous_holder_is_reserved_and_cannot_take_a_checkout(tmp_path):
    """The sentinel is only unforgeable while nothing can hold it. `holder` is
    caller-supplied on POST .../checkout, so without this refusal a caller could
    take the lock AS the anonymous id and every unnamed write would match it —
    turning the fail-closed default straight back into a fail-open one."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    with pytest.raises(ValueError, match="reserved"):
        store.acquire_checkout(be, "t", did, holder=store.ANONYMOUS_HOLDER, ttl_s=300)
    assert store.load_manifest(be, "t", did).get("checkout") is None


def test_anonymous_writer_refused_against_a_PERSISTED_sentinel_lock(tmp_path):
    """sol-critic r2 BLOCKER. acquire_checkout's refusal only guards NEW
    acquisitions. A lock taken as the sentinel under an EARLIER release — or
    restored from a backup, or written straight into a manifest — is already
    persisted, and comparing it to the anonymous caller matched EQUAL and let the
    write through. Exploiting it needs no knowledge of another holder and no
    acquisition after deploy, only pre-existing state.

    Seeded directly here, bypassing acquire_checkout, because that is exactly the
    path the acquire-time refusal cannot see."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    now = datetime.now(timezone.utc)
    m = store.load_manifest(be, "t", did)
    m["checkout"] = {
        "holder": store.ANONYMOUS_HOLDER,          # pre-rule persisted state
        "acquired": now.isoformat(),
        "expires": (now + timedelta(seconds=600)).isoformat(),
    }
    store.save_manifest(be, "t", did, m)

    with pytest.raises(store.CheckoutDenied, match="names no session"):
        store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                          parent_version=1, holder=store.ANONYMOUS_HOLDER)
    m2 = store.load_manifest(be, "t", did)
    assert m2["head"] == 1 and m2["latest"] == 1
    assert [v["v"] for v in m2["versions"]] == [1]


def test_a_stale_fence_is_refused_even_when_the_caller_names_no_holder(tmp_path):
    """sol-critic r4 residual. `holder` and `fence` are INDEPENDENT claims.
    Returning early on `holder is None` skipped the fence check too, so a caller
    naming no session but presenting a stale fence passed the pre-flight and was
    refused only at the commit — _pg_put checks any supplied fence regardless of
    holder. A pre-flight that permits what the commit refuses is the one thing it
    must never do, since its whole job is to refuse before the APS bill."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    # legacy locks carry no fence, so seed one that does (postgres-shaped)
    now = datetime.now(timezone.utc)
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    m = store.load_manifest(be, "t", did)
    m["checkout"]["fence"] = 2
    store.save_manifest(be, "t", did, m)

    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.authorize_checkout(be, "t", did, None, fence=1)
    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                          parent_version=1, holder=None, fence=1)
    # the CURRENT generation passes both, and naming nothing at all still bypasses
    store.authorize_checkout(be, "t", did, None, fence=2)
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                             parent_version=1, holder=None, fence=2) == 2
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v3.dwg", b"V3"),
                             parent_version=2) == 3


def test_reserved_holder_and_bad_ttl_raise_the_NARROW_param_error(tmp_path):
    """sol-critic r3 MINOR. The route maps this to 400, so it must not be a bare
    ValueError: acquire_checkout also decodes the stored manifest, and a corrupt
    one raises JSONDecodeError (itself a ValueError). A broad catch would blame
    the caller for damaged storage. CheckoutParamError still subclasses
    ValueError, so existing (KeyError, ValueError) callers are unaffected."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    with pytest.raises(store.CheckoutParamError):
        store.acquire_checkout(be, "t", did, holder=store.ANONYMOUS_HOLDER, ttl_s=300)
    assert issubclass(store.CheckoutParamError, ValueError)

    # a CORRUPT manifest is a storage fault, NOT a checkout-parameter fault
    be.put(store.manifest_key("t", did), b"{not json")
    with pytest.raises(ValueError) as caught:
        store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300)
    assert not isinstance(caught.value, store.CheckoutParamError)


def test_anonymous_writer_still_publishes_on_an_unlocked_drawing(tmp_path):
    """Fail-closed must not become fail-shut: an unnamed write to a drawing
    nobody has locked is the ordinary case and must keep working."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                             parent_version=1,
                             holder=store.ANONYMOUS_HOLDER) == 2


def test_legacy_locks_now_carry_a_fence_and_a_stale_one_is_refused(tmp_path):
    """The legacy authority stamps a generation too, so the checkout capability
    rotates under BOTH authorities. The current generation publishes; a stale one
    is refused even though the holder id matches."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    fence = store.load_manifest(be, "t", did)["checkout"]["fence"]
    assert isinstance(fence, int)

    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                          parent_version=1, holder="s1", fence=fence + 1)
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                             parent_version=1, holder="s1", fence=fence) == 2


def test_legacy_fence_survives_release_so_a_generation_is_never_reused(tmp_path):
    """The generation counter lives on the MANIFEST, not inside the checkout dict
    that release clears. A counter that restarted at 1 on the next acquire would
    let a capability minted for the earlier lease verify against the later one."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    seen = []
    for holder in ("s1", "s2", "s3"):
        assert store.acquire_checkout(be, "t", did, holder=holder, ttl_s=300) is True
        seen.append(store.load_manifest(be, "t", did)["checkout"]["fence"])
        assert store.release_checkout(be, "t", did, holder=holder) is True

    assert seen == sorted(set(seen)), f"generations repeated or went backwards: {seen}"


def test_strict_owner_acquire_refuses_a_live_lease_without_the_generation(tmp_path):
    """The refresh path is how the readable holder id leaked authority: session B
    reads A's holder from GET /versions, re-acquires as A, and the store called it
    a refresh. Under strict_owner the holder label is not consulted at all — only
    the generation, which B cannot read from any public surface."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300,
                                  strict_owner=True) is True
    fence = store.load_manifest(be, "t", did)["checkout"]["fence"]

    # B knows the holder id and still cannot refresh: no generation, wrong generation.
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300,
                                  strict_owner=True) is False
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300,
                                  strict_owner=True,
                                  expected_fence=fence + 1) is False
    # the real owner refreshes, and the generation advances
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300,
                                  strict_owner=True, expected_fence=fence) is True
    assert store.load_manifest(be, "t", did)["checkout"]["fence"] > fence


def test_strict_owner_acquire_still_grants_a_free_or_expired_lock(tmp_path):
    """strict_owner tightens who may take over a LIVE lease, nothing else: an
    unlocked drawing and an expired lock stay freely acquirable, or a forgotten
    lock would wedge the drawing forever."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=0.05,
                                  strict_owner=True) is True
    time.sleep(0.2)
    assert store.acquire_checkout(be, "t", did, holder="s2", ttl_s=300,
                                  strict_owner=True) is True
    assert store.load_manifest(be, "t", did)["checkout"]["holder"] == "s2"


def test_release_by_generation_refuses_a_lease_that_moved_on(tmp_path):
    """A release that carries a proven generation must not land on a DIFFERENT
    lease: between the capability check and the release, the old lease can expire
    and someone else can take the lock."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    stale = store.load_manifest(be, "t", did)["checkout"]["fence"]
    assert store.release_checkout(be, "t", did, holder="s1") is True
    assert store.acquire_checkout(be, "t", did, holder="s2", ttl_s=300) is True

    assert store.release_checkout(be, "t", did, expected_fence=stale) is False
    assert store.load_manifest(be, "t", did)["checkout"]["holder"] == "s2"
    current = store.load_manifest(be, "t", did)["checkout"]["fence"]
    assert store.release_checkout(be, "t", did, expected_fence=current) is True


def test_undo_and_redo_refuse_a_caller_that_does_not_hold_the_lock(tmp_path):
    """undo/redo move head, which every other session reads, so they are subject
    to the SAME single-writer rule as a publish. Without it they were the way
    around it: a caller refused at put_drawing could still walk head backwards."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=300) is True
    fence = store.load_manifest(be, "t", did)["checkout"]["fence"]
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                             parent_version=1, holder="s1", fence=fence) == 2

    with pytest.raises(store.CheckoutDenied, match="may not undo"):
        store.undo(be, "t", did, holder="s2")
    with pytest.raises(store.CheckoutDenied, match="names no session"):
        store.undo(be, "t", did, holder=store.ANONYMOUS_HOLDER)
    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.undo(be, "t", did, holder="s1", fence=fence + 1)
    assert store.load_manifest(be, "t", did)["head"] == 2   # nothing moved

    assert store.undo(be, "t", did, holder="s1", fence=fence) == 1
    with pytest.raises(store.CheckoutDenied, match="may not redo"):
        store.redo(be, "t", did, holder="s2")
    assert store.redo(be, "t", did, holder="s1", fence=fence) == 2


def test_pg_row_authorization_matches_the_legacy_rule(tmp_path):
    """The postgres branches of put/undo/redo authorize against a `FOR UPDATE`
    manifest ROW, not a manifest dict, and those branches only execute with a
    live database (their suite skips without one). The predicate itself needs no
    database, so it is pinned here — otherwise the rule that guards the postgres
    authority would be covered on the legacy authority alone."""
    now = store.datetime.now(store.timezone.utc)
    live = {"checkout_holder": "s1", "checkout_fence": 4,
            "checkout_expires_at": now + timedelta(seconds=300)}
    lapsed = {**live, "checkout_expires_at": now - timedelta(seconds=1)}
    free = {"checkout_holder": None, "checkout_fence": 4,
            "checkout_expires_at": None}

    # permitted: caller names nobody at all, no lock, an expired lock, the owner
    store._authorize_checkout_row(live, None, None, "publish a version")
    store._authorize_checkout_row(free, "s2", None, "publish a version")
    store._authorize_checkout_row(lapsed, "s2", None, "publish a version")
    store._authorize_checkout_row(live, "s1", 4, "publish a version")

    # refused: another holder, the reserved sentinel, a stale generation
    for holder, fence in (("s2", None), (store.ANONYMOUS_HOLDER, None), ("s1", 3)):
        with pytest.raises(store.CheckoutDenied):
            store._authorize_checkout_row(live, holder, fence, "publish a version")


def test_undo_and_redo_are_unrestricted_when_no_lock_is_held(tmp_path):
    """Same permitted cases as a publish: no lock at all, and an expired lock.
    The demo's undo/redo buttons never take a checkout, and must keep working."""
    be = store.InMemoryBackend()
    a = _tmpfile(tmp_path, "v1.dwg", b"V1")
    did = store.ingest_drawing(be, "t", a)["drawing_id"]
    assert store.put_drawing(be, "t", did, _tmpfile(tmp_path, "v2.dwg", b"V2"),
                             parent_version=1) == 2

    assert store.undo(be, "t", did, holder="s2") == 1        # unlocked
    assert store.redo(be, "t", did, holder="s2") == 2

    assert store.acquire_checkout(be, "t", did, holder="s1", ttl_s=0.05) is True
    time.sleep(0.2)
    assert store.undo(be, "t", did, holder="s2") == 1        # lease expired


# --------------------------------------------------------------------------- #
# Version-aware extract / run_tool dry-run bodies (no network)
# --------------------------------------------------------------------------- #
def _hostdwg_url(dry_body: dict) -> str:
    return dry_body["workitem"]["body"]["arguments"]["HostDwg"]["url"]


def test_extract_version_aware_dry_run_references_version_key():
    res = client.extract(DWG, tenant_id="acme",
                         drawing_id="0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
                         version="head", dry_run=True)
    assert res["_dry_run"] is True
    # HostDwg references the versioned store key, NOT a throwaway in/<ts>_ object
    assert "/v/0000000" in res["input_object"]
    assert not res["input_object"].startswith("in/")
    assert "in/" not in res["input_object"]
    assert res["store_version"] == 1  # head placeholder w/o a live manifest -> v1
    # the actual WorkItem body carries the same versioned key (url-encoded)
    assert "/v/0000000" in urllib.parse.unquote(_hostdwg_url(res))


def test_run_tool_version_aware_dry_run_references_version_key():
    tool = {"name": "count-by-layer", "engine_op": "count_by_layer", "version": "1.0.0"}
    res = client.run_tool(DWG, tool, {}, tenant_id="acme", drawing_id="draw-x",
                          version=1, dry_run=True)
    assert res["_dry_run"] is True
    assert res["input_object"] == "tenants/acme/drawings/draw-x/v/00000001.dwg"
    assert "/v/0000000" in res["input_object"]
    assert not res["input_object"].startswith("in/")
    assert "/v/0000000" in urllib.parse.unquote(_hostdwg_url(res))


# --------------------------------------------------------------------------- #
# Legacy (no tenant/drawing) dry-run bodies unchanged — FROZEN §5
# --------------------------------------------------------------------------- #
def test_legacy_extract_dry_run_unchanged():
    res = client.extract(DWG, dry_run=True)
    assert res["_dry_run"] is True
    assert res["input_object"].startswith("in/")
    assert res["input_object"].endswith("_rooftop_demo.dwg")
    body = res["workitem"]["body"]
    assert body["activityId"].endswith("+prod")
    assert set(body["arguments"]) == {"HostDwg", "Result"}


def test_legacy_run_tool_dry_run_unchanged():
    tool = {"name": "count-by-layer", "engine_op": "count_by_layer", "version": "1.0.0"}
    res = client.run_tool(DWG, tool, {"foo": 1}, dry_run=True)
    assert res["_dry_run"] is True
    assert res["input_object"].startswith("in/")
    assert res["output_object"].endswith("count_by_layer.result.json")
    body = res["workitem"]["body"]
    assert set(body["arguments"]) == {"HostDwg", "Params", "Result"}
