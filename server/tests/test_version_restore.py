"""
Binary acceptance for ruling-4 (lane S5): version delta chips + restore-to-version.

GET /api/drawings/{id}/versions now carries a per-row `delta`
({added, modified, deleted} | null), computed at READ time by diffing each
version's stored intake payload against its parent's -- NEVER stamped at write
time (the write path is owned by another lane, mid-authority-migration, and is
not touched by this file).

POST /api/drawings/{id}/versions/{v}/restore creates a NEW head version whose
content equals the target version's, with parent = the CURRENT head. Restore
is an ordinary write (history is never rewritten), composed entirely from
existing store/write_loop primitives.

Hermetic: in-process TestClient wrapping only the drawings router (mirrors
tests/test_drawings_bootstrap.py), an isolated LEAF_STORE_DIR per test
(tmp_path), APS_LIVE=0. The 3-version chain is built directly through
store.ingest_drawing + write_loop._put_bytes_version (the same primitives the
route itself composes) rather than a full drawing.write tool run, so every
entity delta in the assertions is exact and hand-checkable.

Run:  cd server && python -m pytest tests/test_version_restore.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import write_loop  # noqa: E402

TENANT = "s5-version-restore"
DRAWING = "chain-demo"


# --------------------------------------------------------------------------- #
# fixture intake payloads
# --------------------------------------------------------------------------- #
def _poly(handle: str, layer: str = "Panels") -> Dict:
    return {"layer": layer, "closed": True,
            "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
            "xdata": None, "handle": handle}


def _intake(polylines: List[Dict]) -> Dict:
    return {"dwg": {}, "layers": ["Panels", "Roof"], "polylines": polylines,
            "inserts": [], "faces3d": [], "blockdefs": [], "geodata": None,
            "images": [], "imageNames": []}


# --------------------------------------------------------------------------- #
# HTTP harness: minimal FastAPI app wrapping just the drawings router
# (identical pattern to tests/test_drawings_bootstrap.py)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("APS_LIVE", raising=False)  # APS_LIVE off -> FilesystemBackend, no da/creds
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)  # legacy X-Tenant-Id header stub

    from routers import drawings as drawings_router  # noqa: PLC0415 (import after env is set)
    from envelopes import install_error_handlers  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _backend(tmp_path):
    import store  # noqa: PLC0415

    return store.FilesystemBackend(str(tmp_path / "drawings"))


def _seed_chain(tmp_path):
    """Builds a 3-version chain with a hand-checkable delta at each hop:

        v1 = {A, B, C}
        v2 = {A(modified: Roof layer), C, D}    -- +1 (D), ~1 (A), -1 (B)
        v3 = {A(modified), C, D, E}             -- +1 (E) only

    Uses store.ingest_drawing (v1) + write_loop._put_bytes_version (v2, v3) --
    the SAME primitives the restore route itself composes -- rather than a
    full drawing.write tool run, so the exact resulting payload is known.
    """
    import store  # noqa: PLC0415

    backend = _backend(tmp_path)
    v1_intake = _intake([_poly("A"), _poly("B"), _poly("C")])
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    Path(tmp).write_bytes(json.dumps(v1_intake, separators=(",", ":")).encode("utf-8"))
    try:
        store.ingest_drawing(backend, TENANT, tmp, drawing_id=DRAWING)
    finally:
        Path(tmp).unlink(missing_ok=True)

    v2_intake = _intake([_poly("A", layer="Roof"), _poly("C"), _poly("D")])
    v2 = write_loop._put_bytes_version(
        backend, TENANT, DRAWING,
        json.dumps(v2_intake, separators=(",", ":")).encode("utf-8"),
        parent_version=1, meta={"tool": "test-seed", "note": "v2"},
    )
    assert v2 == 2

    v3_intake = _intake([_poly("A", layer="Roof"), _poly("C"), _poly("D"), _poly("E")])
    v3 = write_loop._put_bytes_version(
        backend, TENANT, DRAWING,
        json.dumps(v3_intake, separators=(",", ":")).encode("utf-8"),
        parent_version=2, meta={"tool": "test-seed", "note": "v3"},
    )
    assert v3 == 3
    return backend


# --------------------------------------------------------------------------- #
# delta chips
# --------------------------------------------------------------------------- #
def test_delta_correct_on_a_three_version_chain(client, tmp_path):
    _seed_chain(tmp_path)

    r = client.get(f"/api/drawings/{DRAWING}/versions",
                   params={"include_deltas": 1}, headers=_h(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["head"] == 3 and body["latest"] == 3
    rows = {row["v"]: row for row in body["versions"]}
    assert set(rows) == {1, 2, 3}

    assert rows[1]["parent"] is None
    assert rows[1]["delta"] is None  # root version -- nothing to diff against

    assert rows[2]["parent"] == 1
    assert rows[2]["delta"] == {"added": 1, "modified": 1, "deleted": 1}

    assert rows[3]["parent"] == 2
    assert rows[3]["delta"] == {"added": 1, "modified": 0, "deleted": 0}


def test_delta_is_null_when_a_stored_payload_is_unparseable(client, tmp_path):
    """A version blob that fails to parse as JSON must not 500 the whole chain
    -- its own delta (and its child's, since the child's parent-side read also
    fails) come back null, and every OTHER row is unaffected."""
    import store  # noqa: PLC0415

    backend = _seed_chain(tmp_path)
    vkey = store.drawing_version_key(TENANT, DRAWING, 2)
    backend.put(vkey, b"{not valid json")

    body = client.get(f"/api/drawings/{DRAWING}/versions",
                      params={"include_deltas": 1}, headers=_h(TENANT)).json()
    rows = {row["v"]: row for row in body["versions"]}
    assert rows[1]["delta"] is None       # unaffected: still the root
    assert rows[2]["delta"] is None       # its own payload is corrupt
    assert rows[3]["delta"] is None       # its PARENT (v2) is corrupt
    assert body["error"] is None          # the corruption did not fault the whole read


def test_default_versions_shape_has_no_delta_key(client, tmp_path):
    """Deltas are opt-in (?include_deltas=1): the DEFAULT response keeps the
    exact pre-feature row shape (tests/test_ui_wave.py pins it), because the
    delta computation loads every version payload and the app reads this
    route at startup merely for checkout state."""
    _seed_chain(tmp_path)

    body = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    assert all("delta" not in row for row in body["versions"])


# --------------------------------------------------------------------------- #
# restore-to-version
# --------------------------------------------------------------------------- #
def test_restore_creates_new_head_preserving_chain(client, tmp_path):
    _seed_chain(tmp_path)

    r = client.post(f"/api/drawings/{DRAWING}/versions/1/restore", headers=_h(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert body["restored_from"] == 1
    assert body["new_version"] == {"drawing_id": DRAWING, "version": 4, "parent": 3}
    assert body["head"] == 4 and body["latest"] == 4

    versions = client.get(f"/api/drawings/{DRAWING}/versions",
                          params={"include_deltas": 1}, headers=_h(TENANT)).json()
    assert versions["head"] == 4 and versions["latest"] == 4
    rows = {row["v"]: row for row in versions["versions"]}
    assert set(rows) == {1, 2, 3, 4}
    # Raw-byte fidelity: restore copies the stored payload VERBATIM (never a
    # re-serialization), so the restored version's sha256 equals its source's.
    # In live representation the payload is DWG bytes; storing re-serialized
    # intake JSON there would poison the next APS write.
    assert rows[4]["sha256"] == rows[1]["sha256"]
    assert rows[4]["bytes"] == rows[1]["bytes"]
    # the chain is APPENDED to, never rewritten: v1..v3 are untouched
    assert rows[1] == {
        "v": 1, "parent": None, "created": rows[1]["created"],
        "bytes": rows[1]["bytes"], "sha256": rows[1]["sha256"],
        "tool": None, "workitem_id": None, "note": "initial ingest", "delta": None,
    }
    assert rows[4]["parent"] == 3

    # v4's content equals v1's, byte for byte
    restored = client.get(f"/api/drawings/{DRAWING}/intake",
                          params={"version": "head"}, headers=_h(TENANT)).json()["intake"]
    v1_intake = client.get(f"/api/drawings/{DRAWING}/intake",
                           params={"version": "1"}, headers=_h(TENANT)).json()["intake"]
    assert restored == v1_intake

    # and v4's delta is computed against its PARENT (v3), not the restored
    # source: v3={A(Roof),C,D,E} -> v4={A(Panels),B,C} is +1 (B), ~1 (A), -2 (D,E)
    assert rows[4]["delta"] == {"added": 1, "modified": 1, "deleted": 2}


def test_concurrent_restores_stay_linear(client, tmp_path):
    """Round-2 BLOCKING: the legacy authority accepts any parent_version, so
    two racing restores forked the chain (both parent 1). The in-process
    restore lock serializes them: each re-reads a fresh head, so the chain
    stays LINEAR (v4 parent 3, v5 parent 4 — in either completion order)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _seed_chain(tmp_path)
    barrier = threading.Barrier(2)

    def _restore(v):
        # The barrier forces both requests to reach the route concurrently so
        # the pass cannot come from favorable sequential scheduling.
        barrier.wait(timeout=10)
        return client.post(f"/api/drawings/{DRAWING}/versions/{v}/restore", headers=_h(TENANT))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(_restore, [1, 2])
    assert first.status_code == 200 and second.status_code == 200

    versions = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    rows = {row["v"]: row for row in versions["versions"]}
    assert set(rows) == {1, 2, 3, 4, 5}
    assert rows[4]["parent"] == 3
    assert rows[5]["parent"] == 4  # NOT a fork: the second saw the first's head
    assert versions["head"] == 5


