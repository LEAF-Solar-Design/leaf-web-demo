# X01 frozen acceptance: synthetic offline evidence map

This map describes synthetic support for the parent-owned full X01 grader.
The normal product export and string-tag workflow is currently incomplete.
No command here establishes a source-bound live grade, a PDF plot result, or a
successful normal product workflow. Merge status, builds, health endpoints and
source availability are not grades. All examples use synthetic inputs only;
there are no recorded execution receipts or measured results in this document.
No command writes an actual-grade or readiness marker or adds an approval gate.

Run the commands below from the repository root with Python and its standard
library. They work in PowerShell as written and use no shell-specific redirection,
heredocs, environment variables, external packages, browser, credentials or network.
Each filename in the table is under `tools/xbench/x01_offline/`.

## Frozen steps and their evidence boundaries

| Step and exact acceptance target | Current synthetic commands | Independent controls and exact limits |
| --- | --- | --- |
| 1. Use the normal export route, record the export SHA-256, and keep original fixture bytes unchanged. | `python tools/xbench/x01_offline/test_synthetic_workflow.py`; `python tools/xbench/x01_offline/run_offline_checks.py --output "../x01-synthetic-scratch/offline-run.json"`; source-custody command below. | The joined specimen compares hand-authored before/after bytes and detects deliberate mutation. The runner hashes three checker files and three JSON vectors before and after execution; the custody verifier compares them with current bytes. These are support-source hashes, not product fixture or export custody. Equal endpoint hashes cannot exclude intervening changes. The normal export route and unchanged real fixture bytes still require the parent grader's export replay; a synthetic byte copy is no substitute. |
| 2. Preserve every module's count and position, displacement at most 0.5 inch. | `python tools/xbench/x01_offline/check_cases.py --self-test`; `python tools/xbench/x01_offline/test_geometry_invariants.py`; `python tools/xbench/x01_offline/test_geometry_numeric_failures.py`. | Literal world coordinates, nested insert cases, translation, quarter-turn, input-order and independent complex-number transform controls cover the declared synthetic matching policy. Moved/missing/extra objects retain IDs; numeric overflow is a named refusal. Geometry vectors use inches and the implementation's 1e-10 epsilon at the 0.5 boundary. This does not enumerate modules in a real drawing or validate all product entity types. |
| 3. Paper tag text height at least 1.8 mm; tag overlap at most 10 percent. | `python tools/xbench/x01_offline/check_cases.py --self-test`; `python tools/xbench/x01_offline/check_unit_equivalence.py --self-test`; `python tools/xbench/x01_offline/test_tag_polygon_oracle.py`; `python tools/xbench/x01_offline/test_oblique_tag_oracle.py`; `python tools/xbench/x01_offline/test_unit_precision_boundaries.py`. | Literal rotated and multiline boxes separate text height from box height. Independent Fraction oracles cover right-angle intervals and bounded rational oblique rectangles using exact segment intersections and hull area. They do not establish arbitrary glyph or hatch geometry. Overlap is intersection area divided by the smaller box area. Case geometry uses 1e-10 epsilon; unit legibility rejects every representable height below 1.8. Boundary controls expose these distinct contracts rather than asserting identical tolerances. Paper conversion uses explicit paper-mm/model-mm sheet ratio, not arbitrary viewport custom_scale. Fonts, glyph extents, plotted text and PDF legibility remain unmeasured. |
| 4. No module or tag is on an off, frozen or nonplot layer. | `python tools/xbench/x01_offline/check_plot_layers.py --self-test`; `python tools/xbench/x01_offline/test_dxf_mutation_corpus.py`; `python tools/xbench/x01_offline/test_layer_resource_limits.py`; `python tools/xbench/x01_offline/test_array_insert_layers.py`; `python tools/xbench/x01_offline/test_synthetic_workflow.py`. | Hand-authored ASCII DXF records exercise flags, inherited layer zero, nested INSERT paths, missing/cyclic blocks, malformed/resource-limited input and named handles/layers. The joined control requires both module and tag roles; the checker's ordinary default can PASS with only one classified role, so a default PASS alone cannot establish both module and tag completeness. Array controls retain zero-based row/column occurrence IDs and inherited plot flags under explicit cell/total/depth caps; spacing and transformed coordinates are not evaluated. No binary DXF/DWG coverage or renderer/plot proof is implied. |
| 5. Above-module fill covers at most 5 percent. | `python tools/xbench/x01_offline/check_cases.py --self-test`; `python tools/xbench/x01_offline/test_fill_grid_oracle.py`; `python tools/xbench/x01_offline/test_geometry_invariants.py`. | Independent cell enumeration checks rectangle unions, holes, overlap, exact 5 percent and above-threshold cases, with per-fill IDs. Coverage is union intersection area divided by module area; the case checker applies 1e-10 epsilon. Synthetic axis-aligned rectangles do not establish arbitrary hatch boundaries, draw order, visual opacity or actual above-module fills. |
| 6. Every fixture with INSUNITS 0, 1 and 6 satisfies steps 2 through 5. | `python tools/xbench/x01_offline/check_unit_equivalence.py --self-test`; `python tools/xbench/x01_offline/test_unit_precision_boundaries.py`; `python tools/xbench/x01_offline/test_synthetic_workflow.py`. | Hand-authored signed coordinate arrays and heights compare physical equivalents: inches use 0.0254 metres/unit, metres use 1, unitless requires an explicit positive finite scale. Wrong units, absent scales, underflow, sign/order changes and adjacent boundary values are controlled; equivalence uses zero relative and 1e-12 absolute tolerance. The joined scene compares arrays for all three codes, but does not replay every step 2 through 5 on every real fixture/header. Full fixture coverage is still a parent-grader obligation. |
| 7. Numeric PASS combined with any step 3 through 5 failure must produce a named FAIL. | `python tools/xbench/x01_offline/test_synthetic_workflow.py`; `python tools/xbench/x01_offline/run_offline_checks.py --self-test`; `python tools/xbench/x01_offline/validate_offline_report.py --self-test`; `python tools/xbench/x01_offline/run_regression_suite.py --self-test`. | The joined control detects concealed named failure under numeric PASS and separately injects text, layer and fill failures. Runner controls preserve named findings, apply FAIL precedence over UNREADABLE, and continue to later rows. The validator accepts a consistent FAIL as valid evidence; validity does not mean design PASS. These local policies do not implement or certify the complete product grading/tag workflow. |

