# APS-to-Leaf callback translation adapter (L3.1)

Status: IMPLEMENTED (`server/da/aps_callback_adapter.py`). Enabling
callback-primary in production is a separate operator step (L3.7) and still
requires `LEAF_CALLBACK_PRIMARY=1`, `LEAF_CALLBACK_SECRET`, `LEAF_CALLBACK_URL`,
and a real APS `APS_LIVE=1` run.

## The gap this closes

`server/da/callbacks.py` verifies a signed Leaf completion envelope on
`POST /da/callback`, but native APS `onComplete` cannot emit that envelope. Its
own docstring reserved `LEAF_CALLBACK_PRIMARY=1` and failed closed "until an
APS-to-Leaf callback translation adapter exists." This adapter is that seam: it
turns a native APS WorkItem completion plus the persisted output into exactly
the envelope `callbacks.consume_callback` accepts.

## One signing authority

The envelope is signed with `callbacks.sign_payload` (loaded from the sibling
module by file path, the same mechanism `broker.py` uses), never a parallel
HMAC. There is one signer and one verifier, so the wire format cannot drift
between producer and consumer. The signature covers the length-prefixed
`(timestamp, nonce, body)` triple, exactly as `callbacks.verify_signature`
recomputes it.

## Signed body — the bindings

`translate()` emits a canonical JSON body (`sort_keys`, `(",",":")`) binding:

| Field | Binds | Source |
|-------|-------|--------|
| `job_id` | the job | APS WorkItem completion |
| `workitem_id` | the WorkItem | APS WorkItem completion |
| `attempt` | the exact attempt | completion, checked against the job's authoritative attempt |
| `nonce` | one delivery | APS delivery nonce (also the signed + replay-store key) |
| `output.sha256` / `output.size` | the evidence | sha256 + length of the persisted output bytes |
| `produced_at` | freshness | translation time; the consumer's max-age window rejects a stale replay |
| `status` | success only | always `"success"` — a non-success WorkItem never reaches here |

## Fail-closed modes

`translate()` raises `AdapterError(reason)` and emits nothing when:

| `reason` | Condition | Why it must fail |
|----------|-----------|------------------|
| `missing_job` | no job id | nothing to complete |
| `missing_workitem` | blank WorkItem id | the receipt must bind to a real WorkItem |
| `workitem_not_success` | WorkItem not succeeded | a failure is not a completion receipt; polling reports it |
| `missing_output` | no persisted output bytes | an empty receipt would attest a completion that produced nothing |
| `wrong_workitem` | completion's WorkItem id is not EXACTLY the one the job dispatched | nonblank is not enough. Compared exactly, with no `strip()`: a WorkItem id is opaque, and normalizing it let `" wi-1 "` and `" wi-1"` match `"wi-1"` while the envelope carried the untrimmed string |
| `wrong_attempt` | completion attempt ≠ job's current attempt, either is not a real `int`, or it is below 1 | a stale retry's late callback must not complete a newer attempt. `bool` is an `int` subclass and `True == 1`, so `attempt=True` is rejected explicitly rather than satisfying `job_attempt=1` |
| `expired_lease` | `now` past the lease deadline, or the deadline is not finite | a late completion is untrustworthy; `NaN` defeats every comparison (`now > nan` is `False`), so non-finite deadlines are refused outright |
| `bad_clock` | non-finite translation clock | a `NaN` clock would make the freshness stamp meaningless |
| `bad_nonce` | empty delivery nonce | the nonce is the signed + replay key |
| `no_completion_guard` | no duplicate-completion authority supplied | the guard is **required**, not optional: defaulting it to `None` is fail-open for a signing seam, since a caller who omits it silently regains the duplicate-completion hole |
| `duplicate_completion` | this `(job, ATTEMPT)` already produced a receipt | **keyed on completion identity, not the nonce.** The consumer's nonce store only rejects a repeat of the *same* nonce, so a second delivery bearing a *fresh* nonce would otherwise mint a second accepted envelope and complete one attempt twice. This adapter is the authority for one-receipt-per-attempt; the nonce store remains the second line against a verbatim replay |


