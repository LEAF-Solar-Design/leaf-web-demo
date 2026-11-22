# X04 original bytes and saved replay integrity

Parent: Studio X-bench X04 intake, track 2. Retaining an upload's digest and
counting saved geometry prove different things.

The count tool now returns `original_bytes_integrity: "unverified"` in both
listings and selected results. It never opens the original source or decodes
optional raw-byte payloads. It cannot authenticate them from geometry or from
a stored digest alone. This status is about this count operation; it does not
retract independent upload-service retention evidence.

Known geometry fields and selectors are validated before counting. Unknown
saved JSON extensions remain opaque and unchanged. The offline tests add
synthetic `original_bytes_base64`, `original_byte_length` and `original_sha256`
fields. They are test extensions, not a documented product storage schema.
Valid, tampered, malformed, incompatible and absent values all stay unverified.
They cannot silently turn into verified integrity claims or alter the geometry
count. No input mutation, original-file open or target fetch occurs.

Real byte verification requires the actual original byte sequence and an
independent trusted expected digest. Recompute SHA-256 over those bytes and
compare length and digest against the trusted receipt. A digest copied from
the same editable saved document does not authenticate that document. A
successful saved geometry replay is not proof of original DWG retention,
decoding fidelity or completeness.

Run from the X04 worktree with existing dependencies:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_saved_byte_integrity.py server/tests/test_x04_original_bytes_replay.py server/tests/test_x04_selection_envelope_consistency.py -q
npm --prefix web run test:unit -- src/components/ModuleSetPicker.test.jsx src/components/DrawingDependencyReport.test.jsx
node scripts/bench_x04_picker_async_recovery.mjs --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-picker-async-recovery.json
```

The first file alters payload, base64 encoding, length, digest and metadata
types. Every replay must explicitly return unverified, with exact seven-module
geometry count and parent highlights A/B. The original-byte replay control
hashes its retained synthetic buffer after each pipeline operation. Only that
independent buffer comparison establishes its unchanged bytes.

For incompatible known geometry, re-import the original through normal intake
and list the sets again. For an unverified raw-byte claim, obtain the retained
original and its trusted upload receipt; do not repair a digest from editable
saved metadata. Staging's ten-drawing original-upload receipt remains separate
from these offline synthetic controls.

The DOM checks change labels/counts while pending, replace callbacks, and
transition a report from valid to malformed to valid without remounting.
The async benchmark repeatedly rejects then resolves selection promises,
requires one callback per activation and no unhandled rejections, records time
and heap without thresholds, then unmounts and closes its DOM in finally.