def test_restore_of_unreadable_live_source_is_refused(client, tmp_path):
    """Round-2 MAJOR: a live-representation version (raw DWG bytes) with no
    intake cache must be refused up front — promoting it would 200 and then
    break every read_intake on the new head."""
    backend = _seed_chain(tmp_path)
    v4 = write_loop._put_bytes_version(
        backend, TENANT, DRAWING, b"\x00\x01DWGBYTES-NOT-JSON",
        parent_version=3, meta={"tool": "test-seed", "note": "live-rep, no cache"},
    )
    assert v4 == 4

    r = client.post(f"/api/drawings/{DRAWING}/versions/4/restore", headers=_h(TENANT))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["retryable"] is False

    versions = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    assert versions["head"] == 4  # nothing was appended


def test_corrupt_manifest_matches_the_mutating_route_family(client, tmp_path):
    """Round-2 MAJOR follow-through: a malformed manifest must surface from
    restore EXACTLY as it does from the rest of the mutating family. All of
    them hit it in the shared preflight (ensure_demo_drawing), whose
    documented idiom answers 400 non-retryable — never a retryable 409 that
    would invite duplicate appends. Undo is the family witness."""
    import store  # noqa: PLC0415

    backend = _seed_chain(tmp_path)
    backend.put(store.manifest_key(TENANT, DRAWING), b"{not valid json")

    restore = client.post(f"/api/drawings/{DRAWING}/versions/1/restore", headers=_h(TENANT))
    undo = client.post(f"/api/drawings/{DRAWING}/undo", headers=_h(TENANT))
    assert restore.status_code == undo.status_code == 400, (restore.text, undo.text)
    assert restore.json()["error"]["retryable"] is False


