# X04 drawing binding scope

Studio X-bench X04 checks caller-supplied drawing labels within named packet artifacts before trusted validation. This guard complements artifact/report inventories and semantic recomputation.

| Check | Source | Boundary |
| --- | --- | --- |
| Literal mapping parser | scripts/validate_x04_evidence_packet.py:28-32 | Split the first equals sign, validate the local key and nonblank drawing label. |
| Duplicate caller keys | scripts/validate_x04_evidence_packet.py:135-138 | Refuse before path checks and reads. |
| Bounded input and pin | scripts/validate_x04_evidence_packet.py:148-150 | Pin and decode the actual buffer before examining labels. |
| Section inventories | scripts/validate_x04_evidence_packet.py:151-160 | Supplied artifact/report ID sets precede drawing bindings. |
| Drawing map | scripts/validate_x04_evidence_packet.py:161-174 | Named keys must exist; drawing.id compares literally. |
| Semantics and output | scripts/validate_x04_evidence_packet.py:175-176 | Recompute trusted claims, then publish atomically. |

Synthetic counterexamples:

- A partial map naming only artifact a permits a valid drawing-label replacement in artifact b. Both artifacts still receive semantic validation.
- An omitted map permits consistent drawing relabelling when drawing.id and request.drawing_id agree. Semantic consistency remains required.
- Replacing a valid drawing label and its caller expectation together satisfies the label guard. It does not establish producer identity or customer DWG custody.
- Keeping a matching label while forging a physical count still fails trusted recomputation.
- An NFC label and a canonically equivalent decomposed label differ literally and refuse. JSON escapes, reordered keys, BOM and CRLF preserve decoded equality when supplied byte pins match each encoding.

File/stdin short-read controls prove that digest and map decisions use one buffer without reopening the source. Concurrent calls use independent maps. Optimized and seeded network-denied subprocesses retain verdicts and refusal outputs. The byte-volume benchmark fixes two artifacts and grows ignored padding, recording one read/hash, trusted calls, elapsed time and peak memory without timing thresholds.

Drawing labels establish equality to the caller's label. Digest pins establish equality to expected encoded bytes. Semantics establish internal consistency of embedded claims. Producer authenticity and original DWG custody need independent receipts. Original-byte integrity remains unverified. Frozen X04 live dependency inventory, anonymous-block geometry and browser acceptance remain separate obligations.
