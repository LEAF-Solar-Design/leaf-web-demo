"""APS-to-Leaf callback translation adapter (the reserved L3.1 seam).

WHY THIS EXISTS
---------------
``server/da/callbacks.py`` verifies a signed Leaf completion envelope on
``POST /da/callback``, but its own docstring records the gap: native APS
``onComplete`` cannot emit that envelope, so ``LEAF_CALLBACK_PRIMARY=1`` stays
reserved and fails closed "until an APS-to-Leaf callback translation adapter
exists." This module is that adapter. It takes a native APS WorkItem completion
plus the persisted output bytes and produces exactly the envelope
``callbacks.consume_callback`` accepts — no more, no less.

ONE SIGNING AUTHORITY
---------------------
The envelope is signed with ``callbacks.sign_payload`` (loaded from the sibling
module by file path, the same way broker.py loads it), never a parallel HMAC.
So a change to the wire format in callbacks.py cannot silently diverge from what
this adapter produces — there is one signer and one verifier.

BINDINGS AND FAIL-CLOSED MODES
------------------------------
The signed body binds the job, the attempt, the delivery nonce, and the output
evidence (sha256 + size of the persisted output). Translation refuses, raising
``AdapterError(reason)``, when:

  * ``missing_job``          — no job id on the completion;
  * ``workitem_not_success`` — the WorkItem did not succeed (a failure is not a
                               completion receipt; polling reports the failure);
  * ``missing_output``       — no persisted output to attest (an empty receipt
                               would sign a completion that produced nothing);
  * ``wrong_attempt``        — the completion's attempt disagrees with the job's
                               authoritative current attempt (a stale retry's
                               late callback must not complete a newer attempt),
                               is not a real ``int``, or is below 1 (attempts are
                               1-based, so 0 and negatives never ran);
  * ``wrong_workitem``       — the completion names a different WorkItem than the
                               one this job dispatched;
  * ``expired_lease``        — the APS lease deadline has passed (a completion
                               delivered after the lease is not trustworthy);
  * ``bad_nonce``            — empty delivery nonce;
  * ``bad_clock``            — non-finite translation clock;
  * ``missing_workitem``     — no WorkItem id to bind the receipt to;
  * ``no_completion_guard`` — no reservation authority was supplied. REQUIRED:
                               an optional guard defaulting to None is fail-open;
  * ``duplicate_completion`` — the reservation for this (job, ATTEMPT) was already
                               claimed. Keyed on completion identity, not the
                               nonce, AND taken atomically: a read-only "have you
                               seen this?" check let two deliveries both translate
                               before either receipt was recorded, so both were
                               accepted and one attempt completed twice.

Run:  cd server && python -m pytest tests/test_aps_callback_adapter.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_CALLBACKS = None


def _callbacks():
    """Load the sibling callback module by path — the single signing authority.
    Mirrors broker.py's ``_load_server_da_module`` so no sys.path ordering or
    package layout is assumed."""
    global _CALLBACKS
    if _CALLBACKS is None:
        path = Path(__file__).resolve().parent / "callbacks.py"
        spec = importlib.util.spec_from_file_location("leaf_aps_adapter_callbacks", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _CALLBACKS = module
    return _CALLBACKS


_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "complete"})


class AdapterError(Exception):
    """A fail-closed refusal to translate. ``reason`` is a stable machine tag."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ApsWorkItemCompletion:
    """The native APS WorkItem completion metadata the adapter consumes.

    A plain class (not a dataclass): this module can be loaded by
    ``importlib.util.spec_from_file_location`` WITHOUT being registered in
    ``sys.modules`` (broker.py's ``_load_server_da_module`` does exactly that),
    and ``@dataclass`` fails under that loader because it resolves
    ``cls.__module__`` through ``sys.modules``.
    """

    __slots__ = ("job_id", "workitem_id", "attempt", "status", "nonce", "lease_expiry")

    def __init__(self, job_id: str, workitem_id: str, attempt: int, status: str,
                 nonce: str, lease_expiry: float):
        self.job_id = job_id
        self.workitem_id = workitem_id
        self.attempt = attempt
        self.status = status
        self.nonce = nonce
        self.lease_expiry = lease_expiry


class CallbackEnvelope:
    """A signed Leaf callback envelope ready to POST to ``/da/callback``."""

    __slots__ = ("body", "timestamp", "nonce", "signature")

    def __init__(self, body: bytes, timestamp: str, nonce: str, signature: str):
        self.body = body
        self.timestamp = timestamp
        self.nonce = nonce
        self.signature = signature

    def headers(self) -> Dict[str, str]:
        cb = _callbacks()
        return {
            cb.SIGNATURE_HEADER: self.signature,
            cb.TIMESTAMP_HEADER: self.timestamp,
            cb.NONCE_HEADER: self.nonce,
            "Content-Type": "application/json",
        }


