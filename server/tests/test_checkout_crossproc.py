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
import json
import multiprocessing
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DA_DIR = str(_PROJECT_ROOT / "da")
_SERVER_DIR = str(_PROJECT_ROOT / "server")
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


class PausedAtPrecheckBackend(store.FilesystemBackend):
    """Parks a writer in the window the purge has to fit through.

    `_legacy_checkout_guard` asks whether the drawing exists BEFORE it waits for
    the lock, and that answer can go stale while it waits. This backend stops the
    writer at exactly that point: it computes the honest answer FIRST, then holds
    until the purge has finished, then returns what it saw. So the writer really
    did observe a live drawing, and really does reach the lock afterwards, which
    is the sequence a purge running in a second process produces on its own.

    The pause is ONE-SHOT, so the guard's re-check inside the lock is a genuine
    unpaused read of the post-purge filesystem rather than a second wait.
    """

    def __init__(self, root_dir: str, precheck_done=None, purge_done=None) -> None:
        super().__init__(root_dir)
        self.precheck_done = precheck_done
        self.purge_done = purge_done
        self.paused = False

    def exists(self, key: str) -> bool:
        answer = super().exists(key)
        if (key.endswith("manifest.json") and not self.paused
                and self.precheck_done is not None):
            self.paused = True
            self.precheck_done.set()
            self.purge_done.wait(timeout=_PEER_START_S)
        return answer


def _paused_writer_child(da_dir, root, precheck_done, purge_done, results):
    """A legacy writer that passes the pre-lock existence check, is held there
    while the drawing is purged, and then reaches the lock."""
    child_store = _child_store(da_dir)
    backend = PausedAtPrecheckBackend(root, precheck_done, purge_done)
    try:
        fence = child_store.acquire_checkout_fence(
            backend, TENANT, DRAWING, "parked-writer", 300.0)
        results.put(("writer", "ok", fence))
    except Exception as exc:
        results.put(("writer", "err", f"{type(exc).__name__}: {exc}"))


class HoldsTheManifestOpenBackend(store.FilesystemBackend):
    """Parks a writer INSIDE the guard, between its manifest read and its save.

    This is where the resurrect-after-purge gap actually lives. A writer stopped
    BEFORE the lock is refused by the guard's existence check, and one stopped
    after its save has already finished; only a writer holding the record open
    across a deletion can put the drawing back. The read is the load every legacy
    writer does as its first act inside the guard.
    """

    def __init__(self, root_dir: str, inside=None, release=None) -> None:
        super().__init__(root_dir)
        self.inside = inside
        self.release = release
        self.held = False

    def get(self, key: str) -> bytes:
        data = super().get(key)
        if key.endswith("manifest.json") and not self.held and self.inside is not None:
            self.held = True
            self.inside.set()
            self.release.wait(timeout=_PEER_START_S)
        return data


def _midwrite_writer_child(da_dir, root, inside, release, results):
    """A legacy writer holding one drawing's manifest open across a purge."""
    child_store = _child_store(da_dir)
    backend = HoldsTheManifestOpenBackend(root, inside, release)
    try:
        fence = child_store.acquire_checkout_fence(
            backend, TENANT, DRAWING, "mid-write", 300.0)
        results.put(("writer", "ok", fence))
    except Exception as exc:
        results.put(("writer", "err", f"{type(exc).__name__}: {exc}"))


def _purge_child(server_dir, da_dir, guest_root, uploads_dir, results,
                 lock_budget_s=None):
    """Run the REAL guest purge, in its own OS process.

    `lock_budget_s` shortens the store's checkout budget so a test that expects
    the purge to be BLOCKED does not have to sit through the production 30s.
    """
    for path in (server_dir, da_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.environ["LEAF_GUEST_STORE_DIR"] = guest_root
    os.environ["LEAF_UPLOADS_DIR"] = uploads_dir
    os.environ["LEAF_DRAWING_STORE"] = "legacy"
    try:
        if lock_budget_s is not None:
            import store as child_store
            child_store._CHECKOUT_LOCK_TIMEOUT_S = float(lock_budget_s)
        import guest_uploads
        results.put(("ok", guest_uploads.purge_expired()))
    except Exception as exc:
        results.put(("err", f"{type(exc).__name__}: {exc}"))


def _receipts(guest_root) -> list:
    log = Path(guest_root) / "purge.log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").strip().splitlines() if line]