def test_checkout_acquired_after_preflight_is_refused_at_the_store(client, tmp_path, monkeypatch):
    """Round-3 BLOCKING pin: a checkout acquired AFTER the preflight saw none
    must be refused ATOMICALLY at put time. The route passes ANONYMOUS_HOLDER
    (never None) for a lease-less caller, and the store's own holder check
    fails closed under an active checkout. The race is simulated by letting
    the preflight report (None, None) while the checkout is already active."""
    import store  # noqa: PLC0415
    from routers import drawings as drawings_router  # noqa: PLC0415

    backend = _seed_chain(tmp_path)
    store.acquire_checkout(backend, TENANT, DRAWING, holder="racing-session", ttl_s=300)
    monkeypatch.setattr(drawings_router, "_lock_authorization",
                        lambda *args, **kwargs: (None, None))

    r = client.post(f"/api/drawings/{DRAWING}/versions/1/restore", headers=_h(TENANT))
    assert r.status_code == 403, r.text

    versions = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    assert versions["head"] == 3  # the refused write never landed


def test_valid_but_mismatched_live_cache_is_refused(client, tmp_path):
    """A valid intake cache from another source must not authorize restore."""
    backend = _seed_chain(tmp_path)
    live_bytes = b"AC1032\x00SOURCE-A"
    v4 = write_loop._put_bytes_version(
        backend, TENANT, DRAWING, live_bytes,
        parent_version=3, meta={"tool": "test-seed", "note": "live source"})
    write_loop.publish_intake_cache(
        backend, TENANT, DRAWING, v4, live_bytes,
        _intake([_poly("SOURCE-A")]))
    # The replacement is well-formed intake JSON, but its digest no longer
    # matches the source binding published with the raw DWG.
    backend.put(
        write_loop.intake_cache_key(TENANT, DRAWING, v4),
        json.dumps(_intake([_poly("OTHER-SOURCE")])).encode("utf-8"))

    r = client.post(f"/api/drawings/{DRAWING}/versions/{v4}/restore", headers=_h(TENANT))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["retryable"] is False


