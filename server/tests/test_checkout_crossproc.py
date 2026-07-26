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

import contextlib
import multiprocessing
import os
import shutil
import sys
import threading
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

# How long a racer holds the manifest read open AFTER the peer has announced it
# is entering the store, giving the peer time to reach its own read. Spent in
# full only on a passing run; see `WidenedBackend` for what it does and does not
# guarantee.
_READ_GRACE_S = 3.0
# Waiting for the peer's announcement, which no lock can delay. Generous because
# it is only process startup, and it costs nothing once the peer has arrived.
_PEER_START_S = 60.0
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
    """A FilesystemBackend that holds the manifest read open while the other
    racer gets its chance to read too.

    It waits on `peer_started`, an event the other racer sets IMMEDIATELY BEFORE
    it calls into the store — deliberately not on the other racer reaching its
    read. A correct guard blocks the second racer before its read, so waiting on
    the read would be waiting for something a correct implementation must never
    let happen; the wait would always time out and the widening would do nothing.
    Waiting on "the peer has started trying" is something no lock can prevent, so
    the hold reliably spans the window where a BROKEN guard lets both racers read
    the same free manifest.

    After the peer has started, `grace_s` is the time it is given to reach its
    read. Under a broken guard it gets there and both compute the same generation
    from the same manifest. Under a correct one it is parked on the lock, the
    grace elapses, and this racer proceeds alone.

    WHAT THIS DOES NOT GUARANTEE, stated because the round-2 review correctly
    caught the earlier version of this docstring claiming otherwise: the grace is
    still a timeout, so it can still MASK a defect. If a broken build's second
    racer is descheduled for longer than `grace_s` between setting its event and
    reaching its read, the first racer finishes, the second reads a manifest that
    already carries the lease, and the run looks correct. The window is much
    narrower than waiting on the read was, and there is no blocking call inside
    it, but it is not zero.

    So this test is the end-to-end DEMONSTRATION, not the proof. The proof that
    the lock is not a no-op is
    `test_cross_process_lock_actually_excludes_a_second_process`, which asserts a
    contender was genuinely made to wait and cannot pass on a broken build at any
    scheduling, and `test_no_legacy_manifest_writer_escapes_the_guard`, which is
    static. `test_drawing_upload_authority_postgres.py` slows a backend the same
    way for the same reason.
    """

    def __init__(self, root_dir: str, peer_started=None, self_started=None,
                 grace_s: float = _READ_GRACE_S) -> None:
        super().__init__(root_dir)
        self.peer_started = peer_started
        self.self_started = self_started
        self.grace_s = grace_s

    def announce(self) -> None:
        """Set before entering the store, so the peer's hold covers this racer."""
        if self.self_started is not None:
            self.self_started.set()

    def _hold(self) -> None:
        # The peer is trying; a lock cannot stop it having got this far.
        self.peer_started.wait(timeout=_PEER_START_S)
        time.sleep(self.grace_s)

    def get(self, key: str) -> bytes:
        data = super().get(key)
        if key.endswith("manifest.json") and self.peer_started is not None:
            self._hold()
        return data

    def exists(self, key: str) -> bool:
        present = super().exists(key)
        # `ingest_drawing` decides whether the drawing is new with `exists`, not
        # `get`, so widening only the read would leave its check/save window
        # untouched. Version keys are not widened: only the manifest matters here.
        if key.endswith("manifest.json") and self.peer_started is not None:
            self._hold()
        return present


def _acquire_child(da_dir, root, peer_started, self_started, holder, barrier,
                   results):
    """Acquire a checkout on a FREE drawing, reporting the generation stamped."""
    child_store = _child_store(da_dir)
    backend = WidenedBackend(root, peer_started, self_started)
    try:
        barrier.wait(timeout=_JOIN_S)  # both processes enter the window together
        backend.announce()  # before the store call, so no lock can delay it
        fence = child_store.acquire_checkout_fence(
            backend, TENANT, DRAWING, holder, 300.0)
        results.put((holder, "ok", fence))
    except Exception as exc:  # reported, not raised: the parent asserts on it
        results.put((holder, "err", f"{type(exc).__name__}: {exc}"))