def _purge_holder_child(da_dir, root, ready, release, results=None):
    """Sit inside `legacy_purge_guard` until told to leave."""
    child_store = _child_store(da_dir)
    try:
        backend = child_store.FilesystemBackend(root)
        with child_store.legacy_purge_guard(backend, TENANT, DRAWING):
            ready.set()
            release.wait(timeout=_JOIN_S)
        if results is not None:
            results.put(("holder", "ok", None))
    except BaseException as exc:  # reported, not raised: the parent asserts on it
        if results is not None:
            results.put(("holder", "err", f"{type(exc).__name__}: {exc}"))
        raise


def _ingest_after_purge_child(da_dir, root, local_path, marker_path, ready, go,
                              results):
    """The CREATING writer: an extraction that finishes after its upload was
    purged. Its precondition is the same marker read `run_extraction` does."""
    child_store = _child_store(da_dir)
    backend = child_store.FilesystemBackend(root)
    ready.set()
    go.wait(timeout=_PEER_START_S)
    try:
        out = child_store.ingest_drawing(
            backend, TENANT, local_path, drawing_id=DRAWING,
            precondition=lambda: os.path.exists(marker_path))
        results.put(("ingest", "ok", out["version"]))
    except Exception as exc:
        results.put(("ingest", "err", f"{type(exc).__name__}: {exc}"))


def _lock_waiter_child(da_dir, root, parked, acquired, release, results):
    """Park on the lock file's CURRENT inode, then take whatever it gets.

    `parked` is set from inside the wait loop's first sleep, which is reached
    only after the file is open and one lock attempt has already been refused.
    That makes "this child is holding the pre-retirement inode open" a fact the
    parent can wait for rather than a delay it has to guess at.
    """
    child_store = _child_store(da_dir)
    real_sleep = time.sleep

    def announce_then_sleep(seconds):
        parked.set()
        real_sleep(seconds)

    child_store.time.sleep = announce_then_sleep
    backend = child_store.FilesystemBackend(root)
    try:
        with backend.cross_process_lock(child_store.checkout_lock_key(TENANT, DRAWING)):
            acquired.set()
            release.wait(timeout=_JOIN_S)
        results.put(("waiter", "ok", None))
    except Exception as exc:
        results.put(("waiter", "err", f"{type(exc).__name__}: {exc}"))


def _free_drawing(root: Path) -> str:
    """Create a manifest with NO checkout, the state both racers will read."""
    backend = store.FilesystemBackend(str(root))
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    return str(root)


def _expired_guest_drawing(guest_root: Path) -> Path:
    """A guest drawing the purge will consider due: a real store manifest plus
    an upload marker stamped in the past."""
    backend = store.FilesystemBackend(str(guest_root))
    store.save_manifest(backend, TENANT, DRAWING,
                        store._new_manifest(TENANT, DRAWING))
    ddir = guest_root / "tenants" / TENANT / "drawings" / DRAWING
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    (ddir / "upload.state.json").write_text(
        json.dumps({"attempt": "a1", "status": "ready",
                    "retention_expires_at": stale.isoformat()}),
        encoding="utf-8")
    return ddir


def _lock_files(root) -> list:
    prefix = Path(store.FilesystemBackend(str(root))._path("checkout-locks"))
    return sorted(prefix.rglob("*.lock")) if prefix.exists() else []


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
                # Forwarded, not swallowed: the guard reclaims the lock FILE
                # through this value when it refuses a purged drawing, and a spy
                # that yielded None would turn that into an AttributeError.
                with super().cross_process_lock(key) as held:
                    yield held
            finally:
                os_depth["n"] -= 1

    @contextlib.contextmanager
    def spy(backend, tid, did, **kwargs):
        seen.append((tid, did))
        depth["n"] += 1
        try:
            with real_guard(backend, tid, did, **kwargs) as held:
                yield held
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


