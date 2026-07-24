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
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
import store  # noqa: E402

DWG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "rooftop_demo.dwg")
VERSION_KEY_RE = re.compile(r"^tenants/[a-z0-9_-]+/drawings/[a-z0-9_-]+/v/\d{8}\.dwg$")

# These dry_run paths never reach the network, but they still build a signed
# client, so client._load_creds() runs and raises without a credential source.
# On an operator box ~/.aps/credentials.json makes that invisible; on a clean
# CI runner it is 5 hard errors. Skip-with-reason keeps the other 9 tests in
# the gate instead of failing the whole suite on a host-only artifact.
requires_aps_creds = pytest.mark.skipif(
    not (os.environ.get("APS_CREDENTIALS_JSON", "").strip()
         or os.path.exists(client.CRED_PATH)),
    reason=f"APS credentials absent (no APS_CREDENTIALS_JSON, no {client.CRED_PATH})",
)


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
    yield


# --------------------------------------------------------------------------- #
# Bucket policy
# --------------------------------------------------------------------------- #
@requires_aps_creds
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


@requires_aps_creds
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
# Version-aware extract / run_tool dry-run bodies (no network)
# --------------------------------------------------------------------------- #
def _hostdwg_url(dry_body: dict) -> str:
    return dry_body["workitem"]["body"]["arguments"]["HostDwg"]["url"]


@requires_aps_creds
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


@requires_aps_creds
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
@requires_aps_creds
def test_legacy_extract_dry_run_unchanged():
    res = client.extract(DWG, dry_run=True)
    assert res["_dry_run"] is True
    assert res["input_object"].startswith("in/")
    assert res["input_object"].endswith("_rooftop_demo.dwg")
    body = res["workitem"]["body"]
    assert body["activityId"].endswith("+prod")
    assert set(body["arguments"]) == {"HostDwg", "Result"}


@requires_aps_creds
def test_legacy_run_tool_dry_run_unchanged():
    tool = {"name": "count-by-layer", "engine_op": "count_by_layer", "version": "1.0.0"}
    res = client.run_tool(DWG, tool, {"foo": 1}, dry_run=True)
    assert res["_dry_run"] is True
    assert res["input_object"].startswith("in/")
    assert res["output_object"].endswith("count_by_layer.result.json")
    body = res["workitem"]["body"]
    assert set(body["arguments"]) == {"HostDwg", "Params", "Result"}
