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

`aggregate_fixtures(expected_ids, fixtures)` requires exactly three explicitly
expected IDs and one bound `{fixture_id, evidence}` record for each. It composes
the six-stage offline checks, rejects missing/duplicate/foreign IDs and gives
UNREADABLE precedence. `reconcile_json(text)` first rejects duplicate JSON keys,
nonfinite constants and malformed JSON. Valid JSON decimals retain exact values.
Neither API establishes live provenance or changes the live grader.

The next independent PowerShell replay uses a synthetic frozen discrepancy.
It prints `1 FAIL PASS PASS`, then `1 FAIL FAIL FAIL`. These fields are the raw
panel delta, row verdict, policy verdict and composed offline verdict:

```powershell
@'
from copy import deepcopy
import csv
import io
import sys
sys.path.insert(0, "tools/xbench")
from benches.x05_reconcile import TABLE_COLUMNS, canonical_snapshot, reconcile_offline
strings = [{"string_id": "A", "optimizers": 5}, {"string_id": "B", "optimizers": 7}]
saved = dict(panels=25, optimizers=12, inverters=1, strings=2, kw_dc="9.6")
oracle = dict(saved, panels=24)
rows = [dict(metric=k, studio=v, oracle=oracle[k], delta=1 if k == "panels" else 0,
    verdict="FAIL" if k == "panels" else "PASS") for k, v in saved.items()]
buffer = io.StringIO(newline="")
writer = csv.DictWriter(buffer, fieldnames=TABLE_COLUMNS)
writer.writeheader()
writer.writerows(rows)
hashes = canonical_snapshot(strings, rows)
snapshot_rows = dict(strings=deepcopy(strings), table_rows=deepcopy(rows))
record = dict(fixture_id="synthetic", metric="panels", delta=1,
    name="synthetic panel revision", frozen_at="2026-11-15T12:00:00Z")
evidence = dict(oracle_strings=deepcopy(strings), saved_strings=deepcopy(strings),
    saved_values=saved, oracle_values=oracle, view_rows=rows, csv_text=buffer.getvalue(),
    report_grid_count=2, recommendations=[], no_recommendations_shown=True,
    fixture_id="synthetic", discrepancy_records=[record], run_started_at="2026-11-15T13:00:00Z",
    before=dict(hashes), reopened=dict(hashes), before_rows=deepcopy(snapshot_rows),
    reopened_rows=deepcopy(snapshot_rows))
for frozen_at in ("2026-11-15T12:00:00Z", "2026-11-15T14:00:00Z"):
    record["frozen_at"] = frozen_at
    result = reconcile_offline(evidence)
    print(rows[0]["delta"], rows[0]["verdict"], result["stages"]["policy"]["verdict"], result["verdict"])
'@ | python -P
```

The accepted synthetic policy leaves the truthful nonzero delta and FAIL row
visible. Its composed offline PASS is not zero-delta proof or product acceptance.
The late record fails policy and composition. This example approves no real
fixture discrepancy; none is pre-approved in the live X05 manifest. Stop on an
unexpected output. The replay creates no files, network calls or retained state.

The strict JSON envelope entry point is `aggregate_fixtures_json(text)` with
exact keys `expected_ids` and `fixtures`. Input is bounded to262144 characters
and32 container levels; quoted brackets do not count as containers.
`serialize_result` emits finite Decimal values as exact JSON numbers, including
integral Decimal exponent markers, without float conversion. It is a result
transport contract, separate from canonical snapshot serialization.

Offline CLI, from the repository root:

```powershell
# Pipe one complete synthetic evidence object to the single-fixture command:
python -P tools/xbench/benches/x05_reconcile.py
# Or pipe one explicit three-fixture envelope:
python -P tools/xbench/benches/x05_reconcile.py --aggregate
```