def _commit_child(da_dir, root, local_path, peer_started, self_started, barrier,
                  results):
    """Publish a version, which rewrites the WHOLE manifest.

    The other half of the lost-update pair: `put_drawing` carries `checkout` and
    `checkout_fence` back from whenever it loaded, so unguarded it can erase a
    lease acquired in between.
    """
    child_store = _child_store(da_dir)
    backend = WidenedBackend(root, peer_started, self_started)
    try:
        barrier.wait(timeout=_JOIN_S)
        backend.announce()
        version = child_store.put_drawing(
            backend, TENANT, DRAWING, local_path, None)
        results.put(("commit", "ok", version))
    except Exception as exc:
        results.put(("commit", "err", f"{type(exc).__name__}: {exc}"))


def _hold_lock_forever_child(da_dir, root, ready):
    """Take the OS checkout lock, say so, then block until killed."""
    child_store = _child_store(da_dir)
    backend = child_store.FilesystemBackend(root)
    with backend.cross_process_lock(child_store.checkout_lock_key(TENANT, DRAWING)):
        ready.set()
        time.sleep(3600)


def _ingest_child(da_dir, root, local_path, peer_started, self_started, barrier,
                  results):
    """Create version 1 of a NEW drawing. Unguarded, its "does this already
    exist" check and its save are a read and a write with a window between."""
    child_store = _child_store(da_dir)
    backend = WidenedBackend(root, peer_started, self_started)
    try:
        barrier.wait(timeout=_JOIN_S)
        backend.announce()
        out = child_store.ingest_drawing(backend, TENANT, local_path, DRAWING)
        results.put(("ingest", "ok", out["version"]))
    except Exception as exc:
        results.put(("ingest", "err", f"{type(exc).__name__}: {exc}"))


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
    # Each racer holds its manifest read open while the OTHER gets its chance to
    # read; see `WidenedBackend` for why the wait is on "peer started trying"
    # rather than on the peer's read.
    started_a, started_b = ctx.Event(), ctx.Event()

    procs = [
        _spawn(_acquire_child, _DA_DIR, root, started_b, started_a,
               "session-a", barrier, results),
        _spawn(_acquire_child, _DA_DIR, root, started_a, started_b,
               "session-b", barrier, results),
    ]
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
    started_1, started_2 = ctx.Event(), ctx.Event()

    procs = [
        _spawn(_acquire_child, _DA_DIR, root, started_2, started_1,
               "first", barrier, results),
        _spawn(_acquire_child, _DA_DIR, root, started_1, started_2,
               "second", barrier, results),
    ]
    outcomes = [results.get(timeout=_JOIN_S) for _ in procs]
    for proc in procs:
        proc.join(timeout=_JOIN_S)

    assert sorted(f is None for _, _, f in outcomes) == [False, True], (
        f"expected one grant and one refusal, got {outcomes}")


def test_two_processes_ingesting_one_drawing_yield_exactly_one_success(tmp_path):
    """`ingest_drawing` is the most damaging writer to leave unguarded.

    Its "already exists" refusal and its save are a read and a write with a
    window between, so two ingests of one drawing id can both find it absent and
    both write. What they write is a FRESH manifest — no checkout and no
    `checkout_fence` key at all — so the loser does not merely carry stale
    checkout fields back, it deletes the generation counter and lets
    `_next_fence` start again from 1. An outstanding capability for the erased
    lease then verifies against the reissued generation.

    Missed by round 1 and by round 2's first pass; found by review both times,
    which is why `test_no_legacy_manifest_writer_escapes_the_guard` now
    enumerates the writers from the source instead of from a list.
    """
    root = str(tmp_path / "store")
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    started_1, started_2 = ctx.Event(), ctx.Event()

    procs = [
        _spawn(_ingest_child, _DA_DIR, root, str(payload), started_2, started_1,
               barrier, results),
        _spawn(_ingest_child, _DA_DIR, root, str(payload), started_1, started_2,
               barrier, results),
    ]
    outcomes = [results.get(timeout=_JOIN_S) for _ in procs]
    for proc in procs:
        proc.join(timeout=_JOIN_S)
        assert proc.exitcode == 0, f"child died: exit {proc.exitcode}"

    statuses = sorted(status for _, status, _ in outcomes)
    assert statuses == ["err", "ok"], (
        f"exactly one ingest may create a drawing, got {outcomes}. Two "
        f"successes means the second overwrote the first's manifest with a "
        f"fresh one, discarding any lease and the fence counter with it.")
    refusal = [v for _, status, v in outcomes if status == "err"][0]
    assert "already exists" in refusal, (
        f"the loser must be refused for the RIGHT reason, got {refusal}")