### Exact types, and why

Every field is checked with `type(x) is ...`, not `isinstance`. A subclass of a
built-in can override the very method used to validate it, and three real defects
came from exactly that:

- `LyingStatus("failed")` whose `strip()` returned `"success"` produced a SUCCESS
  receipt for a FAILED WorkItem;
- `LyingInt(7)` whose `__int__()` returned 2 was signed as `attempt: 2`, a receipt
  for an attempt the completion never claimed (this one was introduced by an
  earlier fix that "normalized" with `int()`);
- `str` subclasses made every `.strip()` in the validator untrustworthy.

These values arrive from parsed JSON and the job store, where exact `str`, `int`
and `bytes` are what occur, so demanding exact types costs nothing real. `bool` is
excluded for free, since `type(True) is bool`.

### Read every field exactly once, at entry

`translate()` captures `job_id`, `workitem_id`, `attempt`, `status`, `nonce`,
`lease_expiry` and an immutable `bytes` copy of `output` BEFORE validating any of
them, and never reads `completion` again. The ordering is the defence, and it is
easy to get subtly wrong:

- Re-reading a field after validating it let a completion whose attributes are
  properties hand `job-1`/`wi-1` to the validator and `victim-job`/`wi-victim` to
  the payload builder, emitting AND reserving a signed receipt for a job that was
  never validated.
- Snapshotting *after* validation is not enough either. The validator reads some
  fields twice (a type check, then a comparison), so any read past the first is
  already untrusted. Validate exactly the values you will sign.
- `output` gets one `bytes()` copy because a `bytearray` read twice (hash, then
  `len`) could sign a receipt whose `size` and `sha256` describe different content.
  `memoryview` is accepted alongside `bytes`/`bytearray`, since database drivers
  return it for BLOB columns and `bytes()` snapshots it safely.

### The claim is the last gate

`reserve_completion` is called AFTER hashing, serialization and signing. It used to
run before them, so an exception in between consumed the identity without
producing a receipt, and the legitimate retry was refused as
`duplicate_completion` — the job could never be completed at all. A wasted hash is
cheaper than a permanently unclosable job.

### Why the replay key changed

The first version of this adapter keyed its guard on `(job_id, nonce)`, which
mirrored the consumer and therefore added no defence: the same job, attempt,
WorkItem and output delivered under two different nonces produced two envelopes
that the real `consume_callback` accepted against one replay authority. That is a
duplicate completion, and it is now covered end-to-end by
`test_two_nonces_for_one_attempt_cannot_both_be_consumed`, which drives the real
consumer and asserts exactly one acceptance.

## Defence in depth (verified by the round-trip test)

`tests/test_aps_callback_adapter.py` proves an adapter-produced envelope is
accepted by the REAL `verify_signature` and `consume_callback` exactly once, and
that a replay is rejected by the durable nonce store, a tampered body breaks the
signature, and a late delivery is rejected by the consumer's freshness window.

**One receipt per attempt holds only because the reservation is ATOMIC.** An
earlier version of this document claimed the tests proved a job could not be
completed twice while the guard was still a read-only "have you seen this?"
query. That claim was false: two deliveries could both be translated before
either receipt was recorded, and the real consumer accepted both. The guard is
now `reserve_completion(job_id, attempt)`, which must claim the identity and
return False if it was already claimed, so the check and the record are one
indivisible step. `test_two_translations_before_either_receipt_is_recorded_cannot_both_be_accepted`
drives that exact race against the real consumer.

A caller that supplies a non-atomic reservation (for example a plain set
membership test followed by a later insert) reintroduces the race. Back it with
the job store's own conditional write.

Run: `cd server && python -m pytest tests/test_aps_callback_adapter.py -q`
