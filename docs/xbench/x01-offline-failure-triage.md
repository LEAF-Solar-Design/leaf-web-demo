# X01 offline failure triage

Run these bounded Python 3 standard-library replays from the repository root
in PowerShell. They use synthetic data and unique `TemporaryDirectory` scratch
directories, preserve original evidence, and make zero live-grade claims.
Disable bytecode writes for these commands so imports leave the checkout alone:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
```

## Read the three independent results

* Validity checks the saved report's schema and internal consistency. A valid
  report can contain observed `FAIL` or `UNREADABLE` checker outcomes.
* Observed verdict is the saved checker aggregate, preserved without rerunning
  or endorsing a checker. Even an observed aggregate `PASS` is not live grade.
* Custody requires exactly six allowlisted sources under the supplied helper
  directory: three checker `.py` files and three vector `.json` files. Their
  current hashes must match both recorded hashes. Legacy reports can validate
  while custody is `UNREADABLE` because provenance is absent or partial.

The inspector has no default paths, `--output`, checker execution, subprocess,
or input writes. Supply both reports and both source directories explicitly:

```powershell
python tools/xbench/x01_offline/inspect_evidence_bundle.py OLD.json NEW.json --old-source-root OLD_SOURCES --new-source-root NEW_SOURCES
python tools/xbench/x01_offline/inspect_evidence_bundle.py --self-test
```

The first command's four path tokens must be replaced with explicit local
evidence paths; the scratch replay below supplies runnable fixtures. The second
command is the bounded inspector check (expected exit 0), not a full regression.

All result objects carry `evidence_kind: synthetic_offline` and
`live_grade: false`. Exact schema identifiers and verdict/exit meanings:

| Tool/result | Schema | PASS / 0 | FAIL / 1 | UNREADABLE / 4 |
| --- | --- | --- | --- | --- |
| Saved runner report | `leaf.xbench.x01.offline-run.v1` | Observed aggregate | Observed failure | Observed inability to evaluate |
| Validator | `leaf.xbench.x01.offline-report-validation.v1` | Structurally consistent | Invalid structure/consistency | IO, malformed JSON or read limit |
| Source verifier | `leaf.xbench.x01.offline-source-check.v1` | All six sources current | `SOURCE_CHANGED` | Invalid report/root, missing source/hash, unstable/read error |
| Comparator | `leaf.xbench.x01.offline-report-comparison.v1` | Both reports valid, including differing findings | Structurally invalid report | Unreadable input/JSON |
| Inspector | `leaf.xbench.x01.offline-evidence-bundle.v1` | Valid reports, current custody, comparison PASS | Readable valid bundle with custody FAIL | Any invalid/malformed report, IO, unknown state, or argument error |

Runner reports express observed status; `run_checks()` returns a dictionary,
not a process exit. The replay explicitly maps it to 0/1/4. Existing helper
CLI usage errors can exit 2; inspector argument errors instead emit JSON
`ARGUMENT_ERROR` and exit 4. `--help` displays usage and exits 0.

The inspector JSON has `schema`, `evidence_kind`, `live_grade`, `verdict`,
`valid` (both reports' schema validity), `reports`, `comparison`, and
`diagnostics`. `reports.old` and `reports.new` each contain `validity` (the
full validator result), `custody` (the full source verifier result), and
`observed_verdict` (saved aggregate, or null if unavailable). `comparison`
retains the full existing comparator result, including transitions, provenance
changes, and added/removed diagnostic objects. Top diagnostics retain helper
names and add `side`, `stage`, and `argument`; source `input` identities remain
intact. Argument failures have empty `reports`, null `comparison`, and an
`ARGUMENT_ERROR` diagnostic with a reason. UNREADABLE takes precedence over
FAIL. A descriptive comparison PASS can coexist with custody FAIL or observed
FAIL. Top PASS is possible with observed FAIL when validity and custody pass.

Sources and reports must be trusted and quiescent throughout inspection,
including repeated bounded report reads by the composed APIs. The two-pass
source verifier detects exercised mutations; it is not an atomic multi-file
snapshot or proof against every transient restored-byte event. Never retry into
a newer source generation or rewrite recorded hashes to make custody pass.

## Child timeout and output flood

This replay injects only tiny Python children into `run_checks`; it never invokes
the real checkers. Sources are synthetic scratch files, and saved evidence is
written only inside the temporary directory. Run the same script for both modes:

```powershell
$childReplay = @'
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path('tools/xbench/x01_offline').resolve()))
import run_offline_checks as runner
import validate_offline_report as validator
import verify_report_sources as custody

mode = sys.argv[1]
source = ('import time; time.sleep(30)' if mode == 'timeout' else
          'import os\nwhile True: os.write(1, b"x" * 4096)')
reason = 'CHILD_TIMEOUT' if mode == 'timeout' else 'CHILD_OUTPUT_LIMIT'
with tempfile.TemporaryDirectory(prefix='x01-child-triage-') as directory:
    root = Path(directory)
    provenance = []
    for name, filename in custody.SOURCES.items():
        path = root / filename
        path.write_bytes(b'synthetic source, never executed\n')
        provenance.append((name, path))
    commands = [(name, [sys.executable, '-B', '-c', source if i == 0 else 'pass'])
                for i, name in enumerate(validator.CHECKERS)]
    report = runner.run_checks(commands, timeout=0.2 if mode == 'timeout' else 5,
                               provenance_paths=provenance)
    path = root / 'report.json'
    path.write_text(json.dumps(report), encoding='utf-8')
    row = report['checks'][0]
    assert row['diagnostics'][0]['name'] == reason, row
    assert row['timeout'] == (mode == 'timeout'), row
    assert validator.validate_file(path)['verdict'] == 'PASS'
    assert custody.verify_report_sources(path, root)['verdict'] == 'PASS'
    print(json.dumps(dict(reason=reason, observed=report['verdict'],
                         validity='PASS', custody='PASS')))
    code = {'PASS': 0, 'FAIL': 1, 'UNREADABLE': 4}[report['verdict']]