def test_store_compare_and_set_refuses_a_stale_parent(client, tmp_path):
    """Round-4 BLOCKING pin, store level: put_drawing(require_parent_is_head=
    True) refuses inside its own critical section when head has moved."""
    import store  # noqa: PLC0415

    backend = _seed_chain(tmp_path)
    with pytest.raises(ValueError, match="stale parent"):
        write_loop._put_bytes_version(
            backend, TENANT, DRAWING, b'{"stale": true}',
            parent_version=1, meta={"tool": "test", "note": "stale"},
            require_parent_is_head=True)
    m = store.load_manifest(backend, TENANT, DRAWING)
    assert int(m["head"]) == 3  # nothing landed


def test_restore_racing_a_broker_write_returns_409_not_a_fork(client, tmp_path, monkeypatch):
    """Round-4 BLOCKING pin, route level: a write that lands between restore's
    fresh head read and its put makes the store's compare-and-set refuse the
    stale parent; the route answers retryable 409 and the DAG never forks."""
    _seed_chain(tmp_path)
    original = write_loop._put_bytes_version
    state = {"raced": False}

    def _race(backend_, tenant, drawing, data, parent_version, meta, **kwargs):
        if meta.get("tool") == "restore" and not state["raced"]:
            state["raced"] = True
            # Deterministic interleave: a broker-style write commits AFTER the
            # route read head but BEFORE its put reaches the store.
            original(backend_, tenant, drawing, b'{"racer": true}',
                     parent_version=parent_version,
                     meta={"tool": "test-racer", "note": "interleaved write"})
        return original(backend_, tenant, drawing, data,
                        parent_version=parent_version, meta=meta, **kwargs)

    monkeypatch.setattr(write_loop, "_put_bytes_version", _race)
    r = client.post(f"/api/drawings/{DRAWING}/versions/1/restore", headers=_h(TENANT))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["retryable"] is True

    versions = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    rows = {row["v"]: row for row in versions["versions"]}
    assert versions["head"] == 4 and rows[4]["parent"] == 3  # racer landed, linear
    assert 5 not in rows  # the stale restore never forked the DAG


def test_non_dict_intake_json_is_refused(client, tmp_path):
    """Round-4 MAJOR pin: a payload that is valid JSON but not an intake
    OBJECT (a list) must be refused 422, not restored."""
    backend = _seed_chain(tmp_path)
    v4 = write_loop._put_bytes_version(
        backend, TENANT, DRAWING, b"[1, 2, 3]",
        parent_version=3, meta={"tool": "test-seed", "note": "list payload"})
    assert v4 == 4

    r = client.post(f"/api/drawings/{DRAWING}/versions/4/restore", headers=_h(TENANT))
    assert r.status_code == 422, r.text