# --------------------------------------------------------------------------- #
# 6. The purge and the store hold ONE lock, so a receipt cannot be undone.
# --------------------------------------------------------------------------- #
def test_the_purge_takes_the_same_lock_the_writers_take(tmp_path):
    """The premise everything below rests on, asked as a BOOLEAN.

    The purge used to hold only `guest_uploads.drawing_lock`, a dict on one
    interpreter's heap that a second process cannot see, so "both take a lock"
    was true and meant nothing. What has to be true is that they take the SAME
    one. `_try_os_lock` is non-blocking: it cannot return True while another
    process holds the file, and cannot return False if the purge is holding
    something else. No scheduling changes either answer.
    """
    root = _free_drawing(tmp_path / "store")
    path = store.FilesystemBackend(root)._path(store.checkout_lock_key(TENANT, DRAWING))
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    results = ctx.Queue()

    holder = _spawn(_purge_holder_child, _DA_DIR, root, ready, release, results)
    try:
        if not ready.wait(timeout=_JOIN_S):
            holder.join(timeout=5)
            raise AssertionError(
                f"the purge never entered the guard; child said "
                f"{results.get(timeout=5) if not results.empty() else 'nothing'} "
                f"(exit {holder.exitcode})")
        with open(path, "a+b") as handle:
            assert store._try_os_lock(handle) is False, (
                "took the drawing's checkout lock while the purge was inside its "
                "own guard: the two are not the same lock, so the purge and the "
                "store never exclude each other across processes")
    finally:
        release.set()
        holder.join(timeout=_JOIN_S)

    # And it was the purge excluding us, not the file being unavailable forever.
    with open(path, "a+b") as handle:
        assert store._try_os_lock(handle) is True


def test_the_purge_cannot_delete_a_drawing_a_writer_is_mid_write_on(tmp_path):
    """GAP 1, end to end, with the REAL purge in its own OS process.

    The writer is parked where the gap actually is: inside the guard, holding
    one drawing's manifest open between its read and its save. Before this fix
    the purge held only its own `drawing_lock`, which that writer's process
    cannot see, so the purge deleted the directory and wrote a "deleted" receipt
    while the writer was mid-record — and `FilesystemBackend.put` recreates
    missing parents, so the save put the drawing straight back behind the
    receipt.

    The invariant asserted is the receipt's meaning, not a lock's presence: a
    drawing named in a "deleted" line must be gone once every writer that was in
    flight has finished. The purge blocking and reporting the drawing as not
    deleted is the honest outcome, and the sweep after the writer leaves proves
    the drawing is delayed rather than made immortal.

    Nothing here is timed. The purge starts only once the writer announces it is
    inside, and the writer is released only once the purge has reported.
    """
    guest_root = tmp_path / "guest"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    ddir = _expired_guest_drawing(guest_root)
    ctx = multiprocessing.get_context("spawn")
    inside, release = ctx.Event(), ctx.Event()
    writer_results, purge_results = ctx.Queue(), ctx.Queue()

    writer = _spawn(_midwrite_writer_child, _DA_DIR, str(guest_root),
                    inside, release, writer_results)
    try:
        assert inside.wait(timeout=_JOIN_S), (
            "the writer never reached its manifest read")
        purger = _spawn(_purge_child, _SERVER_DIR, _DA_DIR, str(guest_root),
                        str(uploads), purge_results, 2.0)
        status, first = purge_results.get(timeout=_JOIN_S)
        purger.join(timeout=_JOIN_S)
        assert status == "ok", first
    finally:
        release.set()

    _, writer_status, writer_value = writer_results.get(timeout=_JOIN_S)
    writer.join(timeout=_JOIN_S)
    assert writer_status == "ok", (
        f"the writer lost its write to the purge: {writer_value}")

    deleted = [r for r in _receipts(guest_root) if r["status"] == "deleted"]
    assert not deleted, (
        f"the purge wrote a 'deleted' receipt for a drawing a writer was "
        f"mid-write on, and that writer then saved: {deleted}")
    assert first["count"] == 0, f"the purge deleted a drawing under a writer: {first}"
    assert ddir.exists(), "the writer's own save was destroyed mid-write"
    assert [r["status"] for r in _receipts(guest_root)] == ["failed"], (
        f"a purge that deleted nothing must say so: {_receipts(guest_root)}")

    # Delayed, not immortal: with the writer gone the next sweep takes it, and
    # THAT receipt is true.
    purger = _spawn(_purge_child, _SERVER_DIR, _DA_DIR, str(guest_root),
                    str(uploads), purge_results)
    status, second = purge_results.get(timeout=_JOIN_S)
    purger.join(timeout=_JOIN_S)
    assert status == "ok", second
    assert second["count"] == 1, f"the drawing was never collected: {second}"
    assert not ddir.exists()
    assert [r["status"] for r in _receipts(guest_root)] == ["failed", "deleted"]


