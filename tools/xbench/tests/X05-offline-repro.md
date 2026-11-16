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

Additional offline CLI modes are `--assurance` for the complete assurance
envelope and `--provenance-aggregate` for three provenance records. All four
explicit modes are mutually exclusive. Each emits one sanitized JSON result
with exit0 PASS,1 FAIL,4 UNREADABLE or5 output failure. Invalid combinations
exit2 on stderr without a reconciliation result. Differential synthetic tests
compare direct provenance JSON diagnostics with its CLI output for missing
fields and readable mismatches.

`aggregate_assurance(expected_ids, fixtures)` and
`aggregate_assurance_json(text)` compose three complete assurance records.
Each record adds `fixture_id` to the single-assurance envelope; the JSON outer
envelope has exactly `expected_ids` and `fixtures`. Coverage errors remain
UNREADABLE. Malformed aggregate records retain an exact `envelope_error_count`
and at most10 index/reason examples, never input text. Unique valid siblings
retain their diagnostics; duplicated IDs are excluded from sibling results.
Missing records are a coverage error, not counted as malformed supplied records.

Each hex role is limited to16384 decoded bytes (32768 hex characters) before
any byte allocation. Exact-limit bytes can pass when their supplied manifest
matches. Excess emits `input_hex_too_large`. JSON size limits also apply. These
bounds are transport limits for synthetic offline evidence, not live upload
limits or a claim about real customer files.

`--assurance-aggregate` exposes the existing strict three-fixture assurance JSON
entry point. All five explicit modes are mutually exclusive, with the same
0/1/4/5 verdict/output exits and2 for usage errors. Missing BOM is supported when
manifest roles, supplied bytes and every stage binding all omit it. Asymmetric
BOM claims remain UNREADABLE. Invalid UTF-8 and read failures return a single
sanitized `cli_evidence_unreadable` result in every mode.

Executable three-fixture assurance replay (source-only synthetic fixture builder,
requires pytest):

```powershell
@'
import sys, runpy, json
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import aggregate_assurance_json
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
expected, records = fixtures['assurance_records']()
records[1]['evidence']['recommendations'][0]['group_count'] = 1
records[2]['inputs_hex']['report'] = b'substituted'.hex()
result = aggregate_assurance_json(json.dumps(dict(expected_ids=expected, fixtures=records)))
print(result['verdict'])
a, b, c = [result['fixtures'][key] for key in expected]
print(a['verdict'], b['verdict'], c['verdict'])
print(b['checks']['provenance']['reconciliation']['stages']['grouping']['reason'])
print(c['checks']['input_bytes']['reason'])
assert result['verdict'] == 'UNREADABLE'
assert [a['verdict'], b['verdict'], c['verdict']] == ['PASS', 'FAIL', 'UNREADABLE']
'@ | python -P
```

Exact stdout:

```text
UNREADABLE
PASS FAIL UNREADABLE
grouping_count_mismatch
input_digest_conflict
```

The readable PASS sibling remains visible beside both failures. All supplied
bytes are synthetic; no network, customer file or authenticated-origin claim is
involved. Aggregate precedence does not erase sibling diagnostics or accept X05.

Foreign `inputs_hex` roles now reject as `input_hex_roles_unreadable` before any
payload is decoded. Aggregate duplicate tracking counts every occurrence of an
expected fixture ID, including malformed or unbound occurrences. Ambiguous
fixtures cannot appear as PASS; unaffected unique siblings retain their results.

Executable malformed-envelope recovery (synthetic source-only builder, requires
pytest; each comparison receives JSON only through stdin):

```powershell
@'
import sys, runpy, json, subprocess
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
expected, rows = fixtures['assurance_records']()
base = dict(expected_ids=expected, fixtures=rows)
foreign = deepcopy(base)
foreign['fixtures'][0]['inputs_hex']['foreign'] = 'zz'
duplicate = deepcopy(base)
duplicate['fixtures'].insert(0, dict(fixture_id=expected[0], evidence=None))
command = [sys.executable, '-P', 'tools/xbench/benches/x05_reconcile.py', '--assurance-aggregate']
for name, envelope, code in [('foreign', foreign, 4), ('duplicate', duplicate, 4), ('corrected', base, 0)]:
    result = subprocess.run(command, input=json.dumps(envelope).encode(), capture_output=True, timeout=20)
    assert result.returncode == code and not result.stderr
    value = json.loads(result.stdout)
    if name == 'foreign':
        detail = value['fixtures'][expected[0]]['reason']
    elif name == 'duplicate':
        assert expected[0] not in value['fixtures']
        assert value['fixtures'][expected[1]]['verdict'] == 'PASS'
        detail = value['reason']
    else:
        detail = ','.join(value['fixtures'][key]['verdict'] for key in expected)
    print(name, result.returncode, value['verdict'], detail)
'@ | python -P
```

Exact stdout:

```text
foreign 4 UNREADABLE input_hex_roles_unreadable
duplicate 4 UNREADABLE fixture_coverage_unreadable
corrected 0 PASS PASS,PASS,PASS
```

No network or customer input is used. The source-only builder creates synthetic
claims; the successful recovery does not authenticate their origin or accept X05.

`compare_grid_correspondence(expected_grid_ids, saved_group_ids, rows,
require_all_matched=False)` compares explicit independent IDs. Each row has
`grid_id`, `matches` and `cause`. A match requires exactly one supplied saved
group and no cause. An unmatched grid requires `SOLAREDGE_GRID_UNMATCHED` or
`SOLAREDGE_GRID_AMBIGUOUS`; all-matched mode rejects either. Foreign, duplicate,
missing or multiply matched identities FAIL; unreadable evidence is UNREADABLE.
Exact error counts retain at most10 fixed reason examples, without input text.
This helper establishes no geometry or identity provenance.

`reconcile_seven_steps(import_evidence, grid_evidence, assurance)` composes only
supplied offline evidence. Import fields `normal_user`, `dwg_imported` and
`report_imported` must be explicit booleans. Grid evidence supplies all four grid
helper arguments. Assurance supplies the six keyword arguments of
`reconcile_assured`. Seven step diagnostics and all assurance checks survive;
missing evidence gives UNREADABLE precedence. Even a PASS cannot accept X05
without the real staging acquisition required by the frozen bench contract.

Result serialization permits262144 ASCII JSON characters and100000 visited
nodes, with the existing32-container depth bound. Oversized expansion raises
fixed `ValueError('json_result_too_large')` before emission. The CLI returns one
sanitized UNREADABLE result and exit4 when this budget is exceeded; it never
truncates exact counts into an apparently successful partial result. Small
acyclic shared containers remain lossless; compact graphs whose expanded output
is excessive stop at the same budget.

CLI stdin is collected through bounded short reads until EOF or the byte limit,
then decoded as strict UTF-8. Split tokens and multibyte sequences are preserved.
All strict decoders and CLI modes accept legal surrounding JSON whitespace;
leading BOMs, concatenated documents and trailing non-whitespace reject as
`json_malformed`. No framing rejection echoes input text.

`reconcile_seven_steps_json(text)` requires exactly `import`, `grid` and
`assurance`. Assurance uses the existing strict hex-byte envelope. Duplicate
keys, input size/depth and missing envelope fields use the existing named JSON
errors. All seven diagnostic steps survive readable FAIL and UNREADABLE states.
It is a pure Python entry point, not a live grader or CLI mode.

Grid membership uses indexed sets, with generated operation-count tests rather
than timing thresholds. Capped grid reasons are sorted by fixed reason name;
their exact total and first10 examples are invariant under supplied row and ID
order. Exact Unicode identity is preserved, without normalization or trimming.

Executable seven-step replay (synthetic source-only builder, requires pytest):

```powershell
@'
import sys, runpy, json
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = builders['seven_step_envelope']()
result = reconcile_seven_steps_json(json.dumps(envelope))
print(result['verdict'])
unmatched = deepcopy(envelope)
unmatched['grid']['rows'][0].update(matches=[], cause='SOLAREDGE_GRID_UNMATCHED')
unmatched['grid']['require_all_matched'] = False
allowed = reconcile_seven_steps_json(json.dumps(unmatched))
print(allowed['verdict'], allowed['steps']['2']['reason'], unmatched['grid']['rows'][0]['cause'])
unmatched['grid']['require_all_matched'] = True
required = reconcile_seven_steps_json(json.dumps(unmatched))
print(required['verdict'], required['steps']['2']['errors'][0])
missing = dict(envelope, **{'import': None})
uncertain = reconcile_seven_steps_json(json.dumps(missing))
print(uncertain['verdict'], uncertain['steps']['1']['reason'])
assert result['verdict'] == allowed['verdict'] == 'PASS'
assert required['verdict'] == 'FAIL'
assert uncertain['steps']['2']['verdict'] == 'PASS'
'@ | python -P
```

Exact stdout:

```text
PASS
PASS grid_correspondence_equal SOLAREDGE_GRID_UNMATCHED
FAIL required_grid_unmatched
UNREADABLE import_evidence_unreadable
```

The allowed unmatched case demonstrates only the helper's named-cause policy.
Frozen Copenhagen and USML acceptance requires all matched. This replay acquires
no live evidence and cannot authenticate inputs or accept the X05 bench.

Complete seven-step JSON envelope:

| Field | Required shape | Missing, null or empty behavior |
|---|---|---|
| `import` | Exactly `normal_user`, `dwg_imported`, `report_imported`, all bool | Missing outer field rejects envelope; null/empty object yields step1 UNREADABLE; false yields readable FAIL |
| `grid` | `expected_grid_ids`, `saved_group_ids`, `rows`, `require_all_matched`, plus `string_assignments`, `reopened_string_assignments` and independent `optimizer_totals`, plus explicit `panel_counts`, `before_panel_counts`, `reopened_panel_counts` and independent `panel_totals`, plus explicit `dc_power`, `dc_totals`, `before_dc_power`, `reopened_dc_power` and independent `string_totals` | Missing outer field rejects envelope; null/empty object yields step2 UNREADABLE; empty rows with valid nonempty IDs yields missing-grid FAIL |
| `assurance` | Exactly `evidence`, `manifest`, `bindings`, `inputs_hex`, `identity`, `stage_identities` | Missing outer field rejects envelope; null/empty object yields assurance UNREADABLE and absent downstream steps |

