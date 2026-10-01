"""A version blob orphaned by a failed manifest save never blocks the drawing.

The legacy (blob manifest) write path of ``da/store.py put_drawing`` writes the
version blob and then saves the manifest. When the save fails, the blob at
``latest + 1`` is left with no manifest row naming it. These rows pin what the
next plain (non graph bundle) write does about it:

* identical bytes are adopted (no second blob write);
* different bytes take the next free slot, where the checkout guard really is
  exclusive, and are still refused on a backend with no cross-process lock;
* nothing is ever written over an existing version blob;
* every unreadable or out-of-bound case refuses before anything is written.

Hermetic: in-memory and tmp_path filesystem stores only. No network, no
PostgreSQL, no optional tool.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import warnings
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
for _path in (str(SERVER_DIR), str(REPO_ROOT / "da")):
    if _path not in sys.path:
        sys.path.append(_path)

import store  # noqa: E402  (needs the da/ path appended above)
import write_loop  # noqa: E402
import test_w1_graph_versions as graph_versions  # noqa: E402  (the bundle commit helpers)
from test_w1_design_graph import graph  # noqa: E402, F401  (fixture)

TENANT = "orphan-tenant"
DRAWING = "orphan-drawing"
V1 = b"DWG-V1"
FIRST = b"DWG-V2-first-attempt"
OTHER = b"DWG-V2-other-bytes!!"  # same length as FIRST on purpose
REFUSAL = "refuse to overwrite immutable version key"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(version: int) -> str:
    return store.drawing_version_key(TENANT, DRAWING, version)


def _canonical(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Faults:
    """Fault and call recording mixed over a real backend."""

    def _init_faults(self) -> None:
        self.fail_manifest_puts = 0
        self.exists_error = {}
        self.get_error = {}
        self.get_value = {}
        self.calls = []

    def put(self, key, data):
        self.calls.append(("put", key))
        if key.endswith("/manifest.json") and self.fail_manifest_puts > 0:
            self.fail_manifest_puts -= 1
            raise OSError("simulated manifest write failure")
        return super().put(key, data)

    def get(self, key):
        self.calls.append(("get", key))
        if key in self.get_error:
            raise self.get_error[key]
        if key in self.get_value:
            return self.get_value[key]
        return super().get(key)

    def exists(self, key):
        self.calls.append(("exists", key))
        if key in self.exists_error:
            raise self.exists_error[key]
        return super().exists(key)


class _Memory(_Faults, store.InMemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self._init_faults()


class _Filesystem(_Faults, store.FilesystemBackend):
    def __init__(self, root: str) -> None:
        super().__init__(root)
        self._init_faults()


class _NoCrossProcessLock(_Memory):
    """Shaped like the object-storage backend: no OS lock to take."""

    cross_process_checkout_safe = False


class _FilesystemNoCrossProcessLock(_Filesystem):
    cross_process_checkout_safe = False


@pytest.fixture(autouse=True)
def _legacy_authority(monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")


@pytest.fixture(params=["memory", "filesystem"])
def backend(request, tmp_path):
    if request.param == "memory":
        return _Memory()
    return _Filesystem(str(tmp_path / "store"))


def _file(tmp_path, data: bytes) -> str:
    path = tmp_path / f"payload-{_sha(data)[:12]}-{len(data)}.dwg"
    path.write_bytes(data)
    return str(path)


def _ingest(be, tmp_path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", store.CrossProcessCheckoutLockMissing)
        store.ingest_drawing(be, TENANT, _file(tmp_path, V1), drawing_id=DRAWING)
    be.calls.clear()


def _put(be, tmp_path, data: bytes, parent=1, **kwargs) -> int:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", store.CrossProcessCheckoutLockMissing)
        return store.put_drawing(be, TENANT, DRAWING, _file(tmp_path, data),
                                 parent_version=parent, meta={"tool": "orphan-test"},
                                 **kwargs)


def _orphan(be, tmp_path, data: bytes = FIRST) -> None:
    """The real failure: the blob lands, the manifest save after it does not."""
    be.fail_manifest_puts = 1
    with pytest.raises(OSError, match="simulated manifest write failure"):
        _put(be, tmp_path, data)
    assert be.fail_manifest_puts == 0
    be.calls.clear()


def _manifest(be) -> dict:
    return store.load_manifest(be, TENANT, DRAWING)


def _shape(be) -> dict:
    m = _manifest(be)
    return {"head": m["head"], "latest": m["latest"],
            "versions": [[e["v"], e["parent"], e["bytes"], e["sha256"]]
                         for e in m["versions"]]}


def _version_puts(be) -> list:
    return [key for op, key in be.calls if op == "put" and key.endswith(".dwg")]


def _base(be):
    return (store.FilesystemBackend if isinstance(be, store.FilesystemBackend)
            else store.InMemoryBackend)


def _raw(be, key: str) -> bytes:
    """Read a blob without recording the call or tripping an injected fault."""
    return _base(be).get(be, key)


def _seed(be, key: str, data: bytes) -> None:
    """Plant a blob the way a failed publication leaves one: no manifest row."""
    _base(be).put(be, key, data)


# --------------------------------------------------------------------------- #
# The failure and the two outcomes
# --------------------------------------------------------------------------- #
def test_orphan_adopt_failed_manifest_save_leaves_a_blob_beyond_latest(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path)
    assert _shape(backend) == {
        "head": 1, "latest": 1, "versions": [[1, None, 6, _sha(V1)]]}
    assert _raw(backend, _key(2)) == FIRST


def test_orphan_adopt_identical_bytes_are_adopted_without_a_second_write(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path)
    assert _put(backend, tmp_path, FIRST) == 2
    assert _version_puts(backend) == []
    assert _raw(backend, _key(2)) == FIRST
    assert _shape(backend) == {
        "head": 2, "latest": 2,
        "versions": [[1, None, 6, _sha(V1)], [2, 1, 20, _sha(FIRST)]]}
    entry = _manifest(backend)["versions"][-1]
    assert entry["tool"] == "orphan-test" and entry["note"] is None


def test_orphan_adopt_different_bytes_take_the_next_free_slot(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path)
    assert _put(backend, tmp_path, OTHER) == 3
    assert _version_puts(backend) == [_key(3)]
    assert _raw(backend, _key(2)) == FIRST  # the orphan is never overwritten
    assert _raw(backend, _key(3)) == OTHER
    assert _shape(backend) == {
        "head": 3, "latest": 3,
        "versions": [[1, None, 6, _sha(V1)], [3, 1, 20, _sha(OTHER)]]}


def test_orphan_adopt_every_later_writer_is_unblocked(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path)
    assert [_put(backend, tmp_path, OTHER),
            _put(backend, tmp_path, b"third", parent=3),
            _put(backend, tmp_path, b"fourth", parent=4)] == [3, 4, 5]
    assert [e["v"] for e in _manifest(backend)["versions"]] == [1, 3, 4, 5]


def test_orphan_adopt_matching_blob_beyond_a_different_one_is_adopted(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path, FIRST)          # v2 orphan
    _seed(backend, _key(3), OTHER)             # v3 orphan, other bytes
    assert _put(backend, tmp_path, OTHER) == 3
    assert _version_puts(backend) == []
    assert _raw(backend, _key(2)) == FIRST and _raw(backend, _key(3)) == OTHER
    assert [e["v"] for e in _manifest(backend)["versions"]] == [1, 3]


def test_orphan_adopt_survives_a_second_manifest_failure(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path)
    backend.fail_manifest_puts = 1
    with pytest.raises(OSError):
        _put(backend, tmp_path, FIRST)
    assert _shape(backend)["latest"] == 1
    assert _put(backend, tmp_path, FIRST) == 2
    assert _raw(backend, _key(2)) == FIRST


def test_orphan_adopt_same_length_different_bytes_are_not_adopted(backend, tmp_path):
    assert len(FIRST) == len(OTHER) and FIRST != OTHER
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path, FIRST)
    assert _put(backend, tmp_path, OTHER) == 3


@pytest.mark.parametrize("orphan,payload,expected", [
    (b"", b"", 2),          # an empty orphan equals an empty payload
    (b"", b"x", 3),
    (b"x", b"", 3),
])
def test_orphan_adopt_empty_payloads(tmp_path, orphan, payload, expected):
    be = _Memory()
    _ingest(be, tmp_path)
    _seed(be, _key(2), orphan)
    assert _put(be, tmp_path, payload) == expected
    assert be._blobs[_key(2)] == orphan


# --------------------------------------------------------------------------- #
# Readers tolerate the gap
# --------------------------------------------------------------------------- #
def test_orphan_adopt_readers_tolerate_the_gap(backend, tmp_path):
    _ingest(backend, tmp_path)
    _orphan(backend, tmp_path)
    assert _put(backend, tmp_path, OTHER) == 3
    assert store.resolve_version(backend, TENANT, DRAWING) == (3, _key(3))
    assert store.resolve_version(backend, TENANT, DRAWING, "latest") == (3, _key(3))
    assert store.resolve_version(backend, TENANT, DRAWING, 1) == (1, _key(1))
    with pytest.raises(ValueError, match=r"version 2 not in manifest .*known=\[1, 3\]"):
        store.resolve_version(backend, TENANT, DRAWING, 2)
    assert store.resolve_version_entry(backend, TENANT, DRAWING)[2]["sha256"] == _sha(OTHER)
    assert store.undo(backend, TENANT, DRAWING) == 1
    assert _shape(backend)["head"] == 1 and _shape(backend)["latest"] == 3
    assert store.redo(backend, TENANT, DRAWING) == 3
    with pytest.raises(ValueError, match="nothing to redo"):
        store.redo(backend, TENANT, DRAWING)
    assert store.undo(backend, TENANT, DRAWING) == 1
    assert _put(backend, tmp_path, b"branch") == 4      # a branch from the undone head
    assert [[e["v"], e["parent"]] for e in _manifest(backend)["versions"]] == [
        [1, None], [3, 1], [4, 1]]


def test_orphan_adopt_reconciler_accepts_the_gapped_manifest(tmp_path):
    be = _Filesystem(str(tmp_path / "store"))
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    assert _put(be, tmp_path, OTHER) == 3
    path = REPO_ROOT / "scripts" / "reconcile_drawing_authority.py"
    spec = importlib.util.spec_from_file_location("orphan_adopt_reconciler", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checked = module.validate_manifest(
        _manifest(be), tenant_id=TENANT, drawing_id=DRAWING, blob_dir=be.root)
    assert [checked["head"], checked["latest"]] == [3, 3]
    assert [[e["v"], e["parent"], e["sha256"]] for e in checked["versions"]] == [
        [1, None, _sha(V1)], [3, 1, _sha(OTHER)]]


def test_orphan_adopt_write_loop_seam_reads_back_the_new_version(tmp_path):
    be = _Filesystem(str(tmp_path / "store"))
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    version = write_loop._put_bytes_version(
        be, TENANT, DRAWING, OTHER, parent_version=1, meta={"tool": "seam"},
        require_parent_is_head=True)
    assert version == 3
    resolved, key = store.resolve_version(be, TENANT, DRAWING)
    assert (resolved, be.get(key)) == (3, OTHER)


# --------------------------------------------------------------------------- #
# The bound
# --------------------------------------------------------------------------- #
def test_orphan_adopt_bound_is_the_shared_constant():
    assert store.MAX_ABANDONED_VERSION_SLOTS == 1000


def test_orphan_adopt_last_slot_inside_the_bound_is_used(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    for version in range(2, 1001):            # 999 orphans: v2..v1000
        _seed(be, _key(version), b"orphan-%d" % version)
    assert _put(be, tmp_path, OTHER) == 1001
    assert _version_puts(be) == [_key(1001)]
    assert [e["v"] for e in _manifest(be)["versions"]] == [1, 1001]


def test_orphan_adopt_one_past_the_bound_refuses_and_writes_nothing(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    for version in range(2, 1002):            # 1000 orphans: v2..v1001
        _seed(be, _key(version), b"orphan-%d" % version)
    before = dict(be._blobs)
    be.calls.clear()
    with pytest.raises(ValueError, match="too many abandoned version reservations"):
        _put(be, tmp_path, OTHER)
    assert be._blobs == before
    assert [op for op, _ in be.calls if op == "put"] == []
    assert sum(1 for op, key in be.calls if op == "exists" and key.endswith(".dwg")) == 1000


def test_orphan_adopt_version_field_overflow_refuses_and_writes_nothing(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    m = _manifest(be)
    m["latest"] = 99999998
    store.save_manifest(be, TENANT, DRAWING, m)
    _seed(be, _key(99999999), b"orphan")
    before = dict(be._blobs)
    with pytest.raises(ValueError, match="overflows the 8-digit key field"):
        _put(be, tmp_path, OTHER)
    assert be._blobs == before


# --------------------------------------------------------------------------- #
# A backend with no cross-process lock keeps the refusal for every occupied slot
# --------------------------------------------------------------------------- #
def test_orphan_adopt_no_cross_process_lock_still_refuses_different_bytes(tmp_path):
    be = _NoCrossProcessLock()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    before = dict(be._blobs)
    with pytest.raises(ValueError) as caught:
        _put(be, tmp_path, OTHER)
    assert str(caught.value) == f"{REFUSAL} {_key(2)}"
    assert be._blobs == before
    assert [op for op, _ in be.calls if op == "put"] == []


def test_orphan_adopt_no_cross_process_lock_still_refuses_identical_bytes(tmp_path):
    # With no cross-process lock, identical bytes beyond latest may be another replica's write whose
    # manifest save is still in flight. Adopting them lets that stale save drop this writer's later
    # versions, so the slot is refused as it is on main, before its blob is read.
    be = _NoCrossProcessLock()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    before = dict(be._blobs)
    seen = len(be.calls)
    with pytest.raises(ValueError) as caught:
        _put(be, tmp_path, FIRST)
    assert str(caught.value) == f"{REFUSAL} {_key(2)}"
    assert be._blobs == before
    assert [op for op, key in be.calls[seen:] if key == _key(2)] == ["exists"]
    assert [e["v"] for e in _manifest(be)["versions"]] == [1]


# --------------------------------------------------------------------------- #
# Fail closed on anything unreadable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("error", [OSError("transport"), KeyError("vanished")],
                         ids=["oserror", "keyerror"])
def test_orphan_adopt_unreadable_orphan_refuses_and_writes_nothing(tmp_path, error):
    be = _Memory()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    be.get_error[_key(2)] = error
    before = dict(be._blobs)
    with pytest.raises(type(error)):
        _put(be, tmp_path, OTHER)
    assert be._blobs == before
    assert [op for op, _ in be.calls if op == "put"] == []


@pytest.mark.parametrize("value", [None, "DWG-V2-first-attempt", 20, [b"x"]],
                         ids=["none", "str", "int", "list"])
def test_orphan_adopt_wrong_typed_blob_refuses_and_writes_nothing(tmp_path, value):
    be = _Memory()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    be.get_value[_key(2)] = value
    before = dict(be._blobs)
    with pytest.raises(ValueError) as caught:
        _put(be, tmp_path, FIRST)
    assert str(caught.value) == f"version key {_key(2)} holds an unreadable object"
    assert be._blobs == before
    assert [op for op, _ in be.calls if op == "put"] == []


def test_orphan_adopt_bytearray_blob_with_identical_bytes_is_adopted(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    be.get_value[_key(2)] = bytearray(FIRST)
    assert _put(be, tmp_path, FIRST) == 2
    assert _version_puts(be) == []


def test_orphan_adopt_failed_existence_check_refuses_and_writes_nothing(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    be.exists_error[_key(2)] = OSError("transport")
    before = dict(be._blobs)
    be.calls.clear()
    with pytest.raises(OSError, match="transport"):
        _put(be, tmp_path, OTHER)
    assert be._blobs == before
    assert [op for op, _ in be.calls if op == "put"] == []


def test_orphan_adopt_failed_blob_write_in_the_reserved_slot_changes_nothing(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    real_put = be.put

    def failing_put(key, data):
        if key == _key(3):
            raise OSError("disk full")
        return real_put(key, data)

    be.put = failing_put
    with pytest.raises(OSError, match="disk full"):
        _put(be, tmp_path, OTHER)
    assert _shape(be)["latest"] == 1 and _key(3) not in be._blobs
    be.put = real_put
    assert _put(be, tmp_path, OTHER) == 3


# --------------------------------------------------------------------------- #
# What does not change
# --------------------------------------------------------------------------- #
def test_orphan_adopt_clean_write_costs_the_same_calls_as_before(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    be.calls.clear()
    assert _put(be, tmp_path, FIRST) == 2
    manifest = store.manifest_key(TENANT, DRAWING)
    assert be.calls == [("exists", manifest), ("exists", manifest), ("get", manifest),
                        ("exists", _key(2)), ("put", _key(2)), ("put", manifest)]


def test_orphan_adopt_checkout_is_authorized_before_any_blob_is_read(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    store.acquire_checkout(be, TENANT, DRAWING, "owner", 300)
    be.calls.clear()
    with pytest.raises(store.CheckoutDenied):
        _put(be, tmp_path, FIRST, holder="someone-else")
    assert [key for _, key in be.calls if key.endswith(".dwg")] == []
    assert _shape(be)["latest"] == 1


def test_orphan_adopt_stale_parent_is_refused_before_any_blob_is_read(tmp_path):
    be = _Memory()
    _ingest(be, tmp_path)
    assert _put(be, tmp_path, b"second") == 2
    _orphan(be, tmp_path)                      # orphan at v3, head 2
    with pytest.raises(ValueError, match="stale parent 1: head is now 2"):
        _put(be, tmp_path, FIRST, parent=1, require_parent_is_head=True)
    assert [key for _, key in be.calls if key.endswith(".dwg")] == []
    assert _put(be, tmp_path, FIRST, parent=2, require_parent_is_head=True) == 3


@pytest.mark.parametrize("backend_class", [_Filesystem, _FilesystemNoCrossProcessLock],
                         ids=["locked", "no-cross-process-lock"])
def test_orphan_adopt_graph_bundle_path_keeps_reserving_on_every_backend(
        tmp_path, monkeypatch, graph, backend_class):  # noqa: F811
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    tenant, drawing = graph_versions.TENANT, graph_versions.DRAWING
    be = backend_class(str(tmp_path / "store"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", store.CrossProcessCheckoutLockMissing)
        store.ingest_drawing(be, tenant, _file(tmp_path, b"synthetic original DWG"),
                             drawing_id=drawing)
        fence = store.acquire_checkout_fence(be, tenant, drawing, "writer", 300)
        orphan_key = store.drawing_version_key(tenant, drawing, 2)
        _seed(be, orphan_key, b"abandoned other bytes")
        request = graph_versions.request_for(be, graph)
        assert graph_versions.commit((be, fence), request) == 3
    assert _raw(be, orphan_key) == b"abandoned other bytes"
    assert [e["v"] for e in store.load_manifest(be, tenant, drawing)["versions"]] == [1, 3]


def test_orphan_adopt_ingest_still_refuses_a_version_one_blob_without_a_manifest(tmp_path):
    be = _Memory()
    _seed(be, _key(1), V1)
    with pytest.raises(ValueError) as caught:
        _ingest(be, tmp_path)
    assert str(caught.value) == f"{REFUSAL} {_key(1)}"
    assert sorted(be._blobs) == [_key(1)]


def test_orphan_adopt_manifest_digest_of_the_gapped_drawing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_now_iso", lambda: "2026-10-01T00:00:00+00:00")
    be = _Memory()
    _ingest(be, tmp_path)
    _orphan(be, tmp_path)
    assert _put(be, tmp_path, OTHER) == 3
    assert _put(be, tmp_path, FIRST, parent=3) == 4
    assert _canonical(_manifest(be)) == (
        "ea264a32fc56eca9ca0ee2de7f8222847b0a9302488238cfcafdf310c9fcae68")
