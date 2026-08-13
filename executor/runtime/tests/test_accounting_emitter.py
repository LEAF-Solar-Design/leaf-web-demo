from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from executor.runtime.accounting import AccountingEmissionError, StructuredAccountingEmitter
from executor.runtime.environment import ENVIRONMENT_VARIABLE, UNSET_ENVIRONMENT_LABEL

RECORD = {
    "invocation_id": "11111111-1111-4111-8111-111111111111",
    "tenant_id": "tenant-demo",
    "session_id": "22222222-2222-4222-8222-222222222222",
    "lease_id": "33333333-3333-4333-8333-333333333333",
    "code_digest": "sha256:" + "a" * 64,
    "state": "accepted",
    "occurred_at": "2026-07-29T22:00:00Z",
}


def emit(record: dict, environment: str | None = "staging") -> dict:
    """Emit one record through a real pipe and return the parsed envelope."""
    patched = {} if environment is None else {ENVIRONMENT_VARIABLE: environment}
    read_fd, write_fd = os.pipe()
    try:
        with mock.patch.dict(os.environ, patched, clear=True):
            StructuredAccountingEmitter(write_fd).emit(record)
        return json.loads(os.read(read_fd, 4096))
    finally:
        os.close(read_fd)
        os.close(write_fd)


class StructuredAccountingEmitterTests(unittest.TestCase):
    def test_writes_one_payload_free_atomic_json_line(self) -> None:
        envelope = emit(RECORD)
        self.assertEqual("leaf.instant.accounting", envelope["event"])
        # Pinned by EQUALITY against the caller's record PLUS the stamped
        # environment label, not by a subset check.  The emitter is the single
        # writer of this envelope, so an unnoticed extra field here is a field
        # that reaches CloudWatch Logs on every accounting line; and this
        # record is payload-free by contract, which a subset check would stop
        # enforcing.
        self.assertEqual({**RECORD, "env": "staging"}, envelope["record"])
        self.assertNotIn("params", json.dumps(envelope).lower())

    def test_every_record_carries_the_environment_the_filters_dimension_on(self) -> None:
        """The InvocationErrors / InvocationLatencyMs filters select
        `$.record.env` as a DIMENSION.  A record without the field publishes NO
        datapoint rather than an undimensioned one, so presence on every record
        is the invariant, and the terraform alarms read nothing without it.
        """
        for state in ("accepted", "started", "terminal"):
            with self.subTest(state=state):
                envelope = emit({**RECORD, "state": state})
                self.assertEqual("staging", envelope["record"]["env"])

    def test_the_stamp_wins_over_a_caller_supplied_env(self) -> None:
        """A caller cannot mislabel which deployment produced the record."""
        envelope = emit({**RECORD, "env": "production"})
        self.assertEqual("staging", envelope["record"]["env"])

    def test_an_unset_variable_still_produces_the_field(self) -> None:
        """Rule 1 of executor.runtime.environment: the key is never omitted.

        An omitted key makes the metric filter drop the event entirely, which
        is a silently missing metric.  The sentinel keeps the line well-formed;
        the LOUD half of this failure is the service entrypoint refusing to
        boot, covered in test_service.
        """
        envelope = emit(RECORD, environment=None)
        self.assertEqual(UNSET_ENVIRONMENT_LABEL, envelope["record"]["env"])

    def test_a_malformed_label_cannot_fail_an_invocation(self) -> None:
        """This emitter RAISES on failure, and the supervisor turns that into
        ACCOUNTING_UNAVAILABLE plus a failed invocation.  A bad environment
        variable must therefore never propagate out of the stamp: it is
        downgraded to the sentinel instead.
        """
        envelope = emit(RECORD, environment="two words")
        self.assertEqual(UNSET_ENVIRONMENT_LABEL, envelope["record"]["env"])

    def test_rejects_a_record_larger_than_the_atomic_pipe_limit(self) -> None:
        with self.assertRaisesRegex(AccountingEmissionError, "atomic"):
            StructuredAccountingEmitter().emit({"unexpected": "x" * 5000})

    def test_the_size_limit_is_checked_after_the_stamp(self) -> None:
        """The stamp adds bytes, so a record that fits BEFORE it must be
        refused rather than written truncated.

        The payload size is load-bearing and was computed, not guessed.  The
        stamp costs exactly 16 bytes (`"env":"staging",`), so only a payload in
        [4018, 4033] fits at 4096 unstamped and exceeds it stamped.  An
        arbitrary oversized payload would raise whichever order the emitter
        used and would prove nothing about the ordering; the control below
        makes that explicit by showing the same record is accepted when no
        stamp is added.
        """
        payload = {"unexpected": "x" * 4025}

        # Control: without the stamp this exact record is under the limit.
        bare = json.dumps(
            {"event": "leaf.instant.accounting", "record": payload},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii") + b"\n"
        self.assertLessEqual(len(bare), 4096, "control: the record must fit unstamped")

        with self.assertRaisesRegex(AccountingEmissionError, "atomic"):
            with mock.patch.dict(os.environ, {ENVIRONMENT_VARIABLE: "staging"}, clear=True):
                StructuredAccountingEmitter().emit(payload)


if __name__ == "__main__":
    unittest.main()