Evidence and assurance schemas are the existing six-stage contracts above.
Unknown outer keys reject as `seven_step_envelope_unreadable`. The CLI exposes
`--seven-step` with existing0/1/4/5 result exits, mutually exclusive with all other
explicit modes. `aggregate_seven_steps(expected_ids, fixtures)` is a pure Python
entry point: each of three records has `fixture_id` and `envelope`. The record ID
must equal the supplied assurance evidence ID. Each fixture retains import,
grid, assurance and downstream diagnostics with UNREADABLE precedence.

`validate_import_grid_provenance(manifest, import_binding, grid_binding)` is a
separate pure comparison. Each binding uses exactly the manifest's `fixture_id`
and `inputs` digest map. Malformed claims or substitutions return named
UNREADABLE with stage `import` or `grid`. This does not hash files, authenticate
frozen origin or automatically add those claims to the seven-step schema.

Executable typo-field recovery, using synthetic stdin (source builder requires
pytest):

```powershell
@'
import sys, runpy, json, subprocess
sys.path.insert(0, 'tools/xbench')
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['seven_step_envelope']()
typo = dict(correct)
typo['improt'] = typo.pop('import')
command = [sys.executable, '-P', 'tools/xbench/benches/x05_reconcile.py', '--seven-step']
for name, envelope, expected in [('typo', typo, 4), ('corrected', correct, 0)]:
    run = subprocess.run(command, input=json.dumps(envelope).encode(), capture_output=True, timeout=20)
    assert run.returncode == expected and not run.stderr
    result = json.loads(run.stdout)
    print(name, run.returncode, result['verdict'], result['reason'])
'@ | python -P
```

Exact stdout:

```text
typo 4 UNREADABLE seven_step_envelope_unreadable
corrected 0 PASS offline_seven_steps
```

The correction changes a field name, not evidence custody. Both objects remain
synthetic supplied claims and cannot establish live X05 acceptance.

`aggregate_seven_steps_json(text)` requires exactly `expected_ids` and `fixtures`
and uses the existing bounded strict decoder. Per-fixture records have
`fixture_id` and `envelope`, as in the pure aggregator. Malformed/duplicate
fixture records retain bounded index-only diagnostics and readable unique
siblings. Every occurrence of an ambiguous ID excludes that fixture from PASS.

`reconcile_bound_seven_steps(envelope, manifest, import_binding, grid_binding)`
combines the existing import/grid provenance comparison with seven-step
reconciliation. Its `provenance` and `reconciliation` results remain visible,
with UNREADABLE precedence. The assurance manifest must also equal the supplied
frozen manifest. Agreement checks supplied claims and cannot authenticate origin.
The `--seven-step` transport uses the same strict UTF-8, read-error and output
failure behavior as the existing modes, including exit5 for closed stdout.

Executable three-fixture seven-step replay (synthetic source-only builder,
requires pytest; the pure comparison reads no file and makes no network request):

```powershell
@'
import sys, runpy, json
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import aggregate_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
expected, rows = builders['seven_fixture_records']()
rows[0]['envelope']['import']['dwg_imported'] = False
rows[1]['envelope']['grid'] = None
result = aggregate_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=rows)))
print(result['verdict'])
print(*(result['fixtures'][key]['verdict'] for key in expected))
print(result['fixtures'][expected[0]]['steps']['1']['reason'])
print(result['fixtures'][expected[1]]['steps']['2']['reason'])
assert result['verdict'] == 'UNREADABLE'
expected, rows = builders['seven_fixture_records']()
corrected = aggregate_seven_steps_json(json.dumps(dict(expected_ids=expected, fixtures=rows)))
print(corrected['verdict'], *(corrected['fixtures'][key]['verdict'] for key in expected))
assert corrected['verdict'] == 'PASS'
'@ | python -P
```

Exact stdout:

```text
UNREADABLE
FAIL UNREADABLE PASS
supplied_import_evidence
grid_evidence_unreadable
PASS PASS PASS PASS
```

The correction preserves independent sibling diagnostics and demonstrates the
aggregate precedence for supplied synthetic evidence. Live acquisition and
frozen-origin authentication remain required by the X05 acceptance contract.

`--seven-step-aggregate` exposes the existing aggregate JSON helper, with
0/1/4/5 result/output exits and mutual exclusion with every other mode.
`reconcile_bound_seven_steps_json(text)` requires exactly `envelope`, `manifest`,
`import_binding` and `grid_binding`. It uses the existing bounded strict decoder.
`aggregate_bound_seven_steps(expected_ids, fixtures)` expects three records with
`fixture_id` and those four fields. Swapped import/grid claims become UNREADABLE
while each fixture's seven-step diagnostics and readable siblings remain visible.

Output-budget tests inject a controlled synthetic aggregate result into the CLI
producer. Each fixture fits alone, but their combined serialized result exceeds
262144 characters. The CLI emits only the fixed `json_result_too_large` result
and exit4, retaining the original counts in caller-owned data. This exercises
the emission boundary without claiming a live producer generated that shape.

Executable bound seven-step replay (synthetic source-only builder, requires
pytest):

```powershell
@'
import sys, runpy, json
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_bound_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
base = builders['bound_seven_envelope']()
print(reconcile_bound_seven_steps_json(json.dumps(base))['verdict'])
for stage in ['import', 'grid']:
    changed = deepcopy(base)
    changed[stage + '_binding']['inputs']['report'] = 'd'*64
    result = reconcile_bound_seven_steps_json(json.dumps(changed))
    print(stage, result['verdict'], result['provenance']['reason'], result['reconciliation']['verdict'])
changed = deepcopy(base)
changed['import_binding']['inputs']['report'] = 'd'*64
changed['envelope']['assurance']['inputs_hex']['report'] = b'swapped'.hex()
changed['envelope']['import']['normal_user'] = False
result = reconcile_bound_seven_steps_json(json.dumps(changed))
print(result['verdict'], result['reconciliation']['assurance']['checks']['input_bytes']['reason'],
      result['reconciliation']['steps']['1']['verdict'])
print('corrected', reconcile_bound_seven_steps_json(json.dumps(base))['verdict'])
'@ | python -P
```

Exact stdout:

```text
PASS
import UNREADABLE import_grid_binding_conflict PASS
grid UNREADABLE import_grid_binding_conflict PASS
UNREADABLE input_digest_conflict FAIL
corrected PASS
```

The supplied manifest and bindings are synthetic claims. Agreement validates
their consistency while frozen-origin authentication and live acquisition are
established by the separate bench acceptance process.

`aggregate_bound_seven_steps_json(text)` accepts exactly `expected_ids` and
`fixtures` through the existing bounded duplicate-key-rejecting decoder.
`--seven-step-bound` exposes the strict single bound envelope with0/1/4/5 exits
and mutual exclusion with every other mode. Invalid UTF-8, read/write/flush
failures and closed pipes follow the documented sanitized transport behavior.
Missing bound fields reject the schema while retaining available seven-step
reconciliation diagnostics. Direct and strict JSON helpers preserve those
diagnostics across malformed bindings, foreign roles and optional-BOM asymmetry.

Executable bound three-fixture replay (synthetic source-only builder, requires
pytest):

```powershell
@'
import sys, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import aggregate_bound_seven_steps
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
expected, rows = builders['bound_seven_fixture_records']()
correct = deepcopy(rows)
rows[0]['import_binding'] = deepcopy(correct[1]['import_binding'])
rows[1]['grid_binding'] = deepcopy(correct[0]['grid_binding'])
result = aggregate_bound_seven_steps(expected, rows)
print(result['verdict'])
for key in expected[:2]:
    fixture = result['fixtures'][key]
    print(fixture['verdict'], fixture['provenance']['stage'], fixture['reconciliation']['verdict'])
print('sibling', result['fixtures'][expected[2]]['verdict'])
fixed = aggregate_bound_seven_steps(expected, correct)
print('corrected', fixed['verdict'])
assert result['verdict'] == 'UNREADABLE' and fixed['verdict'] == 'PASS'
'@ | python -P
```

Exact stdout:

```text
UNREADABLE
UNREADABLE import PASS
UNREADABLE grid PASS
sibling PASS
corrected PASS
```

The swaps preserve equal totals but change supplied fixture provenance. The
helper checks those claims against the supplied manifest. The replay establishes
consistency and rejection behavior, with frozen-origin authentication and live
acquisition supplied separately by the bench acceptance process.

`--seven-step-bound-aggregate` exposes `aggregate_bound_seven_steps_json` with
the documented verdict/output exits and mutual exclusion with every other mode.
The strict outer JSON object has exactly `expected_ids` and `fixtures`.
Expected IDs contain exactly three unique nonblank strings of at most128
characters. Each fixture record supplies these fields:

| Field | Meaning |
|---|---|
| `fixture_id` | Exact expected identity, also bound to assurance evidence |
| `envelope` | Complete import/grid/assurance seven-step object |
| `manifest` | Supplied frozen fixture ID and input digest map |
| `import_binding` | Supplied import fixture/input binding |
| `grid_binding` | Supplied grid fixture/input binding |

Valid unique fixtures run seven-step reconciliation once each. Coverage errors
preserve named uncertainty, ambiguous IDs stay excluded, and readable unique
siblings keep their bound diagnostics. Exact serializer tests exercise100000
visited nodes successfully and reject100001 below the independent character
and depth budgets, preserving caller data.

Executable stdin-only strict-decoder replay (synthetic source-only builder,
requires pytest):

