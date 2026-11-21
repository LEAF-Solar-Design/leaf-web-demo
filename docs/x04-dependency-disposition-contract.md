# X04 decoded dependency dispositions

Parent: Studio X-bench X04 raw drawing intake. This report describes records retained by the existing local decoder. A separate pinned raw inventory is needed to prove original DWG completeness.

| Dependency | Decoded record and owning section | Name and disposition |
| --- | --- | --- |
| External drawing | BLOCK in BLOCKS with flag70 bit4 or bit8 | First group1, fallback2; detached because the local reader does not follow drawings. |
| Primary font | STYLE in TABLES, group3 | font unless flag70 bit1 marks shape_font; font remapped to sans-serif. |
| Big or shape font | STYLE in TABLES, group4 | shape_font; font remapped to sans-serif. |
| Image | IMAGEDEF in OBJECTS | First group1, fallback5; image omitted. |
| Underlay | PDFDEFINITION, DWFDEFINITION or DGNDEFINITION in OBJECTS | First group1, fallback2 then5; underlay omitted. |
| Proxy entity | ACAD_PROXY_ENTITY or PROXY_ENTITY in ENTITIES or BLOCKS | First group5, fallback1 then record kind; proxy omitted. |
| Proxy object | ACAD_PROXY_OBJECT or PROXY_OBJECT in OBJECTS | First group5, fallback1 then record kind; proxy omitted. |

`bound` is a UI-recognized disposition but this local extractor does not claim to bind references. It never opens a target, downloads a font or loads an image. Missing names produce no invented path. Repeated group codes use the first value within that record. Names stay literal after decoding: Unicode normalization, case folding and slash rewriting are not applied. Equal kind/name pairs deduplicate, and output order is deterministic.

For a full decoded DXF, SECTION selects the owning scope and ENDSEC clears it. EOF terminates discovery. Dependency-like fields in HEADER, unrelated records, the wrong section or after EOF cannot create references or supply a previous record's missing name. Legacy record-fragment callers without SECTION markers remain supported until ENDSEC or EOF. This compatibility mode does not certify the fragment's original file context.

A synthetic TABLES/STYLE record with group3 `notes.shx` produces a font remap. The same STYLE in OBJECTS is ignored. A BLOCKS/BLOCK with70=4 and1=`external.dwg` reports detached; it opens no file. OBJECTS/IMAGEDEF with1=`image.png` reports image omitted. A missing STYLE name followed by ENDSEC group3=`neighbor.shx` reports no font. Tests use synthetic data only.

A valid report with an empty dependency list means no supported references were found in the retained local records. An absent or malformed report is unavailable, not an empty inventory. The UI shows unconfirmed coverage for unavailable reports, and unknown for unsupported disposition strings. Neither an empty report nor successful saved replay proves there were no original raw references. Omitted converter metadata remains an unresolved boundary.

The existing reader now consumes a UTF-8 BOM before group-code parsing so the first SECTION remains intact. Its replacement-decoding policy and original bytes are preserved. This is not strict encoding validation or a substitute for raw dependency inventory.

Run focused checks sequentially from the repository root:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_dependency_section_boundaries.py server/tests/test_drawing_dependency_report.py server/tests/test_x04_dependency_flag_contract.py server/tests/test_x04_original_byte_encodings.py -q
npm --prefix web run test:unit -- src/components/ModuleSetPicker.test.jsx
python scripts/bench_x04_sparse_dependencies.py --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-sparse-dependencies.json
```

The sparse benchmark holds two valid references fixed while adding1000/10000/100000 unrelated records. Exact dispositions and unchanged input are assertions; elapsed time and traced allocations have no thresholds. Input construction is excluded. Browser layout, physical assistive technology and final real-drawing acceptance still require source-bound staging evidence.
