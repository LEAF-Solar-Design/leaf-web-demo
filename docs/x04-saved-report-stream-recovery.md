# X04 saved-report stream recovery

Studio X-bench X04 checks saved report internal consistency through stdin as well as explicit files. This recipe uses only a synthetic comparison. Run the Python block from the repository root on the assigned off-host verification rail. It creates only `x04-stream-check.json` in the current directory; use an empty scratch location or change that output path.

```python
import json
from pathlib import Path
import subprocess
import sys

report = {
    'schema': 'leaf.x04.offline-comparison.v1', 'drawing_id': 'synthetic-stream',
    'count': {'before': 7, 'after': 6, 'delta': -1},
    'selected_ids': {'removed': ['opaque-outline'], 'added': []},
    'highlight_handles': {'removed': ['B'], 'added': []},
    'literal_dependencies': {'removed': [], 'added': []},
    'original_bytes_integrity': 'unverified',
}
encoded = json.dumps(report, ensure_ascii=True).encode('utf-8')
script = Path('scripts/validate_x04_review_report.py').resolve()
output = Path('x04-stream-check.json')
def run(data, *arguments):
    return subprocess.run([sys.executable, '-P', str(script), '-', *map(str, arguments)],
                          input=data, capture_output=True, timeout=30)

# Successful stdin validation emits one complete summary and exits 0.
healthy = run(encoded)
assert healthy.returncode == 0 and healthy.stderr == b''
summary = json.loads(healthy.stdout)
assert summary['validation_scope'] == 'saved_report_internal_consistency'
assert summary['original_bytes_integrity'] == 'unverified'
assert summary['underlying_artifacts_revalidated'] is False
assert summary['embedded_paths_opened'] is False

# A truncated pipe exits 2, emits no success JSON and preserves the output.
output.write_bytes(b'prior output')
truncated = run(encoded[:-1], '--out', output)
assert truncated.returncode == 2 and truncated.stdout == b'' and truncated.stderr
assert output.read_bytes() == b'prior output'

# Explicit one-byte-under input and output ceilings refuse before replacement.
for option, ceiling in [('--max-input-bytes', len(encoded) - 1),
                        ('--max-output-bytes', len(healthy.stdout) - 1)]:
    refused = run(encoded, '--out', output, option, ceiling)
    assert refused.returncode == 2 and refused.stdout == b'' and refused.stderr
    assert output.read_bytes() == b'prior output'

# Exact encoded-byte ceilings recover the request and publish the full summary.
recovered = run(encoded, '--out', output, '--max-input-bytes', len(encoded),
                '--max-output-bytes', len(healthy.stdout))
assert recovered.returncode == 0 and recovered.stdout == recovered.stderr == b''
assert output.read_bytes() == healthy.stdout
```

These byte ceilings include input whitespace/BOM and the output newline. The default input limit is 16 MiB; the output limit is optional. Exact `-` selects stdin while `./-` reads a literal file. Path aliases to the input refuse before file replacement. File publication uses an owned temporary file and atomic replacement. Malformed input or a failed file publication preserves the previous destination and reports exit 2. A valid retry exits 0.

A stdout write or flush failure reports exit 2. The sink may have received part or all of the summary before it fails, so the exit status does not retract bytes already sent and does not prove durable delivery. File output and stdout have different delivery limits. Concurrent calls retain their own byte ceilings and destinations; process stderr is shared.

The nesting benchmark compares nested opaque extensions up to and beyond the supported container-depth boundary with flat controls of identical encoded size. It asserts exact verdicts and immutable reports and records time and peak memory without performance thresholds:

```text
python -P scripts/bench_x04_review_report_nesting.py
```

The checker opens neither embedded artifact paths nor dependency targets. A summary proves internal consistency only. It cannot prove underlying artifact validity, a simultaneous snapshot or authenticated source bytes. Real intake, rendered module selection and the full-ten staging grader remain separate X04 receipts.
