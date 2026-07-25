"""Concurrent first job submits must start EXACTLY ONE orphan-reaper daemon.

Companion to test_job_migration_thread_race.py, which covers the same class of
in-process thread race on the lazy DB connection. This covers the check-and-set
race the sol-critic review of PR #155 flagged and deliberately left out of scope
there (#155 was observability-only, so a threading fix would have widened its
blast radius). #155 has since merged, so the throttle state its review reasoned
about -- _reaper_failure_state, whose own updates _reaper_log_lock keeps atomic --
is live: two independent sweep schedules reset each other's failure streak, which
no per-update lock can prevent.

jobs.ensure_started() runs on every submit and used to read the module-level
`_reaper_started` flag and the `_executors` dict, then assign them, with nothing
serializing the two steps. Several FastAPI worker threads can enter it at once on
a cold process, so two concurrent first submits could both observe the flag as
False and each start a "job-reaper" daemon. Consequences: duplicate reaper sweeps
running concurrently, interleaved log lines, and overlapping success and failure
sweeps resetting each other's throttle counters. The same window let each racing
thread build a full set of lane executors, leaking all but one set.

The fix serializes both check-and-sets under `_start_lock`, so the losing threads
observe the already-set state and skip.

Each test WIDENS the check-and-set window from inside the guarded region (a sleep
in the reaper thread's start(), and in the executor constructor). Unsynchronized,
every racing caller slips through the check while the winner is still mid-region;
under the lock the losers simply block, so the widening is paid once by the winner
(once for the reaper, once per lane for the executors) instead of by all 8. That is what makes these tests load-bearing rather than
timing-lucky: reverting the `with _start_lock:` block turns both red.

Run:  cd server && python -m pytest tests/test_jobs_reaper_start_race.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module before any project dir shadows it (the local
# platform/ package), matching the sibling job tests' defensive import order.
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault("LEAF_JOBS_STORE", "legacy")

import jobs  # noqa: E402

# Bound to the REAL class at import time: the reaper test replaces
# jobs.threading.Thread, and this file's own racing threads must not be that stub.
_RealThread = threading.Thread

_N_THREADS = 8
# Long enough that every racing thread reaches the check-and-set while the winner
# is still inside it, short enough to keep the test quick. Only the winner pays it,
# because the fix makes the losers block instead of re-running the body.
_WIDEN_S = 0.25


class _StubExecutor:
    """Stands in for ThreadPoolExecutor: records its shape, spawns no threads."""

    def __init__(self, max_workers=None, thread_name_prefix=""):
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix

    def submit(self, *args, **kwargs):  # pragma: no cover - never dispatched here
        raise AssertionError("these tests never dispatch work")


class _ThreadingShim:
    """Replaces ONLY `.Thread` in the jobs module's view of `threading`.

    Patching the stdlib module in place would also swap the class backing this
    file's own racing threads and the Barrier that releases them.
    """

    def __init__(self, thread_cls):
        self.Thread = thread_cls

    def __getattr__(self, name):
        return getattr(threading, name)


def _run_race(errors_out):
    """Release _N_THREADS into ensure_started() at the same instant."""
    barrier = threading.Barrier(_N_THREADS)
    errors_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            jobs.ensure_started()
        except BaseException as exc:  # capture, don't swallow - asserted below
            with errors_lock:
                errors_out.append(exc)

    threads = [_RealThread(target=worker, name=f"race-{i}") for i in range(_N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a racing ensure_started() hung"


def test_concurrent_ensure_started_starts_one_reaper(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    # Pre-built executors isolate the reaper flag as the only contended state, and
    # keep ensure_started() off the submitted-row scan.
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: _StubExecutor(1)})
    monkeypatch.setattr(jobs, "_reaper_started", False)
    monkeypatch.setattr(jobs, "ThreadPoolExecutor", _StubExecutor)

    started: list[str] = []
    started_lock = threading.Lock()

    class _RecordingThread:
        """Records the daemon instead of running it - no live reaper in the suite."""

        def __init__(self, target=None, daemon=None, name=None, **kwargs):
            self._name = name

        def start(self) -> None:
            time.sleep(_WIDEN_S)  # widen the window the fix must close
            with started_lock:
                started.append(self._name)

    monkeypatch.setattr(jobs, "threading", _ThreadingShim(_RecordingThread))

    errors: list[BaseException] = []
    _run_race(errors)

    assert not errors, f"ensure_started() raised under the race: {errors[0]!r}"
    assert started == ["job-reaper"], (
        f"expected exactly 1 orphan-reaper daemon, got {len(started)}: {started}")
    assert jobs._reaper_started is True


def test_concurrent_ensure_started_builds_one_set_of_executors(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_executors", {})
    monkeypatch.setattr(jobs, "_reaper_started", True)  # reaper already up

    built: list[str] = []
    built_lock = threading.Lock()

    class _WideningExecutor(_StubExecutor):
        def __init__(self, max_workers=None, thread_name_prefix=""):
            super().__init__(max_workers, thread_name_prefix)
            time.sleep(_WIDEN_S)  # widen the window the fix must close
            with built_lock:
                built.append(thread_name_prefix)

    monkeypatch.setattr(jobs, "ThreadPoolExecutor", _WideningExecutor)

    errors: list[BaseException] = []
    _run_race(errors)

    assert not errors, f"ensure_started() raised under the race: {errors[0]!r}"
    # One pool per lane, total - not one set per racing thread.
    assert sorted(built) == ["jobworker-fast", "jobworker-slow"], (
        f"expected 1 executor per lane, got {len(built)}: {sorted(built)}")
    assert set(jobs._executors) == {jobs.LANE_FAST, jobs.LANE_SLOW}