def test_a_writer_that_wakes_up_behind_a_purge_is_refused(tmp_path):
    """The other half: a writer whose pre-lock existence check went stale.

    It passes that check while the drawing is still there, is held at exactly
    that point, and reaches the lock only after a REAL `purge_expired` in another
    process has deleted the drawing. It must find the drawing gone and refuse,
    rather than saving a whole manifest into a directory `put` would recreate.
    """
    guest_root = tmp_path / "guest"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    ddir = _expired_guest_drawing(guest_root)
    ctx = multiprocessing.get_context("spawn")
    precheck_done, purge_done = ctx.Event(), ctx.Event()
    writer_results, purge_results = ctx.Queue(), ctx.Queue()

    writer = _spawn(_paused_writer_child, _DA_DIR, str(guest_root),
                    precheck_done, purge_done, writer_results)
    try:
        assert precheck_done.wait(timeout=_JOIN_S), (
            "the writer never reached its pre-lock existence check")
        purger = _spawn(_purge_child, _SERVER_DIR, _DA_DIR, str(guest_root),
                        str(uploads), purge_results)
        status, payload = purge_results.get(timeout=_JOIN_S)
        purger.join(timeout=_JOIN_S)
        assert status == "ok", payload
        assert payload["count"] == 1, f"the purge did not delete the drawing: {payload}"
    finally:
        purge_done.set()

    _, writer_status, writer_value = writer_results.get(timeout=_JOIN_S)
    writer.join(timeout=_JOIN_S)

    assert not ddir.exists(), (
        f"the writer recreated {ddir} after the purge had already written its "
        f"'deleted' receipt")
    assert writer_status == "err" and "KeyError" in writer_value, (
        f"a writer that woke up behind a purge was allowed to proceed "
        f"({writer_status}: {writer_value})")
    assert [r["status"] for r in _receipts(guest_root)] == ["deleted"], (
        _receipts(guest_root))


def test_an_extraction_that_finishes_after_the_purge_does_not_resurrect_it(tmp_path):
    """GAP 1 for the one writer the existence check cannot cover.

    `ingest_drawing` CREATES, so "the drawing is missing" is its normal case and
    it has no manifest to re-read. It carries its own evidence instead: the
    upload marker its extraction belongs to, re-read inside the lock. The purge
    deletes that marker with the rest of the drawing, so an extraction that
    finishes afterwards aborts rather than writing version 1 into a directory a
    receipt already called gone.
    """
    guest_root = tmp_path / "guest"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    ddir = _expired_guest_drawing(guest_root)
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"extracted-bytes")
    marker = ddir / "upload.state.json"

    ctx = multiprocessing.get_context("spawn")
    ready, go = ctx.Event(), ctx.Event()
    ingest_results, purge_results = ctx.Queue(), ctx.Queue()

    child = _spawn(_ingest_after_purge_child, _DA_DIR, str(guest_root),
                   str(payload), str(marker), ready, go, ingest_results)
    try:
        assert ready.wait(timeout=_JOIN_S), "the ingest child never started"
        purger = _spawn(_purge_child, _SERVER_DIR, _DA_DIR, str(guest_root),
                        str(uploads), purge_results)
        status, result = purge_results.get(timeout=_JOIN_S)
        purger.join(timeout=_JOIN_S)
        assert status == "ok", result
        assert result["count"] == 1, result
    finally:
        go.set()

    _, ingest_status, ingest_value = ingest_results.get(timeout=_JOIN_S)
    child.join(timeout=_JOIN_S)

    assert not ddir.exists(), (
        f"a late extraction recreated {ddir} behind a 'deleted' receipt")
    assert ingest_status == "err" and "DrawingVanished" in ingest_value, (
        f"the creating writer was allowed to commit after its upload was purged "
        f"({ingest_status}: {ingest_value})")


def test_the_precondition_is_refused_on_the_postgres_authority(monkeypatch):
    """Two mechanisms for one question, and neither may be silently dropped.

    The postgres path settles "is this attempt still mine" with row-level
    authority through `authority_guard`. If `ingest_drawing` merely ignored a
    precondition there, a caller would believe it had protection it did not have.
    """
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    with pytest.raises(ValueError, match="legacy-authority mechanism"):
        store.ingest_drawing(store.InMemoryBackend(), TENANT, "unused",
                             DRAWING, precondition=lambda: True)


