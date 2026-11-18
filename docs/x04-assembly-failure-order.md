# X04 assembler failure order

Studio X-bench X04's offline assembler binds explicit local synthetic artifacts and saved reports. Each row may optionally include `expected_sha256`, exactly 64 hexadecimal characters. It is checked against the same raw byte buffer that is decoded, including a BOM and whitespace. Uppercase hexadecimal is accepted. Omitting the field preserves unpinned behavior; providing null or another malformed value refuses. Pins are consumed during assembly and omitted from packet entries. They do not authenticate an original drawing, make the manifest trustworthy, or upgrade the packet's `unverified` integrity.

Minimal row change, using the SHA-256 of the actual synthetic artifact file:

```python
row['expected_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
```

The producer chooses the pin before assembly. An independent consumer must still establish whether that manifest and its expected digests are trustworthy. The assembler never reopens a source to hash a second version after decoding.

| Boundary | Work already incurred when it refuses | Publication state |
|---|---|---|
| Option parsing | Parser construction only; invalid ceilings and missing/unknown operands stop here | No path resolution, source read or output creation |
| Manifest/output alias check | Operand parsed and manifest path resolved | No manifest read or output replacement |
| Manifest byte/JSON read | Manifest bytes read up to per-file/aggregate detection boundary, then strict decoding | Prior destination retained |
| Manifest shape/depth/row checks | Parsed manifest and full-container traversal; section, ID, path, pin-format and source/output alias checks | No artifact/report reads yet |
| Explicit source reads and optional pins | Each earlier occurrence was loaded; current bounded bytes were read and hashed before JSON decoding | Later occurrences stop; prior destination retained |
| Packet binding checks | Every explicit source was decoded; IDs/references, trusted entity counts and saved-report claims are checked | No output temporary yet |
| Output encoding/ceiling | Assembly and all bindings succeeded; JSON encoder accumulates complete ASCII output with newline | Oversized encoded output refuses before temporary/stdout write |
| Temporary write/flush/close | Encoding complete and one owned temporary created | Failures clean only that path and retain prior destination |
| Atomic replacement | Complete temporary closed successfully | Replacement success publishes one complete packet; failed replacement retains prior destination and cleans owned temporary |

Source trace: `scripts/assemble_x04_evidence_packet.py:93-102` parses options. Lines 103-109 resolve/check the manifest, select the call-local reader, assemble, then write. Lines 52-60 guard the entire manifest depth, including ignored extensions; lines 64-84 validate explicit entries and aliases, including optional pin syntax at line 75. Lines 86-90 load every explicit occurrence and invoke packet validation. Its reference, artifact and report phases are in `scripts/validate_x04_evidence_packet.py:50-104`.

Raw-byte trace: `scripts/assemble_x04_evidence_packet.py:23-42` counts actual per-file/aggregate bytes and passes its single buffer to the decoder. `scripts/export_x04_offline_evidence.py:25-35` bounds ordinary short reads; lines 38-48 validate/compare optional pins; lines 49-66 apply strict duplicate-key, nonfinite and integer-token decoding. Lines 69-81 read each unbudgeted source once and decode that same buffer. The manifest itself is not pinned by these per-source row fields.

Writer trace: `scripts/export_x04_offline_evidence.py:170-188` fully encodes and checks the output ceiling before emission. Lines 190-207 create, write, flush, close and replace the owned temporary, with cleanup on exceptions and cancellations. Cleanup failure can leave the owned temporary and is reported. A cancellation retains its original exception identity when cleanup succeeds. Atomic replacement is not fsync durability; no promise is made about hostile concurrent alias replacement or source snapshot consistency.

Sources are read successively, and repeated paths are read again. A changed repeated source must satisfy that occurrence's pin, if present, and the embedded report bindings. A matching hash is not proof of valid claims: decoding and trusted semantic checks still follow. Output aliases to any source refuse; a symlink to an unrelated destination is replaced as a link without modifying its target. Embedded dependency names and report path labels remain inert data, though assembly opens every explicitly named file. No failure-order claim here establishes complete raw dependencies, original drawing authentication, redaction, browser behavior or live ten-drawing acceptance.
