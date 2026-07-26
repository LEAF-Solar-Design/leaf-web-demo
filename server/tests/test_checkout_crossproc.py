"""Cross-process acceptance for the LEGACY (non-postgres) checkout authority.

The legacy manifest is a load, edit, save with nothing holding the record in
between. The per-process `threading.Lock` that used to be the whole guard is
invisible to a SECOND OS process, so two app replicas sharing one store
directory both read generation N, both compute N+1, and the second save wins.
Both callers are told they took the lease, and both mint a checkout capability
that verifies against the single persisted lease, because
`server/checkout_capability.py` binds the capability to the lock GENERATION and
the two generations are equal. That is the ownership bypass the fence exists to
prevent.

So these tests spawn real OS PROCESSES. Threads would pass against the old code
and prove nothing. They also run on both platforms with no skip: `fcntl` is the
production (Linux) path and `msvcrt` is the development (Windows) path, and a
guard that quietly no-ops on one of them is exactly the failure this closes.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DA_DIR = str(_PROJECT_ROOT / "da")
if _DA_DIR not in sys.path:
    sys.path.append(_DA_DIR)

import store  # noqa: E402  (needs the da/ path appended above)

TENANT = "t-crossproc"
DRAWING = "d-crossproc"

# Long enough that process startup jitter after the barrier cannot separate the
# two read-modify-writes on its own, short enough to stay a unit test.
_WINDOW_S = 0.6
# The parent never waits on a child forever; a wedged child fails the test.
_JOIN_S = 90.0


# --------------------------------------------------------------------------- #
# Child-process helpers. Module level so `spawn` can import them by name, and
# each re-inserts the da/ path itself rather than trusting an inherited one.
# --------------------------------------------------------------------------- #
def _child_store(da_dir: str):
    if da_dir not in sys.path:
        sys.path.append(da_dir)
    os.environ["LEAF_DRAWING_STORE"] = "legacy"  # never the postgres authority
    import store as child_store
    return child_store


class WidenedBackend(store.FilesystemBackend):
    """A FilesystemBackend that holds the manifest read open for `delay_s`.

    Only the timing changes. Widening the load/edit/save window turns a race
    that is real but rarely observed into one that is observed every run, which
    is the difference between a test that pins the defect and a test that
    happens to pass. `test_drawing_upload_authority_postgres.py` slows a backend
    the same way for the same reason.
    """

    def __init__(self, root_dir: str, delay_s: float) -> None:
        super().__init__(root_dir)
        self.delay_s = delay_s

    def get(self, key: str) -> bytes:
        data = super().get(key)
        if key.endswith("manifest.json"):
            time.sleep(self.delay_s)
        return data


def _acquire_child(da_dir, root, delay_s, holder, barrier, results):
    """Acquire a checkout on a FREE drawing, reporting the generation stamped."""
    child_store = _child_store(da_dir)
    backend = WidenedBackend(root, delay_s)
    try:
        barrier.wait(timeout=_JOIN_S)  # both processes enter the window together
        fence = child_store.acquire_checkout_fence(
            backend, TENANT, DRAWING, holder, 300.0)
        results.put((holder, "ok", fence))
    except Exception as exc:  # reported, not raised: the parent asserts on it
        results.put((holder, "err", f"{type(exc).__name__}: {exc}"))


def _hold_lock_forever_child(da_dir, root, ready):
    """Take the OS checkout lock, say so, then block until killed."""
    child_store = _child_store(da_dir)
    backend = child_store.FilesystemBackend(root)
    with backend.cross_process_lock(child_store.checkout_lock_key(TENANT, DRAWING)):
        ready.set()
        time.sleep(3600)


def _timed_lock_child(da_dir, root, ready, results):
    """Report how long the OS checkout lock took to acquire."""
    child_store = _child_store(da_dir)
    backend = child_store.FilesystemBackend(root)
    ready.set()
    started = time.monotonic()
    try:
        with backend.cross_process_lock(child_store.checkout_lock_key(TENANT, DRAWING)):
            results.put(("ok", time.monotonic() - started))
    except Exception as exc:
        results.put(("err", f"{type(exc).__name__}: {exc}"))


def _free_drawing(root: Path) -> str:
    """Create a manifest with NO checkout, the state both racers will read."""
    backend = store.FilesystemBackend(str(root))
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    return str(root)


def _spawn(target, *args):
    proc = multiprocessing.get_context("spawn").Process(target=target, args=args)
    proc.start()
    return proc


@pytest.fixture(autouse=True)
def _legacy_authority(monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")


# --------------------------------------------------------------------------- #
# 1. The defect itself: two processes, one FREE drawing, one winner.
# --------------------------------------------------------------------------- #
def test_two_processes_acquiring_a_free_drawing_yield_exactly_one_lease(tmp_path):
    """FAILS on the pre-fix code: both children are handed generation 1."""
    root = _free_drawing(tmp_path / "store")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()

    procs = [_spawn(_acquire_child, _DA_DIR, root, _WINDOW_S, holder, barrier, results)
             for holder in ("session-a", "session-b")]
    outcomes = [results.get(timeout=_JOIN_S) for _ in procs]
    for proc in procs:
        proc.join(timeout=_JOIN_S)
        assert proc.exitcode == 0, f"child died: exit {proc.exitcode}"

    errors = [o for o in outcomes if o[1] == "err"]
    assert not errors, f"children raised: {errors}"

    fences = [fence for _, _, fence in outcomes]
    granted = [f for f in fences if f is not None]

    assert len(granted) == 1, (
        f"exactly one process may take a FREE drawing, got {fences}. Two "
        f"non-None generations means both callers were told they hold the "
        f"lease against ONE persisted checkout.")
    assert len(set(granted)) == len(granted), (
        f"two leases share generation {granted}: capabilities minted for each "
        f"would both verify against the single persisted lease")

    # And the persisted record agrees with the one winner.
    final = store.load_manifest(store.FilesystemBackend(root), TENANT, DRAWING)
    checkout = final["checkout"]
    assert checkout is not None and checkout["fence"] == granted[0]
    winner = [h for h, _, f in outcomes if f is not None][0]
    assert checkout["holder"] == winner


def test_second_process_is_refused_not_merely_delayed(tmp_path):
    """The loser must be REFUSED, not queued behind the winner and then granted.

    Serializing alone would be a silent lease handover: B waits, sees an active
    lock it does not own, and must come back None.
    """
    root = _free_drawing(tmp_path / "store")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()

    procs = [_spawn(_acquire_child, _DA_DIR, root, _WINDOW_S, holder, barrier, results)
             for holder in ("first", "second")]
    outcomes = [results.get(timeout=_JOIN_S) for _ in procs]
    for proc in procs:
        proc.join(timeout=_JOIN_S)

    assert sorted(f is None for _, _, f in outcomes) == [False, True], (
        f"expected one grant and one refusal, got {outcomes}")


# --------------------------------------------------------------------------- #
# 2. The lock is real, and it lets go.
# --------------------------------------------------------------------------- #
def test_cross_process_lock_actually_excludes_a_second_process(tmp_path):
    """Guards against a VACUOUS pass.

    If the platform primitive silently granted everyone, every test above would
    still pass on a fast enough machine. This one asserts the contender was
    genuinely made to wait, so the lock cannot quietly become a no-op.
    """
    root = str(tmp_path / "store")
    ctx = multiprocessing.get_context("spawn")
    contender_ready, results = ctx.Event(), ctx.Queue()

    backend = store.FilesystemBackend(root)
    with backend.cross_process_lock(store.checkout_lock_key(TENANT, DRAWING)):
        proc = _spawn(_timed_lock_child, _DA_DIR, root, contender_ready, results)
        assert contender_ready.wait(timeout=_JOIN_S), "contender never started"
        time.sleep(1.0)  # held here; the contender must still be blocked
        assert results.empty(), "contender took a lock this process holds"

    status, waited = results.get(timeout=_JOIN_S)
    proc.join(timeout=_JOIN_S)
    assert status == "ok", waited
    assert waited >= 0.5, (
        f"contender acquired in {waited:.3f}s while the lock was held for ~1s: "
        f"the OS lock is not excluding anything")


def test_lock_is_released_when_the_body_raises(tmp_path):
    """Acceptance 2, exception path: a failed acquire must not wedge the drawing.

    Both layers have to let go. The threading lock is checked by acquiring again
    in THIS process; the OS lock is checked by acquiring from a SECOND one,
    which is the half a same-process retry cannot see.
    """
    root = _free_drawing(tmp_path / "store")

    class ExplodingBackend(store.FilesystemBackend):
        def put(self, key: str, data: bytes) -> None:
            if key.endswith("manifest.json"):
                raise RuntimeError("save blew up inside the guarded section")
            super().put(key, data)

    with pytest.raises(RuntimeError, match="save blew up"):
        store.acquire_checkout_fence(
            ExplodingBackend(root), TENANT, DRAWING, "doomed", 300.0)

    # Same process: the threading lock was released by the guard's `finally`.
    fence = store.acquire_checkout_fence(
        store.FilesystemBackend(root), TENANT, DRAWING, "after", 300.0)
    assert fence is not None, "the in-process lock stayed held after an exception"

    # Another process: the OS lock went with the closed descriptor.
    store.release_checkout(store.FilesystemBackend(root), TENANT, DRAWING,
                           expected_fence=fence)
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(1), ctx.Queue()
    proc = _spawn(_acquire_child, _DA_DIR, root, 0.0, "other-process",
                  barrier, results)
    _, status, value = results.get(timeout=_JOIN_S)
    proc.join(timeout=_JOIN_S)
    assert status == "ok", value
    assert value is not None, "the OS lock stayed held after an exception"


def test_lock_is_released_when_the_holder_process_dies(tmp_path):
    """Acceptance 2, death path: a killed holder must not wedge the drawing.

    This is why the lock is an advisory lock on an open descriptor rather than a
    lockfile with a staleness heuristic: the kernel drops it when the process
    dies, so nothing here has to guess whether a live holder is dead.
    """
    root = _free_drawing(tmp_path / "store")
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()

    holder = _spawn(_hold_lock_forever_child, _DA_DIR, root, ready)
    assert ready.wait(timeout=_JOIN_S), "holder never took the lock"
    holder.kill()
    holder.join(timeout=_JOIN_S)

    fence = store.acquire_checkout_fence(
        store.FilesystemBackend(root), TENANT, DRAWING, "survivor", 300.0)
    assert fence is not None, "a killed holder permanently wedged the drawing"


# --------------------------------------------------------------------------- #
# 3. The boundary is declared, not implied.
# --------------------------------------------------------------------------- #
def test_backends_declare_whether_they_are_cross_process_safe():
    assert store.FilesystemBackend.cross_process_checkout_safe is True
    assert store.InMemoryBackend.cross_process_checkout_safe is True
    # Object storage has no descriptor to lock, and the base class defaults to
    # False so a backend added later must answer deliberately.
    assert store.OSSBackend.cross_process_checkout_safe is False
    assert store.StorageBackend.cross_process_checkout_safe is False


def test_a_backend_without_an_os_lock_refuses_rather_than_faking_one():
    """`OSSBackend` must not hand back a lock object that grants everyone."""
    with pytest.raises(store.CrossProcessLockUnavailable):
        store.OSSBackend().cross_process_lock(
            store.checkout_lock_key(TENANT, DRAWING))


def test_the_missing_lock_is_warned_not_silently_skipped():
    """The degradation the legacy OSS path still has must be detectable."""
    backend = store.InMemoryBackend()
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    # Same blobs, but now declaring it cannot exclude a second process.
    backend.cross_process_checkout_safe = False

    with pytest.warns(store.CrossProcessCheckoutLockMissing, match="postgres"):
        assert store.acquire_checkout_fence(
            backend, TENANT, DRAWING, "holder", 300.0) is not None


def test_in_memory_and_filesystem_checkouts_emit_no_warning(recwarn):
    """The two backends that ARE safe must not cry wolf."""
    backend = store.InMemoryBackend()
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    assert store.acquire_checkout_fence(
        backend, TENANT, DRAWING, "holder", 300.0) is not None
    assert not [w for w in recwarn
                if issubclass(w.category, store.CrossProcessCheckoutLockMissing)]


def test_lock_sidecar_sits_beside_the_manifest_and_is_never_the_manifest(tmp_path):
    """`save_manifest` replaces the manifest inode on every save, so a lock held
    on that inode would exclude nobody. The sidecar is a separate file."""
    root = str(tmp_path / "store")
    key = store.checkout_lock_key(TENANT, DRAWING)
    assert key != store.manifest_key(TENANT, DRAWING)
    assert os.path.dirname(key) == os.path.dirname(store.manifest_key(TENANT, DRAWING))

    backend = store.FilesystemBackend(root)
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    assert store.acquire_checkout_fence(
        backend, TENANT, DRAWING, "holder", 300.0) is not None

    lock_path = Path(backend._path(key))
    assert lock_path.exists(), "the sidecar lockfile was never created"
    # Surviving a save is the whole point: the next caller must open the SAME
    # inode a current holder owns.
    inode_before = lock_path.stat().st_ino
    store.save_manifest(backend, TENANT, DRAWING,
                        store.load_manifest(backend, TENANT, DRAWING))
    assert lock_path.stat().st_ino == inode_before


def test_release_is_guarded_the_same_way_as_acquire(tmp_path):
    """A release racing an acquire loses the update the other way round, so it
    runs under the same guard. Covers the second call site."""
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))

    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "holder", 300.0)
    assert store.release_checkout(backend, TENANT, DRAWING, expected_fence=fence)

    # The generation still moves forward across release, so a capability minted
    # for the released lease cannot verify against the next one.
    again = store.acquire_checkout_fence(backend, TENANT, DRAWING, "next", 300.0)
    assert again is not None and again > fence
