# X04 INSERT array counting

This contract belongs to Studio X-bench X04 raw drawing intake and its explicit module-set selection tool. Synthetic proof here does not establish the ten real drawings' live acceptance.

The existing local DXF decoder preserves explicit INSERT array fields:

| DXF code | Stored field | Default |
| --- | --- | --- |
| 70 | columns | 1 |
| 71 | rows | 1 |
| 44 | column_spacing | 0 |
| 45 | row_spacing | 0 |

Ordinary INSERT records without these fields keep their existing DTO shape. Counting treats absent rows and columns as one. Spacing and rotation affect placement, but do not change cardinality. The Autodesk [INSERT reference](https://help.autodesk.com/cloudhelp/2024/ENU/AutoCAD-DXF/files/GUID-28FA4CFB-9D5E-4880-9F11-36C97578252F.htm) defines these fields. The [row setter](https://help.autodesk.com/cloudhelp/2027/ENU/OARX-RefGuide/files/OARX-RefGuide-AcDbMInsertBlock__setRows_Adesk__UInt16.html) uses UInt16.

The tool accepts integer dimensions from 1 through 65535. Zero, negative, fractional, boolean, string, null and oversized dimensions return an invalid array multiplicity error. Two eligible records with the same normalized handle but different dimensions return a conflicting array multiplicity error, even if they appear in different sets or one is a polygon. Both errors refuse the whole result with no partial count or highlight overlay. Equal multiplicities sharing a handle count once across selected sets. A polygon contributes dimensions (1, 1).

For example, parent A with rows 2 and columns 3 contributes six modules. Parent B with neither field contributes one. Selecting both gives seven and highlights only A and B. Rotation of A preserves six. An alias `0xa` deduplicates A. If that alias reports rows 3 and columns 3, counting refuses the conflict. No synthetic child handles or cell records are allocated. Highlight identity belongs to the original parent, so an array has one highlight even when it represents many modules. Array rendering, export fidelity and cell-specific highlighting remain outside this contract.

Polygon eligibility requires four finite, distinct corners, nonzero turns and finite nonzero projected area. Missing Z means zero. A normal from the first three corners defines the plane; the fourth corner's distance divided by local extent must be at most 64 times the floating-point ULP at one. This allows rounding error rather than a drawing-unit tolerance. Projection drops the strongest normal axis, supporting vertical and tilted planes. Clearly twisted quadrilaterals are excluded. Numeric overflow or underflow can make a candidate ineligible. Self-intersecting geometry classification is not added here.

Saving the intake as strict JSON preserves array metadata, candidate IDs and dependency dispositions. Replaying selection uses the stored intake and the trusted registry tool, without reopening the original drawing. Dependency dispositions describe local decoded records; they do not certify raw DWG dependency completeness.

Run these commands sequentially from the repository root on the PC:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_polygon_planarity.py server/tests/test_x04_cross_kind_handle_conflicts.py server/tests/test_x04_saved_intake_replay.py server/tests/test_x04_minsert_arrays.py server/tests/test_x04_polygon_numeric_limits.py -q
python scripts/bench_x04_array_multiplicity.py --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-array-multiplicity.json
```

The benchmark fixes 128 parents and increases dimensions from 1x1 through 65535x65535. Every row asserts the exact weighted count and 128 parent highlights. Elapsed time and traced allocation peaks are observations without thresholds. Input construction is excluded. These commands use only synthetic data, with no customer file or source-file replay access. Picker component tests add malformed-ID refusal in the existing PC-only pair verification command; DOM evidence does not establish browser rendering or physical keyboard behavior.
