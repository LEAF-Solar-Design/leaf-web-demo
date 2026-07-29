from __future__ import annotations

import json
import os
import unittest

from executor.runtime.accounting import AccountingEmissionError, StructuredAccountingEmitter


class StructuredAccountingEmitterTests(unittest.TestCase):
    def test_writes_one_payload_free_atomic_json_line(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            record = {
                "invocation_id": "11111111-1111-4111-8111-111111111111",
                "tenant_id": "tenant-demo",
                "session_id": "22222222-2222-4222-8222-222222222222",
                "lease_id": "33333333-3333-4333-8333-333333333333",
                "code_digest": "sha256:" + "a" * 64,
                "state": "accepted",
                "occurred_at": "2026-07-29T22:00:00Z",
            }
            StructuredAccountingEmitter(write_fd).emit(record)
            envelope = json.loads(os.read(read_fd, 4096))
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self.assertEqual("leaf.instant.accounting", envelope["event"])
        self.assertEqual(record, envelope["record"])
        self.assertNotIn("params", json.dumps(envelope).lower())

    def test_rejects_a_record_larger_than_the_atomic_pipe_limit(self) -> None:
        with self.assertRaisesRegex(AccountingEmissionError, "atomic"):
            StructuredAccountingEmitter().emit({"unexpected": "x" * 5000})


if __name__ == "__main__":
    unittest.main()
