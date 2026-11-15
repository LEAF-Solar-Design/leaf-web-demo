# X01 synthetic offline report contract

This contract describes the stdlib implementations in
[`run_offline_checks.py`](../../tools/xbench/x01_offline/run_offline_checks.py)
(`SCHEMA = leaf.xbench.x01.offline-run.v1`) and
[`validate_offline_report.py`](../../tools/xbench/x01_offline/validate_offline_report.py)
(`REPORT_SCHEMA` as above; validation result `SCHEMA =
leaf.xbench.x01.offline-report-validation.v1`). These are versioned synthetic
offline records. Both use `evidence_kind: synthetic_offline` and literal
`live_grade: false`. They establish no live-source, staging, readiness,
publication, customer-input, or DXF-export claim.

## Rows, verdicts and validity

The normal runner invokes these three children sequentially, each with
`--self-test`, using the current Python executable, without a shell:

| Required checker (exactly once) | Synthetic subject |
| --- | --- |
| `check_cases.py` | Geometry/case contracts |
| `check_plot_layers.py` | Layer parsing and plot policy |
| `check_unit_equivalence.py` | Unit conversion and physical equivalence |

The validator requires exactly these three rows, with no unknown or duplicate
checker. Internal runner command overrides are test seams; arbitrary stub rows
do not constitute a normal valid report. The separate precision boundary suite
is additional evidence; it is not a fourth runner row.

Each row contains `checker`, `verdict`, `exit_code` (integer excluding bool, or
null when launch or containment fails before an exit code is available), boolean `timeout`, finite nonnegative
`duration_seconds`, a list of named diagnostic objects, string `output`, and
boolean `output_truncated`. The parent contains `schema`, `evidence_kind`,
`live_grade`, `verdict`, `checks`, and normally `provenance` and `diagnostics`.
Unknown fields do not grant additional authority. Any nested `live_grade` key
must also have the literal value false.

Ordinary child exits map as follows: **0 = PASS, 1 = FAIL, 4 = UNREADABLE**.
A JSON body's claimed verdict never overrides its exit. Capture, parsing and
resource failures instead produce UNREADABLE, retaining named reasons:

| Reason | Meaning / consistency rule |
| --- | --- |
| `CHILD_LAUNCH_ERROR` | No exit code (null); not a timeout |
| `CHILD_CONTAINMENT_ERROR` | Safe process ownership could not be established; UNREADABLE, without running uncontained children |
| `CHILD_TIMEOUT` | Timeout true, non-null exit code after termination |
| `CHILD_UNEXPECTED_EXIT` | Non-null exit outside 0/1/4, including signal exits |
| `CHILD_OUTPUT_LIMIT` | Capture byte limit exceeded |
| `CHILD_DIAGNOSTIC_LIMIT` | Diagnostic count/bytes or JSON nesting exceeded |
| `CHILD_OUTPUT_UNREADABLE` | Capture read failure or unsafe/unparseable JSON |
| `CHILD_DIAGNOSTICS_INVALID` | JSON is not an object, or diagnostics have invalid container types |

The validator recognizes only that exception vocabulary; arbitrary diagnostic
names cannot turn an exit 1 into UNREADABLE. Plain unittest text is allowed;
when no structured diagnostics are available the runner supplies `CHILD_OUTPUT`
with a checker name and up to 2,048 characters of retained text. Structured
diagnostics retain named findings and associated synthetic identifiers.

Aggregation is **FAIL > UNREADABLE > PASS**. All children are attempted, so a
failure does not hide later rows. Source provenance is the explicit override:
`INPUT_CHANGED` or `INPUT_UNREADABLE` at the parent forces UNREADABLE even if a
child failed; the original child FAIL and its diagnostic remain. An
`OUTPUT_WRITE_ERROR` only changes a would-be PASS to UNREADABLE; an existing FAIL
or UNREADABLE remains. Invalid timeout arguments yield runner exit 4 and
`INVALID_TIMEOUT` with empty checks; this diagnostic envelope is not a complete
three-row report accepted by the normal validator.

The validator's **validity** is separate from the **observed verdict**:

| Validator result / CLI exit | Interpretation |
| --- | --- |
| `verdict: PASS`, `valid: true` / 0 | Consistent report; `observed_verdict` can be PASS, FAIL or UNREADABLE |
| `verdict: FAIL`, `valid: false` / 1 | Parsed report violates the contract |
| `verdict: UNREADABLE`, `valid: false` / 4 | Input cannot be read or safely parsed within limits |

Validation is read-only. It checks consistency, not whether findings are true,
whether a checker was actually executed, or whether a stored digest matches a
current file. A valid named FAIL is useful retained synthetic evidence and is
not a successful design or a live grade.

## Limits and lifecycle

| Boundary | Source limit and behavior |
| --- | --- |
| Child time | Default 30 seconds per child; caller value must be positive, finite, non-bool and at most 300 seconds |
| Capture | Combined stdout/stderr, at most 65,536 bytes retained per child, read in 4,096-byte chunks; excess triggers termination and `CHILD_OUTPUT_LIMIT` |
| Row output | At most 16,384 UTF-8 bytes; replacement decoding then safe byte clipping; `output_truncated` records truncation |
| Structured child JSON | At most 8 nested array/object containers across the payload before parsing; nonfinite constants and overflowed floats rejected |
| Diagnostics | At most 64 objects and 16,384 serialized UTF-8 bytes per list (row and validator parent); JSON serialization uses ASCII escaping |
| Validator diagnostic depth | In-memory diagnostics traversal cap 8, counted from the diagnostics list at depth 0 |
| Validator file | At most 1,048,576 bytes; reads cap plus one byte to detect excess; API `max_bytes` can lower this but cannot raise it |
| Validator depth | File container nesting cap 32; API `max_depth` can lower it but cannot raise it; in-memory report traversal also caps depth at 32 (root depth 0) |
| Membership/count | Exactly three known checker rows; provenance names must be unique when present; no separate provenance-count cap beyond file/depth bounds |
| Hash reading | SHA-256 streams in 65,536-byte blocks; this is a buffer size, not a total input-size cap |

The validator file parser rejects invalid UTF-8, duplicate object keys,
nonfinite numbers, malformed JSON and excess depth as `INVALID_JSON` /
UNREADABLE. Byte excess is `INPUT_BYTE_LIMIT`. Invalid API limit arguments
produce `INVALID_LIMIT`. The in-memory API applies type/depth/consistency checks;
the file byte cap belongs to `validate_file`, not `validate_report`. Atomic
serialization has no independent total report-size cap; a written file still
must meet the validator's limits.

The capture deadline covers execution and pipe draining. A descendant retaining
stdout cannot cause an indefinite join: capture uses bounded nonblocking polling,
with no reader thread. Deadline expiry emits `CHILD_TIMEOUT`; unsafe capture or
duplicate-key child JSON emits `CHILD_OUTPUT_UNREADABLE`. Windows children start
suspended and are assigned to a private kill-on-close job before resume; assignment
failure emits `CHILD_CONTAINMENT_ERROR`. POSIX uses a private process group and
keeps the leader waitable through group termination. Linux temporarily enables
subreaping to reap orphan descendants and then restores its previous state.
Cleanup has a separate one-second budget after capture, including job termination
or group kill and direct-child wait. This is not a host-wide process cleanup tool.
Other POSIX systems leave non-child reaping to the system reaper. Windows controls
verify the recorded child and descendant PIDs are gone after capture.

Both child JSON parsing and validator file parsing reject duplicate object keys
at every depth. Plain unittest text remains accepted. Report fields alone do not
prove that stored findings are true. The scratch example below uses no child
processes and cleans its own bounded set of three files.

## Atomic output and input hash scope

`--output PATH` writes through a same-directory temporary file, JSON with
`allow_nan=False`, trailing newline, flush and file `fsync`, then `os.replace`.
The parent directory must already exist. The temporary file is removed in
`finally` on failure; a replacement failure preserves any prior destination.
No directory fsync or crash-durability promise is made. A caught write failure
adds `OUTPUT_WRITE_ERROR` to the stdout report, subject to the precedence above;
an old destination is not evidence of the failed attempt. Filesystem operations
and cleanup are not promised to finish within the child's timeout.

