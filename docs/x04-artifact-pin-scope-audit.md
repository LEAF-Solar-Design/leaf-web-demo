# Artifact byte pins within X04's offline evidence chain

Studio X-bench X04 can compare an artifact's exact bytes with a caller-retained digest before decoding or recomputation. This is separate from drawing-label equality and semantic consistency. It does not establish producer authenticity or frozen live real-drawing acceptance.

## Enforcement order

| Boundary | Current source | Proven scope |
| --- | --- | --- |
| Expected digest syntax | `scripts/validate_x04_offline_evidence.py:47-49`, shared `scripts/export_x04_offline_evidence.py:38-41` | The optional CLI value contains exactly 64 hexadecimal characters. Malformed values refuse during argument parsing before I/O. |
| Alias and bounded buffer | `scripts/validate_x04_offline_evidence.py:51-53`, shared `scripts/export_x04_offline_evidence.py:69-81` | Alias checks precede one bounded file or binary stdin read. Input ceiling refusal precedes digest/decode. A stat result is not the bytes being matched. |
| Digest before decoding | `scripts/export_x04_offline_evidence.py:44-66` | The exact buffer is hashed, including BOM and whitespace, before strict JSON decoding. No separate digest reopen occurs. |
| Optional expected label | `scripts/validate_x04_offline_evidence.py:54-57` | After decoding, the optional nonblank caller expectation compares literally with `drawing.id`. It is not a path lookup. |
| Recomputed semantics | `scripts/validate_x04_offline_evidence.py:58` and `:12-28` | Original export, dependency, request/selection, count, highlight and unverified-integrity checks still run. Matching bytes and label do not waive them. |
| Publication | `scripts/validate_x04_offline_evidence.py:59-61` | Existing bounded encoding and atomic replacement follow all checks. Handled pre-publication refusal retains prior output. |

The omitted-pin path retains the previous two-argument reader call. The pure `validate_evidence` API takes decoded claims and does not perform raw-byte matching. File streams are reader-owned and close after reading; caller-owned binary stdin remains open. Cancellation is not converted into a successful JSON result.

## Synthetic counterexamples

| Example | Behavior and remaining limit |
| --- | --- |
| Omit `--expected-artifact-sha256` | Decoding, optional identity and semantics still run. No caller-expected byte match is asserted. |
| Put `expected_artifact_sha256` or `expected_sha256` inside artifact JSON | Unknown metadata does not set the CLI option. A hash-looking field is not raw-buffer pinning. |
| Put a digest on a report or packet instead | Those separate CLIs compare their own buffers. Their expected values do not implicitly pin the original artifact file. |
| Append whitespace and retain the original expected digest | The artifact CLI refuses before recomputation even though decoded claims remain equivalent. Explicitly refreshing the expected digest is a separate caller decision. |
| Use another JSON serialization's digest | Reordered keys, escaped Unicode, BOM and CRLF produce distinct raw digests despite equal decoded claims. Each exact encoding needs its own expected value. |
| Replace the source between stat and open | Matching uses the actual read buffer, not statted metadata or a second file open. A digest of the old disk bytes refuses when the supplied stream differs. |
| Match the digest but expect the wrong drawing label | Byte comparison passes; optional label equality refuses before recomputation. |
| Match digest and label but forge a count or highlight | Existing semantic recomputation still refuses. Byte matching is not a claim-truth bypass. |
| Replace artifact and expected digest together | These values can match. No signature or trusted producer has been established. |
| Relabel a valid artifact and request consistently, then replace expected digest and label | Both caller checks can pass on unchanged physical entities. This is unauthenticated bytes and labels, not original-DWG custody. |

Expected values require independent trusted custody. A separately retained digest detects later unexpected byte changes relative to that value; it does not prove who chose the value or how the artifact was produced.

## Focused proof and limits

Initial controls cover malformed pin parsing, matching/default/stale/refreshed file/stdin bytes, failure precedence and substituted short reads. Follow-up controls cover hash/decode cancellation identity and stream ownership, equivalent serialization digests, optimized and seeded network-denied subprocesses, and barrier-interleaved expected values. The volume benchmark holds one physical entity fixed while growing ignored padding and records reads, hashes, recomputation, elapsed time and peak memory without timing gates.

All fixtures are synthetic and offline. `original_bytes_integrity` stays `unverified`. Original drawing bytes, raw dependency completeness, intended real-drawing module selection, browser geometry and live exact counts remain unproved here. Atomic replacement adds neither fsync durability nor hostile alias-race guarantees. Successive reads do not create a simultaneous snapshot.
