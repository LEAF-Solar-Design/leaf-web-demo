# X04 pin coverage audit

Studio X-bench X04's offline assembler can check expected bytes at two boundaries. Neither boundary authenticates a person, a manifest producer or an original drawing. A trusted expected digest can detect different bytes; an attacker who supplies both the bytes and their expected digest can replace both. Entity counts and saved-report bindings remain separate semantic checks.

| Location | Enforcement |
|---|---|
| CLI `--expected-manifest-sha256` | Optional exact raw manifest buffer, before explicit source reads |
| Artifact/comparison/batch row `expected_sha256` | Optional exact raw buffer read for that occurrence |
| Row without `expected_sha256` | No byte pin; strict decode and semantic checks still apply |
| Top-level JSON `expected_sha256` or `expected_manifest_sha256` | Ignored manifest metadata, not a CLI manifest pin |
| Row `extension.expected_sha256` or another nested lookalike | Ignored manifest extension, not a recognized row pin |
| A similarly named field inside an artifact/report | Source data, not a manifest row pin; it can remain embedded without enabling byte enforcement |
| Packet after assembly | Row pin metadata is omitted; validation checks embedded semantics, not vanished source files |

Source basis: `scripts/assemble_x04_evidence_packet.py:102-110` parses and applies the optional manifest digest through the selected reader. Lines 68-88 inspect `expected_sha256` only directly on each explicit source row, construct recognized packet fields and read each occurrence with its own optional pin. Lines 89-90 require packet validation before return. The full-manifest depth check at lines 52-60 still applies to ignored metadata, so ignored does not mean exempt from depth limits.

`scripts/export_x04_offline_evidence.py:38-48` validates 64 hexadecimal characters, normalizes case and compares SHA-256 before decoding. Lines 65-81 decode the same buffer, including raw BOM and whitespace bytes in the digest. `scripts/assemble_x04_evidence_packet.py:23-42` does the same with actual per-file/aggregate budgets. Byte ceilings precede hash/decode work. Hash/decode cancellation occurs after the read context has closed and leaves publication unstarted.

Minimal synthetic handoff failure:

```python
source.write_bytes(source.read_bytes() + b' ')
manifest['artifacts'][0]['expected_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
```

The changed file remains valid JSON with equal decoded claims. If the recipient accepts this changed manifest without an independently fixed manifest digest, the refreshed row digest passes. If a fixed `--expected-manifest-sha256` covers the old manifest bytes, the changed manifest refuses before source reads. If the recipient also takes a replacement manifest digest from that same untrusted sender, the byte checks can again pass. This is why a digest supplied with its own untrusted bytes is not provenance authentication.

Before manually relying on a synthetic handoff, check:

- Record which independent receipt or producer supplied the expected manifest digest. State when no such source is established.
- Enumerate every explicit row and its optional pin. Repeated paths need an intentional pin for each occurrence.
- Distinguish direct row fields from ignored top-level/nested lookalikes and unpinned occurrences.
- Confirm whether source bytes, including BOM/whitespace, were the intended bytes. A digest refresh is a new expected value, not semantic approval.
- Keep the packet's original-byte integrity `unverified`. Matching hashes do not override count/report refusals or establish raw dependency completeness.

The network-denied portability control assembles with manifest and row pins, deletes those files, then validates the self-contained packet from unrelated cwd. It proves portability of embedded consistency, with exact counts and integrity still `unverified`. The packet contains no continuing authentication proof for deleted files. Reads are successive, not a simultaneous snapshot; publication uses atomic replacement without an fsync promise. No browser, real drawing or live acceptance follows from this audit.
