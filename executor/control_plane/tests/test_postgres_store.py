"""PostgreSQL adapter tests.

Set POSTGRES_CONTROL_PLANE_TEST_URL to run the integration test. The test uses
an isolated temporary schema and never reads a general application URL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
import uuid

from executor.control_plane.models import Lease, Session
from executor.control_plane.store import NoCapacity, PostgresStore, StoreUnavailable


NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


class ScriptedCursor:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.executed = []
        self.rowcount = 1

    def __enter__(self): return self
    def __exit__(self, *unused): return False

    def execute(self, sql, args=()):
        self.executed.append((sql, args))
        if self.error:
            raise self.error

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class ScriptedConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *unused): return False
    def cursor(self): return self.cursor_value
    def close(self): self.closed = True


class MissingTableError(Exception):
    sqlstate = "42P01"


class LostConnectionError(Exception):
    sqlstate = "08006"


class PostgresStoreAdapterTests(unittest.TestCase):
    def test_candidate_filters_missing_heartbeats_and_randomizes_selection(self):
        row = ("executor-1", "slot-1", "https://executor.test", 3, 7, "READY", "code", "runtime", 12)
        cursor = ScriptedCursor([row])
        timeout = timedelta(seconds=17)
        store = PostgresStore(lambda: ScriptedConnection(cursor), heartbeat_timeout=timeout)

        slot = store.candidate()

        sql, args = cursor.executed[0]
        self.assertEqual(slot.slot_id, "slot-1")
        self.assertIn("h.last_heartbeat_at IS NOT NULL", sql)
        self.assertIn("h.last_heartbeat_at >= now() - %s", sql)
        self.assertIn("ORDER BY random()", sql)
        self.assertEqual(args, (timeout,))

    def test_atomic_ready_claim_retries_locked_candidates(self):
        row = (
            "executor-2", "slot-2", "https://executor-2.test", 3, 7, "CLAIMED", "code", "runtime", 12,
            uuid.uuid4(), "executor-2", "slot-2", 12, NOW + timedelta(seconds=60), "ACTIVE",
        )
        cursor = ScriptedCursor([None, row])
        store = PostgresStore(lambda: ScriptedConnection(cursor))

        slot, claim = store.claim_ready_slot(NOW, timedelta(seconds=60), max_attempts=2)

        statements = [sql for sql, _ in cursor.executed if "FOR UPDATE OF s SKIP LOCKED" in sql]
        self.assertEqual(len(statements), 2)
        self.assertEqual((slot.slot_id, claim.slot_id), ("slot-2", "slot-2"))
        self.assertIn("h.last_heartbeat_at IS NOT NULL", statements[0])

    def test_atomic_ready_claim_reports_true_exhaustion_after_the_bound(self):
        cursor = ScriptedCursor([None, None])
        store = PostgresStore(lambda: ScriptedConnection(cursor))

        with self.assertRaises(NoCapacity):
            store.claim_ready_slot(NOW, timedelta(seconds=60), max_attempts=2)

        self.assertEqual(len(cursor.executed), 2)

    def test_claim_is_one_atomic_compare_and_set_statement(self):
        claim_id = uuid.uuid4()
        row = (
            "executor-1", "slot-1", "https://executor.test", 3, 7, "CLAIMED", "code", "runtime", 12,
            claim_id, "executor-1", "slot-1", 12, NOW + timedelta(seconds=60), "ACTIVE",
        )
        cursor = ScriptedCursor([row])
        connection = ScriptedConnection(cursor)
        store = PostgresStore(lambda: connection)

        slot, claim = store.claim_slot("executor-1", "slot-1", 11, NOW, timedelta(seconds=60))

        statements = [sql for sql, _ in cursor.executed if "WITH claimed AS" in sql]
        self.assertEqual(len(statements), 1)
        self.assertIn("s.state='READY' AND s.version=%s", statements[0])
        self.assertEqual((slot.state, slot.version, claim.claim_epoch), ("CLAIMED", 12, 12))
        self.assertTrue(connection.closed)

    def test_programming_errors_are_not_relabelled_as_availability(self):
        cursor = ScriptedCursor(error=MissingTableError("instant_sessions is missing"))
        store = PostgresStore(lambda: ScriptedConnection(cursor))

        with self.assertRaises(MissingTableError):
            store.get_session(str(uuid.uuid4()))

    def test_connection_loss_is_fail_closed(self):
        cursor = ScriptedCursor(error=LostConnectionError("connection lost"))
        store = PostgresStore(lambda: ScriptedConnection(cursor))

        with self.assertRaises(StoreUnavailable):
            store.get_session(str(uuid.uuid4()))

    def test_session_adapter_reads_runtime_digest_and_capability(self):
        session_id, claim_id, assignment_id, lease_id = (uuid.uuid4() for _ in range(4))
        row = (
            assignment_id, "tenant-1", session_id, "executor-1", "slot-1", claim_id, 2,
            "catalog", "code", "artifact", '{"capability_id":"drawing.read"}', "runtime", "ACTIVE",
            lease_id, 4, NOW + timedelta(seconds=60), NOW,
        )
        store = PostgresStore(lambda: ScriptedConnection(ScriptedCursor([row])))

        session = store.get_session(str(session_id))

        self.assertEqual(session.runtime_digest, "runtime")
        self.assertEqual(session.capability, {"capability_id": "drawing.read"})
        self.assertEqual(session.lease_sequence, 4)


TEST_URL = os.environ.get("POSTGRES_CONTROL_PLANE_TEST_URL")


@unittest.skipUnless(TEST_URL, "set POSTGRES_CONTROL_PLANE_TEST_URL to run the PostgreSQL integration test")
class PostgresStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            import psycopg
        except ImportError as exc:
            self.skipTest(f"psycopg is required for integration testing: {exc}")
        self.psycopg = psycopg
        self.schema = "control_plane_test_" + uuid.uuid4().hex
        with psycopg.connect(self._test_url(), autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.options = f"-c search_path={self.schema}"
        migrations = sorted((Path(__file__).parents[1] / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
        with self.connect() as conn:
            for migration in migrations:
                conn.execute(migration.read_text(encoding="utf-8"))
        self.store = PostgresStore(self.connect)

    def tearDown(self):
        with self.psycopg.connect(self._test_url(), autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def connect(self):
        return self.psycopg.connect(self._test_url(), options=self.options)

    def _test_url(self):
        if "connect_timeout=" in TEST_URL:
            return TEST_URL
        separator = "&" if "?" in TEST_URL else "?"
        return f"{TEST_URL}{separator}connect_timeout=3"

    def test_claim_activate_renew_release_and_reclaim_slot(self):
        now = datetime.now(timezone.utc)
        executor_id, slot_id = "executor-1", "slot-1"
        self.store.register_host(executor_id, "https://executor.test", [slot_id])
        self.store.set_readiness(executor_id, slot_id, True, "code", "runtime")
        slot = self.store.candidate()
        slot, claim = self.store.claim_slot(executor_id, slot_id, slot.version, now, timedelta(seconds=60))
        session_id = str(uuid.uuid4())
        session = Session(str(uuid.uuid4()), "tenant-1", session_id, executor_id, slot_id, claim.claim_id,
                          1, "catalog", "code", "artifact", "runtime", {"capability_id": "drawing.read"})
        self.store.create_session(session)
        lease = Lease(str(uuid.uuid4()), session_id, executor_id, slot_id, claim.claim_id,
                      slot.host_epoch, slot.slot_epoch, claim.claim_epoch, 1, 1, now, now + timedelta(seconds=60))
        self.store.activate(session_id, lease)
        renewed = Lease(str(uuid.uuid4()), session_id, executor_id, slot_id, claim.claim_id,
                        slot.host_epoch, slot.slot_epoch, claim.claim_epoch, 1, 2, now, now + timedelta(seconds=120))
        self.store.renew(session_id, 1, renewed)

        released = self.store.release(session_id, "done")

        self.assertEqual(released.state, "INVALID")
        with self.assertRaises(NoCapacity):
            self.store.candidate()
        self.store.set_readiness(executor_id, slot_id, True, "code", "runtime")
        self.assertEqual(self.store.candidate().state, "READY")

    def test_accounting_terminal_retry_charges_once_with_one_outbox(self):
        invocation_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        lease_id = str(uuid.uuid4())
        claim_id = str(uuid.uuid4())
        identity = {
            "invocation_id": invocation_id,
            "tenant_id": "tenant-1",
            "session_id": session_id,
            "lease_id": lease_id,
            "code_digest": "sha256:" + "a" * 64,
        }
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO instant_sessions
                     (session_id, tenant_id, code_digest, host_id, slot_id, claim_id,
                      binding_epoch, state, lease_id, lease_sequence, expires_at)
                   VALUES (%s, %s, %s, 'executor-accounting', 'slot-1', %s, 1,
                           'ACTIVE', %s, 1, %s)""",
                (session_id, identity["tenant_id"], identity["code_digest"], claim_id,
                 lease_id, NOW + timedelta(minutes=1)),
            )
            conn.execute(
                """INSERT INTO executor_leases
                     (lease_id, host_id, slot_id, host_epoch, slot_epoch, claim_id,
                      session_id, binding_epoch, lease_sequence, not_before, expires_at, state)
                   VALUES (%s, 'executor-accounting', 'slot-1', 1, 1, %s, %s, 1, 1,
                           %s, %s, 'ACTIVE')""",
                (lease_id, claim_id, session_id, NOW - timedelta(seconds=1),
                 NOW + timedelta(minutes=1)),
            )
        accepted = {**identity, "state": "accepted", "occurred_at": NOW}
        started = {**identity, "state": "started", "occurred_at": NOW + timedelta(milliseconds=1)}
        terminal = {
            **identity,
            "state": "terminal",
            "occurred_at": NOW + timedelta(milliseconds=2),
            "outcome": "succeeded",
            "cpu_ms": 2,
            "wall_ms": 4,
            "memory_peak_bytes": 4096,
            "input_bytes": 10,
            "output_bytes": 20,
        }
        self.store.record_invocation(accepted)
        self.store.record_invocation(started)
        first = self.store.record_invocation(terminal)
        retry = self.store.record_invocation(terminal)

        self.assertTrue(first["charged"])
        self.assertFalse(retry["charged"])
        with self.connect() as conn:
            charges = conn.execute(
                "SELECT count(*) FROM instant_accounting WHERE invocation_id=%s", (invocation_id,),
            ).fetchone()[0]
            outbox = conn.execute(
                "SELECT count(*) FROM control_outbox WHERE kind='instant.accounting.recorded' AND entity_id=%s",
                (invocation_id,),
            ).fetchone()[0]
        self.assertEqual(1, charges)
        self.assertEqual(1, outbox)


if __name__ == "__main__":
    unittest.main()