```powershell
@'
import sys, runpy, json, subprocess
sys.path.insert(0, 'tools/xbench')
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
expected, rows = builders['bound_seven_fixture_records']()
rows[0]['envelope']['import']['normal_user'] = False
rows[1]['grid_binding'] = None
mixed = json.dumps(dict(expected_ids=expected, fixtures=rows))
expected, rows = builders['bound_seven_fixture_records']()
correct = json.dumps(dict(expected_ids=expected, fixtures=rows))
program = ("import sys;sys.path.insert(0,'tools/xbench');from benches.x05_reconcile import "
           "aggregate_bound_seven_steps_json,serialize_result;"
           "r=aggregate_bound_seven_steps_json(sys.stdin.read());print(serialize_result(r));"
           "sys.exit({'PASS':0,'FAIL':1,'UNREADABLE':4}[r['verdict']])")
cases = [('duplicate', '{"fixtures":[],"fixtures":[]}', 4), ('mixed', mixed, 4), ('corrected', correct, 0)]
for name, text, expected_exit in cases:
    run = subprocess.run([sys.executable, '-P', '-c', program], input=text.encode(), capture_output=True, timeout=20)
    assert run.returncode == expected_exit and not run.stderr
    result = json.loads(run.stdout)
    detail = result['reason'] if name == 'duplicate' else ','.join(result['fixtures'][key]['verdict'] for key in expected)
    print(name, run.returncode, result['verdict'], detail)
'@ | python -P
```

Exact stdout:

```text
duplicate 4 UNREADABLE json_duplicate_key
mixed 4 UNREADABLE FAIL,UNREADABLE,PASS
corrected 0 PASS PASS,PASS,PASS
```

The decoder reads each synthetic envelope only from stdin. The parent process
loads source fixtures. The supplied claims retain the origin and live-acquisition
limits stated in the X05 acceptance contract.

`diff_snapshot_rows(before, reopened)` accepts exactly `strings` and `table_rows`
in each snapshot. It localizes string-ID/optimizer-count changes with exact
added/removed/changed totals and at most20 sorted examples, plus changed table
metrics in fixed metric order. It uses supplied rows and preserves caller data.
It establishes no panel geometry or provenance beyond those row identities.

The discrepancy policy already rejects duplicate records for one fixture and
metric as `duplicate_discrepancy_record`. New permutation tests cover conflicting
deltas, names and freeze timestamps while preserving unrelated stage results.
CLI result JSON is emitted as ASCII bytes through binary stdout when available,
so output encoding and UTF-8 mode settings preserve identical bytes and exits.
String-only injected streams retain the testable text-output interface.

Minimal synthetic repairs follow the evidence producer fields:

| Uncertainty | Synthetic field to repair |
|---|---|
| `import_evidence_unreadable` | Supply all three explicit boolean import flags |
| `grid_evidence_unreadable` | Supply expected grid IDs, saved group IDs, rows and all-matched flag |
| `input_digest_conflict` | Restore supplied bytes to the bytes independently bound by the manifest |
| `snapshot_rows_missing` | Restore the explicit reopened string and table rows |

Executable before/after repair examples (synthetic source-only builder, requires
pytest):

```powershell
@'
import sys, runpy, json
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
base = builders['seven_step_envelope']()
fixed = reconcile_seven_steps_json(json.dumps(base))
for name in ['import', 'grid', 'bytes', 'reopen']:
    damaged = deepcopy(base)
    if name in ['import', 'grid']:
        damaged[name] = None
    elif name == 'bytes':
        damaged['assurance']['inputs_hex']['report'] = b'swapped'.hex()
    else:
        damaged['assurance']['evidence']['reopened_rows'] = None
    result = reconcile_seven_steps_json(json.dumps(damaged))
    if name == 'bytes':
        reason = result['assurance']['checks']['input_bytes']['reason']
    else:
        step = {'import':'1', 'grid':'2', 'reopen':'7'}[name]
        reason = result['steps'][step]['reason']
    affected = {'import':{'1'}, 'grid':{'2'}, 'bytes':set(), 'reopen':{'7'}}[name]
    for step in set('1234567') - affected:
        assert result['steps'][step] == fixed['steps'][step]
    repaired = reconcile_seven_steps_json(json.dumps(base))
    assert result['verdict'] == 'UNREADABLE' and repaired['verdict'] == 'PASS'
    print(name, result['verdict'], reason, repaired['verdict'])
'@ | python -P
```

Exact stdout:

```text
import UNREADABLE import_evidence_unreadable PASS
grid UNREADABLE grid_evidence_unreadable PASS
bytes UNREADABLE input_digest_conflict PASS
reopen UNREADABLE snapshot_rows_missing PASS
```

The repair restores the known synthetic source. Real evidence must come from
the independent producer and preserve manifest custody. The guide grants no
authority to alter frozen manifests to fit substituted product output.

`validate_grid_string_ownership(expected_grid_ids, saved_string_ids, assignments)`
requires explicit rows with exactly `grid_id` and `string_id`. Every saved string
must occur once with an expected grid owner. Missing, duplicate and foreign
assignments FAIL, with exact category counts and at most20 sorted IDs per category.
Malformed inputs are UNREADABLE. Equal aggregate totals provide no ownership.

Save/reopen composition now includes `row_diff` for changed rows or hash binding
failures, with bounded deterministic string/metric diagnostics. Missing or
malformed snapshots remain UNREADABLE. Signed discrepancy records bind both
metric and exact signed delta. A positive revision cannot authorize its negative
or a different metric, including tiny nonzero kW deltas. Decimal caller flags
remain unchanged on successful and rejected numeric/snapshot/composed paths.

Current offline CLI decision table:

| Mode | Required envelope |
|---|---|
| default | Complete six-stage evidence |
| `--aggregate` | `expected_ids`, `fixtures` with fixture evidence |
| `--provenance` | `evidence`, `manifest`, `bindings` |
| `--assurance` | `evidence`, `manifest`, `bindings`, `inputs_hex`, `identity`, `stage_identities` |
| `--provenance-aggregate` | `expected_ids`, `fixtures` with evidence/manifest/bindings |
| `--assurance-aggregate` | `expected_ids`, `fixtures` with complete assurance records |
| `--seven-step` | `import`, `grid`, `assurance` |
| `--seven-step-aggregate` | `expected_ids`, `fixtures` with seven-step envelopes |
| `--seven-step-bound` | `envelope`, `manifest`, `import_binding`, `grid_binding` |
| `--seven-step-bound-aggregate` | `expected_ids`, `fixtures` with bound records |

Each evidence mode emits one ASCII JSON result and exits0 for PASS,1 for FAIL,
4 for UNREADABLE or5 for output delivery failure. Modes are mutually exclusive.
`--help` exits0 with usage text. Unknown options or conflicting modes exit2 with
usage on stderr and no evidence result. For example, from the repository root:

```powershell
# Usage failure, exit2, no evidence verdict:
python -P tools/xbench/benches/x05_reconcile.py --unknown-option
# Malformed evidence, exit4, one fixed JSON result:
'{' | python -P tools/xbench/benches/x05_reconcile.py --seven-step
```

The malformed-evidence command emits exactly:

```json
{"reason":"json_malformed","verdict":"UNREADABLE"}
```

These commands use synthetic stdin and source code only. They perform no network
request or customer-file read.

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
## Explicit grid ownership replay

X05's offline seven-step comparator requires `grid.string_assignments`, a list
of `{grid_id, string_id}` rows. It checks `grid.expected_grid_ids` and the actual
`assurance.evidence.saved_strings` IDs. Missing ownership evidence yields
UNREADABLE while all existing step diagnostics remain available. The separate
`ownership` result contributes to the composed verdict.

Run this synthetic replay from the repository root in PowerShell.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import validate_grid_string_ownership, compare_grid_optimizer_totals
saved = [dict(string_id='A', optimizers=3), dict(string_id='B', optimizers=7)]
expected = dict(east=3, west=7)
correct = [dict(grid_id='east', string_id='A'), dict(grid_id='west', string_id='B')]
swapped = [dict(grid_id='west', string_id='A'), dict(grid_id='east', string_id='B')]
for name, rows in [('swapped', swapped), ('duplicate', correct + [correct[0]]), ('corrected', correct)]:
    ownership = validate_grid_string_ownership(list(expected), ['A', 'B'], rows)
    totals = compare_grid_optimizer_totals(expected, saved, rows)
    print(name, ownership['verdict'], totals['verdict'], totals['reason'])
    if name == 'swapped':
        assert totals['mismatch_count'] == 2
        assert [row['delta'] for row in totals['examples']] == [4, -4]
    if name == 'duplicate':
        assert ownership['counts']['duplicate'] == 1
'@ | python -P -
```

Expected output

```text
swapped PASS FAIL grid_optimizer_totals_mismatch
duplicate FAIL FAIL grid_optimizer_ownership_invalid
corrected PASS PASS grid_optimizer_totals_equal
```

Both grand totals are 10. Unique ownership establishes one supplied valid grid per
string. Independently supplied per-grid totals expose this swap. These helpers
compare supplied evidence and do not authenticate its producer. Equal-count
strings can swap without changing per-grid totals, so string identity evidence
remains necessary. Synthetic results do not establish live X05 acceptance.

## Per-grid optimizer composition replay

X05 seven-step input also requires independent `grid.optimizer_totals`, an object
mapping each expected grid ID to an integer optimizer count. Its keys must cover
the expected grids exactly, and its sum must equal `oracle_values.optimizers`.
The `grid_optimizers` result retains both comparisons. Missing totals yield
UNREADABLE while the existing steps and ownership diagnostics remain available.
The separate pure `compare_ownership_snapshots` helper detects owner swaps across
save/reopen even when both strings have equal optimizer counts.

Run from the repository root in PowerShell.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import compare_grid_optimizer_totals, compare_grid_oracle_totals
owners = [dict(grid_id='east', string_id='A'), dict(grid_id='west', string_id='B')]
independent = dict(east=3, west=7)
swapped = [dict(string_id='A', optimizers=7), dict(string_id='B', optimizers=3)]
correct = [dict(string_id='A', optimizers=3), dict(string_id='B', optimizers=7)]
for name, oracle, saved in [('swapped', independent, swapped), ('missing', None, swapped), ('corrected', independent, correct)]:
    result = compare_grid_optimizer_totals(oracle, saved, owners)
    print(name, sum(row['optimizers'] for row in saved), result['verdict'], result['reason'])
    if name == 'swapped':
        assert result['mismatch_count'] == 2
        assert [row['delta'] for row in result['examples']] == [4, -4]
consistency = compare_grid_oracle_totals(['east', 'west'], independent, dict(optimizers=10))
print('report', consistency['verdict'], consistency['reason'])
'@ | python -P -
```

Expected output

```text
swapped 10 FAIL grid_optimizer_totals_mismatch
missing 10 UNREADABLE grid_optimizer_totals_unreadable
corrected 10 PASS grid_optimizer_totals_equal
report PASS grid_oracle_total_equal
```

