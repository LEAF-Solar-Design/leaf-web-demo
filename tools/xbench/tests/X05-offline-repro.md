# X05 offline reproduction

Parent: Studio X-bench X05 engineering-set reconciliation. These checks cover
the partial grader and independent text oracle, not staging acceptance.

From the repository root, with Python and pytest installed:

```powershell
python -P -m pytest tools/xbench/tests/test_x05.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench
python -P -m pytest tools/xbench/tests/test_x05_oracle.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench
```

The `-P` flag avoids the repository's `platform/` package shadowing the Python
standard library. Disabling the unrelated xonsh plugin avoids its console
requirement. Both suites can run in one process by listing both test paths.
No browser, network, provider credentials, real drawing or installed PDF reader
is required. PDF tests replace pdfplumber with in-memory page objects.

The synthetic report has 24 modules, 12 optimizers, one inverter, two strings
and 9600 W. Its two string rows contain six optimizers each. Tests construct
contradictions by appending a different explicit total, duplicate a string ID,
or substitute invalid numbers. Repeated summaries are generated in memory;
numeric-operation counts, rather than elapsed time, check linear bookkeeping.
Provenance remains limited to five snippets of at most 200 characters per field.

The mocked Studio transport uses a four-panel, two-frame design. Complete
unique associations pass steps 1 and 2. Missing or duplicate associations fail
step 2. Unsupported wire shapes, malformed fixture metadata, source drift,
authentication and unknown server codes remain UNREADABLE. Steps 3 through 7
always remain UNREADABLE in this slice, so it cannot issue a full X05 PASS.
Order permutations must preserve these verdicts.

For a no-network malformed-report and injection reproduction:

```powershell
python -P -m pytest tools/xbench/tests/test_x05.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench -k 'unknown_report_format or server_codes_are_allowlisted or malformed_fixture_metadata'
```

Injection cases use synthetic canaries only. They write `x05-evidence.json` in
pytest's temporary directory and check that no injected text remains. Unknown
codes map to a fixed allowlisted diagnostic, never a product FAIL. Known refusal
codes keep the existing guarded behavior. Blank middle pages, later extraction
exceptions and empty documents produce named unreadable oracle outcomes.

These fixtures do not prove real PDF-layout coverage, Ground setup, accepted
saved strings, report-aware recommendations, reconciliation CSV/UI or reopen
parity. Real engineering files and their evidence stay outside the repository.

The offline comparator accepts independent lists with `string_id` and positive
integer `optimizers` fields. It is not connected to live verdicts. Run its suite:

```powershell
python -P -m pytest tools/xbench/tests/test_x05_reconcile.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench
```

Its equal-total example uses oracle rows A=5 and B=7, versus saved rows A=6 and
B=6. Both total12 optimizers. `compare_strings` returns FAIL with
`string_identity_mismatch` and two mismatches: A oracle5/saved6 and B
oracle7/saved6. Missing and foreign IDs are listed separately; duplicate IDs
make the input UNREADABLE. Permuting rows leaves the evidence unchanged.
Aggregate equality therefore cannot establish either string identity or each
string's optimizer count. This construction uses synthetic Python dictionaries
only and needs no staging access, credentials or customer input.

The same focused comparator command also checks these pure input contracts:

- `compare_recommendations(report_grid_count, recommendations)` takes an
  independent positive integer report count and shown rows with `group_count`.
  Every shown count must match. An empty list requires explicit
  `no_recommendations_shown=True`; missing absence evidence or invalid counts
  are UNREADABLE. A nonempty list conflicts with an assertion of no shown rows.
- `compare_table(rows, saved_values, oracle_values)` requires exactly five
  unique metrics: `panels`, `optimizers`, `inverters`, `strings`, `kw_dc`.
  Each row has `metric`, `studio`, `oracle`, `delta`, `verdict`. Values must
  equal the independent maps, delta is saved minus oracle, and verdict is
  PASS for zero or FAIL for nonzero. Table integrity is separate from policy.
- `check_discrepancies(fixture_id, deltas, records, run_started_at)` takes all
  five signed deltas. Every nonzero row needs one matching record with
  `fixture_id`, `metric`, exact `delta`, nonempty `name` and an aware ISO
  `frozen_at` strictly before the aware ISO run timestamp. This checker tests
  supplied policy evidence and cannot create or authenticate an approval.
- `compare_snapshots(before, reopened)` requires `strings_sha256` and
  `table_sha256` in both inputs. Synthetic tests hash string assignments and
  table rows, then detect changes with equal aggregate totals. The comparator
  checks supplied hashes; it cannot prove their producer or provenance.
- `compare_csv_view(csv_text, view_rows)` accepts a leading Unicode BOM,
  quoted cells and reordered columns or rows. It rejects duplicate headers
  and missing or duplicate metrics, then compares values, deltas and verdicts.
  Agreement between CSV and view does not establish independent correctness.

