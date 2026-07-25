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
  * ``expired_lease``        — the job store's recorded lease deadline has passed,
                               or is not finite (a completion delivered after the
                               lease is not trustworthy);
  * ``malformed_lease``      — the completion's lease field is not a finite number.
                               It is INFORMATIONAL only: authority supplies the
                               deadline, so this value is never compared. Requiring
                               it to EQUAL authority (round 7) was float equality on
                               a timestamp, which would refuse every completion
                               whose lease had been re-serialized and leave the job
                               unclosable;
  * ``bad_nonce``            — the delivery nonce is empty, over-long, or contains
                               anything outside a header-safe alphabet. It is signed
                               AND sent as the ``X-Leaf-Nonce`` header, so a nonce
                               containing CRLF injected header lines;
  * ``field_too_long``       — a string field exceeded the length cap. Capping
                               before normalizing keeps every pre-copy step bounded,
                               so a huge input cannot raise MemoryError in place of
                               a cheap refusal;
  * ``bad_clock``            — non-finite translation clock;
  * ``missing_workitem``     — no WorkItem id to bind the receipt to;
  * ``no_completion_guard`` — no reservation authority was supplied. REQUIRED:
                               an optional guard defaulting to None is fail-open;
  * ``duplicate_completion`` — the reservation for this (job, ATTEMPT) was already
                               claimed and the guard holds no stored receipt for
                               it. Keyed on completion identity, not the nonce,
                               AND taken atomically: a read-only "have you seen
                               this?" check let two deliveries both translate
                               before either receipt was recorded, so both were
                               accepted and one attempt completed twice.

THE RESERVATION CONTRACT
------------------------
``reserve_completion(job_id, attempt, envelope)`` must atomically claim
``(job_id, attempt)`` AND record ``envelope`` in the same indivisible step, then
return:

  * ``True``              — freshly claimed; the adapter returns the new envelope;
  * a ``CallbackEnvelope`` — already claimed, and this is the receipt stored with
                            that claim. The adapter returns it VERBATIM, so a
                            delivery that crashed after claiming is recoverable
                            and the retry is idempotent;
  * ``False``             — already claimed with no recoverable receipt;
                            ``duplicate_completion``.

Anything else is ``bad_completion_guard``. Storing the envelope with the claim is
what makes a crash between the claim and the POST survivable: reserving the
identity alone burned the attempt and left the receipt nowhere.

Run:  cd server && python -m pytest tests/test_aps_callback_adapter.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
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


# APS DesignAutomation reports exactly one success state: `success`. The other
# documented values are pending, inprogress, cancelled and the failed* family. The
# aliases this used to accept ("succeeded", "completed", "complete") are not APS
# vocabulary at all, so accepting them meant inventing success states the protocol
# never emits and promoting any upstream string that happened to match.
_SUCCESS_STATUSES = frozenset({"success"})

# The delivery nonce is placed in an HTTP header (`X-Leaf-Nonce`) and signed. A
# nonce containing CRLF therefore injects header lines: `"abc\r\nX-Evil: yes"`
# produced an envelope whose header carried the forged line. Restrict it to a
# header-safe, bounded alphabet. A COLON is excluded on purpose so the emitted
# `"<attempt>:<delivery nonce>"` form has exactly one colon and splits
# unambiguously.
_NONCE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,128}\Z")

# Length caps make the pre-copy work genuinely BOUNDED. `len()` is O(1), while
# `.strip()`, `.lower()` and the derived-nonce f-string all allocate in proportion
# to their input, so an enormous but otherwise valid string could raise MemoryError
# before a cheap refusal was reported. Cap first, then normalize.
_MAX_ID_LEN = 512

# The attempt is a NUMBER, so `_MAX_ID_LEN` never bounded it. That gap was real:
# the derived nonce is built as `f"{claimed_attempt}:{delivery_nonce}"`, and
# formatting a huge int raises a raw `ValueError` from CPython's int-to-str digit
# limit BEFORE the reservation, in place of a tagged refusal. A job store whose
# attempt counter has reached this bound is corrupt, not busy: attempts increment
# once per lease reclaim, so refusing is the correct reading either way.
_MAX_ATTEMPT = 1_000_000