These checks rely on explicitly supplied independent grid evidence. They acquire
no live data and make no claim about the origin of the input.

## Ownership save/reopen composition replay

The seven-step input requires `grid.reopened_string_assignments` in addition to
the saved `string_assignments`. Each map must cover the string identities in its
respective `before_rows` or `reopened_rows` snapshot. The `ownership_reopen` result
contributes to the composed verdict while the existing step diagnostics remain
available. Missing reopened owners yield UNREADABLE. The pure
`compare_grid_optimizer_snapshots` helper separately derives per-grid sums from
each supplied snapshot's strings and owners.

From the repository root, run this synthetic PowerShell replay.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['equal_count_ownership_envelope']()
swapped = deepcopy(correct)
for row in swapped['grid']['reopened_string_assignments']:
    row['grid_id'] = 'west' if row['grid_id'] == 'east' else 'east'
missing = deepcopy(swapped)
del missing['grid']['reopened_string_assignments']
for name, envelope in [('swapped', swapped), ('missing', missing), ('corrected', correct)]:
    result = reconcile_seven_steps_json(json.dumps(envelope))
    print(name, result['verdict'], result['ownership_reopen']['reason'], result['steps']['7']['verdict'])
    assert result['grid_optimizers']['verdict'] == 'PASS'
    if name == 'swapped':
        assert result['ownership_reopen']['changed_count'] == 2
'@ | python -P -
```

Expected output

```text
swapped FAIL ownership_changed PASS
missing UNREADABLE ownership_snapshots_invalid PASS
corrected PASS ownership_equal PASS
```

Both strings keep six optimizers. Their unchanged counts and per-grid totals do
not identify the changed reopened owners. The explicit ownership maps supply that
evidence. This replay compares synthetic supplied evidence and does not establish
live X05 acceptance or authenticate an input producer.

## Grid optimizer snapshot replay

Seven-step composition includes `grid_optimizer_reopen`, derived from the two
snapshot string rows and their explicit owners. It contributes to verdict
precedence and retains the original step diagnostics. Missing snapshot strings
yield UNREADABLE.

The separate pure `compare_grid_panel_totals` helper accepts independent grid
panel totals, saved `{string_id, panels}` rows and explicit ownership rows.
`compare_grid_panel_oracle_totals` checks exact expected-grid coverage and equality
with `oracle_values.panels`. Panel evidence must supply its own counts.

Run this synthetic optimizer snapshot replay from the repository root.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import compare_grid_optimizer_snapshots
grids = ['east', 'west']
owners = [dict(grid_id='east', string_id='A'), dict(grid_id='west', string_id='B')]
before = [dict(string_id='A', optimizers=5), dict(string_id='B', optimizers=7)]
redistributed = [dict(string_id='A', optimizers=7), dict(string_id='B', optimizers=5)]
assert sum(row['optimizers'] for row in before) == sum(row['optimizers'] for row in redistributed) == 12
for name, reopened in [('redistributed', redistributed), ('missing', None), ('corrected', before)]:
    result = compare_grid_optimizer_snapshots(grids, before, owners, reopened, owners)
    print(name, result['verdict'], result['reason'])
    if name == 'redistributed':
        assert result['changed_count'] == 2
        assert [row['delta'] for row in result['examples']] == [2, -2]
'@ | python -P -
```

Expected output

```text
redistributed FAIL grid_optimizer_snapshots_changed
missing UNREADABLE grid_optimizer_snapshots_invalid
corrected PASS grid_optimizer_snapshots_equal
```

This compares supplied synthetic snapshots. Live input acquisition and the
independence of report evidence require separate receipts.

## Composed per-grid panel replay

Seven-step input requires `grid.panel_counts`, explicit `{string_id, panels}` rows,
and independent `grid.panel_totals`, an object keyed by expected grid ID. The
`grid_panels` result retains the grid comparison, report-total consistency and
binding to `saved_strings`. Missing, duplicate or foreign panel string IDs yield
UNREADABLE even when supplied totals agree. Examples remain bounded.

The pure `compare_grid_panel_snapshots` helper also compares each snapshot's
explicit panel counts and owners. Each snapshot requires its own panel rows.

Run this synthetic PowerShell replay from the repository root.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['equal_count_ownership_envelope']()
swapped = deepcopy(correct)
swapped['grid']['panel_counts'][0]['panels'] = 14
swapped['grid']['panel_counts'][1]['panels'] = 10
contradiction = deepcopy(correct)
contradiction['grid']['panel_totals']['east'] = 11
missing = deepcopy(correct)
del missing['grid']['panel_counts']
for name, envelope in [('swapped', swapped), ('contradiction', contradiction), ('missing', missing), ('corrected', correct)]:
    result = reconcile_seven_steps_json(json.dumps(envelope))
    panels = result['grid_panels']
    print(name, result['verdict'], panels['comparison']['reason'], panels['oracle_consistency']['reason'])
    assert result['steps']['7']['verdict'] == 'PASS'
'@ | python -P -
```

Expected output

```text
swapped FAIL grid_panel_totals_mismatch grid_panel_oracle_total_equal
contradiction FAIL grid_panel_totals_mismatch grid_panel_oracle_total_mismatch
missing UNREADABLE saved_rows_unreadable grid_panel_oracle_total_equal
corrected PASS grid_panel_totals_equal grid_panel_oracle_total_equal
```

The swap preserves the panel grand total of 24. Independent per-grid counts and
explicit string IDs establish the supplied comparison. This replay acquires no
live evidence and does not authenticate its producer.

## Panel save/reopen composition replay

Seven-step input also requires explicit `grid.before_panel_counts` and
`grid.reopened_panel_counts`. The `grid_panel_reopen` result binds each panel map
to its respective snapshot string IDs and compares per-grid sums using the
explicit before/reopened owners. Missing, foreign and duplicate panel IDs yield
UNREADABLE. Existing step diagnostics remain available.

Run this synthetic PowerShell replay from the repository root.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['equal_count_ownership_envelope']()
redistributed = deepcopy(correct)
redistributed['grid']['reopened_panel_counts'][0]['panels'] = 14
redistributed['grid']['reopened_panel_counts'][1]['panels'] = 10
missing = deepcopy(redistributed)
del missing['grid']['reopened_panel_counts']
for name, envelope in [('redistributed', redistributed), ('missing', missing), ('corrected', correct)]:
    result = reconcile_seven_steps_json(json.dumps(envelope))
    snapshot = result['grid_panel_reopen']
    print(name, result['verdict'], snapshot['comparison']['reason'], result['steps']['7']['verdict'])
    if name == 'redistributed':
        assert snapshot['comparison']['changed_count'] == 2
        assert [row['delta'] for row in snapshot['comparison']['examples']] == [4, -4]
'@ | python -P -
```

Expected output

```text
redistributed FAIL grid_panel_snapshots_changed PASS
missing UNREADABLE grid_panel_snapshots_invalid PASS
corrected PASS grid_panel_snapshots_equal PASS
```

Owners and the grand total of 24 stay equal across this synthetic redistribution.
Explicit per-grid snapshot sums expose the change. The supplied evidence does
not establish live X05 acceptance or authenticate an input producer.

## DC-power evidence and units

The pure `compare_grid_dc_totals` helper accepts independent grid totals, explicit
`{string_id, kw_dc}` rows and owners. `compare_grid_dc_oracle_totals` requires exact
expected-grid coverage and equality with `oracle_values.kw_dc`.
`compare_grid_dc_snapshots` compares each supplied snapshot's power and owners.
`compare_dc_string_ids` binds power rows to actual saved string IDs. These helpers
use isolated exact-decimal arithmetic and finite nonnegative kW DC values. They
preserve missing-evidence uncertainty and bounded sorted diagnostic examples.

Panel totals alone cannot supply DC power. A count requires independently supplied
module wattage, units and applicable equipment evidence before it can support a
power calculation. These helpers require explicit power values and do not derive
them from panels. A numeric value does not authenticate its source or its units.

The existing table comparator exposes contradictions between supplied saved,
report and displayed values. Run this synthetic PowerShell replay from the
repository root. The independent report and displayed row explicitly use 9.6 kW.
The mislabeled case supplies 9600 W in a field declared to be kW.

```powershell
@'
import sys, runpy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import compare_table
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
values, rows = builders['table_fixture']()
watts_mislabeled = dict(values, kw_dc='9600')
missing_report = dict(values)
del missing_report['kw_dc']
for name, saved, report in [('mislabeled', watts_mislabeled, values), ('corrected', values, values), ('missing', values, missing_report)]:
    result = compare_table(rows, saved, report)
    print(name, result['verdict'], result['reason'])
'@ | python -P -
```

Expected output

```text
mislabeled FAIL table_value_mismatch
corrected PASS table_values_equal
missing UNREADABLE table_evidence_unreadable
```

The comparison reports the supplied contradiction. It cannot infer that an
arbitrary value should be divided by 1000. Correct units and independent report
values must come from explicit evidence. This replay establishes synthetic
comparator behavior and leaves live X05 acceptance to its staging grader.

## Composed grid DC-power replay

Seven-step input requires explicit `grid.dc_power` rows with `string_id` and
`kw_dc`, independent `grid.dc_totals` by expected grid ID, and explicit
`grid.before_dc_power` and `grid.reopened_dc_power` snapshot rows. `grid_dc` retains
the per-grid comparison, independent report consistency and saved-string ID
binding. `grid_dc_reopen` binds power IDs to each snapshot's string IDs before
combining its per-grid save/reopen comparison. Missing evidence yields UNREADABLE
while unrelated diagnostics remain available.

Run this synthetic PowerShell replay from the repository root.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['equal_count_ownership_envelope']()
swapped = deepcopy(correct)
swapped['grid']['dc_power'][0]['kw_dc'] = '5.6'
swapped['grid']['dc_power'][1]['kw_dc'] = '4'
contradiction = deepcopy(correct)
contradiction['grid']['dc_totals']['east'] = '4.1'
missing = deepcopy(correct)
del missing['grid']['reopened_dc_power']
for name, envelope in [('swapped', swapped), ('contradiction', contradiction), ('missing', missing), ('corrected', correct)]:
    result = reconcile_seven_steps_json(json.dumps(envelope))
    dc = result['grid_dc']
    print(name, result['verdict'], dc['comparison']['reason'], dc['oracle_consistency']['reason'], result['grid_dc_reopen']['comparison']['reason'])
    assert result['steps']['7']['verdict'] == 'PASS'