The CLI reads stdin, emits one JSON line and exits0 for offline PASS,1 for FAIL
or4 for UNREADABLE. An output encoding or broken-pipe failure exits5 without
retrying output or printing a traceback. Close stdin after the object. Malformed
input emits fixed reason codes, never the input. No live connection is made.
Both limits apply to UTF-8 input:262144 characters and1048576 bytes. JSON framing
can make the character limit reject a payload already at the byte limit.
Raw integer tokens are bounded to80 characters; decimal tokens to128 characters
and80-character plain numeric width. Unsupported numeric tokens return
`json_numeric_unreadable` independently of the caller's Decimal traps.
`--help` exits0 with usage on stdout and no comparison. Unknown options exit2
with usage on stderr and no comparison. Both paths ignore synthetic stdin;
they emit no reconciliation JSON. A stdin read error exits4 with the fixed
`cli_evidence_unreadable` result. Escaped Unicode IDs retain exact identity,
including lone surrogate code units; result JSON and snapshot bytes use ASCII
escapes. Neither whitespace trimming nor Unicode normalization changes IDs.

### Frozen acceptance coverage

Parent: Studio X-bench X05 engineering-set reconciliation. A helper PASS proves
only the supplied synthetic evidence agrees. Bench acceptance requires all
seven frozen steps on all three real fixtures at one staging source.

| Frozen step | Existing helper or live slice | Synthetic proof limit | Evidence still needed for live grading |
|---|---|---|---|
| 1. Normal-user DWG and report import | `x05.py` project lifecycle and PDF transport | Mocked transport proves request/cleanup behavior | Readable project-scoped PDF completion after S02 diagnostics are live; supported Ground settings |
| 2. Every report grid matched once or named unmatched | `x05.py` nested grid-report checks | Schema/count/identity validation cannot prove producer geometry | Actual reports for all three fixtures, independent missing grid binding, and required all-matched Copenhagen/USML results |
| 3. Every string and optimizer count equals oracle | `compare_strings`, saved/oracle total cross-checks | Supplied IDs/counts agree; origin is not established | Independent complete PDF string tables and actual saved/drawn Studio string reads |
| 4. Every shown grouping recommendation respects grid count | `compare_recommendations` | Supplied shown counts agree; explicit empty evidence is required | Capture every recommendation actually shown and independent report grid counts |
| 5. Five-row CSV and view bind to saved design and oracle | `compare_table`, `compare_csv_view` | Synthetic saved values, table and CSV agree | Product table/export, independent saved design reads and complete BOM/report values, including missing DC power |
| 6. Zero deltas or prior frozen named discrepancies | `check_discrepancies`, composed policy stage | Timestamped supplied records match supplied deltas | Real row deltas and manifest frozen before run; none is pre-approved today |
| 7. Save/reopen preserves strings and table | `canonical_snapshot`, `compare_bound_snapshots` | Supplied snapshots hash equally and bind to supplied before rows | Actual save/reopen reads for imported strings and product table at the same project/version |

`aggregate_fixtures` requires an explicit three-fixture set and preserves
UNREADABLE precedence. It does not acquire any missing live evidence. The live
grader covers only the current partial import/grid slice; steps3 through7 and
unsupported setup remain UNREADABLE, so offline helper PASS cannot accept X05.

`validate_fixture_provenance(frozen_manifest, stage_bindings)` is a separate pure
comparison. The frozen manifest has exactly `fixture_id` and `inputs`. Inputs
contain lowercase64-character SHA-256 strings for `dwg` and `report`, with an
optional `bom`. Each of `strings`, `grouping`, `table`, `policy`, `reopen` and
`csv_view` must supply the same complete binding. Missing/malformed evidence or
conflicting identity/digests returns a named UNREADABLE result. PASS means only
that these supplied claims agree. The caller must independently establish the
manifest's frozen origin and hash actual input bytes; this helper does neither
and is not wired into live grading or used to infer provenance from the product.

`reconcile_with_provenance(evidence, frozen_manifest, stage_bindings)` preserves
all six checks under `reconciliation`, adds `provenance`, and binds
`evidence.fixture_id` to the supplied manifest. UNREADABLE takes precedence over
FAIL. This entry point is separate from the existing JSON CLI.
`verify_input_bytes(manifest, inputs)` hashes an exact dictionary of supplied
`bytes` for the manifest roles. Missing/wrong-type bytes or unequal digests are
UNREADABLE. It opens no file and makes no network request. Matching actual bytes
still cannot authenticate the supplied manifest's frozen origin.

`validate_saved_design_identity(expected, stages)` separately compares explicit
`project_id` and `version_id` strings across `saved_strings`, `table`, `csv` and
`reopened`. Each stage must carry both exact identifiers; equal totals cannot
replace them. This validates supplied claims, not the producer of those claims.

Executable synthetic provenance replay from the repository root:

```powershell
@'
import sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import (
    PROVENANCE_STAGES, validate_fixture_provenance, reconcile_with_provenance)
manifest = dict(fixture_id='synthetic', inputs=dict(dwg='a'*64, report='b'*64))
bindings = {stage: deepcopy(manifest) for stage in PROVENANCE_STAGES}
matching = validate_fixture_provenance(manifest, bindings)
print(matching['verdict'], matching['reason'])
swapped = deepcopy(bindings)
swapped['strings']['inputs']['report'] = 'c'*64
conflict = validate_fixture_provenance(manifest, swapped)
print(conflict['verdict'], conflict['reason'], conflict['stage'])
foreign = reconcile_with_provenance(dict(fixture_id='foreign'), manifest, bindings)
print(foreign['verdict'], foreign['provenance']['reason'])
assert matching['verdict'] == 'PASS'
assert conflict == dict(verdict='UNREADABLE', reason='stage_binding_conflict', stage='strings')
assert foreign['provenance']['reason'] == 'evidence_fixture_identity_conflict'
'@ | python -P
```

Exact stdout:

```text
PASS supplied_provenance_equal
UNREADABLE stage_binding_conflict strings
UNREADABLE evidence_fixture_identity_conflict
```

The foreign example deliberately omits all six stage inputs; their diagnostics
remain available under `reconciliation`. This replay creates no files or live
state. Matching synthetic hash claims prove agreement only, never frozen origin
or X05 bench acceptance.

`reconcile_assured` retains provenance/reconciliation, input-byte and design
identity results under `checks`, with UNREADABLE before FAIL before PASS.
`aggregate_provenance(expected_ids, fixtures)` requires exactly three explicit
IDs; each record supplies `fixture_id`, `evidence`, `manifest` and `bindings`.
It retains readable siblings when one fixture's provenance is unreadable.
`reconcile_provenance_json(text)` requires exactly `evidence`, `manifest` and
`bindings`, using the existing bounded, duplicate-key-rejecting decoder. These
are pure entry points, not additional CLI modes or live grading integration.

Executable byte and identity replay from the repository root:

```powershell
@'
import sys
from hashlib import sha256
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import verify_input_bytes, validate_saved_design_identity, IDENTITY_STAGES
inputs = dict(dwg=b'synthetic drawing', report=b'synthetic report')
manifest = dict(fixture_id='synthetic', inputs={key: sha256(value).hexdigest() for key, value in inputs.items()})
identity = dict(project_id='project-a', version_id='version-a')
stages = {key: dict(identity) for key in IDENTITY_STAGES}
matching = verify_input_bytes(manifest, inputs)
same_version = validate_saved_design_identity(identity, stages)
print(matching['verdict'], same_version['verdict'])
swapped = dict(inputs, report=b'substituted report')
changed_bytes = verify_input_bytes(manifest, swapped)
print(changed_bytes['verdict'], changed_bytes['reason'], ','.join(changed_bytes['roles']))
# Equal totals cannot establish project/version identity.
totals_before = dict(panels=24, strings=2)
totals_after = dict(totals_before)
mixed = deepcopy(stages)
mixed['reopened']['version_id'] = 'version-b'
changed_version = validate_saved_design_identity(identity, mixed)
assert totals_before == totals_after
print(changed_version['verdict'], changed_version['reason'], changed_version['stage'])
assert matching['verdict'] == same_version['verdict'] == 'PASS'
assert changed_bytes['roles'] == ['report']
assert changed_version['reason'] == 'design_identity_conflict'
'@ | python -P
```

Exact stdout:

```text
PASS PASS
UNREADABLE input_digest_conflict report
UNREADABLE design_identity_conflict reopened
```

This replay reads no file and makes no network request. The bytes and identities
are synthetic supplied values. Matching them does not authenticate frozen
origin, producer custody or live acceptance.

Strict transport additions: `reconcile_assurance_json(text)` accepts exactly
`evidence`, `manifest`, `bindings`, `inputs_hex`, `identity`, `stage_identities`.
Each `inputs_hex` value is an even-length hexadecimal string with no whitespace;
empty bytes are encoded as an empty string. The existing JSON bounds apply.
`aggregate_provenance_json(text)` accepts exactly `expected_ids` and `fixtures`.
The CLI now exposes `--provenance` for the existing single-fixture provenance
envelope. It is mutually exclusive with `--aggregate`, with the same0/1/4 result
exits and5 output-failure exit. Assurance and provenance aggregation remain pure
Python entry points, not CLI modes. None provides authenticated origin.