raise SystemExit(code)
'@
$childReplay | python -B - timeout
$LASTEXITCODE # expected 4, CHILD_TIMEOUT, observed UNREADABLE
$childReplay | python -B - flood
$LASTEXITCODE # expected 4, CHILD_OUTPUT_LIMIT, observed UNREADABLE
```

If containment is unavailable, the named failure is `CHILD_CONTAINMENT_ERROR`;
do not interpret that as a timeout/flood reproduction. Recover only child
execution: fix the stuck child or reduce output, then rerun that failed stage
into fresh scratch evidence. Output truncation alone is not the flood reason;
`CHILD_OUTPUT_LIMIT` means the capture cap was exceeded. Do not alter saved
timeout flags, child exits, or diagnostics to make a report validate.

## Stale provenance and invalid reports

The following standalone replay calls the inspector's CLI entry point without
launching any process. It creates different explicit old/new source roots,
starts with observed FAIL in valid/current reports, then changes only a scratch
vector and a scratch report copy. Original fixture bytes remain untouched.

```powershell
@'
import copy, hashlib, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path('tools/xbench/x01_offline').resolve()))
import inspect_evidence_bundle as inspector
import validate_offline_report as validator
import verify_report_sources as custody

with tempfile.TemporaryDirectory(prefix='x01-bundle-triage-') as directory:
    base = Path(directory)
    roots = [base / 'old-sources', base / 'new-sources']
    paths = [base / 'old.json', base / 'new.json']
    original = validator.ValidatorTests().report('FAIL')
    original['provenance'] = []
    for root in roots:
        root.mkdir()
        for filename in custody.SOURCES.values():
            (root / filename).write_bytes(b'synthetic source, never executed\n')
    for name, filename in custody.SOURCES.items():
        digest = hashlib.sha256((roots[0] / filename).read_bytes()).hexdigest()
        original['provenance'].append(dict(name=name, sha256_before=digest,
                                            sha256_after=digest))
    raw = json.dumps(original).encode('utf-8')
    for path in paths:
        path.write_bytes(raw)
    args = [str(path) for path in paths] + [
        '--old-source-root', str(roots[0]), '--new-source-root', str(roots[1])]
    assert inspector.main(args) == 0  # observed FAIL, valid/current, top PASS
    (roots[1] / custody.VECTORS[0]).write_bytes(b'changed scratch vector\n')
    assert inspector.main(args) == 1  # SOURCE_CHANGED; comparison still PASS
    bad = copy.deepcopy(original)
    bad['schema'] = 'unknown'
    paths[1].write_text(json.dumps(bad), encoding='utf-8')
    assert validator.validate_file(paths[1])['diagnostics'][0]['name'] == 'UNKNOWN_SCHEMA'
    assert inspector.main(args) == 4  # validity FAIL; inspector UNREADABLE
    paths[1].write_bytes(b'{')
    assert validator.validate_file(paths[1])['diagnostics'][0]['name'] == 'INVALID_JSON'
    assert inspector.main(args) == 4
    assert paths[0].read_bytes() == raw
'@ | python -B -
$LASTEXITCODE # expected 0: all asserted inspector exits 0, 1, 4, 4 matched
```

For stale provenance, retain `SOURCE_CHANGED` and the recorded/current hashes.
Locate the original matching source generation, or deliberately rerun only the
evidence-generation stage against the intended generation into new evidence.
For invalid reports, recover only serialization/schema consistency from the
producer; preserve the invalid original. Validator `UNKNOWN_SCHEMA` is FAIL/1,
malformed JSON `INVALID_JSON` is UNREADABLE/4, and either makes inspection
UNREADABLE/4. A missing source likewise makes custody/inspection UNREADABLE/4.

## Unstable source reads

Use existing deterministic read-boundary controls instead of a timing race or a
long raw mutation hook. They mutate only temporary sources and preserve reports,
unrelated scratch files, and checkout sources:

```powershell
python -B tools/xbench/x01_offline/test_source_read_mutation.py SourceReadMutationTests.test_in_place_change_after_chunk SourceReadMutationTests.test_second_pass_when_metadata_does_not_distinguish_change SourceReadMutationTests.test_growth_is_bounded_and_not_adopted
$LASTEXITCODE # expected 0: each test asserts custody UNREADABLE/4
```

The custody diagnostic is `name: UNREADABLE`, `reason: SOURCE_UNSTABLE`, with
the exact source in `input`; no current digest is adopted. Stop the writer and
repeat only custody/inspection on quiescent sources. A stable but different
source is `SOURCE_CHANGED` (FAIL/1), not `SOURCE_UNSTABLE`. If the intended
source generation cannot be recovered, retain the failure and create separate
new evidence rather than editing the old provenance. None of these recoveries
establishes a product, staging, or live-grade verdict.