'@ | python -P -
```

Expected output

```text
swapped FAIL grid_dc_totals_mismatch grid_dc_oracle_total_equal grid_dc_snapshots_equal
contradiction FAIL grid_dc_totals_mismatch grid_dc_oracle_total_mismatch grid_dc_snapshots_equal
missing UNREADABLE grid_dc_totals_equal grid_dc_oracle_total_equal grid_dc_snapshots_invalid
corrected PASS grid_dc_totals_equal grid_dc_oracle_total_equal grid_dc_snapshots_equal
```

The current-design swap preserves the 9.6 kW DC grand total. All power values and
units are explicit synthetic inputs. This comparison does not derive wattage
from panels or authenticate the input producer.

## Combined metric recovery replay

Before-snapshot panel and DC values must match the saved per-string values, as
well as their string IDs. The snapshot diagnostics include `saved_binding` with
exact mismatch counts and capped sorted examples. This detects substitutions
within one grid even when grid and grand totals stay equal.

The separate pure `compare_grid_string_totals` helper counts explicit owners
against independent per-grid string counts. `compare_grid_string_oracle_totals`
requires exact expected-grid coverage and agreement with `oracle_values.strings`.
These two helpers remain standalone in this slice.

Run this synthetic recovery replay from the repository root.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['seven_step_envelope']()
evidence = deepcopy(correct)
evidence['grid']['optimizer_totals']['g'] = 13
evidence['grid']['panel_totals']['g'] = 25
del evidence['grid']['reopened_dc_power']
for name in ['initial', 'optimizer', 'panel', 'dc']:
    if name == 'optimizer':
        evidence['grid']['optimizer_totals']['g'] = 12
    elif name == 'panel':
        evidence['grid']['panel_totals']['g'] = 24
    elif name == 'dc':
        evidence['grid']['reopened_dc_power'] = deepcopy(correct['grid']['reopened_dc_power'])
    result = reconcile_seven_steps_json(json.dumps(evidence))
    print(name, result['verdict'], result['grid_optimizers']['verdict'], result['grid_panels']['verdict'], result['grid_dc_reopen']['verdict'])
    assert result['steps']['7']['verdict'] == 'PASS'
'@ | python -P -
```

Expected output

```text
initial UNREADABLE FAIL FAIL UNREADABLE
optimizer UNREADABLE PASS FAIL UNREADABLE
panel UNREADABLE PASS PASS UNREADABLE
dc PASS PASS PASS PASS
```

Each repair preserves the other diagnostics. The final result establishes the
supplied synthetic comparison. Live X05 acceptance still requires staging evidence.

## Composed grid string-count replay

Seven-step input requires independent `grid.string_totals` for every expected
grid. The `grid_strings` result compares explicit saved-string ownership counts
and checks coverage and summed agreement with `oracle_values.strings`. Missing
counts yield UNREADABLE while unrelated diagnostics remain available.

Run this synthetic PowerShell replay from the repository root.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['three_string_envelope']()
swapped = deepcopy(correct)
swapped['grid']['string_totals'] = dict(east=2, west=1)
contradiction = deepcopy(correct)
contradiction['grid']['string_totals'] = dict(east=1, west=3)
missing = deepcopy(correct)
del missing['grid']['string_totals']
for name, envelope in [('swapped', swapped), ('contradiction', contradiction), ('missing', missing), ('corrected', correct)]:
    result = reconcile_seven_steps_json(json.dumps(envelope))
    counts = result['grid_strings']
    print(name, result['verdict'], counts['comparison']['reason'], counts['oracle_consistency']['reason'])
    assert result['steps']['7']['verdict'] == 'PASS'
'@ | python -P -
```

Expected output

```text
swapped FAIL grid_string_totals_mismatch grid_string_oracle_total_equal
contradiction FAIL grid_string_totals_mismatch grid_string_oracle_total_mismatch
missing UNREADABLE grid_string_totals_unreadable grid_string_oracle_evidence_unreadable
corrected PASS grid_string_totals_equal grid_string_oracle_total_equal
```

The swap preserves the grand total of three strings. The independent grid counts
and explicit ownership provide separate inputs to the comparison. This replay
establishes synthetic behavior and does not authenticate input origin or live
X05 acceptance.

## Inverter identity evidence replay

The pure `validate_grid_inverter_ownership` helper requires explicit unique saved
inverter IDs, expected grid IDs and `{inverter_id, grid_id}` assignments.
`compare_grid_inverter_totals` compares these owners with independent grid counts.
`compare_grid_inverter_oracle_totals` requires exact grid coverage and agreement
with `oracle_values.inverters`. `compare_inverter_snapshots` compares explicit
IDs and owners on each side, with bounded added/removed/owner-change diagnostics.
These helpers supply the seven-step inverter diagnostics.

Run this synthetic existing-table replay from the repository root.

```powershell
@'
import sys, runpy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import compare_table, compare_inverter_snapshots
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
values, rows = builders['table_fixture']()
before_ids = ['INV-A']
before_owners = [dict(inverter_id='INV-A', grid_id='g')]
replacement_ids = ['INV-B']
replacement_owners = [dict(inverter_id='INV-B', grid_id='g')]
for name, ids, owners in [('substituted', replacement_ids, replacement_owners), ('missing', None, None), ('corrected', before_ids, before_owners)]:
    table = compare_table(rows, values, dict(values))
    identity = compare_inverter_snapshots(['g'], before_ids, before_owners, ids, owners)
    print(name, table['verdict'], identity['verdict'], identity['reason'])
    if name == 'substituted':
        assert identity['counts'] == dict(added=1, removed=1, changed=0)
        assert identity['removed_ids'] == ['INV-A'] and identity['added_ids'] == ['INV-B']
'@ | python -P -
```

Expected output

```text
substituted PASS FAIL inverter_snapshots_changed
missing PASS UNREADABLE inverter_snapshots_invalid
corrected PASS PASS inverter_snapshots_equal
```

The table records one inverter on both sides. Stable device IDs and explicit grid
owners are separate evidence needed to settle save/reopen preservation. Missing
identity evidence stays unknown. This replay establishes supplied synthetic
comparison behavior, with input origin and live X05 acceptance requiring their
own receipts.

## Composed inverter recovery replay

Seven-step input requires independent `grid.inverter_totals` and explicit
`assurance.evidence.saved_inverters`, `before_inverters` and `reopened_inverters`
rows. Each row contains `inverter_id` and `grid_id`. `grid_inverters` retains
ownership, independent grid counts and report-total consistency. `inverter_reopen`
compares device IDs and owners across save/reopen and binds the before map to the
saved map through `saved_binding`. Missing identity evidence yields UNREADABLE.

Run this synthetic PowerShell replay from the repository root.

```powershell
@'
import sys, json, runpy
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches.x05_reconcile import reconcile_seven_steps_json
builders = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
correct = builders['equal_count_ownership_envelope']()
swapped = deepcopy(correct)
swapped['grid']['inverter_totals'] = dict(east=0, west=1)
contradiction = deepcopy(correct)
contradiction['grid']['inverter_totals'] = dict(east=1, west=1)
substituted = deepcopy(correct)
substituted['assurance']['evidence']['reopened_inverters'][0]['inverter_id'] = 'INV-B'
missing = deepcopy(correct)
del missing['assurance']['evidence']['reopened_inverters']
for name, envelope in [('swapped', swapped), ('contradiction', contradiction), ('substituted', substituted), ('missing', missing), ('corrected', correct)]:
    result = reconcile_seven_steps_json(json.dumps(envelope))
    grid = result['grid_inverters']
    print(name, result['verdict'], grid['comparison']['reason'], grid['oracle_consistency']['reason'], result['inverter_reopen']['reason'])
    assert result['steps']['7']['verdict'] == 'PASS'
'@ | python -P -
```

Expected output

```text
swapped FAIL grid_inverter_totals_mismatch grid_inverter_oracle_total_equal inverter_snapshots_equal
contradiction FAIL grid_inverter_totals_mismatch grid_inverter_oracle_total_mismatch inverter_snapshots_equal
substituted FAIL grid_inverter_totals_equal grid_inverter_oracle_total_equal inverter_snapshots_changed
missing UNREADABLE grid_inverter_totals_equal grid_inverter_oracle_total_equal inverter_snapshots_invalid
corrected PASS grid_inverter_totals_equal grid_inverter_oracle_total_equal inverter_snapshots_equal
```

The grid swap and substituted device preserve the total of one inverter. Explicit
identity and independent grid evidence expose each mismatch. The inputs in this
replay are synthetic, with input origin and live X05 acceptance requiring their
own receipts.
# String-to-inverter evidence in the X05 parity report

The seven-step report checks inverter identities and grid owners. It does not
establish which inverter each string connects to. Equal per-inverter counts can
hide swapped connections. These new standalone helpers require explicit
`string_id` and `inverter_id` rows, unique existing inverter IDs, and the actual
saved string ID set. An independent oracle must supply its own assignment rows.
Save/reopen proof needs both connection snapshots. Missing evidence is
UNREADABLE. No helper derives a connection from totals or authenticates a source.

Run this synthetic replay from the repository root in PowerShell:

```powershell
@'
import json, runpy, sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['equal_count_ownership_envelope']()
before = [dict(string_id='A', inverter_id='I1'), dict(string_id='B', inverter_id='I2')]
swapped = [dict(string_id='A', inverter_id='I2'), dict(string_id='B', inverter_id='I1')]
print('existing composition', m.reconcile_seven_steps_json(json.dumps(envelope))['verdict'])
for name, rows in [('swapped', swapped), ('missing', None), ('corrected', before)]:
    result = m.compare_string_inverter_snapshots(['I1', 'I2'], ['A', 'B'], before, rows)
    print(name, result['verdict'], result['reason'])
