# X04 synthetic evidence index

Parent: Studio X-bench X04 raw customer drawing intake. These tests prove bounded local invariants. The shared grader on an exact staging source remains the real-drawing acceptance gate.

Run one listed pytest command at a time from the repository root. The common prefix is:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server
```

Append the named files and `-q`. Component checks use the PC-only pair verifier, whose exact commands and outcomes are preserved in each receipt. No browser, external service or customer file is required to read this index or run these synthetic checks.

| Frozen step | Existing focused checks | Proven invariant and limit |
| --- | --- | --- |
| 1 Upload terminal state | Separate upload-cap PR3109 and its x04d-upload-cap receipt | Bounded default cap and input hash preservation. Synthetic source proof cannot prove browser modal absence or ten real upload deadlines. Current root live grade separately proves all ten terminal uploads at source8190df78. |
| 2 Dependency dispositions | server/tests/test_drawing_dependency_report.py; server/tests/test_x04_dependency_record_boundaries.py; server/tests/test_x04_dependency_control_characters.py | Decoded records retain deterministic dispositions and distinct references without opening dependency paths. Raw DWG inventory completeness needs the independent pinned binding. CR/LF control tests start from decoded pairs, not multiline raw DXF. |
| 2 Report display | web/src/components/DrawingDependencyReport.test.jsx | Text stays inert, malformed reports say unavailable, frozen reports survive drawing swaps without mutation. DOM checks prove component behavior, not browser layout or raw metadata completeness. |
| 3 Original bytes | server/tests/test_x04_intake_selection_e2e.py; server/tests/test_x04_saved_intake_replay.py | Synthetic intake/counting leaves bytes and stored intake unchanged; saved selection uses no original source access. Real original-hash proof is the source-bound root grade. |
| 4 Geometry eligibility | server/tests/test_x04_polygon_planarity.py; server/tests/test_x04_polygon_topology.py; server/tests/test_x04_polyline_curve_fields.py; server/tests/test_x04_polygon_numeric_limits.py | Straight, strictly convex, finite planar quads qualify. Bulges and nonzero supplied thickness exclude candidates; supplied normals must be finite and nonzero. Coordinates already represent WCS. Tilted and vertical controls qualify. This does not repair missing anonymous block geometry or prove rendered geometry. Thickness not preserved by an upstream reader cannot be inferred here. |
| 5 Exact counts | server/tests/test_count_modules.py; server/tests/test_x04_count_differential.py; server/tests/test_x04_minsert_arrays.py; server/tests/test_x04_count_precision_boundary.py | Explicit set selection counts physical unions and weighted arrays, with original parent highlights. The independent seeded oracle checks every small set subset. Above2**53-1 refuses before metadata output. Arithmetic boundary tests stream weights, with a reduced-ceiling loader control to avoid millions of retained entities. |
| 5 Identity and invalid replay | server/tests/test_x04_set_identity_collisions.py; server/tests/test_x04_selector_type_failures.py; server/tests/test_x04_replay_schema_failures.py | Tuple encoding avoids delimiter collisions; typed selectors and malformed stored intakes refuse without partial counts. Truncated JSON fails at JSON decoding; a raw JSON string also refuses at the tool boundary. No attempt to reopen the source. |
| 6 Recorded competing sets | web/src/components/ModuleSetPicker.test.jsx; server/tests/test_x04_selection_drawing_isolation.py; server/tests/test_x04_selection_argument_contract.py | Explicit independent choices, drawing isolation, prototype-named IDs, stale-choice pruning, malformed-ID refusal and enclosing-form isolation. Normal catalog wiring remains subject to source-bound browser interaction proof. |
| 7 No per-drawing code | Shared exact-source grader scan | Synthetic fixtures and generic predicates do not replace the deployed-source scan. Root source8190df78 passes; the future module source needs its own scan. |

For a single focused geometry command:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_polyline_curve_fields.py server/tests/test_x04_polygon_topology.py server/tests/test_x04_polygon_planarity.py -q
```

For replay and identity:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_saved_intake_replay.py server/tests/test_x04_replay_schema_failures.py server/tests/test_x04_set_identity_collisions.py -q
```

Performance scripts `scripts/bench_x04_module_count.py`, `scripts/bench_x04_dependency_report.py`, `scripts/bench_x04_selection_fanout.py` and `scripts/bench_x04_array_multiplicity.py` assert exact outputs and record elapsed time/peak allocations. They have no timing gates. The memory scope excludes synthetic input construction. Previous replay guides remain in docs/x04-offline-repro.md, docs/x04-synthetic-acceptance-walk.md and docs/x04-insert-array-contract.md. Later topology and precision tests refine the older array contract's eligibility boundaries.

Unresolved real proof: independently pinned raw dependencies, shared reader anonymous block geometry, exact module counts against real oracles, visible competing-set interaction and actual browser rendering on the delivered source. Owner and receipt conditions live in the X04 status and shared ledger outside this repository. No synthetic PASS is reported as live acceptance.
