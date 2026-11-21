# X04 original bytes and replay recovery

Parent: Studio X-bench X04 raw customer drawing intake. Original-byte custody, decoded geometry and saved-intake replay prove different things. Keep those receipts separate.

The retained upload bytes are the original. Compute their SHA-256 from those bytes and preserve that digest beside the upload evidence. Parsing, dependency reporting, candidate listing, selection and JSON replay must leave them unchanged. A saved intake is a decoded representation; hashing its JSON does not recreate or prove the original drawing digest. Never serialize an intake back into a new file and label that file the original.

Synthetic checks use a mutable input buffer so unintended changes can be detected. They compare exact bytes and SHA-256 after every pipeline stage. Source-file openers are guarded during saved replay, proving that counting uses the supplied snapshot instead of reopening the original. That proves read behavior and byte retention for those fixtures. It does not prove geometry fidelity, correct conversion, complete raw dependencies or real browser rendering.

UTF-8 BOM is consumed for parsing before the first DXF SECTION; the original BOM bytes remain in the retained input. CRLF and non-ASCII reference fixtures also preserve exact originals. Binary DXF is explicitly unsupported by the minimal reader. UTF-16 structure and unparseable bytes fail the group-code parser. The UTF-8 replacement policy can lose undecodable character information while the raw bytes and digest remain intact. A matching original hash therefore does not certify decoded text fidelity.

For recovery, retain the original upload and its digest, preserve the failed saved snapshot as evidence, then re-import the original through the supported route. If a saved intake has incompatible known fields, the count tool returns re-import guidance without partial output. Make a fresh explicit set selection on the new listing. A missing or malformed optional dependency report is unavailable in the report UI; counting does not invent repairs or raw-reference coverage. Unknown additive JSON metadata stays opaque and unchanged.

If the original is unavailable, compatible saved data can still support a recorded count, but original-byte custody cannot be re-established from that JSON alone. State the missing source proof explicitly. Counts after a failed request must match a fresh compatible replay and leave the snapshot unchanged. Async UI failure must preserve the explicit choice for retry; a removed candidate must not reappear when an old request settles.

Run these focused commands sequentially from the repository root:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_original_bytes_replay.py server/tests/test_x04_original_byte_encodings.py server/tests/test_x04_saved_intake_replay.py server/tests/test_x04_selection_failure_recovery.py server/tests/test_x04_dependency_replay_schema.py -q
npm --prefix web run test:unit -- src/components/ModuleSetPicker.test.jsx src/components/DrawingDependencyReport.test.jsx src/components/X04OfflineFlow.test.jsx
python scripts/bench_x04_rejection_recovery.py --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-rejection-recovery.json
```

The composition test supplies synthetic saved-result objects to the report and picker together. It checks drawing-matched rows, candidate labels and recorded summaries; it does not call a real intake service. The recovery benchmark repeats invalid then valid trusted selections over one fixed synthetic intake. Exact384-module counts,64 parent highlights and unchanged inputs are assertions. Time and peak traced allocations have no thresholds, and input construction is excluded.

Final acceptance still needs an exact staging-source receipt, retained real originals, an independent raw dependency inventory, geometry provenance and actual browser interaction/render evidence. Source and CI receipts alone do not close those seams.