assert m.compare_string_inverter_assignments(['I1', 'I2'], ['A', 'B'], before, swapped)['changed_count'] == 2
'@ | python -P -
```

Expected: existing composition PASS, swapped FAIL
`string_inverter_snapshots_changed`, missing UNREADABLE
`string_inverter_snapshots_invalid`, corrected PASS
`string_inverter_snapshots_equal`. The supplied connection rows are separate
evidence, so the existing composition PASS does not settle topology. The replay
uses no customer data, transport, credentials, staging mutation or live grading.
# Composed assignment recovery for the X05 parity report

The seven-step composition now requires `oracle_assignments` and
`saved_assignments`, plus `before_assignments` and `reopened_assignments`.
Each row names `string_id` and `inverter_id`. Snapshot validation uses each
snapshot's own string and inverter IDs. The before connections must also equal
the saved connections. The earlier standalone replay describes the previous
composition boundary; explicit connection evidence now contributes to its verdict.
Input origin and oracle independence still require separate producer evidence.

Run this synthetic replay from the repository root:

```powershell
@'
import json, runpy, sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
for name in ('swapped', 'unrelated before', 'missing reopened', 'corrected'):
    envelope = fixtures['connection_envelope']()
    evidence = envelope['assurance']['evidence']
    if name == 'swapped':
        fields = ('saved_assignments', 'before_assignments', 'reopened_assignments')
    elif name == 'unrelated before':
        fields = ('before_assignments', 'reopened_assignments')
    else:
        fields = ()
    for field in fields:
        evidence[field][0]['inverter_id'], evidence[field][1]['inverter_id'] = 'INV-B', 'INV-A'
    if name == 'missing reopened':
        evidence['reopened_assignments'] = None
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    print(name, result['verdict'], result['assignments']['reason'],
          result['assignment_reopen']['saved_binding']['reason'],
          result['assignment_reopen']['comparison']['reason'])
    assert result['steps']['7']['verdict'] == 'PASS'
'@ | python -P -
```

Expected: swapped FAIL with `string_inverter_assignments_changed`, unrelated
before FAIL with saved binding `string_inverter_assignments_changed`, missing
reopened UNREADABLE with `string_inverter_snapshots_invalid`, corrected PASS.
Each swap preserves the per-inverter counts, device identities and grid totals.
No source independence, customer-data intake or live acceptance is claimed.
# Per-inverter load evidence for X05 reconciliation

Matching connection identities and grid totals can coexist with report loads
that differ for each inverter. The standalone load helpers require independent
per-inverter totals keyed by explicit inverter IDs. String totals count supplied
connections. Optimizer totals use saved per-string optimizer counts. Panel and
kW DC totals require their own rows bound to the saved string ID set. The oracle
consistency helper checks explicit device coverage and each report grand total.
The DC helper uses exact decimal arithmetic and preserves the caller context.

Run this synthetic replay from the repository root:

```powershell
@'
import json, runpy, sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['connection_envelope']()
evidence = envelope['assurance']['evidence']
owners = evidence['saved_assignments']
ids = [row['string_id'] for row in evidence['saved_strings']]
print('composition', m.reconcile_seven_steps_json(json.dumps(envelope))['verdict'])
checks = [
    m.compare_inverter_string_totals({'INV-A':2, 'INV-B':0}, ids, owners),
    m.compare_inverter_optimizer_totals({'INV-A':7, 'INV-B':5}, evidence['saved_strings'], owners),
    m.compare_inverter_panel_totals({'INV-A':14, 'INV-B':10}, ids, envelope['grid']['panel_counts'], owners),
    m.compare_inverter_dc_totals({'INV-A':'5.6', 'INV-B':'4'}, ids, envelope['grid']['dc_power'], owners),
]
for result in checks:
    print(result['verdict'], result['reason'], result['mismatch_count'])
corrected = m.compare_inverter_optimizer_totals({'INV-A':5, 'INV-B':7}, evidence['saved_strings'], owners)
missing = m.compare_inverter_optimizer_totals(None, evidence['saved_strings'], owners)
print('corrected', corrected['verdict'], 'missing', missing['verdict'])
assert all(result['verdict'] == 'FAIL' and result['mismatch_count'] == 2 for result in checks)
assert corrected['verdict'] == 'PASS' and missing['verdict'] == 'UNREADABLE'
'@ | python -P -
```

Expected composition PASS and four per-inverter load FAIL results with two
mismatches each. Corrected optimizer totals PASS. Absent independent totals are
UNREADABLE. All independent count and power maps preserve their report grand
totals in this example. These standalone helpers add per-device comparisons to
the supplied evidence. Producer provenance and independent report extraction
remain requirements for live acceptance.
# Composed inverter load recovery for X05 reconciliation

Seven-step evidence now requires `inverter_loads` with `strings`, `optimizers`,
`panels` and `kw_dc` maps keyed by inverter ID. Each map must cover the explicit
saved inverter set and sum to its independent report metric. Save/reopen loads
come from each snapshot's own connection assignments and per-string counts or
power rows. Before loads also bind to saved values. The report retains each
metric's comparison and oracle consistency result when another metric fails.

Run this synthetic replay from the repository root:

```powershell
@'
import json, runpy, sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['connection_envelope']()
loads = envelope['assurance']['evidence']['inverter_loads']
correct = {metric:dict(values) for metric,values in loads.items()}
for metric in ('optimizers', 'panels'):
    loads[metric]['INV-A'], loads[metric]['INV-B'] = loads[metric]['INV-B'], loads[metric]['INV-A']
loads['kw_dc'] = None
for stage in ('initial', 'optimizer repaired', 'panel repaired', 'power repaired'):
    if stage == 'optimizer repaired': loads['optimizers'] = correct['optimizers']
    if stage == 'panel repaired': loads['panels'] = correct['panels']
    if stage == 'power repaired': loads['kw_dc'] = correct['kw_dc']
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    checks = result['inverter_loads']['metrics']
    print(stage, result['verdict'], *(checks[metric]['comparison']['reason']
          for metric in ('optimizers', 'panels', 'kw_dc')))
    assert result['steps']['7']['verdict'] == 'PASS'
assert result['verdict'] == 'PASS'
'@ | python -P -
```

The first three stages are UNREADABLE while power evidence is absent. The first
stage retains both `inverter_optimizers_totals_mismatch` and
`inverter_panels_totals_mismatch`. Repairs clear only their own metric. The final
stage is PASS. Oracle input origin and independence require producer evidence
from the actual report extraction path before live acceptance.
# Independent per-string metric evidence for X05

Grid and inverter totals can agree after panel or DC values move between strings
on the same inverter. Composition compares supplied snapshots and aggregate
oracles. Independent per-string panel and kW DC rows are needed to detect this
case. The standalone `compare_string_panel_counts` and `compare_string_dc_power`
helpers require explicit oracle IDs and values, with bounded sorted diagnostics.
Missing or malformed rows return UNREADABLE. Explicit identity or value
differences return FAIL. DC comparison uses isolated exact decimals.

Run this synthetic replay from the repository root:

```powershell
@'
import json, runpy, sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['per_string_redistribution_envelope']()
panels = [dict(string_id='A',panels=10),dict(string_id='B',panels=14)]
power = [dict(string_id='A',kw_dc='4'),dict(string_id='B',kw_dc='5.6')]
composition = m.reconcile_seven_steps_json(json.dumps(envelope))
panel = m.compare_string_panel_counts(panels, envelope['grid']['panel_counts'])
dc = m.compare_string_dc_power(power, envelope['grid']['dc_power'])
print('composition', composition['verdict'])
print('panels', panel['verdict'], panel['reason'], panel['counts']['mismatched'])
print('power', dc['verdict'], dc['reason'], dc['counts']['mismatched'])
assert composition['verdict'] == 'PASS'
assert panel['verdict'] == dc['verdict'] == 'FAIL'
'@ | python -P -
```

Expected composition PASS, panels FAIL `string_panels_mismatch` with two changed
strings, power FAIL `string_kw_dc_mismatch` with two changed strings. Both strings
retain their IDs, grid, inverter and aggregate totals. Independent report
extraction must provide the per-string oracle values and their provenance for
live acceptance. These new comparators remain standalone in this slice.
# Independent per-string metric recovery in X05

Seven-step `string_metrics` now includes panel/DC comparisons and independent
oracle coverage/sum checks. Evidence must supply `oracle_panels` and
`oracle_dc_power` rows with explicit string IDs and values. The following
standalone replay preserves each diagnostic while repairing panel and DC swaps.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
panels = [dict(string_id='A',panels=10),dict(string_id='B',panels=14)]
power = [dict(string_id='A',kw_dc='4'),dict(string_id='B',kw_dc='5.6')]
saved_panels = [dict(string_id='A',panels=14),dict(string_id='B',panels=10)]
saved_power = [dict(string_id='A',kw_dc='5.6'),dict(string_id='B',kw_dc='4')]
for stage in ('swapped', 'missing power oracle', 'panels repaired', 'power repaired'):
    oracle_power = None if stage in ('missing power oracle', 'panels repaired') else power
    if stage in ('panels repaired', 'power repaired'): saved_panels = panels
    if stage == 'power repaired': saved_power = power
    panel = m.compare_string_panel_counts(panels, saved_panels)
    dc = m.compare_string_dc_power(oracle_power, saved_power)
    print(stage, panel['verdict'], panel['reason'], dc['verdict'], dc['reason'])
assert panel['verdict'] == dc['verdict'] == 'PASS'
'@ | python -P -
```

Expected panel/DC verdict pairs are FAIL/FAIL, FAIL/UNREADABLE,
PASS/UNREADABLE, PASS/PASS. Diagnostics name `string_panels_mismatch`,
`string_kw_dc_mismatch`, `oracle_rows_unreadable`, then their matching
`string_panels_equal` and `string_kw_dc_equal` results after repair. Composition
uses UNREADABLE precedence and retains readable sibling diagnostics. Previous
proof-limit replays now produce composition FAIL once these independent rows
are supplied. Oracle origin and independence require actual producer evidence.
# Composed string metric recovery for X05

The seven-step `string_metrics` result includes explicit panel/DC snapshot
comparisons, snapshot string-ID bindings and before-to-saved value bindings.
This replay exercises independent oracle failures and incremental repair through
the composer. It uses only synthetic rows.