Independent controls add evidence beyond reproducing a calculator's own output;
they remain bounded to their authored cases. The current joined workflow reuses
checker arithmetic and is an integration control, not an independent geometry
oracle. Missing exports, missing required tags and unresolved scales are
UNREADABLE in the relevant controls, never invented clean passes.

## Smallest synthetic replay

Create a scratch directory outside the checkout. The following relative example
is quoted and can be replaced with another scratch location. An output parent
must already exist; the tools do not create it implicitly.

```powershell
python -c "from pathlib import Path; Path('../x01-synthetic-scratch').mkdir(parents=True, exist_ok=True)"
python tools/xbench/x01_offline/run_regression_suite.py --list
python tools/xbench/x01_offline/run_regression_suite.py --suite test_unit_precision_boundaries.py --timeout-seconds 30 --total-budget-seconds 60 --output "../x01-synthetic-scratch/regression.json"
python tools/xbench/x01_offline/run_offline_checks.py --timeout-seconds 30 --output "../x01-synthetic-scratch/offline-run.json"
python tools/xbench/x01_offline/validate_offline_report.py "../x01-synthetic-scratch/offline-run.json"
python tools/xbench/x01_offline/verify_report_sources.py "../x01-synthetic-scratch/offline-run.json" --source-root "tools/xbench/x01_offline"
```

The normal runner command above means the support runner's ordinary three-checker
mode; it is still synthetic and is not the normal product export route. Retain
its stdout diagnostics with the scratch report. Use fresh output paths for later
replays: an old file after a failed write is not a receipt of the new attempt.

