# X04 saved-report usage and refusal replay

Studio X-bench X04 checks saved reports for internal consistency only. The recipe below creates synthetic reports and records usage, unavailable-stdin and diagnostic refusals with exact exits and a valid retry. It does not open underlying artifacts or dependency paths. Run from the repository root on the assigned off-host verification rail.

```python
import json
from pathlib import Path
import subprocess
import sys

script = Path('scripts/validate_x04_review_report.py').resolve()
source = Path('x04-usage-report.json')
output = Path('x04-usage-checked.json')
report = {
    'schema': 'leaf.x04.offline-comparison.v1', 'drawing_id': 'synthetic-usage',
    'count': {'before': 1, 'after': 1, 'delta': 0},
    'selected_ids': {'removed': [], 'added': []},
    'highlight_handles': {'removed': [], 'added': []},
    'literal_dependencies': {'removed': [], 'added': []},
    'original_bytes_integrity': 'unverified',
}
source.write_text(json.dumps(report), encoding='utf-8')
original = source.read_bytes()
output.write_bytes(b'prior output')

def run(*arguments):
    return subprocess.run([sys.executable, '-P', str(script), *map(str, arguments)],
                          capture_output=True, timeout=30)

help_result = run('--help')
assert help_result.returncode == 0 and b'usage:' in help_result.stdout
for arguments in [(), ('--unknown',), (source, '--max', '5'),
                  (source, '--max-input-bytes', '0'), (source, '--max-output-bytes', '-1')]:
    result = run(*arguments)
    assert result.returncode == 2 and result.stdout == b'' and b'usage:' in result.stderr
    assert output.read_bytes() == b'prior output' and source.read_bytes() == original

# Disable stdin inside an isolated child. A normal empty pipe is a different case.
bootstrap = '''import sys
sys.path.insert(0, sys.argv[1])
from validate_x04_review_report import main
sys.stdin = None
raise SystemExit(main(['-', '--out', sys.argv[2]]))
'''
missing = subprocess.run([sys.executable, '-P', '-c', bootstrap, str(script.parent), str(output)],
                         capture_output=True, timeout=30)
assert missing.returncode == 2 and missing.stdout == b''
assert b'open binary input buffer' in missing.stderr and output.read_bytes() == b'prior output'

# Invalid-schema controls remain inert and cannot create a success line.
for control in ('\r', '\n', '\x1b[32m', '\u202e', '\u2066'):
    forged = dict(report)
    forged['schema'] = control + 'SUCCESS valid=true' + control
    source.write_text(json.dumps(forged), encoding='utf-8')
    result = run(source, '--out', output)
    assert result.returncode == 2 and result.stdout == b''
    assert result.stderr.count(b'\n') == 1 and b'SUCCESS' not in result.stderr
    assert output.read_bytes() == b'prior output'

source.write_bytes(original)
recovered = run(source, '--out', output)
assert recovered.returncode == 0 and recovered.stdout == recovered.stderr == b''
checked = json.loads(output.read_bytes())
assert checked['valid'] and checked['original_bytes_integrity'] == 'unverified'
assert checked['underlying_artifacts_revalidated'] is False
assert checked['embedded_paths_opened'] is False
assert not list(output.parent.glob('.x04-*'))
```

Missing operands, unknown flags, abbreviations and nonpositive or malformed byte ceilings exit 2 during argument parsing, before input or output operations. `--help` exits 0. Absent stdin, a closed binary buffer or a text-only stream exits 2 without replacing an existing output. A real empty pipe also exits 2 because it contains no JSON. Invalid report schema diagnostics use a bounded fixed message; CR, LF, ANSI and bidi data do not become terminal control text.

Two concurrent validators use distinct temporary files. Each successful replacement publishes a complete summary. A failed writer removes only its owned temporary file; another writer can still publish. This is atomic replacement, not a multiwriter ordering guarantee or durable storage proof.

```text
python -P scripts/bench_x04_report_rejection_positions.py
```

The benchmark places a contradictory dependency or batch row first, middle and last at 10/100/1000 rows, records refusal time and peak memory, checks immutable inputs and validates the restored control. It imposes no timing thresholds. Internal consistency proves neither underlying artifact validity, a simultaneous snapshot nor authenticated source bytes. Self-consistent forged reports can pass; real intake and the staging full-ten grader remain separate receipts.