def test_a_commit_racing_an_acquire_loses_neither_write(tmp_path):
    """The SECOND door onto the same bypass, closed by guarding every writer.

    `put_drawing` rewrites the whole manifest, so unguarded it carries
    `checkout`/`checkout_fence` back from whenever it loaded. A commit that
    loaded before a concurrent acquire erases the new lease and restores the
    older generation, and the next acquire then issues that generation a SECOND
    time — two capabilities verifying against one lease, which is exactly what
    the fence exists to prevent.

    Both contributions are asserted, not just the lease, so the test fails
    whichever writer happens to save last: a commit saving last erases the
    lease, an acquire saving last erases the published version.
    """
    root = _free_drawing(tmp_path / "store")
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    started_acq, started_commit = ctx.Event(), ctx.Event()

    procs = [
        _spawn(_acquire_child, _DA_DIR, root, started_commit, started_acq,
               "holder", barrier, results),
        _spawn(_commit_child, _DA_DIR, root, str(payload), started_acq,
               started_commit, barrier, results),
    ]
    outcomes = {name: (status, value)
                for name, status, value in
                (results.get(timeout=_JOIN_S) for _ in procs)}
    for proc in procs:
        proc.join(timeout=_JOIN_S)
        assert proc.exitcode == 0, f"child died: exit {proc.exitcode}"

    assert outcomes["holder"][0] == "ok", outcomes["holder"]
    assert outcomes["commit"][0] == "ok", outcomes["commit"]
    granted, committed = outcomes["holder"][1], outcomes["commit"][1]
    assert granted is not None, "the acquire was refused a FREE drawing"

    final = store.load_manifest(store.FilesystemBackend(root), TENANT, DRAWING)
    checkout = final["checkout"]
    assert checkout is not None, (
        "the commit erased the lease the acquire persisted; the next acquire "
        "would reuse that generation and two capabilities would verify")
    assert checkout["fence"] == granted, (
        f"persisted lease is generation {checkout['fence']}, the acquire was "
        f"told {granted}")
    assert any(int(e["v"]) == committed for e in final["versions"]), (
        "the acquire erased the version the commit published")
    assert int(final["latest"]) == committed