def test_mirror_failure_reports_unreadable_live_head(client, tmp_path, monkeypatch):
    """Round-4 MAJOR pin: when the source is live-representation (DWG blob +
    valid cache) and the cache mirror fails post-commit, the response still
    succeeds (the head is immutable) but restored_head_readable is false."""
    import store  # noqa: PLC0415

    backend = _seed_chain(tmp_path)
    v4 = write_loop._put_bytes_version(
        backend, TENANT, DRAWING, b"\x00\x01DWGBYTES",
        parent_version=3, meta={"tool": "test-seed", "note": "live-rep"})
    write_loop.publish_intake_cache(
        backend, TENANT, DRAWING, v4, b"\x00\x01DWGBYTES",
        _intake([_poly("A")]))

    new_head_ckey = write_loop.intake_cache_key(TENANT, DRAWING, 5)
    original_put = store.FilesystemBackend.put

    def _failing_put(self, key, data):
        if key == new_head_ckey:
            raise OSError("simulated cache-write failure")
        return original_put(self, key, data)

    monkeypatch.setattr(store.FilesystemBackend, "put", _failing_put)
    r = client.post(f"/api/drawings/{DRAWING}/versions/4/restore", headers=_h(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["head"] == 5
    assert body["restored_head_readable"] is False


def test_restore_of_missing_version_404s(client, tmp_path):
    _seed_chain(tmp_path)

    r = client.post(f"/api/drawings/{DRAWING}/versions/999/restore", headers=_h(TENANT))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"

    # nothing was written
    versions = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    assert versions["head"] == 3 and versions["latest"] == 3


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL restore proof requires explicit DATABASE_URL",
)
def test_postgres_live_dwg_restore_preserves_blob_and_readable_cache(
    client, tmp_path, monkeypatch,
):
    """Staging-representative proof without touching staging.

    PostgreSQL owns the manifest, version reservation, checkout fence, and
    head compare-and-set. The blob is a raw DWG representation and its parsed
    intake rides the same sibling cache used by APS live reads. Restoring it
    must append a PostgreSQL-authoritative head, copy the DWG bytes exactly,
    and mirror a readable cache for that new head.
    """
    import store  # noqa: PLC0415
    from routers import drawings as drawings_router  # noqa: PLC0415

    db = store._db()
    db.apply_migration()
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")

    suffix = uuid.uuid4().hex[:12]
    tenant = f"s5-pg-{suffix}"
    drawing = f"live-dwg-{suffix}"
    holder = f"session-{suffix}"
    backend = _backend(tmp_path)

    source = tmp_path / "initial.json"
    source.write_bytes(json.dumps(_intake([_poly("A")])).encode("utf-8"))
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
    fence = store.acquire_checkout_fence(backend, tenant, drawing, holder, 300)
    assert fence is not None

    live_bytes = b"AC1032\x00S5-LIVE-DWG-RESTORE-PROOF"
    live_version = write_loop._put_bytes_version(
        backend, tenant, drawing, live_bytes,
        parent_version=1, meta={"tool": "aps-live-proof", "note": "raw DWG"},
        holder=holder, fence=fence,
    )
    assert live_version == 2
    live_intake = _intake([_poly("A"), _poly("B")])
    write_loop.publish_intake_cache(
        backend, tenant, drawing, live_version, live_bytes, live_intake)

    monkeypatch.setattr(
        drawings_router, "_lock_authorization",
        lambda *_args, **_kwargs: (holder, fence),
    )
    response = client.post(
        f"/api/drawings/{drawing}/versions/{live_version}/restore",
        headers=_h(tenant),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["head"] == 3
    assert body["restored_head_readable"] is True

    source_key = store.drawing_version_key(tenant, drawing, live_version)
    restored_key = store.drawing_version_key(tenant, drawing, 3)
    assert backend.get(restored_key) == backend.get(source_key) == live_bytes
    _, restored_intake = write_loop.read_intake(backend, tenant, drawing, 3)
    assert restored_intake == live_intake
    manifest = store.load_manifest(backend, tenant, drawing)
    restored_row = next(row for row in manifest["versions"] if row["v"] == 3)
    assert restored_row["parent"] == 2


def test_restore_denied_without_the_current_checkout_capability(client, tmp_path):
    """Restore goes through the SAME single-writer gate as undo/redo/publish: an
    active lock this caller cannot prove it holds refuses the write."""
    _seed_chain(tmp_path)

    acquired = client.post(f"/api/drawings/{DRAWING}/checkout",
                           json={"holder": "someone-else"}, headers=_h(TENANT))
    assert acquired.status_code == 200, acquired.text

    r = client.post(f"/api/drawings/{DRAWING}/versions/1/restore", headers=_h(TENANT))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"

    versions = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT)).json()
    assert versions["head"] == 3  # refused write never landed


def test_restore_accepts_the_current_checkout_capability(client, tmp_path, monkeypatch):
    """The real acquire response authorizes restore without a gate monkeypatch."""
    monkeypatch.setenv(
        "LEAF_CHECKOUT_CAP_SECRET", "s5-positive-capability-secret-32-bytes")
    _seed_chain(tmp_path)
    acquired = client.post(
        f"/api/drawings/{DRAWING}/checkout",
        json={"holder": "s5-owner"}, headers=_h(TENANT))
    assert acquired.status_code == 200, acquired.text
    capability = acquired.json()["checkout_capability"]

    restored = client.post(
        f"/api/drawings/{DRAWING}/versions/1/restore",
        headers={**_h(TENANT), "X-Checkout-Capability": capability})
    assert restored.status_code == 200, restored.text
    assert restored.json()["new_version"] == {
        "drawing_id": DRAWING, "version": 4, "parent": 3}


def test_restore_route_is_tenant_isolated(client, tmp_path):
    """A restore in tenant A cannot read or advance tenant B's same drawing id."""
    import store  # noqa: PLC0415

    backend = _seed_chain(tmp_path)
    other = "s5-version-restore-other"
    source = tmp_path / "other.json"
    other_intake = _intake([_poly("OTHER")])
    source.write_bytes(json.dumps(other_intake).encode("utf-8"))
    store.ingest_drawing(backend, other, str(source), drawing_id=DRAWING)
    before = store.load_manifest(backend, other, DRAWING)
    other_blob = backend.get(store.drawing_version_key(other, DRAWING, 1))

    restored = client.post(
        f"/api/drawings/{DRAWING}/versions/1/restore", headers=_h(TENANT))
    assert restored.status_code == 200, restored.text
    after = store.load_manifest(backend, other, DRAWING)
    assert after == before
    assert backend.get(store.drawing_version_key(other, DRAWING, 1)) == other_blob