Complete synthetic assurance replay from the repository root (requires pytest,
because it reuses the checked synthetic fixture builder from the focused tests):

```powershell
@'
import sys, runpy, json
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_assurance_json
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['assurance_envelope']()
passing = reconcile_assurance_json(json.dumps(envelope))
print(passing['verdict'])
changed = deepcopy(envelope)
changed['inputs_hex']['report'] = b'substituted'.hex()
changed['stage_identities']['reopened']['version_id'] = 'foreign'
changed['evidence']['recommendations'][0]['group_count'] = 1
failed = reconcile_assurance_json(json.dumps(changed))
checks = failed['checks']
print(failed['verdict'], checks['input_bytes']['reason'], checks['design_identity']['reason'])
grouping = checks['provenance']['reconciliation']['stages']['grouping']
print(grouping['verdict'], grouping['reason'])
assert passing['verdict'] == 'PASS'
assert failed['verdict'] == 'UNREADABLE'
assert checks['input_bytes']['reason'] == 'input_digest_conflict'
assert checks['design_identity']['reason'] == 'design_identity_conflict'
assert grouping['reason'] == 'grouping_count_mismatch'
'@ | python -P
```

Exact stdout:

```text
PASS
UNREADABLE input_digest_conflict design_identity_conflict
FAIL grouping_count_mismatch
```

The replay reads only source code for its synthetic builder, no customer input.
Helpers make no file or network reads. PASS establishes consistency of supplied
claims and bytes, never their authenticated origin or live X05 acceptance.

Canonical snapshot compatibility contract:

- Hash UTF-8 bytes of compact JSON arrays with ASCII escapes and no spaces.
- String rows are `[exact_id, integer_optimizer_count]`, sorted by exact ID.
  IDs have no Unicode normalization or case folding.
- Table rows follow `panels`, `optimizers`, `inverters`, `strings`, `kw_dc`.
  Each row is `[metric, studio_text, oracle_text, delta_text, verdict]`.
  Numeric text uses normalized plain Decimal spelling; every signed zero is `0`.
- Literal reference digests are pinned in `test_canonical_snapshot_literal_compatibility_vectors`.
  Unicode rows use `711f41979914629b8d9d0a4b9996cd97fb805d667533298f98f5c89c0368e70d`;
  the synthetic five-row table uses `5694f6c3f6fb0736fdb05ddae835185570106f7fe0c57543a4312af71aa27dec`.
  Empty collections and empty IDs are UNREADABLE and acquire no valid hash.
  The reference hash of `[]` is pinned only to prevent absence being mistaken
  for accepted snapshot evidence. Format changes require explicit vector review.

This independent three-fixture replay prints `PASS`, `FAIL`, `UNREADABLE`, then
`UNREADABLE foreign_fixture_id`:

```powershell
@'
from copy import deepcopy
import csv
import io
import sys
sys.path.insert(0, "tools/xbench")
from benches.x05_reconcile import TABLE_COLUMNS, aggregate_fixtures_json, canonical_snapshot, serialize_result
def fixture(identifier):
    strings = [{"string_id": "A", "optimizers": 5}, {"string_id": "B", "optimizers": 7}]
    values = dict(panels=24, optimizers=12, inverters=1, strings=2, kw_dc="9.6")
    rows = [dict(metric=k, studio=v, oracle=v, delta=0, verdict="PASS") for k, v in values.items()]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TABLE_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    hashes = canonical_snapshot(strings, rows)
    snapshot_rows = dict(strings=deepcopy(strings), table_rows=deepcopy(rows))
    evidence = dict(oracle_strings=deepcopy(strings), saved_strings=deepcopy(strings),
        saved_values=dict(values), oracle_values=dict(values), view_rows=rows, csv_text=buffer.getvalue(),
        report_grid_count=2, recommendations=[], no_recommendations_shown=True,
        fixture_id=identifier, discrepancy_records=[], run_started_at="2026-11-15T13:00:00Z",
        before=dict(hashes), reopened=dict(hashes), before_rows=deepcopy(snapshot_rows),
        reopened_rows=deepcopy(snapshot_rows))
    return dict(fixture_id=identifier, evidence=evidence)
expected = ["case-a", "case-b", "case-c"]
records = [fixture(identifier) for identifier in expected]
envelope = dict(expected_ids=expected, fixtures=records)
print(aggregate_fixtures_json(serialize_result(envelope))["verdict"])
records[0]["evidence"]["recommendations"] = [{"group_count": 1}]
records[0]["evidence"]["no_recommendations_shown"] = False
print(aggregate_fixtures_json(serialize_result(envelope))["verdict"])
del records[1]["evidence"]["reopened"]
print(aggregate_fixtures_json(serialize_result(envelope))["verdict"])
records[2]["fixture_id"] = "foreign"
result = aggregate_fixtures_json(serialize_result(envelope))
print(result["verdict"], result["reason"])
'@ | python -P
```