# --------------------------------------------------------------------------- #
# 2. The lock is real, and it lets go.
# --------------------------------------------------------------------------- #
def test_cross_process_lock_actually_excludes_a_second_process(tmp_path):
    """The anti-vacuity check, and it contains NO TIMING.

    An earlier version timed how long a contender waited and required half a
    second. The round-4 review killed it: a contender that is descheduled while
    the holder sleeps reports a long wait caused purely by scheduling, so a
    no-op primitive passed. Any assertion of the form "it took a while" has that
    hole, because slowness is not evidence of exclusion.

    So this asks the primitive a BOOLEAN question instead. The holder signals only
    after it owns the lock, and `_try_os_lock` is non-blocking: a real lock cannot
    return True while another process holds it, and a no-op cannot return False.
    No scheduling can change either answer.
    """
    root = str(tmp_path / "store")
    key = store.checkout_lock_key(TENANT, DRAWING)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()

    holder = _spawn(_hold_lock_forever_child, _DA_DIR, root, ready)
    try:
        assert ready.wait(timeout=_JOIN_S), "holder never took the lock"
        path = store.FilesystemBackend(root)._path(key)
        with open(path, "a+b") as handle:
            assert store._try_os_lock(handle) is False, (
                "took a lock another process demonstrably holds: the OS lock "
                "grants everyone, so every race test above is vacuous")
    finally:
        holder.kill()
        holder.join(timeout=_JOIN_S)

    # And it was the HOLDER excluding us, not the file being permanently
    # unavailable: with the holder gone the same call succeeds.
    with open(store.FilesystemBackend(root)._path(key), "a+b") as handle:
        assert store._try_os_lock(handle) is True, (
            "the lock stayed unavailable after its holder died")


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
    proc = _spawn(_acquire_child, _DA_DIR, root, None, None, "other-process",
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


def test_lock_file_is_not_the_manifest_and_survives_a_manifest_save(tmp_path):
    """`save_manifest` replaces the manifest inode on every save, so a lock held
    on that inode would be held against a file no later caller can open."""
    root = str(tmp_path / "store")
    key = store.checkout_lock_key(TENANT, DRAWING)
    assert key != store.manifest_key(TENANT, DRAWING)

    backend = store.FilesystemBackend(root)
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    assert store.acquire_checkout_fence(
        backend, TENANT, DRAWING, "holder", 300.0) is not None

    lock_path = Path(backend._path(key))
    assert lock_path.exists(), "the lock file was never created"
    # Surviving a save is the whole point: the next caller must open the SAME
    # inode a current holder owns.
    inode_before = lock_path.stat().st_ino
    store.save_manifest(backend, TENANT, DRAWING,
                        store.load_manifest(backend, TENANT, DRAWING))
    assert lock_path.stat().st_ino == inode_before


def test_lock_file_lives_outside_the_drawing_directory(tmp_path):
    """The lock's identity IS its inode, so it must outlive every cleaner that
    clears a drawing while leaving the DRAWING alive.

    `server/guest_uploads.py::_wipe_failed_attempt_files` deletes every child of
    the drawing directory except `upload.state.json` after a failed upload
    attempt. A lock file in there would be unlinked from under a live holder: on
    POSIX the holder keeps the unlinked inode and its lock, the next caller
    creates a fresh inode and locks THAT, and two callers run inside the section.
    So the structural claim is the one worth pinning — not "nothing deletes it
    today", but "no drawing-directory walker can reach it at all".
    """
    manifest_dir = os.path.dirname(store.manifest_key(TENANT, DRAWING))
    key = store.checkout_lock_key(TENANT, DRAWING)
    assert not key.startswith(manifest_dir), (
        f"lock key {key!r} is inside the drawing directory {manifest_dir!r}, "
        f"where a failed-attempt wipe would unlink it under a live holder")

    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "holder", 300.0)
    assert fence is not None

    lock_path = Path(backend._path(key))
    inode_before = lock_path.stat().st_ino

    # Exactly what the failed-attempt wipe does to the drawing directory.
    drawing_dir = Path(backend._path(store.manifest_key(TENANT, DRAWING))).parent
    for child in drawing_dir.iterdir():
        if child.name == "upload.state.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink()

    assert lock_path.exists(), "a failed-attempt wipe destroyed the lock file"
    assert lock_path.stat().st_ino == inode_before, (
        "the lock file was replaced, so a holder and a newcomer would own "
        "different inodes and both enter the section")