# --------------------------------------------------------------------------- #
# 7. The lock FILE is reclaimed, and only ever by a caller holding it.
# --------------------------------------------------------------------------- #
def test_a_purged_drawings_lock_file_is_reclaimed(tmp_path):
    """GAP 2. The prefix is never walked by any drawing cleaner, so without this
    it grows by one empty file per drawing ever purged, forever."""
    guest_root = tmp_path / "guest"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    _expired_guest_drawing(guest_root)

    # A real checkout, so the lock file genuinely exists before the purge.
    backend = store.FilesystemBackend(str(guest_root))
    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "holder", 300.0)
    assert fence is not None
    assert _lock_files(guest_root), "the drawing never got a lock file"

    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    purger = _spawn(_purge_child, _SERVER_DIR, _DA_DIR, str(guest_root),
                    str(uploads), results)
    status, payload = results.get(timeout=_JOIN_S)
    purger.join(timeout=_JOIN_S)
    assert status == "ok", payload
    assert payload["count"] == 1, payload

    assert _lock_files(guest_root) == [], (
        f"the purged drawing's lock file survived the purge: "
        f"{_lock_files(guest_root)}")


def test_a_failed_purge_leaves_the_lock_file_alone(tmp_path):
    """The counterpart. A purge that could NOT delete leaves a LIVE drawing, and
    a live drawing's writers still need their lock file. Retiring it on the way
    out would hand the next two callers two different files."""
    guest_root = tmp_path / "guest"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    _expired_guest_drawing(guest_root)
    backend = store.FilesystemBackend(str(guest_root))
    store.acquire_checkout_fence(backend, TENANT, DRAWING, "holder", 300.0)
    before = _lock_files(guest_root)
    assert before

    import guest_uploads
    os.environ["LEAF_GUEST_STORE_DIR"] = str(guest_root)
    os.environ["LEAF_UPLOADS_DIR"] = str(uploads)
    real_rmtree = guest_uploads.shutil.rmtree
    guest_uploads.shutil.rmtree = lambda *a, **k: None
    try:
        result = guest_uploads.purge_expired()
    finally:
        guest_uploads.shutil.rmtree = real_rmtree

    assert result["count"] == 0, "a surviving drawing must not count as purged"
    assert _lock_files(guest_root) == before, (
        "a purge that deleted nothing still retired the drawing's lock file")


def test_the_reclaim_refuses_a_file_it_no_longer_holds(tmp_path):
    """The identity rule, at the one place that removes a file.

    A lock's identity is its inode. If the name has since been re-created — the
    drawing came back as a fresh upload — the file at that path belongs to
    somebody else's lock, and removing it is the two-callers-one-section defect
    rather than a cleanup.
    """
    mine = tmp_path / "mine.lock"
    theirs = tmp_path / "theirs.lock"
    mine.write_bytes(b"")
    theirs.write_bytes(b"")
    with open(mine, "a+b") as handle:
        # Holding `mine`, pointed at `theirs`: the path no longer names the
        # inode this hold is for.
        held = store._HeldCheckoutLock(str(theirs), handle)
        assert held.reclaim() is False, (
            "reclaimed a lock file this hold does not own")
    assert theirs.exists(), "a file belonging to another lock was removed"


def test_the_identity_check_can_actually_tell_two_files_apart(tmp_path):
    """Anti-vacuity for the check the whole retirement design rests on.

    If `st_ino`/`st_dev` came back as constants on this platform, every
    comparison would answer True, the retired-inode case would never be detected,
    and the tests above would pass while the guard they rely on did nothing. So
    assert both answers, not just the one the happy path needs.
    """
    a = tmp_path / "a.lock"
    b = tmp_path / "b.lock"
    a.write_bytes(b"")
    b.write_bytes(b"")
    with open(a, "a+b") as handle:
        assert store._holds_the_live_file(handle, str(a)) is True
        assert store._holds_the_live_file(handle, str(b)) is False, (
            "two distinct files compare EQUAL on this platform, so a retired "
            "lock file could never be detected")
        assert store._holds_the_live_file(handle, str(tmp_path / "gone.lock")) is False