Default provenance hashes the raw bytes of all three checker `.py` files and
`fill_coverage_cases.json`, `module_geometry_cases.json`,
`tag_overlap_cases.json`, before the sequence and again after all children.
Names are prefixed `checker:` or `vector:`. Each entry contains `name`,
`sha256_before`, `sha256_after`; readable hashes are lowercase 64-character hex,
unreadable hashes are null. Missing/read-error hashes produce
`INPUT_UNREADABLE`; differing readable hashes produce `INPUT_CHANGED`.
Diagnostics identify the matching entry with `input`. The validator requires
matching source diagnostics for each such event when provenance is supplied.

These hashes cover neither the runner/validator, this documentation, additional
test suites, imported dependencies, environment, command line, live source nor
product/fixture bytes beyond the three named vectors. Equal endpoint hashes do
not prove there was no intervening mutation. Older v1 reports may omit
`provenance` and parent `diagnostics`; acceptance of those legacy records makes
no hash-custody claim. An empty provenance list is also accepted by the
validator; it is not the default runner's six-entry custody evidence.

## Copyable stdlib synthetic examples

Save the Python block to a scratch file and run it with Python 3 from the
repository root. This stdlib example authors a
deliberately synthetic named FAIL using the exact known row shape. It validates
that report from scratch storage, then separately rejects a live-grade claim
and an inconsistent parent verdict. It does not run a worker, model, exporter,
or live checker. `TemporaryDirectory` removes only the three generated files;
the printed JSON can be retained outside git as synthetic-only evidence.

```python
import copy
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path('tools/xbench/x01_offline').resolve()))
from run_offline_checks import write_atomic
from validate_offline_report import validate_file

names = ('check_cases.py', 'check_plot_layers.py', 'check_unit_equivalence.py')
rows = [dict(checker=name, verdict='PASS', exit_code=0, timeout=False,
             duration_seconds=0.0, diagnostics=[dict(name='SYNTHETIC_CONTROL')],
             output='', output_truncated=False) for name in names]
rows[2].update(verdict='FAIL', exit_code=1,
               diagnostics=[dict(name='UNIT_EQUIVALENCE_MISMATCH',
                                 object_ids=['synthetic:precision-control'])])
report = dict(schema='leaf.xbench.x01.offline-run.v1',
              evidence_kind='synthetic_offline', live_grade=False,
              verdict='FAIL', checks=rows, diagnostics=[])
# Provenance intentionally omitted: this is a hand-authored legacy-shape control,
# not a claim that the runner executed or hashed inputs.
with tempfile.TemporaryDirectory(prefix='x01-report-control-') as scratch:
    path = Path(scratch) / 'named-fail.json'
    write_atomic(path, report)
    before = path.read_bytes()
    result = validate_file(path)
    assert result['valid'] is True and result['verdict'] == 'PASS', result
    assert result['observed_verdict'] == 'FAIL', result
    assert path.read_bytes() == before
    print(json.dumps(dict(control='valid_named_fail', report=report,
                         validation=result), sort_keys=True))

    live = copy.deepcopy(report)
    live['live_grade'] = True
    path = Path(scratch) / 'rejected-live.json'
    write_atomic(path, live)
    result = validate_file(path)
    assert result['valid'] is False and result['verdict'] == 'FAIL', result
    assert any(d['name'] == 'LIVE_GRADE_CLAIM' for d in result['diagnostics'])
    print(json.dumps(dict(control='reject_live_grade', validation=result), sort_keys=True))

    inconsistent = copy.deepcopy(report)
    inconsistent['verdict'] = 'PASS'
    path = Path(scratch) / 'rejected-parent.json'
    write_atomic(path, inconsistent)
    result = validate_file(path)
    assert result['valid'] is False and result['verdict'] == 'FAIL', result
    assert any(d['name'] == 'INCONSISTENT_PARENT' for d in result['diagnostics'])
    print(json.dumps(dict(control='reject_inconsistent_verdict', validation=result),
                     sort_keys=True))
```

For a separately retained report in scratch storage, the read-only CLI is
`python -B tools/xbench/x01_offline/validate_offline_report.py "C:/tmp/x01 scratch/report.json"`.
It prints the validation schema and exits according to validity (0/1/4), not
the report's observed verdict. Retain that result alongside the original
synthetic report if evidence is needed; this document publishes neither.