def test_no_legacy_manifest_writer_escapes_the_guard():
    """Static: EVERY function in `da/store.py` that calls `save_manifest` must
    also take `_legacy_checkout_guard`.

    Enumerated from the source rather than listed by hand, because the failure
    mode this fix has is a writer ADDED LATER without the guard, and a
    hand-written list is exactly what fails to notice that. Round 1 shipped with
    three writers unguarded and round 2 with one (`ingest_drawing`) still
    unguarded; both were found by review reading the file, which is the job this
    test now does on every run.
    """
    import ast
    import inspect

    def _is_guard_with(node: ast.With) -> bool:
        return any(isinstance(item.context_expr, ast.Call)
                   and isinstance(item.context_expr.func, ast.Name)
                   and item.context_expr.func.id == "_legacy_checkout_guard"
                   for item in node.items)

    def _is_save_call(node) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "save_manifest")

    offenders: list[int] = []

    def _visit(node, inside_guard: bool) -> None:
        """Descend tracking whether we are LEXICALLY inside a guarded `with`.

        Ancestry, not mere co-occurrence in the same function. The round-3 review
        showed why: dedenting a `save_manifest` so it runs AFTER the guard exits
        leaves both names present in the function and is still valid Python, so a
        co-occurrence check reports no offenders while the save is once again
        unprotected.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.With):
                guarded = inside_guard or _is_guard_with(child)
                # A with-item's own expression is evaluated BEFORE the block is
                # entered, so it does not inherit the block's protection.
                for item in child.items:
                    _visit(item, inside_guard)
                for stmt in child.body:
                    _visit(stmt, guarded)
                continue
            if _is_save_call(child) and not inside_guard:
                offenders.append(child.lineno)
            _visit(child, inside_guard)

    _visit(ast.parse(inspect.getsource(store)), False)

    assert not offenders, (
        f"da/store.py writes the manifest OUTSIDE a `_legacy_checkout_guard` "
        f"block at line(s) {offenders}. A whole-document save carries "
        f"`checkout` and `checkout_fence` back to whatever the caller last read, "
        f"which erases a concurrently acquired lease and lets its generation be "
        f"reissued. Note this is deliberately lexical: a helper that saves on a "
        f"guarded caller's behalf will be flagged, and should be inlined or take "
        f"the guard itself rather than rely on its callers.")


def test_the_lock_key_mapping_is_pinned():
    """Pinned against literals, because the mapping is part of the CROSS-PROCESS
    PROTOCOL, not an implementation detail.

    Round 5: two processes agree on a drawing's lock only by deriving the same
    path, so any change to this function under a rolling deploy lets mixed
    versions take different files and both enter the same drawing's section. That
    is the defect the guard exists to prevent, reintroduced by a refactor. A
    literal makes the change impossible to miss: if you are here because this
    test failed, the deploy needs a full drain, not a new expected value.
    """
    assert store.checkout_lock_key("t-pin", "d-pin") == (
        "checkout-locks/0f/"
        "0f2b2ab24a1343d4d3710930eb646b11534c221ced293e8ae17b196bc5686d82.lock")
    assert store.checkout_lock_key(TENANT, DRAWING) == (
        "checkout-locks/fd/"
        "fd53576023d002c02b324b9a600d3dcd505ab53e63268267d024958225614d1d.lock")


def test_the_in_process_lock_map_does_not_grow_with_churn(tmp_path):
    """The map is bounded by CONCURRENT callers, not by drawings ever touched.

    Round 6: with all six writers routed through the guard, a process serving a
    long tail of distinct drawings kept one `threading.Lock` per drawing forever
    and the heap grew with churn.
    """
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")

    before = len(store._LEGACY_CHECKOUT_LOCKS)
    for n in range(25):
        did = f"churn-{n}"
        store.ingest_drawing(backend, TENANT, str(payload), did)
        fence = store.acquire_checkout_fence(backend, TENANT, did, "holder", 300.0)
        store.release_checkout(backend, TENANT, did, expected_fence=fence)

    assert len(store._LEGACY_CHECKOUT_LOCKS) == before, (
        f"25 drawings left {len(store._LEGACY_CHECKOUT_LOCKS) - before} lock-map "
        f"entries behind; the map grows for the life of the process")


def test_the_entry_survives_while_a_caller_still_holds_it(tmp_path):
    """The counterpart risk: evicting too eagerly would hand two callers
    DIFFERENT locks for one drawing and let both into the section."""
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))

    seen = []
    with store._legacy_checkout_lock(TENANT, DRAWING) as outer:
        # A second interested caller must be handed the SAME lock object.
        with store._legacy_checkout_lock(TENANT, DRAWING) as inner:
            assert inner is outer, "two callers got different locks for one drawing"
            seen.append(len(store._LEGACY_CHECKOUT_LOCKS))
        # Inner left; the entry must still be there for the outer caller.
        assert f"{TENANT}/{DRAWING}" in store._LEGACY_CHECKOUT_LOCKS, (
            "the entry was dropped while a caller still held it")
    assert f"{TENANT}/{DRAWING}" not in store._LEGACY_CHECKOUT_LOCKS, (
        "the entry outlived its last caller")
    assert seen == [1]


def test_a_missing_drawing_does_not_create_a_lock_file(tmp_path):
    """Taking the lock creates the file and nothing reclaims it, so a caller must
    not be able to mint one for a drawing id that was never a drawing.

    Round 6: otherwise an authenticated caller grows the prefix by inventing ids.
    """
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    lock_root = Path(backend._path("checkout-locks"))

    for did in ("never-existed-1", "never-existed-2"):
        with pytest.raises(KeyError):
            store.acquire_checkout_fence(backend, TENANT, did, "holder", 300.0)

    existing = list(lock_root.rglob("*.lock")) if lock_root.exists() else []
    assert not existing, f"missing drawings still created lock files: {existing}"

    # And a drawing that DOES exist still gets one, so the check is not blanket.
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    assert store.acquire_checkout_fence(
        backend, TENANT, DRAWING, "holder", 300.0) is not None
    assert list(lock_root.rglob("*.lock")), "a real drawing got no lock file"


def test_each_drawing_gets_its_own_lock_and_no_ids_leak():
    """One lock per drawing, named by digest.

    Per drawing (round 5): `put_drawing` and `ingest_drawing` hold the guard
    across a durable blob write, so two drawings sharing a lock file could push
    each other past `_CHECKOUT_LOCK_TIMEOUT_S` and fail an unrelated tenant's
    request. A bounded shared pool was tried for round 4's file-accumulation
    finding and reverted for exactly that reason.

    By digest (round 4): the prefix is never cleaned, so a raw name would leave a
    tenant and drawing id readable long after the drawing was purged.
    """
    keys = [store.checkout_lock_key(f"t{t}", f"d{d}")
            for t in range(20) for d in range(20)]
    assert len(set(keys)) == len(keys), (
        "two different drawings share a lock file, so one drawing's slow write "
        "can time out an unrelated one")

    leaky = store.checkout_lock_key("acme-corp", "secret-drawing-42")
    assert "acme-corp" not in leaky and "secret-drawing-42" not in leaky, (
        f"the lock path carries the ids it protects: {leaky}")

    # Stable, or it is not a lock at all.
    assert (store.checkout_lock_key(TENANT, DRAWING)
            == store.checkout_lock_key(TENANT, DRAWING))
    # Keyed on BOTH ids, so neither is silently ignored.
    assert (store.checkout_lock_key("t-a", DRAWING)
            != store.checkout_lock_key("t-b", DRAWING))
    assert (store.checkout_lock_key(TENANT, "d-a")
            != store.checkout_lock_key(TENANT, "d-b"))


def test_every_legacy_manifest_writer_runs_inside_the_guard(tmp_path, monkeypatch):
    """The runtime half of the check above: every save actually HAPPENS with the
    guard held.

    Not just that the guard was entered somewhere in the call — the round-3
    review's mutation (a save dedented to run after the guard exits) satisfies
    that weaker reading. This counts guard depth and fails a save taken at depth
    zero, so it catches the dedent dynamically, and also catches the one case the
    static check can only judge lexically: a save reached through a helper.
    """
    seen = []
    depth = {"n": 0}
    os_depth = {"n": 0}
    saves_at_depth_zero = []
    saves_without_os_lock = []
    real_guard = store._legacy_checkout_guard
    real_save = store.save_manifest

    class SpyBackend(store.FilesystemBackend):
        """Counts time spent inside the BACKEND's lock.

        Round 4: being inside `_legacy_checkout_guard` proves nothing on its own,
        because replacing the guard's `with backend.cross_process_lock(...)` with
        a bare `yield` leaves every save both lexically and dynamically inside the
        guard. The cross-process half would be gone and every structural check
        would still pass. So the assertion has to be about the OS lock itself.
        """

        @contextlib.contextmanager
        def cross_process_lock(self, key):
            os_depth["n"] += 1
            try:
                with super().cross_process_lock(key):
                    yield
            finally:
                os_depth["n"] -= 1

    @contextlib.contextmanager
    def spy(backend, tid, did, **kwargs):
        seen.append((tid, did))
        depth["n"] += 1
        try:
            with real_guard(backend, tid, did, **kwargs):
                yield
        finally:
            depth["n"] -= 1

    def checked_save(*args, **kwargs):
        if depth["n"] == 0:
            saves_at_depth_zero.append(args[1:3])
        if os_depth["n"] == 0:
            saves_without_os_lock.append(args[1:3])
        return real_save(*args, **kwargs)

    root = str(tmp_path / "store")
    backend = SpyBackend(root)
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")

    monkeypatch.setattr(store, "_legacy_checkout_guard", spy)
    monkeypatch.setattr(store, "save_manifest", checked_save)
    store.ingest_drawing(backend, TENANT, str(payload), DRAWING)
    first = store.put_drawing(backend, TENANT, DRAWING, str(payload), None)
    # Parented on the first, so head has a parent to walk back to.
    store.put_drawing(backend, TENANT, DRAWING, str(payload), first)
    store.undo(backend, TENANT, DRAWING)
    store.redo(backend, TENANT, DRAWING)
    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "holder", 300.0)
    store.release_checkout(backend, TENANT, DRAWING, expected_fence=fence)

    assert not saves_at_depth_zero, (
        f"the manifest was saved with NO guard held for {saves_at_depth_zero}; "
        f"a save that escapes the guard can erase a concurrently acquired lease")
    assert not saves_without_os_lock, (
        f"the manifest was saved without the BACKEND's cross-process lock held "
        f"for {saves_without_os_lock}; the guard would then exclude only threads "
        f"of this process, which is the defect this whole file exists to close")
    assert len(seen) == 7, (
        f"expected every legacy manifest writer to take the guard, saw {seen}")


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