```powershell
@'
import json, runpy, sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['per_string_redistribution_envelope']()
grid, evidence = envelope['grid'], envelope['assurance']['evidence']
power = deepcopy(evidence['oracle_dc_power'])
evidence['oracle_dc_power'] = None
observed = []
for stage in ('missing power oracle', 'panels repaired', 'power oracle restored',
              'power repaired', 'contradictory report sum', 'sum repaired'):
    if stage == 'panels repaired':
        for field in ('panel_counts','before_panel_counts','reopened_panel_counts'):
            grid[field] = deepcopy(evidence['oracle_panels'])
    if stage == 'power oracle restored': evidence['oracle_dc_power'] = power
    if stage == 'power repaired':
        for field in ('dc_power','before_dc_power','reopened_dc_power'):
            grid[field] = deepcopy(power)
    if stage == 'contradictory report sum': evidence['oracle_panels'][0]['panels'] = 11
    if stage == 'sum repaired': evidence['oracle_panels'][0]['panels'] = 10
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    checks = result['string_metrics']['metrics']
    observed.append(result['verdict'])
    print(stage, result['verdict'], checks['panels']['comparison']['reason'],
          checks['kw_dc']['comparison']['reason'], checks['panels']['oracle_consistency']['reason'])
assert observed == ['UNREADABLE','UNREADABLE','FAIL','PASS','FAIL','PASS']
'@ | python -P -
```

Each repair preserves the other metric's diagnostic. Oracle sum contradictions
produce `string_panels_oracle_total_mismatch`. Independent per-string report
rows still require producer provenance for live acceptance.
# Cross-topology evidence for X05

Balanced cross-grid connections can preserve every device and string count.
Explicit string grid owners, inverter grid owners and string-to-inverter
connections are needed to establish that both endpoints share a grid. The new
standalone `validate_cross_topology` helper checks this equality after validating
all three maps. The existing composer retains its earlier topology boundaries
until this helper is integrated.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
grids, strings, devices = ['east','west'], ['A','B'], ['I1','I2']
owners = [dict(string_id='A',grid_id='east'),dict(string_id='B',grid_id='west')]
inverters = [dict(inverter_id='I1',grid_id='east'),dict(inverter_id='I2',grid_id='west')]
crossed = [dict(string_id='A',inverter_id='I2'),dict(string_id='B',inverter_id='I1')]
optimizers = [dict(string_id=k,optimizers=6) for k in strings]
panels = [dict(string_id=k,panels=12) for k in strings]
power = [dict(string_id=k,kw_dc='4.8') for k in strings]
checks = [
    m.validate_grid_string_ownership(grids,strings,owners),
    m.validate_grid_inverter_ownership(grids,devices,inverters),
    m.validate_string_inverter_assignments(devices,strings,crossed),
    m.compare_inverter_string_totals(dict(I1=1,I2=1),strings,crossed),
    m.compare_inverter_optimizer_totals(dict(I1=6,I2=6),optimizers,crossed),
    m.compare_inverter_panel_totals(dict(I1=12,I2=12),strings,panels,crossed),
    m.compare_inverter_dc_totals(dict(I1='4.8',I2='4.8'),strings,power,crossed),
]
for check in checks: print(check['verdict'],check['reason'])
topology = m.validate_cross_topology(grids,strings,devices,owners,inverters,crossed)
print(topology['verdict'],topology['reason'],topology['mismatch_count'])
assert all(check['verdict'] == 'PASS' for check in checks)
assert topology['verdict'] == 'FAIL' and topology['mismatch_count'] == 2
'@ | python -P -
```

Expected seven PASS results and FAIL `cross_topology_mismatch` with two changed
strings. Missing owner evidence is UNREADABLE. Valid but missing or foreign
assignments fail their explicit coverage checks. Independent producer evidence
must bind these identities to the actual saved design for live acceptance.
# Cross-topology recovery for X05

Seven-step composition now validates saved, before and reopened topology using
each snapshot's string IDs, inverter IDs, grid owners and connections. Grid
evidence must explicitly supply `before_string_assignments` as well as saved
`string_assignments` and `reopened_string_assignments`. The standalone recovery
below combines missing owner evidence with balanced cross-grid connections.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
grids, strings, devices = ['east','west'], ['A','B'], ['I1','I2']
owners = [dict(string_id='A',grid_id='east'),dict(string_id='B',grid_id='west')]
inverters = [dict(inverter_id='I1',grid_id='east'),dict(inverter_id='I2',grid_id='west')]
crossed = [dict(string_id='A',inverter_id='I2'),dict(string_id='B',inverter_id='I1')]
correct = [dict(string_id='A',inverter_id='I1'),dict(string_id='B',inverter_id='I2')]
observed = []
for name, supplied_owners, connections in [('missing owners',None,crossed),
        ('owners repaired',inverters,crossed),('connections repaired',inverters,correct)]:
    result = m.validate_cross_topology(grids,strings,devices,owners,supplied_owners,connections)
    observed.append(result['verdict'])
    print(name,result['verdict'],result['reason'],result.get('mismatch_count'))
assert observed == ['UNREADABLE','FAIL','PASS']
'@ | python -P -
```

Expected reasons are `cross_topology_evidence_invalid`,
`cross_topology_mismatch` with two mismatches, and `cross_topology_equal` with
zero mismatches. Repairing owner evidence leaves the connection violation
visible. Independent producer records must bind every supplied identity and
owner map to its actual design snapshot before live acceptance.
# Composed cross-topology recovery in X05

This synthetic replay keeps every saved, before and reopened phase diagnostic
visible while repairing independent cross-grid violations and missing owners.

```powershell
@'
import json, runpy, sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['equal_count_ownership_envelope']()
grid, evidence = envelope['grid'], envelope['assurance']['evidence']
correct = deepcopy(evidence['saved_assignments'])
before_owners = deepcopy(grid['before_string_assignments'])
for stage in ('saved','reopened'):
    rows = evidence[stage+'_assignments']
    rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
grid['before_string_assignments'] = None
observed = []
for stage in ('initial','saved repaired','before owners repaired','reopened repaired'):
    if stage == 'saved repaired': evidence['saved_assignments'] = deepcopy(correct)
    if stage == 'before owners repaired': grid['before_string_assignments'] = before_owners
    if stage == 'reopened repaired': evidence['reopened_assignments'] = deepcopy(correct)
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    checks = result['cross_topology']['checks']
    observed.append(result['verdict'])
    print(stage,result['verdict'],*(checks[key]['verdict'] for key in ('saved','before','reopened')))
    print(*(checks[key]['reason'] for key in ('saved','before','reopened')))
assert observed == ['UNREADABLE','UNREADABLE','FAIL','PASS']
'@ | python -P -
```

Phase verdicts progress from FAIL/UNREADABLE/FAIL to PASS/UNREADABLE/FAIL,
PASS/PASS/FAIL and PASS/PASS/PASS. Violations name `cross_topology_mismatch`,
missing evidence names `cross_topology_evidence_invalid`, and repaired phases
name `cross_topology_equal`. Each supplied map needs actual producer provenance
before live acceptance.
# Independent owner evidence for X05

Jointly swapping string and inverter grid owners can preserve valid connections
and balanced grid totals. Independent oracle owner rows establish the expected
identity-to-grid mapping. The new string and inverter owner comparators remain
standalone in this slice. Independent inverter ID sets also detect substitutions
of devices that have no strings connected.

```powershell
@'
import sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
grids, strings, devices = ['east','west'], ['A','B'], ['I1','I2']
oracle_strings = [dict(string_id='A',grid_id='east'),dict(string_id='B',grid_id='west')]
oracle_inverters = [dict(inverter_id='I1',grid_id='east'),dict(inverter_id='I2',grid_id='west')]
saved_strings = [dict(string_id='A',grid_id='west'),dict(string_id='B',grid_id='east')]
saved_inverters = [dict(inverter_id='I1',grid_id='west'),dict(inverter_id='I2',grid_id='east')]
connections = [dict(string_id='A',inverter_id='I1'),dict(string_id='B',inverter_id='I2')]
checks = [
    m.validate_cross_topology(grids,strings,devices,saved_strings,saved_inverters,connections),
    m.compare_grid_string_totals(dict(east=1,west=1),strings,saved_strings),
    m.compare_grid_inverter_totals(dict(east=1,west=1),devices,saved_inverters),
]
for check in checks: print(check['verdict'],check['reason'])
string_check = m.compare_string_grid_owners(grids,strings,oracle_strings,saved_strings)
device_check = m.compare_inverter_grid_owners(grids,devices,oracle_inverters,saved_inverters)
for check in (string_check,device_check):
    print(check['verdict'],check['reason'],check['mismatch_count'])
assert all(check['verdict'] == 'PASS' for check in checks)
assert string_check['mismatch_count'] == device_check['mismatch_count'] == 2
'@ | python -P -
```

Expected three PASS results followed by FAIL `string_grid_owners_mismatch` and
FAIL `inverter_grid_owners_mismatch`, each with two mismatches. Missing oracle
owner rows are UNREADABLE. Report extraction must supply independently bound
owner identities and grid mappings for live acceptance.
# Independent owner recovery in X05

Composition now requires `oracle_string_owners`, `oracle_inverter_owners` and
`oracle_inverter_ids` in seven-step evidence. Its `independent_owners` result
retains the three comparisons. This standalone replay repairs balanced owner
swaps, missing oracle owners and an unconnected-device substitution separately.

```powershell
@'
import sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
grids, strings = ['east','west'], ['A','B']
oracle_ids, saved_ids = ['I1','I2','empty-old'], ['I1','I2','empty-new']
oracle_strings = [dict(string_id='A',grid_id='east'),dict(string_id='B',grid_id='west')]
oracle_devices = [dict(inverter_id=k,grid_id=v) for k,v in zip(oracle_ids,['east','west','east'])]
saved_strings = [dict(string_id='A',grid_id='west'),dict(string_id='B',grid_id='east')]
saved_devices = [dict(inverter_id=k,grid_id=v) for k,v in zip(saved_ids,['west','east','east'])]
for stage in ('missing owners','string owners restored','string owners repaired','device identity repaired','device owners repaired'):
    supplied = None if stage == 'missing owners' else oracle_strings
    if stage == 'string owners repaired': saved_strings = deepcopy(oracle_strings)
    if stage == 'device identity repaired':
        saved_ids[-1] = 'empty-old'
        saved_devices[-1]['inverter_id'] = 'empty-old'
    if stage == 'device owners repaired': saved_devices = deepcopy(oracle_devices)
    checks = [m.compare_string_grid_owners(grids,strings,supplied,saved_strings),
              m.compare_inverter_grid_owners(grids,oracle_ids,oracle_devices,saved_devices),
              m.compare_inverter_id_sets(oracle_ids,saved_ids)]
    print(stage,*(check['verdict'] for check in checks))
    print(*(check['reason'] for check in checks))
assert all(check['verdict'] == 'PASS' for check in checks)
'@ | python -P -
```