def test_a_lock_file_is_never_retired_out_from_under_a_live_holder(tmp_path):
    """The trap, and the reason the reclaim is not just an `os.remove`.

    Retiring a file a live holder owns is how two callers end up inside one
    section: on POSIX the holder keeps the unlinked inode and its lock while the
    next caller creates and locks a fresh one. The two platforms rule that out by
    different mechanisms, and this asserts whichever one is real here rather than
    asserting a branch that cannot happen on this host.

    POSIX: a waiter parked on the retired inode is caught by the identity check
    and sent to the live file. Proven by asking whether the LIVE file is still
    lockable while the waiter says it holds the lock — if the waiter were sitting
    on the ghost, it would be.

    Windows: an open handle blocks the unlink outright, so the retirement cannot
    happen at all while any process holds the file. That is the same guarantee
    reached from the other end, and `_HeldCheckoutLock.reclaim` defers to release
    because of it.
    """
    root = _free_drawing(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    path = backend._path(store.checkout_lock_key(TENANT, DRAWING))
    ctx = multiprocessing.get_context("spawn")
    parked, acquired, release = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()

    if not store._CAN_UNLINK_OPEN_FILE:
        holder = _spawn(_purge_holder_child, _DA_DIR, root, parked, release)
        try:
            assert parked.wait(timeout=_JOIN_S), "the holder never took the lock"
            with pytest.raises(OSError):
                os.remove(path)
            assert os.path.exists(path)
        finally:
            release.set()
            holder.join(timeout=_JOIN_S)
        return

    waiter = None
    try:
        with store.legacy_purge_guard(backend, TENANT, DRAWING) as held:
            waiter = _spawn(_lock_waiter_child, _DA_DIR, root, parked, acquired,
                            release, results)
            assert parked.wait(timeout=_JOIN_S), (
                "the waiter never parked on the pre-retirement inode")
            assert held.reclaim() is True, "the holder could not retire its own file"
        assert acquired.wait(timeout=_JOIN_S), "the waiter never got a lock"
        with open(path, "a+b") as handle:
            assert store._try_os_lock(handle) is False, (
                "the LIVE lock file was free while a waiter believed it held the "
                "drawing: the waiter is holding the retired inode, so two callers "
                "can now be inside the section at once")
    finally:
        release.set()
        if waiter is not None:
            waiter.join(timeout=_JOIN_S)


def test_a_refused_writer_does_not_leave_a_fresh_lock_file_behind(tmp_path):
    """Taking the lock CREATES the file, so a writer that wakes up behind a purge
    re-mints one for a drawing that no longer exists. It holds that file
    exclusively at the moment it learns the drawing is gone, which is the same
    proof the purge itself uses, so it retires it on the way out.

    The deletion has to land in the WINDOW, not before it. A drawing already gone
    when the writer starts is refused by the pre-lock check, which never opens
    the lock file and so proves nothing about this path.
    """
    root = _free_drawing(tmp_path / "store")
    store.acquire_checkout_fence(
        store.FilesystemBackend(root), TENANT, DRAWING, "holder", 300.0)
    assert _lock_files(root), "the drawing never got a lock file"

    class PurgeInTheWindowBackend(store.FilesystemBackend):
        """Answers the pre-lock check honestly, then deletes the drawing —
        the interleaving a purge in another process produces by itself."""

        fired = False

        def exists(self, key: str) -> bool:
            answer = super().exists(key)
            if key.endswith("manifest.json") and not self.fired:
                self.fired = True
                shutil.rmtree(Path(self.root) / "tenants")
            return answer

    backend = PurgeInTheWindowBackend(root)
    with pytest.raises(KeyError):
        store.acquire_checkout_fence(backend, TENANT, DRAWING, "late", 300.0)
    assert backend.fired, "the deletion never landed inside the window"

    assert _lock_files(root) == [], (
        f"a writer refused inside the lock left its lock file behind for a "
        f"drawing that no longer exists: {_lock_files(root)}")


def test_an_ingest_that_fails_to_write_does_not_leave_its_lock_file_behind(tmp_path):
    """The CREATING writer's own leak, which the pre-lock refusal cannot cover.

    `ingest_drawing` is the one writer allowed to open the lock file for a
    drawing that does not exist yet, and opening it creates it. If the write then
    fails — a disk error between the version blob and the manifest — the drawing
    never comes to exist, so no purge sweep will ever walk its directory and
    retire the file. One empty file is stranded per occurrence, forever.

    Asserted at BOTH failure points, because they leave different amounts of the
    drawing on disk (a version blob and no manifest, versus neither) and the
    reclaim's proof is about the MANIFEST.
    """
    for fail_on, label in ((".dwg", "version blob"), ("manifest.json", "manifest")):
        root = str(tmp_path / f"store-{label.replace(' ', '-')}")
        backend = store.FilesystemBackend(root)
        payload = tmp_path / "payload.dwg"
        payload.write_bytes(b"dwg-bytes")

        real_put = backend.put

        def exploding_put(key, data, _real=real_put, _on=fail_on):
            if key.endswith(_on):
                raise OSError(28, "No space left on device")
            return _real(key, data)

        backend.put = exploding_put
        with pytest.raises(OSError):
            store.ingest_drawing(backend, TENANT, str(payload), DRAWING)

        assert not backend.exists(store.manifest_key(TENANT, DRAWING)), (
            f"the {label} failure still produced a manifest, so this test is "
            f"not exercising a drawing that never came to exist")
        assert _lock_files(root) == [], (
            f"an ingest that failed at the {label} left a lock file behind for "
            f"a drawing that never came to exist: {_lock_files(root)}")


def test_a_failed_ingest_leaves_an_existing_drawings_lock_file_alone(tmp_path):
    """The counterpart, and the case that keeps the rule about the DRAWING.

    `ingest_drawing` also fails when the drawing ALREADY exists, and that file
    belongs to a live drawing whose other writers still need it. Retiring it
    would hand the next two callers two different files — the same
    two-callers-one-section defect the reclaim is fenced against everywhere else.
    """
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")

    store.ingest_drawing(backend, TENANT, str(payload), DRAWING)
    before = _lock_files(root)
    assert before, "a successful ingest never created a lock file"

    with pytest.raises(ValueError, match="already exists"):
        store.ingest_drawing(backend, TENANT, str(payload), DRAWING)

    assert _lock_files(root) == before, (
        "a refused ingest retired the lock file of a drawing that is still there")


def test_a_failed_ingest_keeps_a_live_uploads_lock_file(tmp_path):
    """The case both review rounds were really about, on the path that matters.

    `run_extraction` calls `ingest_drawing` on a drawing that is ALREADY alive as
    an upload: `upload.state.json` exists, the manifest does not. So the
    production shape of a failed ingest is not "nothing is there" — it is "a live
    upload is there, and its `_mark_failed`, its retry and its purge all still
    want this lock file". Round 2 keyed the reclaim on the manifest alone and
    retired it anyway.

    The proof therefore has to be about the whole drawing, not the manifest:
    anything left under the prefix that this call did not write itself means
    somebody is still to be served.
    """
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")

    ddir = Path(backend._path(store.drawing_prefix(TENANT, DRAWING)))
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "upload.state.json").write_text(
        json.dumps({"attempt": "a1", "status": "extracting"}), encoding="utf-8")
    assert not backend.exists(store.manifest_key(TENANT, DRAWING))

    real_put = backend.put

    def exploding_put(key, data):
        if key.endswith(".dwg"):
            raise OSError(28, "No space left on device")
        return real_put(key, data)

    backend.put = exploding_put
    with pytest.raises(OSError):
        store.ingest_drawing(backend, TENANT, str(payload), DRAWING)

    assert (ddir / "upload.state.json").exists(), "the upload is still live"
    assert _lock_files(root), (
        "a failed ingest retired the lock file of an upload that is still alive "
        "on its marker, which its _mark_failed and purge both still need")