The failed grouping remains visible inside the missing-stage aggregate. The
foreign-ID case rejects coverage before aggregation. Stop on an unexpected
output. All objects are synthetic and this replay creates no persistent state.

Complete single-fixture JSON schema (all rows below are required):

| Field | Consumers | Value |
|---|---|---|
| `oracle_strings` | Strings | Nonempty independent `{string_id, optimizers}` rows |
| `saved_strings` | Strings, reopen | Nonempty saved-design string rows |
| `oracle_values` | Strings, table, policy | Independent map of all five metrics |
| `saved_values` | Strings, table, policy | Saved-design map of all five metrics |
| `view_rows` | Table, CSV/view, reopen | Exactly five unique full table rows |
| `csv_text` | CSV/view | Bounded CSV text containing those five rows |
| `report_grid_count` | Grouping | Independent positive integer grid count |
| `recommendations` | Grouping | Shown rows with positive integer `group_count` |
| `fixture_id` | Policy, aggregate identity | Nonempty fixture identifier |
| `discrepancy_records` | Policy | Array of named frozen fixture/metric/delta records |
| `run_started_at` | Policy | Aware ISO timestamp |
| `before` | Reopen | Claimed `strings_sha256` and `table_sha256` |
| `reopened` | Reopen | Claimed reopened hashes with the same keys |
| `before_rows` | Reopen | `{strings, table_rows}` bound to saved evidence |
| `reopened_rows` | Reopen | Reopened `{strings, table_rows}` bound to reopened hashes |

The optional `no_recommendations_shown` is a strict boolean. Omission defaults
to false for a nonempty shown list. An explicitly empty list needs true;
null, numeric, string and container flags are UNREADABLE. No field is inferred
from a similarly named field. Omitted required fields produce
`stage_evidence_missing` for each consuming stage. Explicit null is present but
invalid, so its consumer returns a named type/evidence error instead. Empty
string arrays or snapshot objects remain UNREADABLE. Empty discrepancy records
are valid evidence of no exceptions; policy then FAILs any nonzero delta.

String rows use exact nonempty IDs and positive integer optimizer counts. Metric
maps require `panels`, `optimizers`, `inverters`, `strings`, `kw_dc`; count values
are integral and nonnegative. Table rows require `metric`, `studio`, `oracle`,
`delta`, `verdict`, with PASS for zero and FAIL for nonzero. Discrepancy records
use `fixture_id`, `metric`, exact signed `delta`, nonempty `name`, and aware
`frozen_at` strictly before the run. Extra top-level fields do not substitute
for required ones. The aggregate envelope itself accepts only `expected_ids`
and `fixtures`, with each fixture record binding its ID to `evidence.fixture_id`.

The CLI decodes stdin bytes as strict UTF-8 before parsing. Invalid bytes or
truncated multibyte sequences emit one fixed UNREADABLE result and exit4 without
input text or a traceback. Cyclic result containers fail with the bounded
`json_result_too_deep` ValueError; repeated acyclic children serialize normally.

This minimal typo replay prints `UNREADABLE stage_evidence_missing`:

```powershell
@'
{"saved_strngs":[]}
'@ | python -P tools/xbench/benches/x05_reconcile.py | python -c "import json,sys; r=json.load(sys.stdin); print(r['verdict'], r['stages']['strings']['reason'])"
```

It deliberately supplies a small invalid object, not complete evidence. The
required-field deletion matrix tests the same omission rule against otherwise
complete evidence and preserves every unaffected stage's diagnostics.
