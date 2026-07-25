"""The PostgreSQL terminal write and its platform mirror must be one transaction.

`async_jobs` (the async-job authority) and the platform `jobs` table are two tables in
one database. They used to be written in two transactions: the authority committed, then
`platform_link.on_terminal` ran and swallowed any failure. A mirror failure therefore left
the platform Job nonterminal forever, and `GET /api/jobs/{job_id}` serves its status
straight from that row, so a caller polled `running` on a run that had already succeeded.

These tests use a fake connection rather than a real database ON PURPOSE. The DB-gated
tests in test_jobs_callbacks_postgres.py skip without DATABASE_URL, and a regression that
only runs in CI is a regression nobody sees locally.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import job_pg_store  # noqa: E402
import platform_link  # noqa: E402


class _Row(dict):
    pass


class FakeConn:
    """Records statements and can be told to blow up on the mirror UPDATE."""

    def __init__(self, *, fail_on_mirror: bool = False):
        self.statements = []
        self.fail_on_mirror = fail_on_mirror

    def execute(self, sql, args=None):
        self.statements.append(sql)
        if "UPDATE jobs SET" in sql:
            if self.fail_on_mirror:
                raise RuntimeError("mirror write failed")
            return _FetchNone()
        if "UPDATE async_jobs" in sql:
            return _FetchOne({"job_id": "job-1"})
        return _FetchNone()


class _FetchOne:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FetchNone:
    @staticmethod
    def fetchone():
        return None


class FakeDb:
    """Mimics platform.db: transaction() commits on clean exit, rolls back on error."""

    def __init__(self, conn):
        self.conn = conn
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def transaction(self, **_kwargs):
        try:
            yield self.conn
        except Exception:
            self.rolled_back = True
            raise
        self.committed = True

    @contextmanager
    def cursor(self):  # pragma: no cover - must never be reached on this path
        raise AssertionError(
            "the postgres terminal path must not take a second pooled connection")


@pytest.fixture
def wired(monkeypatch):
    """Point both the authority store and the mirror at one fake connection."""
    def _build(*, fail_on_mirror=False):
        conn = FakeConn(fail_on_mirror=fail_on_mirror)
        db = FakeDb(conn)
        monkeypatch.setattr(job_pg_store, "_db", lambda: db)
        monkeypatch.setattr(
            platform_link, "_load_platform", lambda: (object(), db, object()))
        return db, conn
    return _build


def _complete(store):
    return store.complete(
        "job-1", 1, "complete", {"ok": True, "cost": {"usd_est": 0.25}}, None,
        {"attempt": 1, "execution_path": "cloud"}, "fp-1", None, 1000.0,
    )


def test_mirror_runs_on_the_authority_connection(wired):
    """Both writes must hit the SAME connection, i.e. the same transaction."""
    db, conn = wired()
    outcome = _complete(job_pg_store.PostgresJobStore())

    assert outcome == "applied"
    authority = [s for s in conn.statements if "UPDATE async_jobs" in s]
    mirror = [s for s in conn.statements if "UPDATE jobs SET" in s]
    assert len(authority) == 1, "authority row must be written once"
    assert len(mirror) == 1, (
        "the platform mirror must be written inside complete(), on the authority's "
        "connection; before this fix it ran afterwards on a separate one")
    assert db.committed is True


def test_mirror_failure_rolls_the_authority_back(wired):
    """A failed mirror must take the authority write down with it, not be swallowed."""
    db, conn = wired(fail_on_mirror=True)

    with pytest.raises(RuntimeError, match="mirror write failed"):
        _complete(job_pg_store.PostgresJobStore())

    assert db.rolled_back is True, (
        "the authority write must roll back when its mirror fails, so the callback is "
        "retried against a clean state")
    assert db.committed is False, (
        "committing the authority while the platform Job stays nonterminal is the bug: "
        "GET /api/jobs/{job_id} would serve 'running' for an already-finished run")


def test_terminal_in_transaction_raises_rather_than_swallowing():
    """The in-transaction path must NOT inherit on_terminal's best-effort swallow."""
    conn = FakeConn(fail_on_mirror=True)

    with pytest.raises(RuntimeError, match="mirror write failed"):
        platform_link.terminal_in_transaction(
            conn, "job-1", "complete", {"ok": True}, None)


def test_on_terminal_still_swallows_for_the_legacy_path(monkeypatch):
    """Legacy behaviour is retained: after a committed write, failure cannot re-raise."""
    conn = FakeConn(fail_on_mirror=True)
    db = FakeDb(conn)
    monkeypatch.setattr(platform_link, "_db_configured", lambda: True)
    monkeypatch.setattr(
        platform_link, "_load_platform", lambda: (object(), db, object()))

    platform_link.on_terminal("job-1", "complete", {"ok": True}, None)  # must not raise


def test_terminal_fields_agree_across_both_paths():
    """One derivation, so the two paths cannot drift apart on status/result/cost."""
    env = {"ok": True, "cost": {"usd_est": 1.5}}
    assert platform_link._terminal_fields("complete", env) == ("succeeded", env, 1.5)
    assert platform_link._terminal_fields("failed", env) == ("failed", None, None)
    assert platform_link._terminal_fields("complete", {"ok": True}) == (
        "succeeded", {"ok": True}, None)
