# X04 report inventory scope

Studio X-bench X04 checks optional comparison and batch ID sets before trusting packet contents. Each specified inventory constrains decoded labels within its own report section. Artifact semantics, report claims, drawing labels and raw packet bytes have separate checks.

| Boundary | Source | Effect |
| --- | --- | --- |
| Local ID syntax | `scripts/validate_x04_evidence_packet.py:23-26`, `:120-125` | Repeated expectations use the shared ASCII label rule. |
| Argument parsing and duplicate expectations | `scripts/validate_x04_evidence_packet.py:126-134` | Duplicate IDs within an expected section refuse before path checks or reads. |
| Alias, input ceiling, pin and decoding | `scripts/validate_x04_evidence_packet.py:136-138`, `scripts/export_x04_offline_evidence.py:44-81` | The existing bounded reader supplies one actual buffer for digest matching and JSON decoding. |
| Section inventory | `scripts/validate_x04_evidence_packet.py:139-148` | Each supplied set must equal the decoded IDs in its named section. Inventory mismatches reach zero trusted artifact checks. |
| Trusted packet checks | `scripts/validate_x04_evidence_packet.py:37-105`, `:149` | Actual global ID uniqueness, references, artifact semantics and recomputed report claims still apply after matching inventories. |
| Output publication | `scripts/validate_x04_evidence_packet.py:150-153` | Existing byte ceilings and atomic output replacement apply after successful validation. |

The optional expectations remain local to each CLI call. The pure `validate_packet` API keeps its existing signature. File/stdin short-read tests verify that inventory and digest decisions use the decoded buffer. Concurrent calls independently accept or refuse their own expectations.

| Synthetic change | Result |
| --- | --- |
| Omit comparison expectations | Comparison structure and semantic checks run with an unconstrained comparison inventory. |
| Expect comparison `c` while the packet has an empty comparison section | Inventory refusal precedes trusted artifact validation. |
| Omit batch expectations and rename a valid batch | Matching comparison expectations succeed and all batch semantics still run. |
| Put a comparison expectation inside packet metadata | Caller CLI options remain the source of expectations. Metadata is decoded as packet data. |
| Move label `c` from comparisons to batches and swap the old batch label back | Global labels can remain unique, while explicit section inventories refuse. |
| Rename every report and update caller expectations to match | Inventory checks agree with the replacement labels. Report semantics are checked separately. |
| Keep report labels but forge a comparison delta or batch count | Existing semantic validation refuses after inventory matching. |
| Reorder JSON keys, use escaped report labels, or add BOM/CRLF | Decoded inventories and summaries agree when each encoding supplies its matching raw-byte pin. |
| Keep report labels and consistently replace valid embedded drawings and reports | Inventory equality can hold. Trusted semantic consistency proves agreement of those embedded operands. |
| Replace packet bytes, expected digest and report expectations together | All supplied checks can agree with coordinated replacement. |

These checks provide label equality, optional byte equality and semantic consistency. Producer authenticity and original customer DWG custody require their own receipts. The summary retains `original_bytes_integrity` as `unverified`.

The focused controls use synthetic packets. The byte-volume benchmark holds two artifacts, one comparison and one batch fixed while growing ignored padding. It records reads, hashes, trusted calls, elapsed time and peak memory. Equality and refusal assertions carry no timing thresholds. Real dependency inventory, anonymous-block intake geometry and browser acceptance remain tracked by the frozen X04 benchmark.
