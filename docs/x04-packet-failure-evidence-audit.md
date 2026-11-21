# X04 packet failure evidence audit

Parent: Studio X-bench X04 offline packet publication. This is a read-only audit of assertion source at local base `56fccc9e7d002bea0a9dc4dc3dea53a17560ca2a`. No tests, fault injection or replay ran for this audit. Prior receipts and current source assertions remain separate evidence.

| Property | Independent setup and assertion | What it supports, and its limit |
|---|---|---|
| Existing destination survives | Packet fault test writes literal `b'prior'` before the call at `server/tests/test_x04_packet_publication_faults.py:14`; compares the exact bytes at line 49. | The sentinel is independent of validator output. It detects replacement by a partial or successful report at the injected write/flush/replace failures. It is not a power-loss test. |
| Source survives | Same test captures bytes before injection at line 13 and compares them at line 49. | Proves a synthetic input-file invariant when executed. It does not compare a raw customer DWG or digest. |
| Owned temporary files disappear | The wrapper records actual created paths at line 21; line 50 requires those paths absent. | Checks cleanup of known temporary paths. It does not log every unlink call. |
| Another writer's file survives | Independent `.x04-other-writer` bytes are written at line 15; line 51 checks both surviving path set and exact other-owner bytes. | End-state isolation for a stationary sentinel. It is not a barrier-interleaved live packet-writer proof. |
| Cancellation identity survives | A specific exception object is constructed at line 18; line 43 uses `is` after `pytest.raises`. | Stronger than checking only exception type or message. The cancel matrix covers write, flush and replace. |
| Truthful refusal | Lines 45,47-48 require exit 2 for ordinary errors, no stdout, a refusal diagnostic and no traceback. Cancellation propagates instead. | These are injected temporary-file failures. Stdout delivery is a different path. |
| Recovery follows failure | Lines 52-53 call the real entry point after patches end, require exit 0 and parse integrity `unverified`. | Recovery is exercised, but this particular test does not compare the entire recovered summary, exact counts or final temporary-file set after retry. |
| Invalid JSON/budgets preserve output | `server/tests/test_x04_packet_json_limits.py:25` supplies the independent sentinel; lines 48-54 assert refusal/bytes/cleanup and compare the full valid recovery summary. | Covers file/stdin JSON and size boundaries. Nesting uses a successful boundary control first, then resets the sentinel at line 37. |
| Artifact substitution preserves a successful predecessor | `server/tests/test_x04_packet_replay_chain.py:29-40` retains a valid packet and successful destination, then substitutes an artifact and compares destination bytes. Lines 41-42 retry the original packet. | Strong cross-binding refusal control. The retry checks exit status, not complete output equality after that last call. |
| Runtime modes retain output | `server/tests/test_x04_packet_runtime_modes.py:29-38` uses an independent sentinel, compares refusal bytes across runtime modes and compares recovered output to the valid baseline. | Tests protocol determinism and recovery, not crash durability. |

The writer's source unlinks only its recorded `temporary` path on `BaseException` (`scripts/export_x04_offline_evidence.py:173-192`). Failed unlink handling retains the primary exception and adds a note. Related helper tests inject unlink failure and check the leftover name, primary diagnostic and retry (`server/tests/test_x04_evidence_cleanup_failures.py:35-64`). They run through export/artifact-validation entry points, not the packet entry point.

Related comparison/batch concurrency tests record exact unlink ownership and keep the surviving temporary path present until the failed writer cleans up (`server/tests/test_x04_review_concurrent_writers.py:21-38,50-59`). This supports the shared writer helper but is not a packet-specific concurrency receipt. The packet argument/alias and stream modules in the current worktree are unexecuted drafts; their source cannot be counted as passing evidence.

## Checks not established by these packet fault assertions

- Injected unlink failure, temporary-file construction failure, close failure and encoder failure are not in the packet fault matrix. Helper coverage must retain its entry-point label.
- Packet-specific partial/zero/invalid temporary-write counts are not injected here. Related helper assertions exist at `server/tests/test_x04_evidence_temp_short_writes.py:25-57`.
- A complete recovered-summary comparison and a final temporary-file assertion after the packet fault retry are absent from that test's lines 52-53.
- A live second packet writer, filesystem crash, file/directory `fsync`, concurrent source replacement and durable storage are not checked. The implementation calls flush and replace, with no `fsync` (`scripts/export_x04_offline_evidence.py:176-181`).

These are limits of the cited assertions, not findings that the implementation fails those cases. This audit adds no gate, test requirement, acceptance claim or new source change.
