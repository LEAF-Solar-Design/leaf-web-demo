from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import unittest

from executor.control_plane.accounting import AccountingError, AccountingService, MAX_OUTPUT_BYTES, validate_record
from executor.control_plane.api import application
from executor.control_plane.store import PostgresStore, StaleFence


def record(*, state="terminal"):
    item = {
        "invocation_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "tenant-demo",
        "session_id": "22222222-2222-4222-8222-222222222222",
        "lease_id": "33333333-3333-4333-8333-333333333333",
        "code_digest": "sha256:" + "a" * 64,
        "state": state,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if state == "terminal":
        item.update({"outcome": "succeeded", "cpu_ms": 9, "wall_ms": 12,
                     "memory_peak_bytes": 1024, "input_bytes": 3, "output_bytes": 4})
    return item


class MemoryAccountingStore:
    def __init__(self):
        self.invocations, self.usage, self.outbox = {}, {}, []

    def record_invocation(self, item):
        key = item["invocation_id"]
        identity = tuple(item[name] for name in ("tenant_id", "session_id", "lease_id", "code_digest"))
        old = self.invocations.get(key)
        if old is None:
            self.invocations[key] = {"identity": identity, "state": "accepted"}
            old = self.invocations[key]
        if old["identity"] != identity:
            raise StaleFence("conflicting immutable invocation identity")
        if item["state"] == "accepted":
            return {"invocation_id": key, "state": old["state"], "charged": False}
        if item["state"] == "started":
            if old["state"] == "accepted": old["state"] = "started"
            return {"invocation_id": key, "state": old["state"], "charged": False}
        if old["state"] == "accepted":
            raise StaleFence("terminal accounting requires a started invocation")
        usage = tuple(item[name] for name in ("outcome", "cpu_ms", "wall_ms", "memory_peak_bytes", "input_bytes", "output_bytes"))
        existing = self.usage.get(key)
        if existing is not None and existing != usage:
            raise StaleFence("conflicting terminal accounting retry")
        charged = existing is None
        self.usage[key] = usage
        old["state"] = item["outcome"]
        if charged: self.outbox.append(key)
        return {"invocation_id": key, "state": old["state"], "charged": charged}


class DummyPlane:
    store = None
    class signer:
        @staticmethod
        def jwks(): return {"keys": []}


class ScriptedCursor:
    def __init__(self, rows, *, fail_outbox=False):
        self.rows, self.executed, self.fail_outbox = list(rows), [], fail_outbox
    def __enter__(self): return self
    def __exit__(self, *unused): return False
    def execute(self, sql, args=()):
        self.executed.append((sql, args))
        if self.fail_outbox and "instant.accounting.recorded" in sql: raise RuntimeError("outbox failed")
    def fetchone(self): return self.rows.pop(0) if self.rows else None
    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class ScriptedConnection:
    def __init__(self, cursor): self.cursor_value, self.exit_error, self.closed = cursor, None, False
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, trace): self.exit_error = exc_type; return False
    def cursor(self): return self.cursor_value
    def close(self): self.closed = True


