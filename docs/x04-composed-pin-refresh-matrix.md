# X04 composed byte-pin refresh matrix

Studio X-bench X04's offline handoff has three separate expected-byte boundaries: a source-row digest, an independently retained manifest digest, and an independently retained assembled-packet digest. None authenticates provenance or an original drawing. Matching bytes still face entity/report checks, and integrity remains `unverified`.

| Edit | Source-row expected digest | Separately retained manifest digest | Separately retained packet digest |
|---|---|---|---|
| Add whitespace to a source file without changing decoded data or order | Refresh that occurrence's row digest | Refresh because the row digest changes manifest bytes | Keep if reassembly emits identical packet bytes |
| Change source JSON key order or semantic content | Refresh affected occurrences after deciding the bytes are acceptable | Refresh when row metadata changes | Refresh if reassembly emits different packet bytes; semantic reports may also need regeneration |
| Correct a wrong row digest while source bytes stay fixed | Correct it to the intended source bytes | Refresh for the edited manifest bytes | Keep if the resulting assembled packet bytes are identical |
| Change only ignored manifest metadata or manifest whitespace | Keep all source-row pins | Refresh the changed manifest bytes | Keep if packet bytes stay identical, since manifest metadata is omitted |
| Change a manifest path or occurrence order | Existing row digest must match the newly selected bytes; do not silently waive it | Refresh to reach the new routing | Refresh if accepted reassembly changes packet bytes; stale report bindings can still refuse |
| Change packet whitespace, key order, Unicode escaping, BOM or line endings | Keep source-row pins | Keep manifest pin | Refresh to accept the exact new packet encoding, even when normalized claims are equal |
| Forge a count/report and recompute its digest | A byte match is possible | A new manifest digest is possible | A byte match is possible, but trusted semantic validation must still refuse the forgery |

Every refresh in this table is a new expected value, not approval of the changed content. Retain the predecessor digest and refusal receipt before accepting a synthetic edit. Do not refresh unrelated expected values just to make a refusal disappear.

Source enforcement is direct row `expected_sha256` at `scripts/assemble_x04_evidence_packet.py:75-90`; strict coverage is optional. `--expected-manifest-sha256` is parsed at lines 104-105 and applied to the manifest buffer at lines 113-114, before source assembly at lines 115-117. Output publication follows at line 118. Row-pin metadata is consumed rather than copied into packet entries.

Packet enforcement is `scripts/validate_x04_evidence_packet.py:118-125`: parse the optional expected digest, read/pin the raw file or stdin buffer, then validate embedded claims. The shared byte comparison is `scripts/export_x04_offline_evidence.py:38-48`; UTF-8 BOM handling and strict JSON decode follow at lines 65-66. Thus byte ceilings precede hash checks, which precede decoding and trusted semantic validation. A pinned file is not reopened to compute a different buffer's digest.

Two useful counterexamples:

- A sender changes source whitespace, refreshes the row digest, rewrites the manifest and supplies a replacement manifest digest through the same untrusted channel. Without an independently fixed expected manifest value, those coordinated bytes can all match. A fixed old manifest digest would refuse before payload reads.
- A sender replaces a whole self-consistent synthetic packet and its expected packet digest together. Matching transit bytes alone cannot distinguish that handoff from the intended packet. Rechecking embedded semantics establishes internal consistency, not sender or original-drawing authenticity.

The composed replay tests source-only whitespace, row-pin-only failure, manifest-only whitespace and packet-only whitespace in order. Each failed boundary retains prior output. Correcting the affected expectation recovers exact count/report claims; changes omitted by assembly need no packet refresh when emitted bytes are unchanged. Serialization controls require each exact encoding's own digest and refuse cross-encoding digests before trusted validation, while normalized summaries remain equivalent.

Owned file streams close before hash/decode cancellation; stdin remains caller-owned. Publication starts only after complete semantic validation and output encoding. Atomic replacement does not imply fsync durability or a simultaneous source snapshot. Embedded paths remain inert data, but explicit source files are read. No raw dependency completeness, redaction, browser behavior or live ten-drawing acceptance is proved by this matrix.
