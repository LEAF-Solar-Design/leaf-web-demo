"""Payload-free accounting events for the executor log delivery plane."""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from .environment import resolve_environment_label


class AccountingEmissionError(RuntimeError):
    """The runtime could not hand an accounting event to its log driver."""


class StructuredAccountingEmitter:
    """Write one bounded JSON line atomically to the container stdout pipe."""

    def __init__(self, file_descriptor: int = 1) -> None:
        self._file_descriptor = file_descriptor
        self._lock = threading.Lock()

    def emit(self, record: dict[str, Any]) -> None:
        # `env` is stamped HERE for the same reason the runtime emitter stamps
        # it: this is the single writer of the `leaf.instant.accounting`
        # envelope, so a field added here cannot be forgotten by a future
        # caller.  The InvocationErrors and InvocationLatencyMs metric filters
        # select `$.record.env` as a DIMENSION, and a record missing the field
        # publishes NO datapoint rather than an undimensioned one, so the
        # invariant that matters is presence on every record, not correctness
        # on most of them.
        #
        # The label is resolved through the total `resolve_environment_label`,
        # which cannot raise.  That is deliberate on THIS path specifically:
        # unlike the runtime emitter, a failure here is not swallowed -- it
        # raises AccountingEmissionError, which the supervisor turns into an
        # ACCOUNTING_UNAVAILABLE response and a failed invocation.  A malformed
        # environment variable must not be able to reach that.
        #
        # Size note: the added key costs exactly sixteen bytes against the 4096
        # atomic limit enforced below.  Accounting records are fixed-shape with
        # bounded identifiers, so this cannot tip a real record over, but the
        # limit is checked after the stamp so an oversized record is still
        # refused rather than truncated.
        #
        # LANDMINE FOR THE RESERVED INGEST LANE, recorded because a passing test
        # suite does not cover it.  `executor.control_plane.accounting.
        # validate_record` compares `set(payload)` to an EXACT expected field
        # set, so a payload carrying `env` is refused outright as "unknown or
        # missing fields".  That is harmless today: the only caller of
        # `AccountingService.ingest` is the `POST /v1/accounting` route, and
        # nothing in this runtime posts to it -- the emitted line goes to fd 1
        # and no further.  It is also why the stamp COPIES rather than mutating:
        # the caller's `record` dict is left untouched, so a delivery lane that
        # ships the in-memory record is unaffected.  Only a lane built by
        # parsing this LOG LINE and forwarding `line["record"]` would break, and
        # that lane must either strip `env` or widen the expected set.
        #
        # This is deliberately NOT fixed by widening the validator here: that
        # boundary's strictness is what keeps the accounting record payload-free,
        # and the lane it guards is not live.
        envelope = {
            "event": "leaf.instant.accounting",
            "record": {**record, "env": resolve_environment_label()},
        }
        encoded = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii") + b"\n"
        if len(encoded) > 4096:
            raise AccountingEmissionError("accounting event exceeds the atomic log limit")
        try:
            with self._lock:
                written = os.write(self._file_descriptor, encoded)
        except OSError as exc:
            raise AccountingEmissionError("accounting event could not reach the log driver") from exc
        if written != len(encoded):
            raise AccountingEmissionError("accounting event was only partially written")