The array, oblique, graph-benchmark and comparison tools are explicit siblings:

```powershell
python tools/xbench/x01_offline/test_array_insert_layers.py
python tools/xbench/x01_offline/test_oblique_tag_oracle.py
python tools/xbench/x01_offline/benchmark_layer_expansion.py --self-test
python tools/xbench/x01_offline/benchmark_layer_expansion.py
python tools/xbench/x01_offline/compare_offline_reports.py --self-test
python tools/xbench/x01_offline/compare_offline_reports.py "../x01-synthetic-scratch/offline-run.json" "../x01-synthetic-scratch/offline-run.json"
```

The last command compares the retained report with itself as a minimal no-change
control. To inspect a transition, pass a second separately retained report.
Comparison validates both reports and describes checker verdict transitions,
provenance changes and added/removed complete named diagnostics. Collection order
and dictionary-key order do not matter; lists within a diagnostic retain their
order because object roles and INSERT ancestry can be meaningful. Identical
whole diagnostics use set semantics within each checker or parent scope.
Durations are not diagnostic or verdict transitions. Comparison PASS can describe
an observed PASS-to-FAIL transition; it does not adopt a baseline or validate
source bytes. The array and oblique tests are explicit entries in the regression
manifest; benchmark and comparison helper CLIs above remain explicit selections.
No script is silently discovered for execution.

The fixed test inventory contains 22 explicit `test_*.py` entries, including
`test_geometry_matching_oracle.py`, `test_fill_tag_numeric_failures.py`,
`test_case_work_limits.py`, `test_source_read_mutation.py`,
`test_regression_inventory.py` and `test_report_comparison_algebra.py`.
The inventory control's default invocation uses scratch directories only. After
all sibling sources are integrated, audit the real offline directory separately:

```powershell
python tools/xbench/x01_offline/test_regression_inventory.py
python tools/xbench/x01_offline/test_report_comparison_algebra.py
python tools/xbench/x01_offline/test_regression_inventory.py --audit-root tools/xbench/x01_offline
```

The audit lists local `test_*.py` filenames and extracts literal `TESTS` and
optional `EXCLUSIONS` from the runner's AST without importing or executing either
the runner or the tests. Omitted tests, missing declared files, duplicates,
malformed declarations/entries, missing exclusion reasons and simultaneous
inclusion/exclusion produce deterministic named diagnostics. Exclusions must be
explicit filename-to-nonempty-reason mappings; the integrated manifest uses none
(`EXCLUSIONS = {}`). Inventory does not run suites or measure timing. Its JSON
uses `leaf.xbench.x01.offline-regression-inventory.v1`,
`evidence_kind: synthetic_offline` and `live_grade: false`; exit 0 means inventory
PASS and exit 1 means a named audit failure, not a checker or live verdict.
The report algebra uses four authored controls, three checker rows, at most four
diagnostics per scope, three provenance entries and 16 KiB per scratch report.
It checks identity, deterministic repetition, collection reordering, diagnostic
addition/removal symmetry, ordered nested payloads and inverse provenance changes,
including valid observed FAIL/UNREADABLE reports. Input bytes and SHA-256 remain
unchanged across comparisons; failure messages retain exact replay JSON.
These are synthetic report controls, not baseline adoption or source custody.

## Schema separation and artifacts

| Artifact | What it establishes |
| --- | --- |
| `leaf.xbench.x01.offline-run.v1` | Runner-observed child exits/findings, retained diagnostics and before/after support-source hashes. |
| `leaf.xbench.x01.offline-report-validation.v1` | Report structure/consistency validity, separate from `observed_verdict`. A valid FAIL is possible. It does not re-execute checks or hash current sources. |
| `leaf.xbench.x01.offline-source-check.v1` | Source-custody result: recorded before/after/current byte agreement for the six explicitly named support sources, separate from report validity and observed findings. It is not full-checkout or product-fixture custody. |
| `leaf.xbench.x01.offline-report-comparison.v1` | Validity of the two reports plus descriptive checker/provenance/diagnostic differences. It does not prove source custody, live behavior or readiness. |
| `leaf.xbench.x01.offline-regression.v1` | Execution results for selected whitelisted suites, not the ordinary runner's three checker rows. `--list` emits a manifest, without an execution verdict or child execution. Do not feed this schema to the normal report validator. |

