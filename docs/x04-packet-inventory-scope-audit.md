# Packet-local inventory within X04's offline review

Studio X-bench X04 optionally checks a caller's expected embedded artifact-local ID set before trusting packet contents. This check compares decoded labels. The packet byte pin, individual drawing labels and artifact/report semantics have their own enforcement points.

## Enforcement order

| Boundary | Current source | Scope |
| --- | --- | --- |
| Expected label syntax | `scripts/validate_x04_evidence_packet.py:20-26` and `:120-122` | Each repeated expected ID uses the existing ASCII local-ID rule. Invalid or missing values refuse during argument parsing. |
| Duplicate expectations | `scripts/validate_x04_evidence_packet.py:123-125` | Duplicate expected labels refuse before output alias checks or input reads. Expected order is irrelevant. |
| Input buffer and optional digest | `scripts/validate_x04_evidence_packet.py:127-129`, shared `scripts/export_x04_offline_evidence.py:44-81` | Existing alias checks and byte ceilings precede raw-buffer digest matching and strict JSON decoding. |
| Expected inventory | `scripts/validate_x04_evidence_packet.py:129-137` | The decoded artifact entries must expose valid local IDs whose set equals the expected set. Missing, extra or renamed labels refuse before trusted validation. |
| Original packet semantics | `scripts/validate_x04_evidence_packet.py:138` and `:37-105` | Existing duplicate actual IDs, references, artifact validation and comparison/batch bindings still apply. Matching inventory does not waive them. |
| Publication | `scripts/validate_x04_evidence_packet.py:139-142` | Existing bounded output encoding and atomic replacement follow validation. Handled refusal preserves prior output. |

Actual source line ranges should be rechecked if surrounding code changes. The pure `validate_packet` function retains its original signature and semantics. The optional expected set belongs to each CLI invocation and reaches no shared mutable inventory state.

## Synthetic counterexamples

| Caller or packet change | Observed scope |
| --- | --- |
| Omit every expected-ID option | Prior packet semantics run with no caller inventory expectation. |
| Put `expected_artifact_ids` in packet metadata | This extension does not set CLI options. The guard checks only caller-provided expectations and decoded `artifacts[].id`. |
| Put expected labels on comparison or batch rows | Those labels do not define the embedded artifact inventory. Report-local IDs and ordered references remain distinct. |
| Reorder artifact entries or expected options | Set equality still succeeds. Normalized summary entry order continues to follow packet insertion order. |
| Escape ASCII ID a as a JSON Unicode escape | Strict decoding yields the same label. Correct expected IDs still match; raw packet digests differ by encoding. |
| Replace an artifact label and update every packet-local reference consistently | Original packet semantics can still pass. A retained expected inventory refuses the new set before trusted calls. |
| Add or remove an otherwise valid unused artifact | Explicit exact inventory refuses even when the remaining packet is internally consistent. |
| Match expected set but duplicate an actual ID or corrupt a reference | Original preflight still refuses. Set equality is not a replacement for structural validation. |
| Match expected set and packet digest but forge a count | Trusted artifact validation still refuses the derived claim. |
| Replace packet bytes, expected digest and expected labels together | The checks can agree. No producer signature, authorization or original-DWG custody is established. |
| Keep local IDs but substitute different internally valid drawings consistently | Labels alone do not authenticate drawing identity or contents. Separate artifact semantics and any caller-owned drawing expectations remain necessary for those claims. |

The same actual byte buffer supplies the optional digest, decoded inventory and trusted operands. Stat/open substitution and short stdin reads do not trigger a digest or inventory reopen. Caller-owned binary stdin remains open; file streams close after reading.

## Focused controls and remaining acceptance

Initial tests cover parser boundaries, internally valid inventory substitutions, ordering of byte ceiling/pin/decode/inventory/semantics, and recovery. Follow-up tests cover key order, escaped ASCII IDs, BOM/CRLF, network-denied optimized and seeded file/stdin runtimes, actual-buffer substitutions and barrier-interleaved expectations. The volume benchmark keeps artifact count and semantic claims fixed while growing ignored bytes. It records reads, hashes, trusted calls, time and memory without timing thresholds.

All these fixtures are synthetic and offline. Original-byte integrity stays `unverified`. Raw dependency inventory, real design-set bindings, anonymous-block intake geometry, browser geometry/selection and source-bound real-drawing acceptance retain their separate product and producer receipts. Atomic replacement adds no fsync or hostile alias-race guarantee. Successive reads do not create a simultaneous snapshot.
