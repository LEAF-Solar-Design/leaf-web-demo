# Expected drawing labels within X04's offline evidence review

Studio X-bench X04's optional artifact drawing-ID guard checks a caller's routing label against one decoded artifact. It does not authenticate a drawing or advance a frozen live real-drawing acceptance step by itself.

## Enforcement order

| Boundary | Current source | Scope |
| --- | --- | --- |
| Expected value | `scripts/validate_x04_offline_evidence.py:31-34` and `:45-47` | Blank or whitespace-only expectations refuse during argument parsing. Nonblank text is returned unchanged. |
| Parser | `scripts/validate_x04_offline_evidence.py:37-47` | Exact long-option spelling is required through `allow_abbrev=False`. Missing operands refuse before I/O. |
| Alias and input bytes | `scripts/validate_x04_offline_evidence.py:49-50`, shared `scripts/export_x04_offline_evidence.py:69-81` | Existing output alias checks precede a bounded read of file or binary stdin. File stat is a precheck, not the identity source. |
| Decoded identity | `scripts/validate_x04_offline_evidence.py:51-54` | A supplied expectation compares exactly with top-level `drawing.id` from the actual decoded buffer. No normalization or path resolution occurs. Wrong identity stops here. |
| Recomputed semantics | `scripts/validate_x04_offline_evidence.py:55` and `:12-28` | Matching identity still runs original artifact validation and count/highlight recomputation. Claimed count, handle membership and integrity must agree. |
| Publication | `scripts/validate_x04_offline_evidence.py:56-58` | Existing bounded output encoding and atomic replacement follow semantics. Handled refusal preserves prior output and reports an error. |

The exact label check follows JSON decoding, not a separate metadata read. It does not reopen the file, inspect a filename or consume a later source version. File streams belong to the reader and close after reading. Binary stdin remains caller-owned.

## Counterexamples

| Input or change | Behavior and limit |
| --- | --- |
| Omit `--expected-drawing-id` | Prior artifact semantics still run. No caller-expected identity is asserted. |
| Put `expected_drawing_id` in artifact JSON or inside `drawing` | Unknown extension fields do not set the CLI expectation. `drawing.id` remains the actual label; `request.drawing_id` must still match it through existing semantics. |
| Supply case-folded or NFC/NFD-equivalent text | Unequal decoded strings refuse. The guard does not fold case or normalize Unicode. |
| Trim a valid label with surrounding spaces | The trimmed expectation refuses. Blank expectations are rejected, but nonblank expectations retain their original spaces. |
| Supply a path-like ID such as `../../private.dwg` | It is compared as a string and is not opened as a file. Its equality is not a file ownership or filesystem identity claim. |
| Replace a file between stat and open | The actual single buffer controls both label equality and subsequent recomputation. A stale expected label can refuse even when the statted file had that label. |
| Keep matching identity but forge count 999 | Identity passes; existing derived-count comparison still refuses after recomputation. |
| Change `drawing.id` without changing `request.drawing_id` | Even a matching caller expectation cannot waive the existing request/drawing consistency check. |
| Relabel both drawing and request consistently, then supply the new expected label | A valid artifact can pass with unchanged physical entities. This is label equality, not source or producer authenticity. |
| Replace the artifact and expected label together | Both can agree. No independent byte digest, signature, producer attestation or authorization is established by this option. |

The pure `validate_evidence` API has no expected-ID parameter; it continues to validate decoded artifact semantics. Callers needing routing intent use the CLI option. Byte pinning on separate report or packet CLIs does not implicitly add this drawing expectation.

## Focused evidence and remaining limits

Initial controls cover parser boundaries, literal case/Unicode/space/path identities without target opens, refusal order, semantic forgeries and corrected retry. Follow-up controls use equivalent encodings, file/stdin network-denied safe-path subprocesses with and without optimization under several hash seeds, replaced short reads, and barrier-interleaved calls. The byte-volume benchmark holds entities fixed while growing ignored padding and records reads, recomputation, time and memory with no timing threshold.

Fixtures remain synthetic and offline. `original_bytes_integrity` stays `unverified`. Raw dependency completeness, intended real-drawing design sets, browser geometry, UI selection and live exact counts remain outside this proof. Expected labels require independent custody. Atomic replacement does not add fsync durability or hostile alias-race guarantees, and successive reads are not a simultaneous snapshot.