All regression envelopes use `evidence_kind: synthetic_offline` and
`live_grade: false`. Whitelisted self-tests are read as bounded unittest text, including JSON examples followed by ordinary logs. An internal stub can explicitly request bounded structured-diagnostic parsing. A child body cannot override its exit. Ordinary exits are
0 PASS, 1 genuine FAIL, 4 UNREADABLE; unexpected exits, unsafe explicitly structured JSON, output limits,
capture failures and timeouts are UNREADABLE with named diagnostics. Aggregate
exit is 1 if any genuine FAIL remains, otherwise 4 if any row is UNREADABLE or
no suites were selected, otherwise 0. Every row remains present, including rows
skipped after the global deadline. Unknown suite names and malformed CLI values
refuse with exit 4 and `INVALID_ARGUMENT`. Repeated `--suite` flags run in the
specified order, including deliberate repeats. There is no filesystem discovery
and no recursive default invocation of the regression entrypoint.

## Runtime and verification limits

The fixed defaults contain 22 explicitly declared `test_*.py` scripts plus three
checker self-tests and runner, validator, source-custody and benchmark self-tests.
Execution is sequential, using absolute `sys.executable` and script argv paths,
with no shell. Script paths and fixture lookup are independent of the caller's
working directory; relative `--output` paths remain relative to that caller.

Default per-suite time is 30 seconds, configurable to a positive finite value
at most 300. Default total capture budget is 300 seconds, configurable to a
positive finite value at most 3600. Each launch uses the smaller of its timeout
and remaining total budget. Before every later launch the deadline is checked;
remaining rows become UNREADABLE/skipped with `REGRESSION_DEADLINE_EXCEEDED`.
The shared `run_offline_checks.capture_child` owns process-tree containment,
bounded pipe draining and termination; this wrapper adds no second supervisor.
Each capture has a separate cleanup allowance up to one second, which counts
against later launches but may extend elapsed time past the total capture budget.
Serialization, filesystem IO and scheduling have no hard wall-clock guarantee.

Capture retains at most 65,536 combined stdout/stderr bytes per child; row text
retains at most 16,384 UTF-8 bytes. Explicitly structured internal commands use the shared diagnostic limits
of 64 objects, 16,384 serialized bytes and eight JSON container levels. Invalid
or excessive diagnostics are replaced by a named UNREADABLE limit/refusal;
bounded raw text is still retained. Duration, exit, timeout flag, failure name,
diagnostics and truncation status are recorded per executed suite.
Optional output reuses the existing same-directory atomic writer with cleanup
on failure; a write error preserves FAIL precedence and otherwise refuses.

The regression self-test runs only bounded Python stub commands for PASS, named
FAIL, UNREADABLE, unexpected exit, timeout and global deadline/order controls,
plus manifest/invalid-CLI controls that prohibit child capture. It never invokes
the default regression list. Planner's exact authoritative verification:

```powershell
python tools/xbench/x01_offline/test_regression_inventory.py
python tools/xbench/x01_offline/test_report_comparison_algebra.py
python tools/xbench/x01_offline/run_regression_suite.py --self-test
```

The selected real suite in the smallest replay supplies a separate check of
ordinary script integration; do not run the full defaults just for local coverage.
For optional synthetic measurements:

```powershell
python tools/xbench/x01_offline/benchmark_synthetic.py --self-test
python tools/xbench/x01_offline/benchmark_synthetic.py --sizes 4,8,16 --repeats 1
python tools/xbench/x01_offline/benchmark_layer_expansion.py
```

The benchmark records elapsed time and Python traced allocation peaks against
analytic known outputs. It has no machine-specific performance threshold and
supports no conclusion about live responsiveness, process RSS or product speed.
