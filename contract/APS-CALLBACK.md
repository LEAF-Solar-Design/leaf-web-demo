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
| `workitem_not_success` | WorkItem not succeeded | a failure is not a completion receipt; polling reports it |
| `missing_output` | no persisted output bytes | an empty receipt would attest a completion that produced nothing |
| `wrong_attempt` | completion attempt ≠ job's current attempt | a stale retry's late callback must not complete a newer attempt |
| `expired_lease` | `now` past the APS lease deadline | a completion delivered after the lease is not trustworthy |
| `bad_nonce` | empty delivery nonce | the nonce is the signed + replay key |
| `replay` | this `(job, nonce)` already translated | producer-side idempotency; the consumer's durable nonce store is the second line |

## Defence in depth (verified by the round-trip test)

`tests/test_aps_callback_adapter.py` proves an adapter-produced envelope is
accepted by the REAL `verify_signature` and `consume_callback` exactly once, and
that a replay is rejected by the durable nonce store, a tampered body breaks the
signature, and a late delivery is rejected by the consumer's freshness window.
So the adapter cannot produce an envelope the verifier would reject, and cannot
be used to complete a job twice.

Run: `cd server && python -m pytest tests/test_aps_callback_adapter.py -q`
