"""A terminal mirror the SQLite authority cannot deliver must survive as durable work.

The PostgreSQL authority mirrors inside its own transaction (test_jobs_terminal_mirror_atomic.py).
The SQLite authority CANNOT: `jobs.db` and the platform `jobs` table are different databases,
so there is no shared transaction to join. It used to commit the terminal row and then call
`platform_link.on_terminal`, which swallowed every failure. A mirror that failed was therefore
lost with no compensating write, leaving the linked platform Job nonterminal forever while the
run had really finished.

Raising instead is not the fix: the authority write has already committed, so re-raising would
double-report a run that genuinely succeeded. The fix commits a `platform_mirror_pending` marker
WITH the terminal row and clears it only on a confirmed mirror, which turns an undelivered mirror
into durable state the sweep drains.

No real PostgreSQL here. The boundary is faked at `platform_link._update_by_spine` on purpose, so
every test runs on every runner: the DB-gated suite in test_jobs_callbacks_postgres.py skips
without DATABASE_URL, and a regression that only runs in CI is a regression nobody sees locally.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import jobs  # noqa: E402
import platform_link  # noqa: E402


@pytest.fixture
def isolated_jobs(monkeypatch, tmp_path):
    """A private jobs.db, with the reaper thread and executors kept out of the way."""
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "mirror.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "PENDING_REAPS_PATH", tmp_path / "pending_reaps.jsonl")
    monkeypatch.setattr(jobs, "_emit_job_terminal", lambda *a, **k: None)
    yield
    monkeypatch.setattr(jobs, "_conn", None)


class MirrorBoundary:
    """Stands in for the platform pool: records spine_refs, fails on demand."""

    def __init__(self, *, failing: bool = False):
        self.failing = failing
        self.delivered: list[str] = []

    def install(self, monkeypatch, *, configured: bool = True):
        monkeypatch.setattr(platform_link, "_db_configured", lambda: configured)

        def _update(spine_job_id, *, status, result=None, cost_usd=None, conn=None):
            if self.failing:
                raise RuntimeError("platform pool unreachable")
            self.delivered.append(str(spine_job_id))

        monkeypatch.setattr(platform_link, "_update_by_spine", _update)
        return self


def _seed(job_id: str, *, status: str = "running", linked: bool = True) -> None:
    """Insert a nonterminal job row straight into the authority."""
    now = time.time()
    conn = jobs._db()
    conn.execute(
        "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, progress, "
        "created_at, updated_at, attempt, execution_json, org_id, project_id) "
        "VALUES (?, 't-1', 'demo', '{}', 'd.dwg', ?, 'running', ?, ?, 1, '{}', ?, ?)",
        (job_id, status, now, now, "org-1" if linked else None,
         "project-1" if linked else None))
    conn.commit()


def _marker(job_id: str) -> int:
    row = jobs._db().execute(
        "SELECT platform_mirror_pending FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return int(row["platform_mirror_pending"])


def _fail(job_id: str) -> str:
    return jobs.complete_callback(
        job_id, "failed", error={"code": "INTERNAL", "message": "boom", "retryable": True})


# --------------------------------------------------------------------------- #
# the marker rides the authority's own transaction
# --------------------------------------------------------------------------- #
def test_a_failed_mirror_leaves_a_committed_marker(isolated_jobs, monkeypatch):
    """The undelivered mirror must be recoverable, not swallowed."""
    boundary = MirrorBoundary(failing=True).install(monkeypatch)
    _seed("job-1")

    assert _fail("job-1") == "applied", "the authority write must still succeed"
    assert boundary.delivered == []
    assert _marker("job-1") == 1, (
        "a mirror that failed must leave durable work behind; before this fix the failure "
        "was swallowed and the linked platform Job stayed nonterminal forever")


def test_a_delivered_mirror_clears_the_marker(isolated_jobs, monkeypatch):
    boundary = MirrorBoundary().install(monkeypatch)
    _seed("job-2")

    assert _fail("job-2") == "applied"
    assert boundary.delivered == ["job-2"]
    assert _marker("job-2") == 0, "a delivered mirror must not leave the sweep any work"


def test_an_unlinked_process_never_writes_a_marker(isolated_jobs, monkeypatch):
    """The DB-less demo must not take a marker write and an immediate clearing write."""
    MirrorBoundary().install(monkeypatch, configured=False)
    _seed("job-3", linked=False)

    assert _fail("job-3") == "applied"
    assert _marker("job-3") == 0


def test_the_terminal_callback_never_raises_when_the_mirror_fails(isolated_jobs, monkeypatch):
    """Re-raising would double-report a run whose authority write really did land."""
    MirrorBoundary(failing=True).install(monkeypatch)
    _seed("job-4")

    assert _fail("job-4") == "applied"  # must not raise


# --------------------------------------------------------------------------- #
# the sweep drains what the terminal callback could not deliver
# --------------------------------------------------------------------------- #
def test_the_sweep_delivers_a_deferred_mirror(isolated_jobs, monkeypatch):
    boundary = MirrorBoundary(failing=True).install(monkeypatch)
    _seed("job-5")
    _fail("job-5")
    assert _marker("job-5") == 1

    boundary.failing = False
    jobs._retry_pending_platform_mirrors()

    assert boundary.delivered == ["job-5"]
    assert _marker("job-5") == 0, "a delivered retry must clear the marker"


def test_the_sweep_leaves_the_marker_when_the_retry_also_fails(isolated_jobs, monkeypatch):
    MirrorBoundary(failing=True).install(monkeypatch)
    _seed("job-6")
    _fail("job-6")

    jobs._retry_pending_platform_mirrors()

    assert _marker("job-6") == 1, "an undeliverable mirror must stay queued, not be dropped"


def test_the_sweep_abandons_the_batch_on_the_first_failure(isolated_jobs, monkeypatch):
    """Linkage down for one row is down for all; grinding the rest only stalls the sweep."""
    boundary = MirrorBoundary(failing=True).install(monkeypatch)
    for n in (7, 8, 9):
        _seed(f"job-{n}")
        _fail(f"job-{n}")

    calls = []
    original = platform_link.try_terminal

    def _counting(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(platform_link, "try_terminal", _counting)
    jobs._retry_pending_platform_mirrors()

    assert len(calls) == 1, (
        f"the batch must stop at the first failure, tried {calls}")
    assert boundary.delivered == []


def test_the_sweep_is_bounded_per_pass(isolated_jobs, monkeypatch):
    """Three deferred mirrors, batch of two, so one must be left for the next pass."""
    boundary = MirrorBoundary(failing=True).install(monkeypatch)
    for n in (10, 11, 12):
        _seed(f"job-{n}")
        _fail(f"job-{n}")

    monkeypatch.setattr(jobs, "PLATFORM_MIRROR_BATCH", 2)
    boundary.failing = False
    jobs._retry_pending_platform_mirrors()

    assert len(boundary.delivered) == 2, (
        f"at most PLATFORM_MIRROR_BATCH per pass, delivered {boundary.delivered}")


def test_an_unreplayable_row_cannot_starve_the_queue(isolated_jobs, monkeypatch):
    """A row whose payload will never parse must be dropped, not left at the head forever."""
    boundary = MirrorBoundary(failing=True).install(monkeypatch)
    _seed("job-13")
    _fail("job-13")
    conn = jobs._db()
    conn.execute("UPDATE jobs SET error_json = ? WHERE job_id = ?", ("{not json", "job-13"))
    conn.commit()

    boundary.failing = False
    jobs._retry_pending_platform_mirrors()

    assert _marker("job-13") == 0, "an unreplayable mirror must be cleared, not retried forever"
    assert boundary.delivered == []


def test_the_sweep_is_wired_into_the_reaper(isolated_jobs, monkeypatch):
    """An unregistered drain is a drain that never runs."""
    called = []
    monkeypatch.setattr(jobs, "_retry_pending_platform_mirrors",
                        lambda: called.append(True))
    monkeypatch.setattr(jobs, "_retry_pending_reaps", lambda: None)
    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")

    jobs._reap_orphans_once()

    assert called == [True], "the orphan sweep must drain deferred platform mirrors"


# --------------------------------------------------------------------------- #
# the boundary contract
# --------------------------------------------------------------------------- #
def test_try_terminal_raises_when_delivery_fails(monkeypatch):
    MirrorBoundary(failing=True).install(monkeypatch)

    with pytest.raises(RuntimeError, match="platform pool unreachable"):
        platform_link.try_terminal("job-x", "complete", {"ok": True})


def test_legacy_on_terminal_still_swallows_delivery_failure(monkeypatch):
    MirrorBoundary(failing=True).install(monkeypatch)

    platform_link.on_terminal("job-x", "complete", {"ok": True})


def test_linked_marker_survives_temporarily_missing_configuration(
        isolated_jobs, monkeypatch):
    """A restart without DATABASE_URL must not erase durable retry intent."""
    MirrorBoundary().install(monkeypatch, configured=False)
    _seed("job-restart", linked=True)

    assert _fail("job-restart") == "applied"
    assert _marker("job-restart") == 1


def test_try_terminal_reports_success_when_unlinked(monkeypatch):
    """No linkage means nothing to deliver, which is success, not deferred work."""
    MirrorBoundary().install(monkeypatch, configured=False)

    assert platform_link.try_terminal("job-x", "complete", {"ok": True}) is True