def _canonical_body(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def translate(
    completion: ApsWorkItemCompletion,
    output: Optional[bytes],
    *,
    job_attempt: int,
    job_workitem_id: str,
    secret: bytes,
    now: float,
    reserve_completion: Callable[[str, int], bool],
) -> CallbackEnvelope:
    """Translate a successful APS WorkItem completion into a signed envelope.

    ``job_attempt`` and ``job_workitem_id`` are the job store's AUTHORITATIVE
    current attempt and dispatched WorkItem id; the completion must match both.
    ``now`` is the epoch seconds at translation.

    ``reserve_completion(job_id, attempt) -> bool`` is REQUIRED and must
    ATOMICALLY claim the completion identity: return True if this call won the
    claim, False if it was already claimed. It is a reservation, not a question.

    Why that matters, because it is subtle and this module got it wrong twice.
    The identity is (job, attempt), NOT the nonce: the consumer's durable nonce
    store only rejects a repeat of the SAME nonce, so a second delivery bearing a
    FRESH nonce would otherwise mint a second envelope the consumer accepts. But
    keying on identity is not sufficient either if the guard merely READS state:
    two deliveries can both be translated before either receipt is recorded, and
    both are then accepted. Only an atomic claim closes that window, because the
    check and the record become one step. One attempt yields at most one receipt,
    and this adapter is the authority for that; the consumer's nonce store remains
    the second line of defence against a captured envelope replayed verbatim.
    """
    # The CLOCK is validated FIRST. Every later check compares against `now`, so
    # validating it late meant `now="170..."` or None raised an untagged
    # TypeError out of the lease comparison instead of a tagged refusal, and
    # `now=inf` was misreported as `expired_lease`.
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        raise AdapterError("bad_clock")
    if not isinstance(completion.job_id, str) or not completion.job_id.strip():
        raise AdapterError("missing_job")
    if not isinstance(completion.workitem_id, str) or not completion.workitem_id.strip():
        raise AdapterError("missing_workitem")
    # Nonblank is not enough: the receipt must name the WorkItem the job actually
    # dispatched, or a completion for some other WorkItem closes this job.
    if not isinstance(job_workitem_id, str) or not job_workitem_id.strip():
        raise AdapterError("missing_workitem")
    if completion.workitem_id.strip() != job_workitem_id.strip():
        raise AdapterError("wrong_workitem")
    if not isinstance(completion.status, str) or completion.status.strip().lower() not in _SUCCESS_STATUSES:
        raise AdapterError("workitem_not_success")
    if output is None or len(output) == 0:
        raise AdapterError("missing_output")
    # `bool` is a subclass of `int` and `True == 1`, so an unguarded isinstance
    # check lets attempt=True satisfy job_attempt=1 and sign `"attempt":true`.
    if (isinstance(completion.attempt, bool) or not isinstance(completion.attempt, int)
            or isinstance(job_attempt, bool) or not isinstance(job_attempt, int)):
        raise AdapterError("wrong_attempt")
    # Compare and emit the COERCED ints. An int subclass may override __eq__ /
    # __ne__ to claim equality it does not have, which previously let attempt=3
    # satisfy job_attempt=2 and then serialize as 3. Coercing first makes the
    # compared value and the signed value the same number by construction.
    claimed_attempt = int(completion.attempt)
    authoritative_attempt = int(job_attempt)
    if claimed_attempt != authoritative_attempt:
        raise AdapterError("wrong_attempt")
    # Attempts are 1-based. 0 and negatives are not attempts that ever ran.
    if claimed_attempt < 1:
        raise AdapterError("wrong_attempt")
    # NaN defeats every comparison (`now > nan` is False), so a non-finite lease
    # deadline would sail past an ordinary expiry check. callbacks.py guards its
    # own timestamp with math.isfinite for the same reason.
    if (isinstance(completion.lease_expiry, bool)
            or not isinstance(completion.lease_expiry, (int, float))
            or not math.isfinite(completion.lease_expiry)
            or now > completion.lease_expiry):
        raise AdapterError("expired_lease")
    if not isinstance(completion.nonce, str) or not completion.nonce.strip():
        raise AdapterError("bad_nonce")
    # RESERVATION, not a query. `seen_completion` used to ask "have you seen
    # this?", which cannot prevent a race: two deliveries could both be
    # translated BEFORE either receipt was recorded, and the consumer accepted
    # both, completing one attempt twice. `reserve_completion` must ATOMICALLY
    # claim (job, attempt) and return False if it was already claimed, so the
    # check and the record are one indivisible step. Required, never optional: a
    # caller who omits it gets no receipt at all.
    if not callable(reserve_completion):
        raise AdapterError("no_completion_guard")
    if not reserve_completion(completion.job_id, claimed_attempt):
        raise AdapterError("duplicate_completion")

    output_sha256 = hashlib.sha256(output).hexdigest()
    payload: Dict[str, Any] = {
        "job_id": completion.job_id,
        "workitem_id": completion.workitem_id,
        "attempt": claimed_attempt,
        "status": "success",
        "nonce": completion.nonce,
        "output": {"sha256": output_sha256, "size": len(output)},
        "produced_at": now,
        "source": "aps-callback-adapter",
    }
    body = _canonical_body(payload)
    # str(float) round-trips through float() in consume_callback's freshness
    # check; keep it a plain decimal string.
    timestamp = repr(float(now))
    signature = _callbacks().sign_payload(body, timestamp, completion.nonce, secret)
    return CallbackEnvelope(body=body, timestamp=timestamp, nonce=completion.nonce, signature=signature)