Verdict triples progress UNREADABLE/FAIL/FAIL, FAIL/FAIL/FAIL, PASS/FAIL/FAIL,
PASS/FAIL/PASS and PASS/PASS/PASS. Identity repair leaves balanced device-owner
swaps visible. Actual report producers must independently bind oracle identities
and owner maps to the design before live acceptance.
# Composed independent owner recovery for X05

This replay combines balanced oracle-owner swaps, missing device-owner rows and
a substituted oracle device ID. Each repair passes through seven-step
composition and leaves the other independent-owner diagnostics visible.

```powershell
@'
import json, runpy, sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['independent_owner_conflict_envelope']('owners')
evidence = envelope['assurance']['evidence']
evidence['oracle_inverter_owners'] = None
evidence['oracle_inverter_ids'][1] = 'replacement'
observed = []
for stage in ('initial','string owners repaired','device IDs repaired','device owners restored','device owners repaired'):
    if stage == 'string owners repaired':
        evidence['oracle_string_owners'] = deepcopy(envelope['grid']['string_assignments'])
    if stage == 'device IDs repaired': evidence['oracle_inverter_ids'][1] = 'INV-B'
    if stage == 'device owners restored':
        evidence['oracle_inverter_owners'] = [dict(inverter_id='INV-A',grid_id='west'),dict(inverter_id='INV-B',grid_id='east')]
    if stage == 'device owners repaired': evidence['oracle_inverter_owners'] = deepcopy(evidence['saved_inverters'])
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    checks = result['independent_owners']['checks']
    observed.append(result['verdict'])
    print(stage,result['verdict'],*(checks[key]['verdict'] for key in ('strings','inverters','inverter_ids')))
    print(*(checks[key]['reason'] for key in ('strings','inverters','inverter_ids')))
assert observed == ['UNREADABLE','UNREADABLE','UNREADABLE','FAIL','PASS']
'@ | python -P -
```

The initial triple is FAIL/UNREADABLE/FAIL. String repair leaves the device
failures visible. Identity repair leaves owner evidence UNREADABLE. Restoring
swapped device owners yields FAIL until their mappings are repaired. Oracle
identities and owners require independent producer provenance for live acceptance.
# Independent owner envelope fields in X05

The seven-step `assurance.evidence` object supplies explicit oracle and saved
identity maps. Consumers retain their own named diagnostics.

| Field | Row shape or value | Consumer |
| --- | --- | --- |
| `oracle_strings` | `string_id`, `optimizers` rows | Oracle string IDs and independent string checks |
| `saved_strings` | `string_id`, `optimizers` rows | Saved string IDs and independent string checks |
| `oracle_string_owners` | `string_id`, `grid_id` rows | Independent string owners and oracle topology |
| `grid.string_assignments` | `string_id`, `grid_id` rows | Saved string owners and saved topology |
| `oracle_inverter_ids` | Unique nonempty ID list | Independent device IDs and oracle topology |
| `oracle_inverter_owners` | `inverter_id`, `grid_id` rows | Independent device owners and oracle topology |
| `saved_inverters` | `inverter_id`, `grid_id` rows | Saved device IDs, owners and topology |
| `oracle_assignments` | `string_id`, `inverter_id` rows | Independent connections and oracle topology |
| `saved_assignments` | `string_id`, `inverter_id` rows | Saved connections and topology |

`grid.string_assignments` belongs to the envelope's separate `grid` object.
Omitted or null owner/connection rows are UNREADABLE. An empty owner/connection
list is readable but fails coverage for this nonempty design. Omitted, null or
empty device ID lists and oracle string rows are UNREADABLE. Duplicate identity
lists are UNREADABLE. Duplicate owner rows fail explicit assignment coverage.

```powershell
@'
import json, runpy, sys
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['equal_count_ownership_envelope']()
evidence = envelope['assurance']['evidence']
evidence['oracle_string_owner'] = evidence.pop('oracle_string_owners')
broken = m.reconcile_seven_steps_json(json.dumps(envelope))
check = broken['independent_owners']['checks']['strings']
print('field typo',broken['verdict'],check['reason'])
assert broken['verdict'] == 'UNREADABLE'
evidence['oracle_string_owners'] = evidence.pop('oracle_string_owner')
fixed = m.reconcile_seven_steps_json(json.dumps(envelope))
print('field repaired',fixed['verdict'],fixed['independent_owners']['checks']['oracle_topology']['reason'])
assert fixed['verdict'] == 'PASS'
'@ | python -P -
```

Expected field-typo result UNREADABLE `string_grid_owner_evidence_invalid`, then
PASS with `cross_topology_equal`. Oracle topology checks only internal supplied
consistency. Independent producer provenance remains required for live acceptance.
# Oracle topology recovery with saved-phase failures in X05

The composer now also retains `oracle_loads`, deriving grid and inverter loads
from explicit oracle values and owners/connections. Each metric retains derived
comparisons and report-total checks. This replay repairs missing oracle
connections and contradictory oracle owners before repairing saved snapshots.

```powershell
@'
import json, runpy, sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['equal_count_ownership_envelope']()
grid, evidence = envelope['grid'], envelope['assurance']['evidence']
correct = deepcopy(evidence['oracle_assignments'])
evidence['oracle_assignments'] = None
rows = evidence['oracle_string_owners']
rows[0]['grid_id'],rows[1]['grid_id'] = 'west','east'
for phase in ('saved','before','reopened'):
    rows = evidence[phase+'_assignments']
    rows[0]['inverter_id'],rows[1]['inverter_id'] = 'INV-B','INV-A'
observed = []
for stage in ('initial','oracle connections restored','oracle owners repaired',
              'saved repaired','before repaired','reopened repaired'):
    if stage == 'oracle connections restored': evidence['oracle_assignments'] = deepcopy(correct)
    if stage == 'oracle owners repaired': evidence['oracle_string_owners'] = deepcopy(grid['string_assignments'])
    for phase in ('saved','before','reopened'):
        if stage == phase+' repaired': evidence[phase+'_assignments'] = deepcopy(correct)
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    oracle = result['independent_owners']['checks']['oracle_topology']
    phases = result['cross_topology']['checks']
    observed.append(result['verdict'])
    print(stage,result['verdict'],oracle['verdict'],oracle['reason'],
          *(phases[key]['verdict'] for key in ('saved','before','reopened')))
assert observed == ['UNREADABLE','FAIL','FAIL','FAIL','FAIL','PASS']
'@ | python -P -
```

Oracle connection repair exposes `cross_topology_mismatch` from the contradictory
oracle owners. Repairing those owners leaves all saved-phase failures visible.
Each saved snapshot then repairs independently. These consistency checks require
actual producer provenance and independent report extraction for live acceptance.
# Oracle load evidence fields in X05

`oracle_loads` derives values only from explicit oracle rows and assignments.
The supplied grid and inverter totals are independent comparison targets.

| Metric | Oracle per-string values | Grid target | Inverter target |
| --- | --- | --- | --- |
| `strings` | One for each explicit `oracle_strings` ID | `grid.string_totals` | `inverter_loads.strings` |
| `optimizers` | `oracle_strings[].optimizers` | `grid.optimizer_totals` | `inverter_loads.optimizers` |
| `panels` | `oracle_panels[].panels` | `grid.panel_totals` | `inverter_loads.panels` |
| `kw_dc` | `oracle_dc_power[].kw_dc` | `grid.dc_totals` | `inverter_loads.kw_dc` |

Grid derivation uses `oracle_string_owners` and `grid.expected_grid_ids`. Device
derivation uses `oracle_assignments` and `oracle_inverter_ids`. Panel/DC row IDs
bind to `oracle_strings`. Every target map also checks its coverage and summed
agreement with `oracle_values[metric]`. Power values are explicit kW DC.

Omitted, null or empty metric-row lists and numeric target maps are UNREADABLE.
Omitted/null owner or connection rows are UNREADABLE. Empty owner/connection
lists fail coverage for the nonempty design. Missing report totals make their
report checks UNREADABLE while derived comparisons remain visible. No count or
power is inferred from another metric.

```powershell
@'
import json, runpy, sys
from copy import deepcopy
sys.path.insert(0, 'tools/xbench')
from benches import x05_reconcile as m
fixtures = runpy.run_path('tools/xbench/tests/test_x05_reconcile.py')
envelope = fixtures['equal_count_ownership_envelope']()
grid, evidence = envelope['grid'], envelope['assurance']['evidence']
grid_panels = deepcopy(grid['panel_totals'])
device_panels = deepcopy(evidence['inverter_loads']['panels'])
grid['panel_totals'] = dict(east=14,west=10)
evidence['inverter_loads']['panels'] = {'INV-A':14,'INV-B':10}
observed = []
for stage in ('both swapped','grid repaired','device repaired'):
    if stage == 'grid repaired': grid['panel_totals'] = grid_panels
    if stage == 'device repaired': evidence['inverter_loads']['panels'] = device_panels
    result = m.reconcile_seven_steps_json(json.dumps(envelope))
    check = result['oracle_loads']['metrics']['panels']
    observed.append(result['verdict'])
    print(stage,result['verdict'],check['grid_comparison']['verdict'],check['inverter_comparison']['verdict'],
          check['grid_report']['verdict'],check['inverter_report']['verdict'])
assert observed == ['FAIL','FAIL','PASS']
'@ | python -P -
```

Both report-total checks remain PASS throughout the equal-grand-total swap.
Derived comparisons repair independently. Actual oracle producers must supply
independent values, owners and connections with provenance for live acceptance.
