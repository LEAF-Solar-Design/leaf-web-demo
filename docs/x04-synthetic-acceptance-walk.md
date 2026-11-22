# X04 synthetic acceptance walk

Parent: Studio X-bench X04 raw customer drawing intake. This offline recipe proves
the synthetic local count/report path only. It does not produce a held-out PASS.

Run sequentially from the X04 module-count worktree:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_intake_selection_e2e.py server/tests/test_x04_minsert_arrays.py server/tests/test_count_modules.py server/tests/test_x04_count_differential.py -q
node web/node_modules/vitest/vitest.mjs run src/components/ModuleSetPicker.test.jsx src/components/DrawingDependencyReport.test.jsx --root web
python scripts/bench_x04_selection_fanout.py --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-selection-fanout.json
```

The normal-intake test constructs one ASCII DXF in memory with a font reference,
one block definition and two INSERT sets. East is a two-row, three-column array;
West is one ordinary reference. The original byte digest and intake stay unchanged.

1. Parse the synthetic bytes through the normal local DXF reader. The source name
   stays synthetic-two-set.dxf and the array metadata survives.
2. Read the intake dependency report. notes.shx is remapped to sans-serif. The report
   explicitly limits coverage to preserved local conversion records.
3. List the normal trusted count-modules tool. East reports six candidates and West
   reports one. The set IDs come from the listing, not a fixture-specific code path.
4. Submit both IDs explicitly. The result records both selected set identities and
   count seven. Highlights are the original parent handles A and B, each once.
5. Inspect the DOM checks. Two mounted drawing pickers retain independent choices.
   Missing metadata is unavailable, unsupported dispositions are unknown, and neither
   state implies complete dependency coverage.

The array contract follows [Autodesk INSERT DXF fields](https://help.autodesk.com/cloudhelp/2024/ENU/AutoCAD-DXF/files/GUID-28FA4CFB-9D5E-4880-9F11-36C97578252F.htm):
columns use group 70, rows use 71, and omitted counts default to one. Spacings use
44 and 45. The [row setter uses UInt16](https://help.autodesk.com/cloudhelp/2027/ENU/OARX-RefGuide/files/OARX-RefGuide-AcDbMInsertBlock__setRows_Adesk__UInt16.html).
The local count contract accepts positive UInt16 integers, refuses invalid or
conflicting dimensions, and aggregates rows times columns without allocating cells.
Rotation changes placement, not cardinality. Highlight identity is the original CAD
reference handle, not an invented per-cell handle. This does not prove array render
or write-roundtrip fidelity in other readers or editors.

The fanout benchmark holds 2,048 input records and 256 physical handles fixed while
varying candidate sets and selected IDs. Expected unions come from a generator-owned
ledger. Time and peak traced allocations are observations, never pass thresholds.

| Frozen real acceptance | Remaining boundary |
|---|---|
| Upload deadline and no modal | Needs real upload and visible drawing-view evidence on staging. |
| Complete dependencies | Needs the pinned raw inventory, not only converted-record evidence. |
| Original bytes | Synthetic digest is checked; all ten real stored originals still need comparison. |
| Rendered module geometry | No browser engine or real INSERT/array rendering is exercised here. |
| Exact real module count | Synthetic seven is independent ground truth; each real oracle remains separate. |
| Competing-set pick | Explicit tool selection and DOM isolation are checked; real Studio selection receipt remains. |
| No per-drawing code | All inputs here are synthetic; final deployed source scan remains required. |