def test_a_backend_that_cannot_enumerate_never_gets_its_lock_file_reclaimed(tmp_path):
    """The fail-safe, made falsifiable.

    `StorageBackend.drawing_object_keys` returns None by default, meaning "I
    cannot tell" — never "empty". A backend added later inherits that, and must
    inherit the answer that FORBIDS the removal rather than a proof it cannot
    make. Without this test the distinction is unexercised: every other test
    uses `FilesystemBackend`, which always enumerates, so treating None as empty
    would pass the whole suite.
    """
    class BlindBackend(store.FilesystemBackend):
        """Cross-process safe, but cannot answer what is under a drawing."""

        def drawing_object_keys(self, tenant_id, drawing_id):
            return None

    root = str(tmp_path / "store")
    backend = BlindBackend(root)
    payload = tmp_path / "payload.dwg"
    payload.write_bytes(b"dwg-bytes")

    real_put = backend.put

    def exploding_put(key, data):
        if key.endswith(".dwg"):
            raise OSError(28, "No space left on device")
        return real_put(key, data)

    backend.put = exploding_put
    with pytest.raises(OSError):
        store.ingest_drawing(backend, TENANT, str(payload), DRAWING)

    assert _lock_files(root), (
        "a backend that cannot prove the drawing is empty still had its lock "
        "file retired, so 'cannot tell' was read as 'nothing is there'")


