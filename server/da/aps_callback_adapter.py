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

  * ``missing_job``          — no job id on the completion, or none supplied as
                               authority;
  * ``wrong_job``            — the completion names a different job than the one
                               whose state was supplied;
  * ``bad_completion_guard`` — the reservation returned something other than True
                               or False, violating its contract;
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
    job_id: str,
    job_attempt: int,
    job_workitem_id: str,
    secret: bytes,
    now: float,
    reserve_completion: Callable[[str, int], bool],
) -> CallbackEnvelope:
    """Translate a successful APS WorkItem completion into a signed envelope.

    ``job_id``, ``job_attempt`` and ``job_workitem_id`` are the job store's
    AUTHORITATIVE identity, current attempt and dispatched WorkItem id; the
    completion must match ALL THREE. ``job_id`` was missing until round 6, so a
    completion naming a different job emitted and reserved a valid receipt for
    that other job: the attempt and WorkItem were checked against authority while
    the job itself was simply taken from the completion.
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
    # EXACT TYPES, not isinstance. This is one rule closing three separate
    # defects, all the same shape: a subclass of a built-in can override the very
    # method used to validate it.
    #   * `LyingStatus("failed")` whose strip() returns "success" turned a FAILED
    #     WorkItem into a success receipt;
    #   * `LyingInt(7)` whose __int__ returns 2 got signed as attempt 2, a receipt
    #     for an attempt the completion never claimed;
    #   * str subclasses generally make every `.strip()` here untrustworthy.
    # These values arrive from parsed JSON and a job store, where exact `str`,
    # `int` and `bytes` are what actually occur, so demanding exact types costs
    # nothing real and removes the whole class of override tricks. `bool` is
    # excluded automatically, since `type(True) is bool`, not `int`.
    # READ EVERY FIELD EXACTLY ONCE, BEFORE VALIDATING ANY OF IT.
    #
    # This ordering is the whole defence, and getting it wrong is subtle: an
    # earlier attempt snapshotted the fields AFTER validation, which still let an
    # object whose attributes are properties hand one value to the validator and a
    # different one to the snapshot. The validator itself reads some fields twice
    # (a type check, then a comparison), so any read after the first is already
    # untrusted. Validate exactly the values you will sign, which means capturing
    # them first and never touching `completion` again.
    #
    # A completion validated as (job-1, wi-1) previously went on to emit AND
    # RESERVE a signed receipt for (victim-job, wi-victim).
    claimed_job_id = completion.job_id
    workitem_id = completion.workitem_id
    attempt = completion.attempt
    status = completion.status
    nonce = completion.nonce
    lease_expiry = completion.lease_expiry

    # CHEAP CHECKS FIRST, then the expensive copy. Copying `output` eagerly at
    # entry was a regression of its own: an already-released memoryview raised a
    # raw ValueError instead of a tagged refusal, and a huge buffer was copied in
    # full before a bad clock or a mismatched identity was ever noticed. The
    # identity fields above are still captured in a single read each, which is what
    # closes the swap; only the copy moves later.
    if type(now) not in (int, float) or not math.isfinite(now):
        raise AdapterError("bad_clock")
    if type(claimed_job_id) is not str or not claimed_job_id.strip():
        raise AdapterError("missing_job")
    if type(job_id) is not str or not job_id.strip():
        raise AdapterError("missing_job")
    # The completion must name the job whose state we were handed. Without this the
    # attempt and WorkItem were validated against authority while the job id was
    # taken on trust, so a completion naming another job produced a receipt
    # reserved for that job.
    if claimed_job_id != job_id:
        raise AdapterError("wrong_job")
    if type(workitem_id) is not str or not workitem_id.strip():
        raise AdapterError("missing_workitem")
    if type(job_workitem_id) is not str or not job_workitem_id.strip():
        raise AdapterError("missing_workitem")
    # EXACT equality, no strip(). A WorkItem id is an opaque identifier, so
    # normalizing it is not leniency but a hole: `strip()` removes unicode
    # whitespace, which let " wi-1 " and "\xa0wi-1" both match "wi-1" while the
    # envelope carried the UNTRIMMED string. Two ids either are the same or they
    # are not, and the receipt must carry exactly the id that was compared.
    if workitem_id != job_workitem_id:
        raise AdapterError("wrong_workitem")
    if type(status) is not str or status.strip().lower() not in _SUCCESS_STATUSES:
        raise AdapterError("workitem_not_success")
    # `output` must be a real byte buffer. A `str` passed len() and then blew up
    # inside sha256. `memoryview` is accepted because database drivers return it
    # for BLOB columns and it is a legitimate producer; the exact-type rule exists
    # to stop OVERRIDABLE behaviour, and these three are all safely snapshotted by
    # bytes().
    if type(output) not in (bytes, bytearray, memoryview):
        raise AdapterError("missing_output")
    # One immutable copy: `output` may be a bytearray, and reading it twice (hash,
    # then len) let a concurrent mutation sign a receipt whose `size` and `sha256`
    # described different content. A released or resized buffer becomes a tagged
    # refusal rather than a raw ValueError escaping the adapter.
    try:
        attested = bytes(output)
    except (ValueError, BufferError):
        raise AdapterError("missing_output")
    if len(attested) == 0:
        raise AdapterError("missing_output")
    if type(attempt) is not int or type(job_attempt) is not int:
        raise AdapterError("wrong_attempt")
    claimed_attempt = attempt
    if claimed_attempt != job_attempt:
        raise AdapterError("wrong_attempt")
    # Attempts are 1-based. 0 and negatives are not attempts that ever ran.
    if claimed_attempt < 1:
        raise AdapterError("wrong_attempt")
    # NaN defeats every comparison (`now > nan` is False), so a non-finite lease
    # deadline would sail past an ordinary expiry check. callbacks.py guards its
    # own timestamp with math.isfinite for the same reason.
    if (type(lease_expiry) not in (int, float)
            or not math.isfinite(lease_expiry)
            or now > lease_expiry):
        raise AdapterError("expired_lease")
    if type(nonce) is not str or not nonce.strip():
        raise AdapterError("bad_nonce")
    if not callable(reserve_completion):
        raise AdapterError("no_completion_guard")

    # Built ONLY from the locals captured at entry. `completion` is never read
    # again past this point, so the receipt names exactly what was validated.
    output_sha256 = hashlib.sha256(attested).hexdigest()
    payload: Dict[str, Any] = {
        "job_id": job_id,          # authoritative, and equal to the claimed one
        "workitem_id": workitem_id,
        "attempt": claimed_attempt,
        "status": "success",
        "nonce": nonce,
        "output": {"sha256": output_sha256, "size": len(attested)},
        "produced_at": now,
        "source": "aps-callback-adapter",
    }
    body = _canonical_body(payload)
    # str(float) round-trips through float() in consume_callback's freshness
    # check; keep it a plain decimal string.
    timestamp = repr(float(now))
    signature = _callbacks().sign_payload(body, timestamp, nonce, secret)
    envelope = CallbackEnvelope(body=body, timestamp=timestamp, nonce=nonce, signature=signature)

    # THE CLAIM IS THE LAST STATEMENT THAT CAN FAIL, and the envelope is already
    # fully built above. Previously the return statement still read
    # `completion.nonce` AFTER the claim, so a property that raised on a later read
    # left the identity consumed with no envelope, and the genuine retry was then
    # refused as `duplicate_completion` — the job could never complete.
    #
    # It is a RESERVATION, not a query: it must ATOMICALLY claim (job, attempt) and
    # return False if already claimed, making the check and the record one
    # indivisible step. A read-only "have you seen this?" cannot close the window
    # in which two deliveries are both translated before either receipt is
    # recorded; the consumer accepted both and one attempt completed twice.
    # NO IMPLICIT TRUTH CONVERSION HERE. `if not reserve_completion(...)` calls
    # __bool__ on whatever comes back, and a guard returning an object whose
    # __bool__ raises produced a RuntimeError AFTER the identity was already
    # recorded, burning the attempt. Compare against the exact singletons instead,
    # so nothing after the claim can raise.
    outcome = reserve_completion(job_id, claimed_attempt)
    if outcome is True:
        return envelope
    if outcome is False:
        raise AdapterError("duplicate_completion")
    # A guard that returns anything else is violating its contract. It may already
    # have recorded the claim, and the adapter cannot undo a side effect it does not
    # own, so this is reported as its own reason rather than guessed at.
    raise AdapterError("bad_completion_guard")
