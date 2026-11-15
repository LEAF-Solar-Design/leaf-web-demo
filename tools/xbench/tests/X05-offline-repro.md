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
  Every shown count must match. An empty shown list passes that condition;
  missing or invalid counts are UNREADABLE.
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