Generated500- and2000-string fixtures count dictionary reads to bound indexing
work without timing assertions. They reverse input order, preserve explicit
missing/foreign/count differences and verify that caller rows remain unchanged.
All these helpers remain offline and are not connected to the live X05 grader.

`reconcile_offline(evidence)` composes strings, grouping, independent table,
discrepancy policy, reopen and CSV/view checks. Each stage needs its supplied
input fields. A missing stage makes the aggregate UNREADABLE even when another
stage FAILs. Policy deltas come from saved minus oracle values. They cannot be
overridden with a separate claimed delta map.

`canonical_snapshot(strings, table_rows)` rejects invalid rows before hashing.
It sorts string IDs and table metrics, normalizes decimal spelling and retains
each string's optimizer assignment. Input row order does not change its hashes.
CSV parsing accepts at most five data rows,256 characters per cell and16384
characters in the input. Excess input is named UNREADABLE. Timestamp checks
normalize aware instants to UTC, reject unrepresentable boundary offsets and
require a strict pre-run freeze. Numeric validation uses its own Decimal context.

Run this PowerShell example from the repository root. It uses in-memory data
only and prints `PASS` then `FAIL`:

```powershell
@'
import csv
import io
import sys
sys.path.insert(0, "tools/xbench")
from benches.x05_reconcile import METRICS, TABLE_COLUMNS, compare_csv_view, compare_table
oracle = dict.fromkeys(METRICS, 0)
saved = dict(oracle, panels=1)
view = [dict(metric=k, studio=0, oracle=0, delta=0, verdict="PASS") for k in METRICS]
buffer = io.StringIO(newline="")
writer = csv.DictWriter(buffer, fieldnames=TABLE_COLUMNS)
writer.writeheader()
writer.writerows(view)
print(compare_csv_view(buffer.getvalue(), view)["verdict"])
print(compare_table(view, saved, oracle)["verdict"])
'@ | python -P
```

CSV and view repeat the same zero-panel claim, so they agree. The independent
saved-design input has one panel, so table integrity fails for its Studio value,
delta and verdict. Agreement between two exports is not saved-design proof.

The composed string stage also checks saved string count and optimizer sum
against `saved_values`. Reopen evidence includes `before_rows` and
`reopened_rows`, each containing `strings` and `table_rows`. Claimed hashes must
equal those rows' canonical hashes; two equal arbitrary hash claims cannot pass.
String differences report exact `counts` for missing, foreign and mismatched
IDs, with at most20 deterministic examples in each diagnostic list. The CSV
helper serializes its own parsers and restores the caller's global field limit
after success, early refusal and parser errors.

This executable six-stage walkthrough prints `PASS`, `FAIL`, `UNREADABLE`:

```powershell
@'
from copy import deepcopy
import csv
import io
import sys
sys.path.insert(0, "tools/xbench")
from benches.x05_reconcile import TABLE_COLUMNS, canonical_snapshot, reconcile_offline
strings = [{"string_id": "A", "optimizers": 5}, {"string_id": "B", "optimizers": 7}]
values = dict(panels=24, optimizers=12, inverters=1, strings=2, kw_dc="9.6")
view = [dict(metric=k, studio=v, oracle=v, delta=0, verdict="PASS") for k, v in values.items()]
buffer = io.StringIO(newline="")
writer = csv.DictWriter(buffer, fieldnames=TABLE_COLUMNS)
writer.writeheader()
writer.writerows(view)
snapshot_rows = dict(strings=deepcopy(strings), table_rows=deepcopy(view))
hashes = canonical_snapshot(strings, view)
evidence = dict(oracle_strings=deepcopy(strings), saved_strings=deepcopy(strings),
    saved_values=dict(values), oracle_values=dict(values), view_rows=view,
    report_grid_count=2, recommendations=[], no_recommendations_shown=True,
    fixture_id="synthetic", discrepancy_records=[], run_started_at="2026-11-15T13:00:00Z",
    before=dict(hashes), reopened=dict(hashes), before_rows=deepcopy(snapshot_rows),
    reopened_rows=deepcopy(snapshot_rows), csv_text=buffer.getvalue())
print(reconcile_offline(evidence)["verdict"])
evidence["recommendations"] = [{"group_count": 1}]
del evidence["no_recommendations_shown"]
print(reconcile_offline(evidence)["verdict"])
del evidence["reopened_rows"]
print(reconcile_offline(evidence)["verdict"])
'@ | python -P
```

The first run supplies all six stages and their cross-stage bindings. The
second contains a readable grouping mismatch. The third preserves that FAIL
inside its grouping stage but makes the aggregate UNREADABLE because reopen
evidence is missing. All inputs remain local synthetic Python objects.