def _is_finite_number(value: Any) -> bool:
    """`math.isfinite` on an exact int is not total: it converts to float first,
    so `math.isfinite(10**5000)` raises `OverflowError` rather than returning
    False. A guard that raises is not a guard — an oversized exact integer
    escaped as a raw `OverflowError` instead of the tagged `malformed_lease`
    refusal. Exact types only, and the conversion itself is caught."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


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
    job_lease_expiry: float,
    secret: bytes,
    now: float,
    reserve_completion: Callable[[str, int, "CallbackEnvelope"], Any],
) -> CallbackEnvelope:
    """Translate a successful APS WorkItem completion into a signed envelope.

    ``job_id``, ``job_attempt`` and ``job_workitem_id`` are the job store's
    AUTHORITATIVE identity, current attempt and dispatched WorkItem id; the
    completion must match ALL THREE. ``job_id`` was missing until round 6, so a
    completion naming a different job emitted and reserved a valid receipt for
    that other job: the attempt and WorkItem were checked against authority while
    the job itself was simply taken from the completion.
    ``now`` is the epoch seconds at translation.

    ``reserve_completion(job_id, attempt, envelope)`` is REQUIRED and must
    ATOMICALLY claim the completion identity AND record ``envelope`` in the same
    step. It returns True if this call won the claim, a previously stored
    ``CallbackEnvelope`` if the claim already exists and its receipt survived, or
    False if it exists with no recoverable receipt. It is a reservation, not a
    question. A returned envelope is CHECKED against this completion's identity
    and signature before it is handed back, because the guard is authority for
    whether a claim exists, never for whose it is.

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
    # Cap BEFORE normalizing, so every later strip/lower/format is bounded.
    for candidate in (claimed_job_id, job_id, workitem_id, job_workitem_id, status, nonce):
        if type(candidate) is str and len(candidate) > _MAX_ID_LEN:
            raise AdapterError("field_too_long")
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
    if type(attempt) is not int or type(job_attempt) is not int:
        raise AdapterError("wrong_attempt")
    claimed_attempt = attempt
    if claimed_attempt != job_attempt:
        raise AdapterError("wrong_attempt")
    # Attempts are 1-based. 0 and negatives are not attempts that ever ran.
    if claimed_attempt < 1:
        raise AdapterError("wrong_attempt")
    # BOUND IT ABOVE TOO. Round 8 capped every string but left this number free,
    # so the derived nonce below (`f"{claimed_attempt}:..."`) raised a raw
    # ValueError from the int-to-str digit limit before the reservation was ever
    # reached. Equality with authority does not save us: it only means a corrupt
    # attempt counter is refused here rather than in the middle of formatting.
    if claimed_attempt > _MAX_ATTEMPT:
        raise AdapterError("wrong_attempt")
    # NaN defeats every comparison (`now > nan` is False), so a non-finite lease
    # deadline would sail past an ordinary expiry check. callbacks.py guards its
    # own timestamp with math.isfinite for the same reason.
    # THE LEASE IS AUTHORITY, AND ONLY AUTHORITY. Checking the completion's own
    # `lease_expiry` was checking the payload against itself: a job whose recorded
    # lease expired an hour ago passed by supplying `lease_expiry = now + 3600`.
    #
    # Round 7 fixed that by ALSO requiring the completion's copy to equal authority
    # exactly. That was a trap of its own: float equality on a timestamp, so an APS
    # payload that echoes a rounded or re-serialized lease would be refused forever
    # and the job could never close. A permanently unclosable job is worse than the
    # hole it replaced, and the comparison buys nothing, because once authority
    # supplies the deadline the completion's copy is untrusted and decides nothing.
    #
    # So the completion's `lease_expiry` is INFORMATIONAL: still type-checked, so a
    # malformed record is caught, but never compared and never decisive.
    if not _is_finite_number(job_lease_expiry):
        raise AdapterError("expired_lease")
    if not _is_finite_number(lease_expiry):
        raise AdapterError("malformed_lease")
    # `>=`, NOT `>`, and the reason is narrower than "match the store" — the store
    # does not agree with ITSELF at this instant. In jobs.py, reclaim tests
    # `lease_expires_at <= now` while owner completion and heartbeat test
    # `lease_expires_at >= now`, so at exactly `now == lease_expiry` the row is
    # BOTH reclaimable by a new worker AND still completable by the old owner.
    #
    # That overlap is the store's, not ours to resolve here. What this adapter can
    # do is refuse to be the half that signs into an ambiguous instant: with a
    # strict `>` it minted a completion receipt for a lease a reclaim could take in
    # the same tick, so the receipt could land against an attempt the store had
    # already moved past. Refusing is the fail-closed side of a boundary that is
    # genuinely undecided, and it costs one tick of a fifteen-second lease.
    if now >= job_lease_expiry:
        raise AdapterError("expired_lease")
    # Header-safe and bounded. `.strip()` alone let control characters through, and
    # the nonce is both signed AND sent as the `X-Leaf-Nonce` header value.
    if type(nonce) is not str or not _NONCE_RE.match(nonce):
        raise AdapterError("bad_nonce")
    if not callable(reserve_completion):
        raise AdapterError("no_completion_guard")
    # BIND THE EMITTED NONCE TO THE ATTEMPT. The consumer's replay store is keyed
    # on (job_id, nonce), so a delivery nonce reused across two attempts of one job
    # collided: attempt 2 was CLAIMED here, its envelope was then rejected
    # downstream as `replay`, and the honest retry with a fresh nonce hit
    # `duplicate_completion`. One repeated nonce could therefore burn a later
    # attempt permanently. Prefixing the attempt makes cross-attempt collision
    # impossible while leaving a verbatim replay of one envelope still detectable,
    # because that envelope's nonce is unchanged.
    delivery_nonce = nonce
    nonce = f"{claimed_attempt}:{delivery_nonce}"

    # Built ONLY from the locals captured at entry. `completion` is never read
    # again past this point, so the receipt names exactly what was validated.
    # THE COPY IS THE LAST VALIDATION STEP, after every cheap check. Round 6 moved
    # it out of the entry block but not far enough: a huge buffer was still copied
    # before the attempt, lease, nonce and guard were checked, so an allocation
    # failure could escape in place of the correct `wrong_attempt` refusal.
    # `output` is read exactly once, into one immutable snapshot, so a concurrent
    # mutation cannot make `size` and `sha256` describe different content, and a
    # released or resized buffer is a tagged refusal rather than a raw exception.
    try:
        attested = bytes(output)
    except (ValueError, BufferError, MemoryError):
        raise AdapterError("missing_output")
    if len(attested) == 0:
        raise AdapterError("missing_output")
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
    # THE ENVELOPE GOES IN WITH THE CLAIM. Reserving the identity alone left the
    # receipt only in this function's return value: claim succeeds, then the
    # process dies before the caller returns or POSTs, and the honest retry gets
    # `duplicate_completion` for an envelope that now exists nowhere. The attempt
    # was burned with nothing to show for it — the same unclosable-job failure
    # round 7 fixed from the other direction. Handing the envelope to the guard
    # makes claiming and recording one indivisible step, so whatever survives the
    # crash holds a receipt, not just a tombstone.
    outcome = reserve_completion(job_id, claimed_attempt, envelope)
    if outcome is True:
        return envelope
    # A STORED ENVELOPE IS A RECOVERY, NOT A REFUSAL. A guard that already holds a
    # receipt for this (job, attempt) returns it, and the retry is idempotent:
    # the caller gets back the identical envelope the crashed delivery signed.
    # Returned as-is, never re-signed, so the nonce and signature still match the
    # receipt the replay store may already have seen.
    #
    # BUT IT MUST BE THIS COMPLETION'S RECEIPT. Returning whatever the guard hands
    # back was the same mistake this module keeps finding in new places: a guard
    # invoked for (job-1, 2) could return an envelope naming (other-job, 99), with
    # any signature at all, and translate would present it as the receipt for
    # (job-1, 2). The guard is authority for WHETHER a claim exists, never for
    # WHOSE it is. Re-derive the identity from the returned body and refuse a
    # receipt that does not name what we just tried to claim.
    if type(outcome) is CallbackEnvelope:
        try:
            recovered = json.loads(outcome.body)
        except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise AdapterError("bad_completion_guard")
        if type(recovered) is not dict:
            raise AdapterError("bad_completion_guard")
        # COMPARE THE WHOLE BODY, NOT A LIST OF FIELDS. Three review rounds went
        # the other way and each found one more field I had not thought to check:
        # first the identity, then the output evidence, then `status` and
        # `workitem_id` (a stored receipt saying "failed" for a different WorkItem
        # was accepted as a successful wi-1 completion and would have failed the
        # job with false provenance). Enumerating trusted fields is a losing game,
        # because the default for anything unlisted is ACCEPT.
        #
        # Inverted: the recovered body must equal the body we just built, and only
        # the two fields that legitimately differ between deliveries are exempt.
        # `produced_at` is the original delivery's clock and `nonce` carries the
        # original delivery nonce; the whole point of returning the receipt
        # verbatim is that those keep their first values, so its signature still
        # matches what the replay store may already hold. Every other field is
        # semantic and must match. A field added to `payload` later is compared
        # automatically instead of being silently trusted.
        _RECOVERY_MAY_DIFFER = ("produced_at", "nonce")
        ours = {k: v for k, v in payload.items() if k not in _RECOVERY_MAY_DIFFER}
        theirs = {k: v for k, v in recovered.items() if k not in _RECOVERY_MAY_DIFFER}
        # `bool` is an int subclass and `True == 1`, so a dict comparison alone
        # would let `attempt: True` satisfy `attempt: 1`. Pin the exact type first.
        if type(recovered.get("attempt")) is not int:
            raise AdapterError("bad_completion_guard")
        if theirs != ours:
            raise AdapterError("bad_completion_guard")
        # And it must be a receipt WE could have signed. An unverifiable body is
        # not a recovery, it is an unsigned claim wearing the envelope type.
        if not _callbacks().verify_signature(outcome.body, outcome.timestamp,
                                             outcome.nonce, outcome.signature, secret):
            raise AdapterError("bad_completion_guard")
        return outcome
    if outcome is False:
        raise AdapterError("duplicate_completion")
    # A guard that returns anything else is violating its contract. It may already
    # have recorded the claim, and the adapter cannot undo a side effect it does not
    # own, so this is reported as its own reason rather than guessed at.
    raise AdapterError("bad_completion_guard")
