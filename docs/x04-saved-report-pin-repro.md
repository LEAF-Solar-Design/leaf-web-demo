# Saved-report byte pins in X04's offline evidence review

Studio X-bench X04 can compare a saved comparison or batch report with a separately retained digest before checking that report's internal consistency. `--expected-report-sha256` accepts exactly 64 hexadecimal characters. The default remains unpinned. Matching bytes do not establish authenticity, original drawing integrity or the truth of underlying artifacts. The summary keeps integrity `unverified` and `underlying_artifacts_revalidated: false`.

Start from this checkout's repository root in PowerShell. Resolve the validator before changing directories. Create only synthetic report claims in a fresh directory:

```powershell
$validator = (Resolve-Path -LiteralPath './scripts/validate_x04_review_report.py').Path
$demo = 'D:/agent-data/leaf-research/x04-synthetic-saved-report-pins'
@'
import json, sys
from pathlib import Path
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
comparison = {'schema': 'leaf.x04.offline-comparison.v1', 'drawing_id': 'synthetic-report',
    'original_bytes_integrity': 'unverified', 'count': {'before': 6, 'after': 1, 'delta': -5},
    'selected_ids': {'removed': ['old'], 'added': ['new']},
    'highlight_handles': {'removed': ['B'], 'added': []},
    'literal_dependencies': {'removed': [], 'added': []}}
batch = {'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 1, 'passed': 1, 'failed': 0,
    'original_bytes_integrity': 'unverified', 'results': [{'index': 0, 'path': '../../unopened.json',
    'valid': True, 'schema': 'leaf.x04.offline-validation.v1', 'drawing_id': 'synthetic-report',
    'recomputed_result': {'count': 1, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}}]}
for name, value in [('comparison', comparison), ('batch', batch)]:
    (root / (name + '.json')).write_bytes(json.dumps(value).encode('utf-8'))
'@ | python -P - $demo
if ($LASTEXITCODE -ne 0) { throw 'Expected fresh synthetic report creation' }
$fixedComparisonPin = (Get-FileHash -LiteralPath "$demo/comparison.json" -Algorithm SHA256).Hash
$fixedBatchPin = (Get-FileHash -LiteralPath "$demo/batch.json" -Algorithm SHA256).Hash
```

Keep those expected values separately from the report files. The next block validates the comparison through its file and the batch through raw binary stdin, both from an unrelated directory. Python sends stdin bytes directly so PowerShell does not re-encode them. Each report first passes, then gains one whitespace byte and refuses the old digest while retaining its prior summary. Only the explicit refresh retries with the new digest.

```powershell
@'
import hashlib, json, subprocess, sys
from pathlib import Path
root, validator = Path(sys.argv[1]), sys.argv[2]
for name, fixed, use_stdin in [('comparison', sys.argv[3], False), ('batch', sys.argv[4], True)]:
    source = root / (name + '.json'); output = root / (name + '-checked.json')
    def run(pin):
        args = [sys.executable, '-P', validator, '-' if use_stdin else str(source),
                '--expected-report-sha256', pin, '--out', str(output)]
        return subprocess.run(args, input=source.read_bytes() if use_stdin else None,
                              cwd=root, capture_output=True, timeout=30)
    result = run(fixed)
    if result.returncode != 0: raise RuntimeError(result.stderr.decode())
    previous = output.read_bytes()
    source.write_bytes(source.read_bytes() + b' ')
    result = run(fixed)
    if result.returncode != 2 or result.stdout or output.read_bytes() != previous:
        raise RuntimeError('Stale pin did not preserve the prior summary')
    if list(root.glob('.x04-*')): raise RuntimeError('Unexpected temporary file')
    refreshed = hashlib.sha256(source.read_bytes()).hexdigest()
    result = run(refreshed)
    if result.returncode != 0 or output.read_bytes() != previous:
        raise RuntimeError('Explicit whitespace-only digest refresh did not recover')
    summary = json.loads(output.read_bytes())
    if summary['validation_scope'] != 'saved_report_internal_consistency': raise RuntimeError('Unexpected scope')
    if summary['underlying_artifacts_revalidated'] is not False: raise RuntimeError('Unexpected artifact claim')
    if summary['embedded_paths_opened'] is not False: raise RuntimeError('Unexpected path claim')
    if summary['original_bytes_integrity'] != 'unverified': raise RuntimeError('Unexpected integrity claim')
    print(name, 'matching bytes, stale refusal and explicit refresh passed')
'@ | python -P - $demo $validator $fixedComparisonPin $fixedBatchPin
if ($LASTEXITCODE -ne 0) { throw 'Expected both synthetic report handoffs to pass' }
```

Expected claims are only internal: comparison arithmetic is 1 minus 6 equals -5; the batch declares one successful row with count 1 and highlight A. No underlying artifact exists in this example. A correctly pinned arithmetic forgery or contradictory batch total still refuses. An internally consistent fabricated report can pass; changing both bytes and their expected digest can also pass the byte check. Neither is producer authentication.

The shared bounded reader checks the input ceiling before digest comparison and hashes the exact decoded buffer, including BOM and whitespace, without reopening the source. Malformed expected values fail during argument parsing. Output is encoded under its ceiling before atomic replacement; prior output survives handled pre-publication refusal. This does not promise fsync durability, redaction, raw dependency completeness, browser behavior or the live ten-drawing result. The pure `validate_report` API remains an internal-consistency check and does not take a byte pin.
