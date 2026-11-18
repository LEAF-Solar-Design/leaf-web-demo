# Expected report kind in X04's offline review

Studio X-bench X04's saved-report validator accepts `--expected-report-schema comparison` or `--expected-report-schema batch`. This is an optional routing check after byte reading, optional digest matching and JSON decoding. A wrong kind refuses before report semantics or publication. A matching kind still faces every existing consistency check. Omitting the option retains support for either recognized report kind.

Start in the repository root in PowerShell. Resolve the script from this checkout and create a fresh directory containing only synthetic comparison and batch claims:

```powershell
$validator = (Resolve-Path -LiteralPath './scripts/validate_x04_review_report.py').Path
$demo = 'D:/agent-data/leaf-research/x04-synthetic-report-schema'
@'
import json, sys
from pathlib import Path
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
comparison = {'schema': 'leaf.x04.offline-comparison.v1', 'drawing_id': 'synthetic-schema',
    'count': {'before': 6, 'after': 1, 'delta': -5}, 'original_bytes_integrity': 'unverified',
    'selected_ids': {'removed': [], 'added': []}, 'highlight_handles': {'removed': [], 'added': []},
    'literal_dependencies': {'removed': [], 'added': []}}
batch = {'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 1, 'passed': 0, 'failed': 1,
    'original_bytes_integrity': 'unverified', 'results': [
    {'index': 0, 'path': '../../unopened.json', 'valid': False, 'error': 'synthetic refusal'}]}
for name, value in [('comparison', comparison), ('batch', batch)]:
    (root / (name + '.json')).write_bytes(json.dumps(value).encode('utf-8'))
'@ | python -P - $demo
if ($LASTEXITCODE -ne 0) { throw 'Expected fresh synthetic fixture creation' }
$comparisonPin = (Get-FileHash -LiteralPath "$demo/comparison.json" -Algorithm SHA256).Hash
$batchPin = (Get-FileHash -LiteralPath "$demo/batch.json" -Algorithm SHA256).Hash
```

Retain the two expected digests separately. This block validates the comparison by file and the batch by binary stdin from an unrelated directory. It first creates a valid summary, then supplies the other expected kind while keeping the correct byte digest. The refusal must preserve the previous summary and every input byte. Retrying the correct kind recovers the identical summary.

```powershell
@'
import json, subprocess, sys
from pathlib import Path
root, validator = Path(sys.argv[1]), sys.argv[2]
for kind, pin, stdin in [('comparison', sys.argv[3], False), ('batch', sys.argv[4], True)]:
    source = root / (kind + '.json'); target = root / (kind + '-checked.json'); raw = source.read_bytes()
    def run(expected):
        args = [sys.executable, '-P', validator, '-' if stdin else str(source), '--out', str(target),
                '--expected-report-sha256', pin, '--expected-report-schema', expected]
        return subprocess.run(args, input=raw if stdin else None, cwd=root, capture_output=True, timeout=30)
    result = run(kind)
    if result.returncode != 0: raise RuntimeError(result.stderr.decode())
    prior = target.read_bytes(); wrong = 'batch' if kind == 'comparison' else 'comparison'
    result = run(wrong)
    if result.returncode != 2 or result.stdout or target.read_bytes() != prior:
        raise RuntimeError('Correct digest with wrong report kind did not preserve output')
    result = run(kind)
    if result.returncode != 0 or target.read_bytes() != prior or source.read_bytes() != raw:
        raise RuntimeError('Corrected expected kind did not recover')
    summary = json.loads(prior)
    if summary['report_schema'] != json.loads(raw)['schema']: raise RuntimeError('Unexpected report schema')
    if summary['original_bytes_integrity'] != 'unverified': raise RuntimeError('Unexpected integrity claim')
    if summary['underlying_artifacts_revalidated'] is not False: raise RuntimeError('Unexpected artifact claim')
    if list(root.glob('.x04-*')): raise RuntimeError('Unexpected temporary file')
    print(kind, 'correct kind, wrong-kind refusal and corrected retry passed')
'@ | python -P - $demo $validator $comparisonPin $batchPin
if ($LASTEXITCODE -ne 0) { throw 'Expected both synthetic schema controls to pass' }
```

The comparison's arithmetic is internally consistent at 1 minus 6 equals -5. The batch contains one declared failed occurrence. These are invented reports without underlying artifacts, not evidence of drawing truth. An internally consistent report of the expected kind can pass even when its underlying claims are fabricated. The report kind is routing intent, the digest compares exact bytes, and semantic validation checks only internal consistency. None authenticates the sender, producer or expected values.

Malformed kind choices and abbreviated flags refuse during argument parsing before I/O. Input byte ceilings precede digest checks, digest checks precede decoding, and decoding precedes the kind check. A matching kind and digest do not waive arithmetic, totals, membership or integrity checks. Default behavior and the pure `validate_report` API remain unchanged. No embedded path is opened. Provenance and original drawing integrity stay unverified; no browser, raw completeness or live ten-drawing acceptance is established. Output uses the existing bounded encoding and atomic replacement, without a new fsync or hostile alias-race guarantee.