def test_a_marker_only_uploads_lock_file_survives_a_failed_section(tmp_path):
    """The reason the reclaim lives in `ingest_drawing` and NOT in the guard.

    Round 2 of review caught this as a RED: the first version of the fix keyed
    on the guard's `creating` flag, which reads as "ingest" but is not. It is
    also set by `legacy_drawing_guard(must_exist=False)`, whose caller
    (`_mark_failed`) serves a PRE-INGEST upload — alive on `upload.state.json`
    with no manifest at all — and by `legacy_purge_guard`. Retiring the file on
    any failure in those sections retires a LIVE drawing's lock file, which is
    the two-callers-one-section defect rather than a cleanup.

    So the proof is not "the manifest is missing". It is "the manifest is
    missing AND this caller knows that means its drawing is gone", which only
    the ingest can say, because an absent drawing is its premise.
    """
    root = str(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    ddir = Path(backend._path(store.drawing_prefix(TENANT, DRAWING)))
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "upload.state.json").write_text(
        json.dumps({"attempt": "a1", "status": "extracting"}), encoding="utf-8")
    assert not backend.exists(store.manifest_key(TENANT, DRAWING)), (
        "this case is only interesting while the manifest is absent")

    # `_mark_failed`'s shape: it gets past its marker read and its write raises.
    with pytest.raises(OSError):
        with store.legacy_drawing_guard(backend, TENANT, DRAWING,
                                        must_exist=False):
            raise OSError(28, "No space left on device")

    assert (ddir / "upload.state.json").exists(), "the upload is still live"
    assert _lock_files(root), (
        "a failed `must_exist=False` section retired the lock file of an upload "
        "that is still alive on its marker, so its next two writers would take "
        "two different files")


def test_a_non_creating_section_that_raises_keeps_a_live_drawings_lock_file(tmp_path):
    """The same rule from the other side, for a drawing that HAD a manifest.

    `guest_uploads._wipe_failed_attempt_files` deletes a failed attempt's
    manifest while deliberately keeping `upload.state.json` — the file that
    routes the next retry — and it does not hold this lock, so a `must_exist`
    section really can raise with its manifest gone and its drawing still alive.
    Retiring the file there would hand that drawing's next two writers two
    different files.
    """
    root = _free_drawing(tmp_path / "store")
    backend = store.FilesystemBackend(root)
    store.acquire_checkout_fence(backend, TENANT, DRAWING, "holder", 300.0)
    before = _lock_files(root)
    assert before, "the drawing never got a lock file"

    manifest = Path(backend._path(store.manifest_key(TENANT, DRAWING)))
    with pytest.raises(RuntimeError, match="wipe raced this section"):
        with store.legacy_drawing_guard(backend, TENANT, DRAWING):
            # The wipe's shape: the manifest goes, the drawing does not.
            manifest.unlink()
            raise RuntimeError("wipe raced this section")

    assert _lock_files(root) == before, (
        "a non-creating section that failed with its manifest already wiped "
        "retired the lock file of a drawing that is still alive")


def test_the_store_never_reaches_back_into_the_upload_module():
    """The lock ORDER, held at the source level.

    Callers take `guest_uploads.drawing_lock` first and the store's checkout
    guard second (`purge_expired` and `run_extraction` both do). That is only
    safe while the store never takes them the other way round, and the cheapest
    proof is that the store cannot reach `guest_uploads` at all. A store function
    that imported it and took a drawing lock inside the guard would complete an
    AB-BA cycle and deadlock the two.

    Checked as IMPORTS rather than as text: the module names `guest_uploads` in
    prose all over this area, and a substring search would either fail on a
    comment or have to be loosened until it stopped meaning anything.
    """
    import ast
    import inspect

    imported: list[str] = []
    for node in ast.walk(ast.parse(inspect.getsource(store))):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert not [name for name in imported if name.split(".")[0] == "guest_uploads"], (
        "da/store.py now imports guest_uploads. The purge takes the upload "
        "module's drawing lock BEFORE the store's checkout guard; anything here "
        "taking them the other way round closes an AB-BA cycle.")