class AccountingTests(unittest.TestCase):
    def setUp(self): self.store = MemoryAccountingStore()

    def ingest_terminal(self):
        service = AccountingService(self.store)
        service.ingest(record(state="accepted"))
        service.ingest(record(state="started"))
        return service, record()

    def test_first_insert_and_exact_terminal_retry_charge_once(self):
        service, terminal = self.ingest_terminal()
        self.assertTrue(service.ingest(terminal)["charged"])
        self.assertFalse(service.ingest(deepcopy(terminal))["charged"])
        self.assertEqual(self.store.outbox, [terminal["invocation_id"]])

    def test_conflicting_identity_or_usage_fails_closed(self):
        service, terminal = self.ingest_terminal()
        service.ingest(terminal)
        different_usage = deepcopy(terminal); different_usage["cpu_ms"] += 1
        with self.assertRaises(AccountingError) as error: service.ingest(different_usage)
        self.assertEqual(error.exception.code, "ACCOUNTING_CONFLICT")
        different_identity = record(state="accepted"); different_identity["tenant_id"] = "tenant-other"
        with self.assertRaises(AccountingError) as error: service.ingest(different_identity)
        self.assertEqual(error.exception.code, "ACCOUNTING_CONFLICT")

    def test_validation_rejects_bounded_payload_and_credential_failures(self):
        invalid = record(); invalid["output_bytes"] = MAX_OUTPUT_BYTES + 1
        with self.assertRaises(AccountingError): validate_record(invalid)
        invalid_time = record(); invalid_time["occurred_at"] = "2026-07-28T16:00:00"
        with self.assertRaises(AccountingError): validate_record(invalid_time)
        secret = record(); secret["request_payload"] = {"token": "Bearer no"}
        with self.assertRaises(AccountingError): validate_record(secret)
        credential = record(); credential["tenant_id"] = "AKIA1234567890ABCDEF"
        with self.assertRaises(AccountingError): validate_record(credential)

    def test_api_requires_app_secret_and_only_ingests(self):
        store = MemoryAccountingStore()
        app = application(
            DummyPlane(), app_control_secret="app-control", host_lifecycle_secret="host-control",
            accounting_secret="accounting-control", accounting=AccountingService(store),
        )
        body = json.dumps(record(state="accepted")).encode()
        def call(secret):
            captured = []
            result = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/accounting", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "HTTP_X_INSTANT_CONTROL_SECRET": secret}, lambda status, headers: captured.append(status))
            return captured[0], json.loads(b"".join(result))
        self.assertEqual(call("host-control")[0], "401 Unauthorized")
        self.assertEqual(call("app-control")[0], "401 Unauthorized")
        status, payload = call("accounting-control")
        self.assertEqual((status, payload["state"]), ("202 Accepted", "accepted"))

    def test_postgres_terminal_write_includes_one_outbox_in_same_transaction(self):
        item = validate_record(record())
        identity_row = (item["invocation_id"], item["tenant_id"], item["session_id"], item["lease_id"], item["code_digest"], "started")
        cursor = ScriptedCursor([(1,), None, identity_row, (item["invocation_id"],)])
        connection = ScriptedConnection(cursor)
        result = PostgresStore(lambda: connection).record_invocation(item)
        self.assertTrue(result["charged"])
        statements = "\n".join(sql for sql, _ in cursor.executed)
        self.assertIn("INSERT INTO instant_accounting", statements)
        self.assertIn("instant.accounting.recorded", statements)
        self.assertNotIn("payload", statements.lower())
        self.assertTrue(connection.closed)

    def test_postgres_outbox_failure_rolls_back_charge_transaction(self):
        item = validate_record(record())
        identity_row = (item["invocation_id"], item["tenant_id"], item["session_id"], item["lease_id"], item["code_digest"], "started")
        cursor = ScriptedCursor([(1,), None, identity_row, (item["invocation_id"],)], fail_outbox=True)
        connection = ScriptedConnection(cursor)
        with self.assertRaisesRegex(RuntimeError, "outbox failed"):
            PostgresStore(lambda: connection).record_invocation(item)
        self.assertIs(connection.exit_error, RuntimeError)
        self.assertTrue(connection.closed)

    def test_late_terminal_after_executor_loss_is_a_no_charge_no_op(self):
        item = validate_record(record())
        identity_row = (item["invocation_id"], item["tenant_id"], item["session_id"],
                        item["lease_id"], item["code_digest"], "failed")
        recovered = (item["tenant_id"], item["session_id"], item["lease_id"],
                     item["code_digest"], "failed", item["occurred_at"], 0, 0, 0, 0, 0,
                     "executor_lost")
        cursor = ScriptedCursor([(1,), None, identity_row, None, recovered])

        result = PostgresStore(lambda: ScriptedConnection(cursor)).record_invocation(item)

        self.assertEqual(result, {"invocation_id": item["invocation_id"],
                                  "state": "failed", "charged": False})

    def test_fabricated_or_mismatched_lease_identity_is_rejected_before_insert(self):
        item = validate_record(record(state="accepted"))
        cursor = ScriptedCursor([None])
        with self.assertRaisesRegex(StaleFence, "valid durable lease"):
            PostgresStore(lambda: ScriptedConnection(cursor)).record_invocation(item)
        statements = "\n".join(sql for sql, _ in cursor.executed)
        self.assertIn("JOIN instant_sessions", statements)
        self.assertIn("s.tenant_id=%s", statements)
        self.assertIn("s.code_digest=%s", statements)
        self.assertNotIn("INSERT INTO instant_invocations", statements)

    def test_stale_recovery_creates_one_zero_usage_terminal_and_outbox(self):
        item = validate_record(record(state="started"))

        class RecoveryCursor(ScriptedCursor):
            def __init__(self):
                super().__init__([])
                self._candidates = [(item["invocation_id"], item["tenant_id"],
                                     item["session_id"], item["lease_id"], item["code_digest"])]
                self._inserted = True
            def fetchall(self):
                rows, self._candidates = self._candidates, []
                return rows
            def fetchone(self):
                if self._inserted:
                    self._inserted = False
                    return (item["invocation_id"],)
                return None

        cursor = RecoveryCursor()
        store = PostgresStore(lambda: ScriptedConnection(cursor))
        recovered = store.recover_stale_invocations(datetime.now(timezone.utc))
        repeated = store.recover_stale_invocations(datetime.now(timezone.utc))

        self.assertEqual(recovered, [item["invocation_id"]])
        self.assertEqual(repeated, [])
        statements = "\n".join(sql for sql, _ in cursor.executed)
        self.assertIn("FOR UPDATE SKIP LOCKED", statements)
        self.assertIn("state IN ('accepted', 'started')", statements)
        self.assertIn("'executor_lost'", statements)
        self.assertIn("COALESCE(started_at, accepted_at)", statements)
        self.assertEqual(statements.count("instant.accounting.recorded"), 1)
        self.assertNotIn("payload", statements.lower())


if __name__ == "__main__": unittest.main()
